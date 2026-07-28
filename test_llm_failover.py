"""
Отказоустойчивость LLM-каскада: ротация ключей, кулдауны, баны моделей.

Проверяется настоящий gemini_client.generate_text. Заглушен только сетевой
клиент OpenAI — вместо него объект, который отдаёт заданные ошибки и считает
реальные запросы. Файлы кулдаунов и банов подменяются на временные, боевые не
читаются и не пишутся.

Ключевое утверждение: попытка тратится только на РЕАЛЬНЫЙ запрос. Раньше ключ
на кулдауне съедал попытку молча, и при десяти рабочих ключах бот докладывал
«All AI attempts exhausted».

Запуск: python test_llm_failover.py
"""
import io
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

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_llm_")

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


REQUESTS = []          # (model, key) каждого фактически ушедшего запроса
_behaviour = {}        # model -> Exception | текст ответа


class FakeCompletions:
    def __init__(self, api_key):
        self.api_key = api_key

    def create(self, model=None, messages=None, temperature=None):
        REQUESTS.append((model, self.api_key))
        outcome = _behaviour.get(model, "ответ модели")
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {"content": outcome})()})()]
        })()


class FakeClient:
    def __init__(self, api_key):
        self.chat = type("Chat", (), {"completions": FakeCompletions(api_key)})()


def fake_client_maker(api_key, base_url, timeout=30.0):
    return FakeClient(api_key)


gc.get_openai_client = fake_client_maker
# Сон между попытками в тесте не нужен: проверяем маршрутизацию, не тайминги.
gc._sleep_with_status = lambda seconds, ctx, attempt, max_attempts, key_id: None
gc.runtime_guard.write_summary_status = lambda payload: None


def reset(behaviour=None):
    REQUESTS.clear()
    _behaviour.clear()
    _behaviour.update(behaviour or {})
    for path in (gc.BANNED_MODELS_FILE, gc.KEY_COOLDOWN_FILE):
        if os.path.exists(path):
            os.remove(path)


def keys_used():
    return [k for _, k in REQUESTS]


def models_used():
    return [m for m, _ in REQUESTS]


print("\n[1] Отпечаток ключа не раскрывает сам ключ")
fp = gc._key_fingerprint("gemini", GOOGLE_KEYS[0])
check("отпечаток стабилен", fp == gc._key_fingerprint("gemini", GOOGLE_KEYS[0]))
check("разные ключи — разные отпечатки", fp != gc._key_fingerprint("gemini", GOOGLE_KEYS[1]))
check("провайдер входит в отпечаток", fp != gc._key_fingerprint("groq", GOOGLE_KEYS[0]))
check("в отпечатке нет фрагментов ключа",
      "secretpart" not in fp and GOOGLE_KEYS[0] not in fp, f"got {fp}")

print("\n[2] Кулдаун ключа переживает процесс (файл, а не память модуля)")
reset()
gc.set_key_cooldown("gemini", GOOGLE_KEYS[0], seconds=300)
check("кулдаун записан на диск", os.path.exists(gc.KEY_COOLDOWN_FILE))
check("перечитывается заново", gc._key_fingerprint("gemini", GOOGLE_KEYS[0]) in gc.get_key_cooldowns())
on_disk = open(gc.KEY_COOLDOWN_FILE, encoding="utf-8").read()
check("сырого ключа в файле нет", "secretpart" not in on_disk, "в файле найден фрагмент ключа")

print("\n[3] Истёкший кулдаун не блокирует ключ")
gc.set_key_cooldown("gemini", GOOGLE_KEYS[1], seconds=-1)
check("протухшая запись отброшена при чтении",
      gc._key_fingerprint("gemini", GOOGLE_KEYS[1]) not in gc.get_key_cooldowns())

print("\n[4] Попытки не сгорают на ключах, стоящих на кулдауне")
# Восемь из десяти ключей Google остывают. При бюджете 3 попытки живых ключей
# хватает — модель обязана быть опрошена, а не пропущена.
reset({"gemini-3.5-flash-lite": None})
for key in GOOGLE_KEYS[:8]:
    gc.set_key_cooldown("gemini", key, seconds=300)
