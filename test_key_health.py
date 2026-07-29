"""
Единое владение здоровьем ключей к моделям: кулдауны, баны, снятие пометок.

Проверяется НАСТОЯЩИЙ gemini_client. Заглушен только сетевой клиент — вместо
него объект, который отдаёт заданные ошибки и считает фактически ушедшие
запросы. Живых сетевых вызовов нет. Файлы кулдаунов, банов и статуса
подменяются на временные: боевые не читаются и не пишутся.

Каждый дефект и его последствие для врача:

  [1] Отбор ключей и проверка бана жили в generate_text своими копиями, поэтому
      любой другой путь копировал их себе или обходился без них. Замер по дереву:
      клиента к модели создают семь модулей, про кулдауны знали два.
      Последствие: путь, создавший клиента мимо учёта, бьёт в ключ, уже упёршийся
      в квоту, и не пишет туда свой результат. Врач ждёт ответа, который приходит
      после лишних попыток, либо не приходит.

  [2] Ключ на кулдауне не должен пробоваться раньше срока. Раньше проверка стояла
      внутри цикла попыток и остывающий ключ съедал попытку молча.
      Последствие: при десятке рабочих ключей бот отвечал «не смог ответить».

  [3] Расшифровка голосового (transcribe_audio_bytes_or_file) ходила мимо учёта,
      хотя живёт в модуле, которому учёт принадлежит: не читала кулдауны и не
      писала их. Замер на живом наборе (7 ключей Groq, бюджет 60 с, доля 7 с):
      три остывающих ключа съедали 21 с из 48 (44% бюджета) и оставляли 3 живые
      попытки вместо 7; шесть — 88% бюджета и НОЛЬ живых попыток.
      Последствие: врач диктует вопрос голосом и получает тишину вместо
      расшифровки при четырёх здоровых ключах на руках.

  [4] 429, найденный расшифровкой, выбрасывался.
      Последствие: следующий текстовый ответ врачу в группе начинал с того же
      мёртвого ключа и тратил на него попытку заново.

  [5] Пометки ставились, но не снимались никогда: кулдаун 300 с и бан 1200 с
      держались до истечения срока, даже когда та же пара только что успешно
      ответила.
      Последствие: доказанно живой ключ простаивает четыре минуты, а работающая
      модель остаётся вне каскада треть часа — ответ врачу идёт по резервным
      путям, которые медленнее и хуже.

  [6] Отказ по квоте писался в журнал механикой («placing key on 300s cooldown»)
      и не отвечал на единственный вопрос, который по такой записи задают:
      осталось ли чем отвечать врачу.

  [7] Бан модели при отказе пути, где замены нет.
      Последствие: бан whisper-large-v3 на 20 минут — это отказ ВСЕХ расшифровок
      на 20 минут, тогда как достаточно было сменить ключ.

Запуск: python test_key_health.py
"""
import io
import logging
import os
import shutil
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config
import runtime_guard

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_keyhealth_")
# Файл статуса уводим в temp: generate_text пишет в него флаг «идёт генерация».
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")

import gemini_client as gc

gc.BANNED_MODELS_FILE = os.path.join(_TMPDIR, "banned_models.json")
gc.KEY_COOLDOWN_FILE = os.path.join(_TMPDIR, "key_cooldowns.json")

GOOGLE_KEYS = [f"gk-{i:02d}-secretpart" for i in range(10)]
GROQ_KEYS = [f"qk-{i:02d}-secretpart" for i in range(7)]
config.GOOGLE_KEYS = GOOGLE_KEYS
config.GROQ_KEYS = GROQ_KEYS
config.GEMINI_MODEL = "gemini-3.6-flash"
config.GROQ_MODEL = "llama-3.3-70b-versatile"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------- заглушки сети
TEXT_REQUESTS = []     # (model, key) каждого фактически ушедшего текстового запроса
AUDIO_REQUESTS = []    # ключ каждой фактически ушедшей расшифровки
_text_behaviour = {}   # model -> Exception | текст
_audio_behaviour = {}  # key   -> Exception | текст ("*" — для всех остальных)
SLEEPS = []


