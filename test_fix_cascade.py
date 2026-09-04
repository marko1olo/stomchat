"""
Каскад LLM: арифметика бюджета, маршрутизация по видам работ, разделение
«ключ исчерпан» / «модель перегружена», причина провала.

Проверяется настоящий gemini_client.generate_text. Заглушены только сеть
(вместо OpenAI-клиента объект, который считает запросы и отдаёт заданные
ошибки), сон между попытками и часы — часы подменены нарочно: без них нельзя
проверить, что худший случай каскада укладывается в бюджет родителя, не ожидая
90 реальных секунд. Файлы банов, кулдаунов и статуса уводятся в tempfile,
боевые не читаются и не пишутся.

Главное утверждение: подпроцесс обязан успеть пройти каскад ДО того, как
родитель (blocking_tools, asyncio.wait_for на тот же timeout) его убьёт.

Запуск: python test_fix_cascade.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time as _real_time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_cascade_")
# Журнал уводим ДО импорта модулей проекта: иначе прогон пишет в боевой bot.log.
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "cascade_test.log")

import config  # noqa: E402
import runtime_guard  # noqa: E402
import gemini_client as gc  # noqa: E402

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

gc.BANNED_MODELS_FILE = os.path.join(_TMPDIR, "banned_models.json")
gc.KEY_COOLDOWN_FILE = os.path.join(_TMPDIR, "key_cooldowns.json")
# _write_generation_status пишет через runtime_guard в bot_summary_status.json.
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")

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


class Clock:
    """
    Часы каскада под управлением теста.

    Настоящий time.monotonic сделал бы проверку бюджета невозможной: чтобы
    увидеть, укладываются ли 12 попыток по 30 с в 90 с, пришлось бы ждать эти
    секунды. Здесь время двигают только два события — запрос (на свой таймаут,
    то есть худший случай: ответа не дождались) и сон между попытками.
    """

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        # Кулдауны и баны живут в реальном времени: их сравнивают с записями
        # на диске, сделанными настоящим time.time().
        return _real_time.time()

    def sleep(self, seconds):
        self.t += float(seconds)


CLOCK = Clock()
gc.time = CLOCK

REQUESTS = []       # (model, key, timeout) каждого фактически ушедшего запроса
EVENTS = []         # ("req", model) / ("sleep", seconds) по порядку
SLEEPS = []
_behaviour = {}     # model -> Exception | текст ответа
_spend_full_timeout = True


class FakeCompletions:
    def __init__(self, api_key, timeout):
        self.api_key = api_key
        self.timeout = timeout

    def create(self, model=None, messages=None, temperature=None):
        REQUESTS.append((model, self.api_key, self.timeout))
        EVENTS.append(("req", model))
        # Худший случай: запрос висит до собственного таймаута. Быстрый отказ
        # (400, 429) стоит миллисекунды — режим переключается флагом.
        CLOCK.t += self.timeout if _spend_full_timeout else 0.01
        outcome = _behaviour.get(model, "ответ модели")
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {"content": outcome})()})()]
        })()


class FakeClient:
    def __init__(self, api_key, timeout):
        self.chat = type("Chat", (), {"completions": FakeCompletions(api_key, timeout)})()


def fake_client_maker(api_key, base_url, timeout=30.0):
    return FakeClient(api_key, timeout)


def fake_sleep(seconds, ctx, attempt, max_attempts, key_id):
    SLEEPS.append(round(float(seconds), 2))
    EVENTS.append(("sleep", round(float(seconds), 2)))
    CLOCK.t += float(seconds)


gc.get_openai_client = fake_client_maker
gc._sleep_with_status = fake_sleep


def reset(behaviour=None, spend_full_timeout=True):
    global _spend_full_timeout
    REQUESTS.clear()
    EVENTS.clear()
    SLEEPS.clear()
    _behaviour.clear()
    _behaviour.update(behaviour or {})
    _spend_full_timeout = spend_full_timeout
    CLOCK.t = 0.0
    for path in (gc.BANNED_MODELS_FILE, gc.KEY_COOLDOWN_FILE,
                 runtime_guard.SUMMARY_STATUS_PATH):
        if os.path.exists(path):
            os.remove(path)


def models_used():
    return [m for m, _, _ in REQUESTS]


def providers_used():
    return ["groq" if k in GROQ_KEYS else "gemini" for _, k, _ in REQUESTS]


def timeouts_used():
    return [t for _, _, t in REQUESTS]


def status():
    try:
        with open(runtime_guard.SUMMARY_STATUS_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


ANY_FAIL = Exception("400 invalid argument: prompt is malformed")

print("\n[1] ГЛАВНОЕ: худший случай каскада укладывается в бюджет родителя")
# Родитель убивает подпроцесс через timeout секунд (blocking_tools:
# asyncio.wait_for). Было req_timeout = timeout/3, как будто запросов три, а их
# моделей x попыток: при бюджете 90 с — 12 x 30 с = 360 с, перерасход вчетверо.
# Процесс умирал на третьей попытке ПЕРВОЙ модели, до резервного провайдера в
# конце каскада каскад не доходил физически.
for kind, budget in (("assistant", 90), ("pm_chat", 90), ("bot_mention_reply", 60),
                     ("llama_triage", 20), ("response_validator", 15),
                     ("referee_analyser", 45), ("daily", 2100),
                     ("transcription_corrector", 20)):
    reset({m: ANY_FAIL for m in (
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b", "llama-3.3-70b-versatile",
        "openai/gpt-oss-120b")})
    res = gc.generate_text("вопрос", {"kind": kind, "thinking_level": "HIGH"}, timeout=budget)
    spent = CLOCK.t
    planned = sum(timeouts_used())
    check(f"«{kind}» бюджет {budget}s: худший случай {spent:.0f}s не превышает бюджет",
          spent <= budget + 0.01,
          f"каскад занял {spent:.1f}s при бюджете {budget}s, запросов {len(REQUESTS)}")
    check(f"«{kind}»: сумма таймаутов запросов {planned:.0f}s влезает в бюджет",
          planned <= budget + 0.01, f"{len(REQUESTS)} запросов на {planned:.1f}s")
    check(f"«{kind}»: хотя бы один запрос сделан", len(REQUESTS) >= 1, "каскад промолчал")
    check(f"«{kind}»: ни один запрос не короче 7s",
          all(t >= min(7.0, budget) - 0.01 for t in timeouts_used()),
          f"таймауты {timeouts_used()}")
    check(f"«{kind}»: провал вернул None", res is None, f"got {res}")

print("\n[2] Резервный провайдер теперь достижим (за ним каскад и написан)")
# Быстрый отказ: 429/400 приходят мгновенно, а не по таймауту.
reset({m: Exception("400 invalid argument") for m in (
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite")}, spend_full_timeout=False)
_behaviour["qwen/qwen3.8-27b"] = "ответ резервного провайдера"
res = gc.generate_text("вопрос", {"kind": "assistant", "thinking_level": "HIGH"}, timeout=90)
check("ответ получен от groq после провала gemini",
      res is not None and res.text == "ответ резервного провайдера", f"got {res}")
check("в каскаде побывали оба провайдера", set(providers_used()) == {"gemini", "groq"},
      f"got {providers_used()}")
check("уложились в бюджет", CLOCK.t <= 90.0, f"got {CLOCK.t:.1f}s")

# То же при полном таймауте каждого запроса: до groq доходим или честно
# останавливаемся, но бюджет не превышаем ни в одном из случаев.
reset({m: ANY_FAIL for m in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                             "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.8-27b")}, spend_full_timeout=True)
_behaviour["openai/gpt-oss-120b"] = "ответ резерва"
gc.generate_text("сводка", {"kind": "daily", "thinking_level": "HIGH"}, timeout=2100)
check("на большом бюджете резерв опрошен", "openai/gpt-oss-120b" in models_used(),
      f"got {models_used()}")
check("ротация ключей на большом бюджете сохранилась",
      len(set(k for _, k, _ in REQUESTS)) > 1, f"got {[k[-8:] for _, k, _ in REQUESTS]}")

print("\n[3] Маршрутизация: виды работ взяты из фактических вызовов")
# Таблица kind -> каскад перечисляла имена, которых не передаёт никто
# (term_explainer, quiz, direct_ask, assistant_media_pm), а настоящие виды
# сваливались в else, то есть в тяжёлый «сводочный» каскад. Список видов
# собираем из самих вызывающих: если в assistant.py появится новый вид, а
# таблицу не поправят, эта проверка упадёт.
KIND_IN_DICT = re.compile(r"[\"']kind[\"']\s*:\s*[\"'](\w+)[\"']")
KIND_AS_ARG = re.compile(r"\bkind\s*=\s*[\"'](\w+)[\"']")
real_kinds = set()
for fname in ("assistant.py", "summarizer.py", "blocking_tools.py"):
    src = open(os.path.join(REPO_DIR, fname), encoding="utf-8").read()
    real_kinds |= set(KIND_IN_DICT.findall(src)) | set(KIND_AS_ARG.findall(src))
check("виды работ найдены в вызывающих файлах", len(real_kinds) >= 17, f"got {sorted(real_kinds)}")

# Сводки — единственные, кому тяжёлый каскад положен: их ждут не в диалоге.
HEAVY_ALLOWED = {"daily", "weekly", "group_summary"}
TRIAGE_FIRST = "gemini-3.5-flash-lite"
CHAT_FIRST = "gemini-3.8-flash"


def first_model_for(kind):
    reset()
    gc.generate_text("вопрос", {"kind": kind, "thinking_level": "HIGH"})
    return models_used()[0] if REQUESTS else None


misrouted = []
for kind in sorted(real_kinds):
    first = first_model_for(kind)
    if kind in HEAVY_ALLOWED:
        expected = config.GEMINI_MODEL
    elif kind in gc.TRIAGE_KINDS:
        expected = TRIAGE_FIRST
    else:
        expected = CHAT_FIRST
    check(f"«{kind}» начинает с {expected}", first == expected, f"got {first}")
    if kind not in HEAVY_ALLOWED and first == config.GEMINI_MODEL:
        misrouted.append(kind)

check("ни один диалоговый вид не попадает в сводочный каскад", not misrouted,
      f"в тяжёлый каскад ушли: {misrouted}")
check("выдуманных имён в таблице не осталось",
      not ({"term_explainer", "quiz", "direct_ask", "assistant_media_pm"}
           & (gc.CHAT_KINDS | gc.TRIAGE_KINDS)),
      f"chat={sorted(gc.CHAT_KINDS)} triage={sorted(gc.TRIAGE_KINDS)}")
check("все классификаторы и диалоги перечислены",
      (gc.CHAT_KINDS | gc.TRIAGE_KINDS | HEAVY_ALLOWED) >= real_kinds,
      f"не перечислены: {sorted(real_kinds - (gc.CHAT_KINDS | gc.TRIAGE_KINDS | HEAVY_ALLOWED))}")
check("неизвестный вид по-прежнему идёт в тяжёлый каскад",
      first_model_for("совершенно_новый_вид") == config.GEMINI_MODEL)

print("\n[4] 429 с числом 500-класса в теле НЕ банит модель")
# Замер: и Gemini, и Groq пишут в тело 429 сам лимит квоты, а он часто
# 500-класса. _SERVER_ERROR_RE находил там «500» отдельным словом, и рабочая
# модель улетала в бан на 20 минут для ВСЕХ ключей и всех подпроцессов (файл
# банов общий), а исчерпанный ключ кулдауна не получал: break уходил из цикла
# раньше строки set_key_cooldown.
for label, text in (
    ("quota_limit_value: 500", "429 RESOURCE_EXHAUSTED: quota exceeded, quota_limit_value: 500 per day"),
    ("limit 500 requests", "429 Too Many Requests: rate limit reached, limit 500 requests per day"),
    ("503 в тексте 429", "429 rate limit; retry later (503 upstream note)"),
):
    reset({"gemini-3.5-flash-lite": Exception(text)}, spend_full_timeout=False)
    gc.generate_text("вопрос", {"kind": "pm_chat"}, timeout=90)
    banned = gc.get_banned_models()
    cooled = gc.get_key_cooldowns()
    check(f"«{label}»: модель не забанена", "gemini-3.5-flash-lite" not in banned, f"got {banned}")
    check(f"«{label}»: ключ отправлен на кулдаун",
          any(gc._key_fingerprint("gemini", k) in cooled for k in GOOGLE_KEYS), f"got {cooled}")
    check(f"«{label}»: причина названа исчерпанием ключа",
          (gc.get_last_failure() or {}).get("reason") != "model_overloaded",
          f"got {gc.get_last_failure()}")

reset({"gemini-3.5-flash-lite": Exception("429 quota exceeded, limit 500 per day")},
      spend_full_timeout=False)
gc.generate_text("вопрос", {"kind": "pm_chat"}, timeout=90)
cooled_keys = [k for k in GOOGLE_KEYS if gc._key_fingerprint("gemini", k) in gc.get_key_cooldowns()]
check("на 429 испробован не один ключ, а несколько", len(cooled_keys) >= 1
      and len(set(k for _, k, _ in REQUESTS)) == len(REQUESTS), f"got {len(REQUESTS)} запросов")
check("следующая модель каскада всё равно опрошена", len(set(models_used())) > 1,
      f"got {models_used()}")

print("\n[5] Настоящая перегрузка модель по-прежнему банит")
reset({config.GEMINI_MODEL: Exception("503 Service Unavailable: model is overloaded")},
      spend_full_timeout=False)
gc.generate_text("сводка", {"kind": "daily"}, timeout=2100)
check("503 банит модель", config.GEMINI_MODEL in gc.get_banned_models(),
      f"got {gc.get_banned_models()}")
check("на 503 не долбили модель остальными ключами",
      models_used().count(config.GEMINI_MODEL) == 1, f"got {models_used()}")
check("503 не ставит ключ на кулдаун — виновата модель, не ключ",
      not any(gc._key_fingerprint("gemini", k) in gc.get_key_cooldowns() for k in GOOGLE_KEYS),
      f"got {gc.get_key_cooldowns()}")
reset({config.GEMINI_MODEL: Exception(
    "Invalid request: max_tokens 1500 exceeds context length of 500000 tokens")},
    spend_full_timeout=False)
gc.generate_text("сводка", {"kind": "daily"}, timeout=2100)
check("посторонний отказ с числом 500 в тексте не банит",
      config.GEMINI_MODEL not in gc.get_banned_models(), f"got {gc.get_banned_models()}")

print("\n[6] После последней попытки последней модели каскад не спит")
# Сон 8-10 с стоял перед самым `return None`: повторять было уже нечем, а врач
# ждал эти секунды впустую.
reset({m: Exception("connection reset by peer") for m in (
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
    "openai/gpt-oss-120b", "llama-3.3-70b-versatile")}, spend_full_timeout=False)
res = gc.generate_text("вопрос", {"kind": "assistant", "thinking_level": "HIGH"}, timeout=90)
check("каскад провалился (условия проверки соблюдены)", res is None and len(REQUESTS) >= 2,
      f"got {res}, запросов {len(REQUESTS)}")
check("последним событием был запрос, а не сон", EVENTS and EVENTS[-1][0] == "req",
      f"хвост событий: {EVENTS[-3:]}")
check("снов ровно на один меньше числа запросов", len(SLEEPS) == len(REQUESTS) - 1,
      f"запросов {len(REQUESTS)}, снов {len(SLEEPS)}: {SLEEPS}")

# Тот же случай без бюджета: сон перед возвратом None не нужен и здесь.
reset({m: Exception("connection reset by peer") for m in (
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
    "openai/gpt-oss-120b", "llama-3.3-70b-versatile")}, spend_full_timeout=False)
gc.generate_text("вопрос", {"kind": "assistant", "thinking_level": "HIGH"})
check("без бюджета тоже не спит перед сдачей", EVENTS[-1][0] == "req",
      f"хвост событий: {EVENTS[-3:]}")
check("а между попытками сон остался (backoff не выключен целиком)", len(SLEEPS) > 0,
      "снов не было вообще — выключен весь backoff")

print("\n[7] Причина провала не теряется")
# Возвращался просто None: «все ключи остывают» (пройдёт через 5 минут) и
# «промпт длиннее контекста» (не пройдёт никогда) выглядели одинаково и в
# журнале, и для вызывающего.
reset()
for key in GOOGLE_KEYS:
    gc.set_key_cooldown("gemini", key, seconds=300)
for key in GROQ_KEYS:
    gc.set_key_cooldown("groq", key, seconds=300)
res = gc.generate_text("вопрос", {"kind": "pm_chat"}, timeout=90)
cooldown_failure = gc.get_last_failure()
check("все ключи остывают: ответа нет", res is None)
check("запросов не было вовсе", not REQUESTS, f"got {REQUESTS}")
check("причина — остывающие ключи",
      (cooldown_failure or {}).get("reason") == "all_keys_on_cooldown", f"got {cooldown_failure}")
check("причина попала в файл статуса",
      status().get("failure_reason") == "all_keys_on_cooldown", f"got {status()}")
check("стадия помечена исчерпанием каскада", status().get("stage") == "all_exhausted",
      f"got {status().get('stage')}")

reset({m: Exception("400 The input token count exceeds the maximum number of tokens allowed")
       for m in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
                 "openai/gpt-oss-120b")}, spend_full_timeout=False)
res = gc.generate_text("очень длинный промпт", {"kind": "pm_chat", "thinking_level": "HIGH"},
                       timeout=90)
long_failure = gc.get_last_failure()
check("слишком длинный промпт: ответа нет", res is None)
check("причина другая, чем у остывающих ключей",
      long_failure and long_failure["reason"] != "all_keys_on_cooldown", f"got {long_failure}")
check("в причине сохранён текст ошибки провайдера",
      "token count" in (long_failure or {}).get("detail", ""), f"got {long_failure}")
check("текст ошибки виден и в файле статуса",
      "token count" in str(status().get("error", "")), f"got {status()}")

reset()
res = gc.generate_text("вопрос", {"kind": "pm_chat"}, timeout=90)
check("после успеха причина провала сброшена",
      res is not None and gc.get_last_failure() is None, f"got {gc.get_last_failure()}")

# Ключ — секрет: он не должен просочиться в файл статуса вместе с причиной.
leaky_key = GOOGLE_KEYS[0]
reset({m: Exception(f"401 invalid api key {leaky_key} rejected")
       for m in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
                 "openai/gpt-oss-120b")}, spend_full_timeout=False)
gc.generate_text("вопрос", {"kind": "pm_chat", "thinking_level": "HIGH"}, timeout=90)
leaked = (gc.get_last_failure() or {}).get("detail", "")
check("ключ из текста ошибки вырезан", leaky_key not in leaked, f"got {leaked}")

print("\n[8] Бюджет не тратится на попытки, которые не успеют завершиться")
reset({m: Exception("504 gateway timeout") for m in ()}, spend_full_timeout=True)
_behaviour.update({m: Exception("connection reset") for m in (
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.8-27b", "qwen/qwen3.6-27b",
    "openai/gpt-oss-120b")})
gc.generate_text("вопрос", {"kind": "assistant", "thinking_level": "HIGH"}, timeout=90)
check("каскад остановился внутри бюджета", CLOCK.t <= 90.0, f"got {CLOCK.t:.1f}s")
check("сон никогда не выходит за бюджет", all(s >= 0 for s in SLEEPS) and CLOCK.t <= 90.0,
      f"сны {SLEEPS}, итог {CLOCK.t:.1f}s")
starts = [t for _, _, t in REQUESTS]
check("каждому запросу отдан осмысленный таймаут", all(t >= 7.0 for t in starts),
      f"got {starts}")

check("боевые файлы не тронуты",
      "stomchat_cascade_" in gc.BANNED_MODELS_FILE
      and "stomchat_cascade_" in gc.KEY_COOLDOWN_FILE
      and "stomchat_cascade_" in runtime_guard.SUMMARY_STATUS_PATH
      and not os.path.exists(os.path.join(REPO_DIR, "banned_models.json.tmp")),
      f"{gc.BANNED_MODELS_FILE} / {runtime_guard.SUMMARY_STATUS_PATH}")

shutil.rmtree(_TMPDIR, ignore_errors=True)
print(f"\n{'=' * 62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