_behaviour["gemini-3.5-flash-lite"] = "готовый ответ"

res = gc.generate_text("вопрос", {"kind": "pm_chat"})
check("ответ получен, несмотря на 8 остывающих ключей", res is not None and res.text == "готовый ответ",
      f"got {res}")
cold = set(GOOGLE_KEYS[:8])
check("ни один запрос не ушёл на остывающий ключ",
      not (set(keys_used()) & cold), f"got {keys_used()}")
check("сделан ровно один запрос", len(REQUESTS) == 1, f"got {REQUESTS}")

print("\n[5] Все ключи провайдера на кулдауне — каскад идёт дальше, а не молчит")
reset({"gemini-3.5-flash-lite": None, "gemini-3.1-flash-lite": None,
       "qwen/qwen3.6-27b": "ответ от groq"})
for key in GOOGLE_KEYS:
    gc.set_key_cooldown("gemini", key, seconds=300)

res = gc.generate_text("вопрос", {"kind": "pm_chat"})
check("ответ пришёл от следующего провайдера", res is not None and res.text == "ответ от groq",
      f"got {res}")
check("к Google не обращались вовсе", not (set(keys_used()) & set(GOOGLE_KEYS)),
      f"got {keys_used()}")

print("\n[6] 429 ставит ключ на кулдаун и переходит к следующему")
reset({"gemini-3.5-flash-lite": Exception("429 Too Many Requests: rate limit exceeded")})
res = gc.generate_text("вопрос", {"kind": "pm_chat"})
tried = keys_used()
check("испробовано несколько разных ключей", len(set(tried)) > 1, f"got {tried}")
cooled = gc.get_key_cooldowns()
check("получившие 429 ключи отмечены",
      any(gc._key_fingerprint("gemini", k) in cooled for k in GOOGLE_KEYS), f"got {cooled}")

print("\n[7] Посторонний отказ с числом 500 в тексте модель НЕ банит")
reset({"gemini-3.6-flash": Exception(
    "Invalid request: max_tokens 1500 exceeds context length of 500000 tokens")})
gc.generate_text("сводка", {"kind": "summary"})
banned = gc.get_banned_models()
check("модель не забанена по подстроке", "gemini-3.6-flash" not in banned, f"got {banned}")

print("\n[8] Настоящий 503 модель банит")
reset({"gemini-3.6-flash": Exception("503 Service Unavailable: model overloaded")})
gc.generate_text("сводка", {"kind": "summary"})
banned = gc.get_banned_models()
check("модель забанена", "gemini-3.6-flash" in banned, f"got {banned}")
overloaded_hits = [m for m in models_used() if m == "gemini-3.6-flash"]
check("на 503 перегруженную модель не долбили остальными ключами",
      len(overloaded_hits) == 1, f"got {REQUESTS}")
check("каскад ушёл к следующей модели",
      "gemini-3.5-flash" in models_used() or len(set(models_used())) > 1, f"got {models_used()}")

print("\n[9] Забаненная модель пропускается на следующем вызове")
reset({"gemini-3.6-flash": "не должно вызваться", "gemini-3.5-flash": "ответ резервной"})
gc.ban_model("gemini-3.6-flash", 1200)
res = gc.generate_text("сводка", {"kind": "summary"})
check("основная модель не опрашивалась", "gemini-3.6-flash" not in models_used(),
      f"got {models_used()}")
check("ответ дала резервная", res is not None and res.text == "ответ резервной", f"got {res}")

print("\n[10] Битый файл банов не блокирует работу")
reset({"gemini-3.6-flash": "ответ"})
with open(gc.BANNED_MODELS_FILE, "w", encoding="utf-8") as handle:
    handle.write('{"gemini-3.6-flash": ')  # обрезанный JSON