class FakeCompletions:
    def __init__(self, api_key):
        self.api_key = api_key

    def create(self, model=None, messages=None, temperature=None):
        TEXT_REQUESTS.append((model, self.api_key))
        outcome = _text_behaviour.get(model, "ответ модели")
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {"content": outcome})()})()]
        })()


class FakeTranscriptions:
    def __init__(self, api_key):
        self.api_key = api_key

    def create(self, model=None, file=None, response_format=None):
        AUDIO_REQUESTS.append(self.api_key)
        outcome = _audio_behaviour.get(self.api_key, _audio_behaviour.get("*", "расшифровка"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, api_key):
        self.chat = type("Chat", (), {"completions": FakeCompletions(api_key)})()
        self.audio = type("Audio", (), {"transcriptions": FakeTranscriptions(api_key)})()


# (api_key, base_url, timeout): timeout здесь не для красоты — он единственное
# доказательство, что доля бюджета попытки доходит до клиента. 30.0 — значение по
# умолчанию у get_openai_client, то есть «долю потеряли».
CLIENT_CALLS = []


def fake_client_maker(api_key, base_url, timeout=30.0):
    CLIENT_CALLS.append((api_key, base_url, timeout))
    return FakeClient(api_key)


gc.get_openai_client = fake_client_maker
gc._sleep_with_status = lambda seconds, ctx, attempt, max_attempts, key_id: SLEEPS.append(seconds)
# Расшифровка спит через time.sleep на 429; тайминги здесь не проверяются.
_real_sleep = time.sleep
gc.time.sleep = lambda seconds: SLEEPS.append(seconds)
# Файл голосового не конвертируем: ffmpeg на этой машине битый, а путь конвертации
# проверяется в test_voice_pipeline. Здесь важен только отбор ключей.
gc.convert_to_wav = lambda path: path

_AUDIO_PATH = os.path.join(_TMPDIR, "voice.ogg")
with open(_AUDIO_PATH, "wb") as _handle:
    _handle.write(b"\x00" * 2048)


class LogCatcher(logging.Handler):
    """Собирает готовые строки журнала: последствие проверяем по тексту записи."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        # getMessage() уже подставляет args. Ещё одно % по ним даёт TypeError и
        # роняло проверку в сырой шаблон со «%s» вместо чисел.
        try:
            self.lines.append(record.getMessage())
        except Exception:
            self.lines.append(str(record.msg))


LOG = LogCatcher()
gc.logger.addHandler(LOG)
gc.logger.setLevel(logging.INFO)


def reset(text_behaviour=None, audio_behaviour=None):
    TEXT_REQUESTS.clear()
    AUDIO_REQUESTS.clear()
    CLIENT_CALLS.clear()
    SLEEPS.clear()
    LOG.lines.clear()
    _text_behaviour.clear()
    _text_behaviour.update(text_behaviour or {})
    _audio_behaviour.clear()
    _audio_behaviour.update(audio_behaviour or {})
    for path in (gc.BANNED_MODELS_FILE, gc.KEY_COOLDOWN_FILE):
        if os.path.exists(path):
            os.remove(path)


def cooled(provider, keys):
    live = gc.get_key_cooldowns()
    return [k for k in keys if gc._key_fingerprint(provider, k) in live]


print("\n[1] Учёт здоровья ключей — один, и он публичный")
reset()
for name in ("available_keys", "active_models", "note_key_failure", "note_success",
             "get_provider_client", "provider_pool"):
    check(f"gemini_client.{name} доступен другим модулям", callable(getattr(gc, name, None)))

fresh, cooling, wait = gc.available_keys("groq", GROQ_KEYS)
check("без пометок все ключи живые", len(fresh) == 7 and not cooling, f"got {len(fresh)}/{len(cooling)}")
gc.set_key_cooldown("groq", GROQ_KEYS[0], seconds=300)
gc.set_key_cooldown("groq", GROQ_KEYS[1], seconds=300)
fresh, cooling, wait = gc.available_keys("groq", GROQ_KEYS)
check("остывающие отделены от живых", len(fresh) == 5 and len(cooling) == 2,
      f"got живых {len(fresh)}, остывающих {len(cooling)}")
check("порядок внутри живых сохранён (квота размазывается вызывающим)",
      fresh == [k for k in GROQ_KEYS if k not in GROQ_KEYS[:2]], f"got {fresh}")
check("ожидание не называется, пока есть живые", wait == 0, f"got {wait}")
for key in GROQ_KEYS[2:]:
    gc.set_key_cooldown("groq", key, seconds=120)
fresh, cooling, wait = gc.available_keys("groq", GROQ_KEYS)
check("когда живых нет — назван срок ближайшего освобождения",
      not fresh and len(cooling) == 7 and 100 <= wait <= 120, f"got {fresh}, wait={wait}")
check("кулдаун одного провайдера не задевает другой",
      len(gc.available_keys("gemini", GOOGLE_KEYS)[0]) == 10,
      f"got {len(gc.available_keys('gemini', GOOGLE_KEYS)[0])}")

reset()
gc.ban_model("gemini-3.6-flash", 600)
active = gc.active_models([("gemini-3.6-flash", "gemini"), ("gemini-3.5-flash", "gemini")])
check("забаненная модель убрана из каскада", active == [("gemini-3.5-flash", "gemini")], f"got {active}")
gc.ban_model("gemini-3.5-flash", 600)
active = gc.active_models([("gemini-3.6-flash", "gemini"), ("gemini-3.5-flash", "gemini")])
check("забанены все — остаётся последняя, а не пустота",
      active == [("gemini-3.5-flash", "gemini")], f"got {active}")

check("адрес провайдера берётся из общей таблицы",
      set(gc.PROVIDER_BASE_URLS) == {"gemini", "groq"}, f"got {gc.PROVIDER_BASE_URLS}")
check("пул ключей провайдера виден через учёт",
      len(gc.provider_pool("groq")) == 7 and len(gc.provider_pool("gemini")) == 10,
      f"got {len(gc.provider_pool('groq'))}/{len(gc.provider_pool('gemini'))}")
try:
    gc.get_provider_client("openrouter", "k")
    _unknown_provider_raised = False
except ValueError:
    _unknown_provider_raised = True
except Exception as _other:
    _unknown_provider_raised = f"другое исключение: {_other!r}"
check("неизвестный провайдер — отказ, а не молчаливый клиент к пустому адресу",
      _unknown_provider_raised is True, f"got {_unknown_provider_raised}")


print("\n[2] Текстовый каскад: ключ на кулдауне НЕ пробуется раньше срока")
reset({"gemini-3.5-flash-lite": "готовый ответ"})
for key in GOOGLE_KEYS[:8]:
    gc.set_key_cooldown("gemini", key, seconds=300)
res = gc.generate_text("вопрос врача", {"kind": "pm_chat"})
check("ответ получен при 8 остывающих из 10", res is not None and res.text == "готовый ответ", f"got {res}")
used = [k for _, k in TEXT_REQUESTS]
check("ни один запрос не ушёл на остывающий ключ", not (set(used) & set(GOOGLE_KEYS[:8])), f"got {used}")
check("клиент создавался только под живые ключи",
      all(k not in GOOGLE_KEYS[:8] for k, _, _ in CLIENT_CALLS), f"got {CLIENT_CALLS}")


print("\n[3] Расшифровка голосового: живые ключи ПЕРВЫМИ, остывающие в хвост")
# Замер: при 7 ключах и бюджете 60 с доля попытки 7 с, три остывающих ключа
# съедали 21 с (44% бюджета) и оставляли 3 живые попытки вместо 7.
reset(audio_behaviour={"*": Exception("500 internal server error")})
cold_three = GROQ_KEYS[:3]
for key in cold_three:
    gc.set_key_cooldown("groq", key, seconds=300)
out = gc.transcribe_audio_bytes_or_file(_AUDIO_PATH, timeout=60)
check("все ключи в итоге испробованы (расшифровка не сдаётся молча)",
      len(AUDIO_REQUESTS) == 7, f"got {len(AUDIO_REQUESTS)}")
first_four = AUDIO_REQUESTS[:4]
check("первые четыре попытки — только живые ключи",
      not (set(first_four) & set(cold_three)), f"got {first_four}")
check("остывающие ушли в хвост очереди",
      set(AUDIO_REQUESTS[4:]) == set(cold_three), f"got {AUDIO_REQUESTS[4:]}")
check("порядок сообщён в журнал",
      any("остывают" in line and "последними" in line for line in LOG.lines),
      f"журнал: {[l for l in LOG.lines if 'остыва' in l]}")

# Тот же расклад, но бюджет кончается: непробованными обязаны остаться остывающие.
reset(audio_behaviour={"*": Exception("500 internal server error")})
for key in GROQ_KEYS[:3]:
    gc.set_key_cooldown("groq", key, seconds=300)
_budget_stop = [0]
_orig_monotonic = gc.time.monotonic


def monotonic_after_four():
    # Каждый вызов сдвигает часы: бюджет 60 с (usable 48) истекает после ~4 попыток.
    _budget_stop[0] += 1
    return _orig_monotonic() + _budget_stop[0] * 12.0


gc.time.monotonic = monotonic_after_four
try:
    gc.transcribe_audio_bytes_or_file(_AUDIO_PATH, timeout=60)
finally:
    gc.time.monotonic = _orig_monotonic
check("бюджет кончился раньше ключей", 0 < len(AUDIO_REQUESTS) < 7, f"got {len(AUDIO_REQUESTS)}")
check("на остывающие ключи бюджет НЕ потрачен",
      not (set(AUDIO_REQUESTS) & set(GROQ_KEYS[:3])),
      f"истрачено на остывающие: {set(AUDIO_REQUESTS) & set(GROQ_KEYS[:3])}")

# Ключи перемешиваются, поэтому ответ задаём для любого ключа ("*"), а не для
# конкретного: иначе проверка ловит не поведение, а результат random.shuffle.
reset(audio_behaviour={"*": "расшифровка врача"})
out = gc.transcribe_audio_bytes_or_file(_AUDIO_PATH, timeout=60)
check("удачная расшифровка возвращается", out == "расшифровка врача", f"got {out!r}")
check("на удачу израсходован ровно один ключ", len(AUDIO_REQUESTS) == 1, f"got {AUDIO_REQUESTS}")


print("\n[4] Отказ по квоте у расшифровки попадает в ОБЩИЙ учёт")
reset(audio_behaviour={"*": Exception("429 Too Many Requests: rate limit exceeded")})
gc.transcribe_audio_bytes_or_file(_AUDIO_PATH, timeout=60)
marked = cooled("groq", GROQ_KEYS)
check("получившие 429 ключи помечены в учёте", len(marked) == 7, f"got {len(marked)}")
check("текстовый каскад видит те же пометки",
      not gc.available_keys("groq", GROQ_KEYS)[0], f"got {gc.available_keys('groq', GROQ_KEYS)[0]}")
check("причина отказа названа исчерпанием ключа, а не поломкой модели",
      (gc.get_last_failure() or {}).get("reason") == "key_rate_limited", f"got {gc.get_last_failure()}")
check("сырой ключ в разбор провала не утёк",
      all(k not in str(gc.get_last_failure()) for k in GROQ_KEYS), f"got {gc.get_last_failure()}")

# Обратная сторона: пометка от расшифровки уводит текстовый каскад к Gemini.
reset({"llama-3.3-70b-versatile": "ответ groq", "gemini-3.5-flash-lite": "ответ gemini"},
      audio_behaviour={"*": Exception("429 quota exceeded")})
gc.transcribe_audio_bytes_or_file(_AUDIO_PATH, timeout=60)
res = gc.generate_text("короткий вопрос", {"kind": "llama_triage"})
check("после 429 у расшифровки триаж не бьёт в те же ключи Groq",
      not (set(k for _, k in TEXT_REQUESTS) & set(GROQ_KEYS)), f"got {TEXT_REQUESTS}")
check("ответ врачу всё равно получен, другим провайдером",
      res is not None and res.text == "ответ gemini", f"got {res}")


print("\n[5] Удачный ответ СНИМАЕТ пометку")
reset(audio_behaviour={GROQ_KEYS[3]: "расшифровка после кулдауна"})
gc.set_key_cooldown("groq", GROQ_KEYS[3], seconds=300)
check("пометка стоит до вызова", GROQ_KEYS[3] in cooled("groq", GROQ_KEYS))
# Остальные ключи отдают отказ, поэтому дело доходит до остывающего в хвосте.
_audio_behaviour.update({k: Exception("500 internal server error") for k in GROQ_KEYS if k != GROQ_KEYS[3]})
out = gc.transcribe_audio_bytes_or_file(_AUDIO_PATH, timeout=225)
check("остывающий ключ всё же ответил", out == "расшифровка после кулдауна", f"got {out!r}")
check("пометка снята удачей", GROQ_KEYS[3] not in cooled("groq", GROQ_KEYS),
      f"остались пометки: {cooled('groq', GROQ_KEYS)}")
check("снятие названо в журнале",
      any("cooldown lifted" in line for line in LOG.lines),
      f"журнал: {LOG.lines[-4:]}")

reset({"gemini-3.5-flash-lite": "ответ модели"})
gc.set_key_cooldown("gemini", GOOGLE_KEYS[0], seconds=300)
gc.generate_text("вопрос", {"kind": "pm_chat"})
check("удача по живому ключу чужих пометок не снимает",
      GOOGLE_KEYS[0] in cooled("gemini", GOOGLE_KEYS), f"got {cooled('gemini', GOOGLE_KEYS)}")

# Бан модели снимается тем же правилом: active_models принудительно берёт
# последнюю забаненную, она отвечает — значит забанена она напрасно.
reset({"llama-3.3-70b-versatile": "ответ резервной"})
for model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.6-27b",
              "llama-3.3-70b-versatile"):
    gc.ban_model(model, 1200)
res = gc.generate_text("вопрос", {"kind": "assistant"})
check("при полном бане каскада ответ всё равно получен",
      res is not None and res.text == "ответ резервной", f"got {res}")
check("ответившая модель разбанена", "llama-3.3-70b-versatile" not in gc.get_banned_models(),
      f"got {sorted(gc.get_banned_models())}")
check("остальные баны не тронуты", "gemini-3.5-flash-lite" in gc.get_banned_models(),
      f"got {sorted(gc.get_banned_models())}")
check("снятие бана названо в журнале", any("ban lifted" in line for line in LOG.lines),
      f"журнал: {LOG.lines[-4:]}")


print("\n[6] Отказ по квоте называет в журнале ПОСЛЕДСТВИЕ, а не механику")
reset({"gemini-3.5-flash-lite": Exception("429 RESOURCE_EXHAUSTED: quota_limit_value: 500 per day"),
       "gemini-3.1-flash-lite": "ответ резервной"})
gc.generate_text("вопрос", {"kind": "pm_chat"}, timeout=90)
quota_lines = [l for l in LOG.lines if "429" in l and "cooldown" in l]
check("запись об отказе по квоте есть", quota_lines, f"журнал: {LOG.lines[:6]}")
check("в записи назван остаток живых ключей",
      any("Последствие" in l and "осталось" in l for l in quota_lines), f"got {quota_lines}")
check("остаток назван числом из живого пула",
      any(" из 10" in l for l in quota_lines), f"got {quota_lines}")

reset({"gemini-3.5-flash-lite": Exception("429 quota exceeded")})
for key in GOOGLE_KEYS[1:]:
    gc.set_key_cooldown("gemini", key, seconds=300)
gc.generate_text("вопрос", {"kind": "pm_chat"}, timeout=90)
last_lines = [l for l in LOG.lines if "429" in l and "cooldown" in l]
check("исчерпание последнего ключа названо отказом врачу",
      any("НЕ ОСТАЛОСЬ" in l and "не смог ответить" in l for l in last_lines), f"got {last_lines}")


print("\n[7] Бан модели ставится только там, где у пути есть замена")
reset()
gc.note_key_failure("gemini", GOOGLE_KEYS[0], "503 Service Unavailable", model_name="gemini-3.6-flash")
check("модель с заменой банится", "gemini-3.6-flash" in gc.get_banned_models(),
      f"got {gc.get_banned_models()}")
check("503 не ставит ключ на кулдаун — виновата модель, не ключ",
      not cooled("gemini", GOOGLE_KEYS), f"got {cooled('gemini', GOOGLE_KEYS)}")

reset(audio_behaviour={"*": Exception("503 Service Unavailable: model overloaded")})
gc.transcribe_audio_bytes_or_file(_AUDIO_PATH, timeout=60)
check("whisper-large-v3 не банится: замены нет, бан = отказ всех расшифровок",
      "whisper-large-v3" not in gc.get_banned_models(), f"got {gc.get_banned_models()}")
check("баны вообще не появились от расшифровки", gc.get_banned_models() == {},
      f"got {gc.get_banned_models()}")

print("\n[8] Порядок разбора отказа: квота ПЕРВОЙ, бан второй")
# В теле 429 провайдер пишет сам лимит квоты, а он часто 500-класса. Если бан
# проверять первым, один выдохшийся ключ выносит РАБОЧУЮ модель из каскада на
# 20 минут для всех подпроцессов сразу.
for label, text in (
    ("quota_limit_value: 500", "429 RESOURCE_EXHAUSTED: quota exceeded, quota_limit_value: 500 per day"),
    ("limit 500 requests", "429 Too Many Requests: rate limit reached, limit 500 requests per day"),
    ("503 в примечании к 429", "429 rate limit; retry later (503 upstream note)"),
):
    reset()
    reason = gc.note_key_failure("gemini", GOOGLE_KEYS[0], text, model_name="gemini-3.6-flash")
    check(f"«{label}»: разобрано как исчерпание ключа", reason == "key_rate_limited", f"got {reason}")
    check(f"«{label}»: модель НЕ забанена", "gemini-3.6-flash" not in gc.get_banned_models(),
          f"got {gc.get_banned_models()}")
    check(f"«{label}»: ключ помечен", GOOGLE_KEYS[0] in cooled("gemini", GOOGLE_KEYS))

reset()
check("посторонний отказ с числом 500 в тексте — не поломка модели",
      gc.note_key_failure("gemini", GOOGLE_KEYS[0],
                          "Invalid request: max_tokens 1500 exceeds context length of 500000 tokens",
                          model_name="gemini-3.6-flash") == "request_failed",
      "разобрано неверно")
check("такая ошибка модель не банит", "gemini-3.6-flash" not in gc.get_banned_models(),
      f"got {gc.get_banned_models()}")
reset()
check("403 разобран как отказ ключу", gc.note_key_failure(
    "groq", GROQ_KEYS[0], "403 Forbidden: permission denied") == "key_denied")
check("403 не ставит кулдаун и не банит",
      not cooled("groq", GROQ_KEYS) and gc.get_banned_models() == {},
      f"got {cooled('groq', GROQ_KEYS)}, {gc.get_banned_models()}")


print("\n[9] Секрет не утекает через учёт")
reset()
gc.set_key_cooldown("groq", GROQ_KEYS[0], seconds=300)
on_disk = io.open(gc.KEY_COOLDOWN_FILE, encoding="utf-8").read()
check("сырого ключа в файле кулдаунов нет", "secretpart" not in on_disk, "в файле найден фрагмент ключа")
reset()
gc.note_key_failure("groq", GROQ_KEYS[0], f"401 invalid api key {GROQ_KEYS[1]} rejected")
check("чужой ключ из текста ошибки вычищен",
      GROQ_KEYS[1] not in str(gc.get_last_failure()), f"got {gc.get_last_failure()}")
disk2 = io.open(gc.KEY_COOLDOWN_FILE, encoding="utf-8").read() if os.path.exists(gc.KEY_COOLDOWN_FILE) else ""
check("и в файл он тоже не попал", "secretpart" not in disk2)


print("\n[10] Общая точка создания клиента НЕ теряет долю бюджета попытки")
# Дописано после саботажа: диверсия «get_provider_client отдаёт клиента с
# таймаутом по умолчанию» не была замечена НИ ОДНОЙ проверкой. А это тот самый
# класс дефекта, за который проект уже платил: у whisper 7 ключей по 30 с вместо
# 7 с складываются в 210 с внутри родительского дедлайна 70 с (60 внешних плюс
# 10 на подъём подпроцесса) — врач не получает расшифровку вовсе.
reset(audio_behaviour={"*": Exception("500 internal server error")})
gc.transcribe_audio_bytes_or_file(_AUDIO_PATH, timeout=60)
audio_timeouts = sorted({t for _, _, t in CLIENT_CALLS})
# Доля попытки при бюджете 60: max(7.0, (60 - 12 резерва) / 7 ключей) = 7.0 с.
check("расшифровка отдаёт клиенту свою долю бюджета, а не 30 с по умолчанию",
      audio_timeouts == [7.0], f"got {audio_timeouts}")
check("суммарный внутренний бюджет расшифровки влезает в родительские 70 с",
      sum(t for _, _, t in CLIENT_CALLS) <= 70, f"got {sum(t for _, _, t in CLIENT_CALLS):.0f} с")

reset({"gemini-3.5-flash-lite": Exception("connection reset by peer")})
gc.generate_text("вопрос", {"kind": "pm_chat"}, timeout=30)
text_timeouts = [t for _, _, t in CLIENT_CALLS]
check("текстовый каскад тоже отдаёт долю, а не значение по умолчанию",
      text_timeouts and all(0 < t < 30.0 for t in text_timeouts), f"got {text_timeouts}")
check("сумма таймаутов запросов не вылезает за выданный бюджет",
      sum(text_timeouts) <= 30, f"got {sum(text_timeouts):.1f} с при бюджете 30")


print("\n[11] Кулдаун держится ПОЛНЫЙ срок, а не формально")
# Дописано после саботажа: диверсия «set_key_cooldown на 5 секунд вместо 300» не
# была замечена. Пометка на 5 с бесполезна: исчерпанный ключ возвращается в пул
# к следующему же сообщению врача и снова съедает попытку.
reset()
for key in GOOGLE_KEYS:
    gc.note_key_failure("gemini", key, "429 Too Many Requests: quota exceeded")
fresh, cooling, wait = gc.available_keys("gemini", GOOGLE_KEYS)
check("после 429 по всем ключам живых не осталось", not fresh and len(cooling) == 10,
      f"got живых {len(fresh)}")
check("срок пометки — минуты, а не секунды", wait >= 240,
      f"осталось {wait} с — пометка снимется раньше, чем квота восстановится")
check("и он не длиннее объявленного", wait <= gc.KEY_COOLDOWN_SECONDS,
      f"got {wait} при объявленных {gc.KEY_COOLDOWN_SECONDS}")

reset()
gc.note_key_failure("gemini", GOOGLE_KEYS[0], "503 Service Unavailable", model_name="gemini-3.6-flash")
ban_left = gc.get_banned_models().get("gemini-3.6-flash", 0) - time.time()
check("бан модели тоже держится полный срок", ban_left >= 1000,
      f"осталось {int(ban_left)} с при объявленных {gc.MODEL_BAN_SECONDS}")


print("\n[12] Ни одного живого сетевого вызова")
check("все клиенты шли через подставную фабрику",
      all(url in gc.PROVIDER_BASE_URLS.values() for _, url, _ in CLIENT_CALLS) or not CLIENT_CALLS,
      f"got {set(u for _, u, _ in CLIENT_CALLS)}")
check("боевые файлы учёта не тронуты",
      gc.KEY_COOLDOWN_FILE.startswith(_TMPDIR) and gc.BANNED_MODELS_FILE.startswith(_TMPDIR),
      f"got {gc.KEY_COOLDOWN_FILE}")
check("боевой файл кулдаунов в корне не создан",
      not os.path.exists("key_cooldowns.json"), "рядом с репозиторием появился боевой файл")

gc.time.sleep = _real_sleep
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