check("битый файл читается как пустой", gc.get_banned_models() == {})
res = gc.generate_text("сводка", {"kind": "summary"})
check("вызов прошёл", res is not None and res.text == "ответ", f"got {res}")

print("\n[11] Запись банов атомарна")
reset()
gc.ban_model("m1", 600)
gc.ban_model("m2", 600)
check("обе записи на месте", set(gc.get_banned_models()) == {"m1", "m2"}, f"got {gc.get_banned_models()}")
check("временный файл не остался", not os.path.exists(gc.BANNED_MODELS_FILE + ".tmp"))

print("\n[12] Классификация ошибок")
retryable = [
    "429 too many requests", "503 service unavailable", "504 gateway timeout",
    "deadline exceeded", "connection reset", "quota exceeded", "rate limit reached",
]
for msg in retryable:
    check(f"«{msg}» повторяемая", gc._is_retryable_gemini_error(msg) is True)

check("«failed to generate content» НЕ повторяемая по слову generate",
      gc._is_retryable_gemini_error("failed to generate content: invalid argument") is False)
check("«context length 500000» не считается серверной ошибкой",
      gc._SERVER_ERROR_RE.search("context length of 500000 tokens") is None)
check("«1500 tokens» не считается серверной ошибкой",
      gc._SERVER_ERROR_RE.search("max_tokens 1500 given") is None)
check("«500 internal server error» считается",
      gc._SERVER_ERROR_RE.search("500 internal server error") is not None)

print("\n[13] Флаг «идёт работа» снимается за обычными вызовами")
# Любой вызов ассистента писал в файл статуса active: True, а снять его было
# некому: clear_summary_status зовёт только summarizer. Сторож саммари убивает
# процесс (os._exit 79), если флаг стоит, а метка старше 30 минут.
# В боевых журналах он не срабатывал ни разу — почему, объяснить не удалось,
# но взведённый os._exit убран независимо от этого.
# Выше по файлу runtime_guard.write_summary_status подменён заглушкой, чтобы
# прогон каскада не писал на диск, и восстановить оригинал из модуля уже нельзя.
# Берём свежий экземпляр модуля и уводим его путь во временный каталог.
import importlib.util as _ilu

_status_dir = tempfile.mkdtemp(prefix="stomchat_status_")
_spec = _ilu.spec_from_file_location(
    "runtime_guard_for_status", os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_guard.py"))
_rg = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_rg)
_rg.SUMMARY_STATUS_PATH = os.path.join(_status_dir, "bot_summary_status.json")
gc.runtime_guard = _rg


def _status():
    import json as _json
    try:
        return _json.load(open(_rg.SUMMARY_STATUS_PATH, encoding="utf-8"))
    except Exception:
        return {}

gc._write_generation_status({"kind": "pm_chat"}, stage="gemini_request")
check("во время генерации флаг взведён", _status().get("active") is True)
gc._release_generation_status({"kind": "pm_chat"})
check("после ответа в ЛС флаг снят", _status().get("active") is False,
      f"got {_status()}")
check("причина снятия записана", _status().get("stage") == "pm_chat_done",
      f"got {_status().get('stage')}")

for kind in ("llama_triage", "response_validator", "group_ask", "assistant_media"):
    gc._write_generation_status({"kind": kind}, stage="req")
    gc._release_generation_status({"kind": kind})
    check(f"«{kind}» снимает за собой", _status().get("active") is False)

gc._write_generation_status({"kind": "daily"}, stage="gemini_generation_start")
gc._release_generation_status({"kind": "daily"})
check("дневной отчёт флаг НЕ снимает — его конвейер ещё идёт",
      _status().get("active") is True, f"got {_status()}")
gc._write_generation_status({"kind": "weekly"}, stage="gemini_generation_start")
gc._release_generation_status({"kind": "weekly"})
check("недельный отчёт флаг НЕ снимает", _status().get("active") is True)

gc._write_generation_status({"kind": "daily"}, stage="telegraph")
gc._release_generation_status({"kind": "llama_triage"})
check("ответ ассистента не снимает защиту с идущего отчёта",
      _status().get("active") is True and _status().get("kind") == "daily",
      f"got {_status()}")

check("пустой контекст не роняет", gc._release_generation_status(None) is None)
shutil.rmtree(_status_dir, ignore_errors=True)

print("\n[14] Поток размышлений модели не выдаётся за ответ")
# Рассуждающие модели (qwen и подобные) заворачивают черновик в <think>.
# Обработка была в двух местах и в обоих неверна по-разному:
#   * gemini_client проверял непустоту ДО срезки — ответ из одних размышлений
#     уходил наружу пустым, но с признаком УСПЕХА, и каскад обрывался на первой
#     такой попытке: резервный провайдер не пробовался вообще;
#   * незакрытый <think> (обрыв по лимиту токенов) шаблон <think>.*?</think> не
#     срезает совсем — черновик уходил врачу дословно;
#   * vision в ветке незакрытого тега при пустом начале брал parts2[1], то есть
#     отдавал САМИ размышления как клиническое описание снимка.
for _label, _source, _expected in (
    ("только размышления", "<think>прикидываю варианты</think>", ""),
    ("размышления и ответ", "<think>прикидываю</think>Ставьте 35 Нсм.", "Ставьте 35 Нсм."),
    ("незакрытый тег в начале", "<think>рассуждаю и обрыв", ""),
    ("незакрытый тег после ответа", "Ответ по существу.<think>а теперь думаю", "Ответ по существу."),
    ("закрывающий без открывающего", "хвост рассуждений</think>Итог: 2 карпулы.", "Итог: 2 карпулы."),
    ("без тегов", "Обычный ответ врачу.", "Обычный ответ врачу."),
    ("пустая строка", "", ""),
    ("None", None, ""),
    ("два блока", "<think>раз</think>Первое.<think>два</think>Второе.", "Первое.Второе."),
):
    check(_label, gc.strip_reasoning(_source) == _expected,
          f"got {gc.strip_reasoning(_source)!r}, ожидалось {_expected!r}")

# Главное свойство: наружу не должно уйти НИ СЛОВА из черновика.
_leaks = [s for s in ("<think>прикидываю варианты</think>",
                      "<think>рассуждаю и обрыв",
                      "Ответ.<think>а теперь думаю")
          if any(w in (gc.strip_reasoning(s) or "")
                 for w in ("think", "прикидыв", "рассужд", "думаю"))]
check("ни один черновик не просочился", not _leaks, f"утечки: {_leaks}")

# Пустой результат обязан читаться вызывающим как НЕУДАЧА попытки, а не ответ.
_src = io.open("gemini_client.py", encoding="utf-8").read()
_after = _src.split("text_result = strip_reasoning(text_result)", 1)
check("срезка стоит отдельным шагом перед проверкой", len(_after) == 2,
      "порядок вернулся к прежнему: проверка до срезки")
if len(_after) == 2:
    # Ветка пустого ответа идёт сразу за блоком успеха; окно берём до конца
    # обработчика попытки, а не наугад по числу символов.
    _tail = _after[1].split("except Exception", 1)[0]
    check("пустой результат уходит в ветку empty_response", "empty_response" in _tail,
          "пустота снова считается успехом")
    check("между срезкой и проверкой нет ничего лишнего",
          _tail.lstrip().startswith("if text_result:"),
          f"после срезки идёт: {_tail.lstrip()[:60]!r}")
# Комментарии убираем: местную срезку в vision описывает как раз комментарий,
# и без этого проверка ловила бы собственное пояснение.
_vis = "\n".join(l for l in io.open("vision.py", encoding="utf-8").read().split("\n")
                 if not l.lstrip().startswith("#"))
check("vision использует общий помощник, а не свою копию",
      "gemini_client.strip_reasoning" in _vis and "parts2" not in _vis,
      "в vision осталась местная срезка")

shutil.rmtree(_TMPDIR, ignore_errors=True)
print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
