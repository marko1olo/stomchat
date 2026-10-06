"""
Проверки перевода блокирующих вызовов в подпроцесс: журнал ребёнка, дедлайн
родителя, убийство внуков, различимость отказов, сохранность флага генерации.

Сеть не трогается ни разу: вместо настоящего blocking_tools.py дочерним
процессом запускается безобидный помощник (B.__file__ подменён), а разбор
ответов проверяется на подставных функциях.

Запуск: python test_fix_subprocess.py
"""
import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_subproc_")

# STOMCHAT_LOG_PATH ставится ДО импорта runtime_guard: LOG_PATH там вычисляется
# на импорте, и без подмены прогон целился бы в боевой bot.log. Переменная уходит
# и дочернему процессу — он наследует окружение.
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "bot_test_subprocess.log")

import runtime_guard  # noqa: E402

# generate_gemini_text_async в finally пишет в файл статуса, а восстановление
# затёртой отметки пишет туда второй раз. Боевые bot_summary_status.json,
# bot_heartbeat.json и дамп сторожа прогон трогать не имеет права.
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")
runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "bot_heartbeat.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "bot_watchdog_dump.txt")

import blocking_tools as B  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# Гейт частоты запросов здесь только мешает: его логика проверяется отдельно
# в test_gemini_pacing. Ускоряем, поведение остальных частей не меняется.
B._GEMINI_MIN_INTERVAL_SECONDS = 0.05


def run(coro):
    """
    Каждый сценарий — своя петля событий.

    _GEMINI_PACE_LOCK создаётся на импорте модуля; asyncio.Lock привязывается к
    петле при первом ожидании и в чужой петле падает. Пересоздаём перед каждым
    прогоном, чтобы порядок сценариев ни на что не влиял.
    """
    B._GEMINI_PACE_LOCK = asyncio.Lock()
    B._LAST_GEMINI_CALL_START = 0.0
    return asyncio.run(coro)


# --- журнал ребёнка ловим в память, а не в файл -----------------------------
class Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


COLLECTOR = Collector()
_child_log = logging.getLogger("blocking_tools")
_child_log.setLevel(logging.DEBUG)
_child_log.addHandler(COLLECTOR)
# Без этого строки ушли бы ещё и в корневой logger (его настраивает _main при
# проверках разбора ответа) и залили бы вывод прогона.
_child_log.propagate = False


def relayed():
    return [rec.getMessage() for rec in COLLECTOR.records]


# --- безобидный дочерний процесс -------------------------------------------
HELPER_SOURCE = '''
import json, os, subprocess, sys, time


def log(line):
    sys.stderr.write(line + "\\n")
    sys.stderr.flush()


action = sys.argv[1]
raw = sys.stdin.buffer.read()
payload = json.loads(raw.decode("utf-8")) if raw else {}

log("INFO gemini_client Gemini request attempt=1/3 key=gemini...ab123 model=gemini-3.6-flash")
log("WARNING gemini_client Gemini failed attempt=1/3 key=gemini...ab123: 429 quota exceeded")
log("ERROR gemini_client All AI attempts exhausted. Summary was not generated.")
log("строка без уровня")

if action == "echo-log":
    # config.py в боевом ребёнке печатает в stdout "Конфигурация загружена" —
    # протокол обязан это переживать.
    sys.stdout.write("посторонняя строка в stdout\\n")
    sys.stdout.write(json.dumps({"ok": True, "text": payload.get("want") or ""}) + "\\n")
    sys.stdout.flush()
    log(json.dumps({"ok": True, "text": "ОТРАВА-ИЗ-STDERR"}, ensure_ascii=False))
    raise SystemExit(0)

if action == "slow-answer":
    time.sleep(float(payload.get("sleep") or 0))
    sys.stdout.write(json.dumps({"ok": True, "text": "успел"}, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
    raise SystemExit(0)

if action == "grandchild":
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)"])
    with open(payload["pid_file"], "w", encoding="utf-8") as handle:
        handle.write(str(kid.pid))
    log("INFO helper внук запущен pid=%s" % kid.pid)
    time.sleep(180)
    raise SystemExit(0)

if action == "web-search" or action == "gemini-text":
    sys.stdout.write(os.environ.get("STOMCHAT_HELPER_REPLY", "{}") + "\\n")
    sys.stdout.flush()
    raise SystemExit(0)

sys.stdout.write(json.dumps({"ok": False, "error": "unknown helper action"}) + "\\n")
sys.stdout.flush()
'''

HELPER_PATH = os.path.join(_TMPDIR, "child_helper.py")
with io.open(HELPER_PATH, "w", encoding="utf-8") as handle:
    handle.write(HELPER_SOURCE)

# _run_json_tool запускает os.path.abspath(__file__) своего модуля — подменяем
# цель, иначе ребёнок пошёл бы в gemini_client и в сеть.
REAL_BLOCKING_TOOLS = os.path.abspath(B.__file__)
B.__file__ = HELPER_PATH


def pid_alive(pid):
    if os.name == "nt":
        # os.kill(pid, 0) на Windows не проверяет, а УБИВАЕТ (TerminateProcess),
        # поэтому только tasklist.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, errors="replace",
        ).stdout or ""
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def hard_kill(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


# --- разбор ответов дочернего процесса (in-process, без сети) ---------------
class _FakeIn:
    def __init__(self, data):
        self.buffer = io.BytesIO(data)


class _FakeOut:
    def __init__(self):
        self.buffer = io.BytesIO()


def run_main(action, payload=None):
    """Прогнать _main внутри процесса и вернуть разобранный JSON-ответ."""
    old = (sys.argv, sys.stdin, sys.stdout)
    sys.argv = ["blocking_tools.py", action]
    sys.stdin = _FakeIn(json.dumps(payload or {}).encode("utf-8"))
    out = _FakeOut()
    sys.stdout = out
    code = None
    try:
        B._main()
    except SystemExit as exc:
        code = exc.code
    finally:
        sys.argv, sys.stdin, sys.stdout = old
    raw = out.buffer.getvalue().decode("utf-8", errors="replace").strip().splitlines()
    return (json.loads(raw[-1]) if raw else {}), code


print("\n[1] ГЛАВНОЕ: журнал решений LLM-вызова снова доезжает до журнала бота")
print("      а) дочерний процесс вообще включает журнал, и на уровне INFO")


def probing_search(query, max_results):
    logging.getLogger("gemini_client").info("проба: ключ gemini...ab123 на кулдауне")
    return [], []


# _main зовётся здесь ПЕРВЫМ: logging.basicConfig действует только пока у
# корневого logger нет обработчиков, поэтому подменённый stderr надо подставить
# до всех остальных прогонов _main.
_saved_search = B._web_search_sync
B._web_search_sync = probing_search
_fake_err = io.StringIO()
_real_err = sys.stderr
sys.stderr = _fake_err
try:
    probe_answer, _ = run_main("web-search", {"query": "к", "max_results": 2})
finally:
    sys.stderr = _real_err
B._web_search_sync = _saved_search
_probe_text = _fake_err.getvalue()
# Без настройки журнала в ребёнке срабатывает logging.lastResort: он глотает всё
# ниже WARNING, то есть ровно те строки, в которых и написано, какой ключ взят и
# почему модель пропущена.
check("ребёнок пишет INFO-решения в stderr", "ключ gemini...ab123 на кулдауне" in _probe_text,
      f"stderr ребёнка: {_probe_text!r}")
check("в формате ребёнка есть уровень и имя — по ним родитель ставит свой уровень",
      "INFO gemini_client" in _probe_text, f"got {_probe_text!r}")
check("журнал не залез в stdout, где идёт JSON-протокол",
      probe_answer.get("ok") is True and "gemini" not in json.dumps(probe_answer), str(probe_answer))

print("      б) родитель перехватывает этот журнал и кладёт в свой")
COLLECTOR.records.clear()
payload, error = run(B._run_json_tool("echo-log", {"want": "настоящий-ответ"}, timeout=20))
lines = relayed()
check("вызов удался", error is None and isinstance(payload, dict), f"error={error!r}")
check("ответ взят из stdout, а не из stderr",
      (payload or {}).get("text") == "настоящий-ответ", f"got {payload!r}")
check("посторонняя строка в stdout (config печатает такую) протокол не ломает",
      (payload or {}).get("ok") is True, f"got {payload!r}")
check("отрава из stderr не подменила ответ протокола",
      "ОТРАВА" not in json.dumps(payload or {}, ensure_ascii=False), f"got {payload!r}")
check("решения LLM-вызова доехали в журнал родителя",
      any("Gemini request attempt=1/3" in line for line in lines), f"строк перелито: {len(lines)}")
check("причина отказа (429) доехала",
      any("429 quota exceeded" in line for line in lines), str(lines))
check("исчерпание каскада доехало",
      any("All AI attempts exhausted" in line for line in lines), str(lines))
check("stderr всё же перехвачен целиком (включая строку с фигурными скобками)",
      any("ОТРАВА-ИЗ-STDERR" in line for line in lines), str(lines))
check("в строках виден источник (имя действия)",
      all(rec.getMessage().startswith("[echo-log] ") for rec in COLLECTOR.records), str(lines))

levels = {}
for rec in COLLECTOR.records:
    if "Gemini request attempt" in rec.getMessage():
        levels["info"] = rec.levelno
    if "429 quota exceeded" in rec.getMessage():
        levels["warning"] = rec.levelno
    if "All AI attempts exhausted" in rec.getMessage():
        levels["error"] = rec.levelno
    if "строка без уровня" in rec.getMessage():
        levels["unknown"] = rec.levelno
# Уровень обязан сохраняться: боевой корневой logger стоит на INFO, поэтому
# перелив в DEBUG был бы не виден в bot.log вообще, а перелив отказов в INFO
# сломал бы grep ERROR — единственный способ найти их в 1.7 МБ журнала.
check("строка INFO ребёнка осталась INFO", levels.get("info") == logging.INFO, str(levels))
check("предупреждение ребёнка осталось WARNING", levels.get("warning") == logging.WARNING, str(levels))
check("ошибка ребёнка осталась ERROR", levels.get("error") == logging.ERROR, str(levels))
check("строка без уровня не потеряна (ушла в INFO)", levels.get("unknown") == logging.INFO, str(levels))

print("\n[2] Дедлайн родителя больше бюджета ребёнка")
# Замер на этой машине: 0.11-0.33 с интерпретатор с импортом blocking_tools плюс
# 0.73-2.48 с на config и gemini_client с клиентом openai. Запас должен покрывать
# этот запуск, иначе родитель убивает ребёнка на его собственном бюджете.
check("запас задан и покрывает измеренный запуск",
      B._SUBPROCESS_STARTUP_SLACK_SECONDS >= 3.0,
      f"got {B._SUBPROCESS_STARTUP_SLACK_SECONDS}")

_real_slack = B._SUBPROCESS_STARTUP_SLACK_SECONDS
B._SUBPROCESS_STARTUP_SLACK_SECONDS = 2.0
payload, error = run(B._run_json_tool("slow-answer", {"sleep": 1.2}, timeout=0.4))
check("ребёнок доработал бюджет: родитель не убил его на запуске",
      error is None and (payload or {}).get("text") == "успел", f"error={error!r} payload={payload!r}")

began = time.monotonic()
payload, error = run(B._run_json_tool("slow-answer", {"sleep": 60}, timeout=0.5))
waited = time.monotonic() - began
check("родитель ждал бюджет + запас, а не только бюджет",
      waited >= 0.5 + B._SUBPROCESS_STARTUP_SLACK_SECONDS - 0.25, f"ждал {waited:.2f}s")
check("зависший ребёнок всё равно убит по дедлайну",
      payload is None and error and "timeout" in error, f"got {error!r}")
check("в ошибке видно оба числа — дедлайн и бюджет ребёнка",
      error and "2.5" in error and "0.5" in error, f"got {error!r}")

print("\n[3] Внуки не переживают убийство по таймауту")
B._SUBPROCESS_STARTUP_SLACK_SECONDS = 0.5
pid_file = os.path.join(_TMPDIR, "grandchild.pid")
payload, error = run(B._run_json_tool("grandchild", {"pid_file": pid_file}, timeout=1.0))
check("родитель отчитался таймаутом", payload is None and error and "timeout" in error, f"got {error!r}")
kid_pid = None
try:
    kid_pid = int(io.open(pid_file, encoding="utf-8").read().strip())
except Exception as exc:
    check("внук успел запуститься и записать pid", False, str(exc))
if kid_pid:
    check("внук успел запуститься и записать pid", True)
    alive = True
    for _ in range(40):
        alive = pid_alive(kid_pid)
        if not alive:
            break
        time.sleep(0.25)
    check("внук убит вместе с ребёнком", not alive, f"pid {kid_pid} жив после убийства родителя")
    if alive:
        hard_kill(kid_pid)
    check("журнал убитого ребёнка тоже сохранён",
          any("внук запущен" in line for line in relayed()), "потерян stderr убитого процесса")
B._SUBPROCESS_STARTUP_SLACK_SECONDS = _real_slack

print("\n[4] Конвертат ffmpeg не остаётся после убийства")
voice_path = os.path.join(_TMPDIR, "voice_42.oga")
wav_path = os.path.join(_TMPDIR, "voice_42_converted.wav")
for path in (voice_path, wav_path):
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write("x")

_real_tool = B._run_json_tool


async def dead_tool(action, payload, timeout=None):
    return None, f"{action} timeout after 70.0s"


B._run_json_tool = dead_tool
text, error = run(B.transcribe_audio_async(voice_path, timeout=60))
check("расшифровка отчиталась ошибкой", text is None and error, f"got {error!r}")
check("временный wav убран после убийства", not os.path.exists(wav_path), wav_path)
check("исходник вызывающего не тронут", os.path.exists(voice_path), voice_path)

with io.open(wav_path, "w", encoding="utf-8") as handle:
    handle.write("x")


async def ok_tool(action, payload, timeout=None):
    return {"ok": True, "text": "расшифровка"}, None


B._run_json_tool = ok_tool
text, error = run(B.transcribe_audio_async(voice_path, timeout=60))
check("успешная расшифровка возвращается", text == "расшифровка" and error is None, f"got {text!r}")
check("на успехе чужой файл не удаляем (уборка ребёнка)", os.path.exists(wav_path), wav_path)
B._run_json_tool = _real_tool


print("\n[5] Отказ web-поиска отличим от «ничего не найдено»")
fake_config = types.ModuleType("config")
fake_config.SEARCH_PROVIDER = "tavily"
fake_config.TAVILY_API_KEY = "fake-key-not-used"
_saved_modules = {name: sys.modules.get(name) for name in ("config", "tavily", "ddgs")}
sys.modules["config"] = fake_config
# None в sys.modules — штатный способ сделать import невозможным.
sys.modules["tavily"] = None
sys.modules["ddgs"] = None
results, errors = B._web_search_sync("кариес", 2)
check("оба провайдера упали — отказы собраны", results == [] and len(errors) == 2, f"{results!r} {errors!r}")
check("в отказе видно, какой провайдер и почему",
      any(e.startswith("tavily:") for e in errors) and any(e.startswith("ddgs:") for e in errors), str(errors))

fake_ddgs = types.ModuleType("ddgs")


class _EmptyDDGS:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, *args, **kwargs):
        return []


fake_ddgs.DDGS = _EmptyDDGS
fake_config.SEARCH_PROVIDER = "duckduckgo"
sys.modules["ddgs"] = fake_ddgs
results, errors = B._web_search_sync("несуществующий термин", 2)
check("живой провайдер без находок — это НЕ отказ", results == [] and errors == [], f"{results!r} {errors!r}")
for name, module in _saved_modules.items():
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module

_real_search = B._web_search_sync
B._web_search_sync = lambda query, max_results: ([], ["ddgs: RuntimeError: сеть недоступна"])
answer, code = run_main("web-search", {"query": "к", "max_results": 2})
check("полный отказ поиска отдаётся как ok: False", answer.get("ok") is False, str(answer))
check("у отказа есть машиночитаемая причина", answer.get("reason") == "providers_failed", str(answer))
check("текст отказа сохранён", "сеть недоступна" in (answer.get("error") or ""), str(answer))

B._web_search_sync = lambda query, max_results: ([], [])
answer, code = run_main("web-search", {"query": "к", "max_results": 2})
check("пусто без отказов остаётся ok: True", answer.get("ok") is True and answer.get("results") == [], str(answer))

B._web_search_sync = lambda query, max_results: (["найдено"], ["tavily: TimeoutError: 5s"])
answer, code = run_main("web-search", {"query": "к", "max_results": 2})
check("находки резервного провайдера не превращаются в отказ",
      answer.get("ok") is True and answer.get("results") == ["найдено"], str(answer))
B._web_search_sync = _real_search

print("      сквозная проверка через настоящий подпроцесс:")
os.environ["STOMCHAT_HELPER_REPLY"] = json.dumps(
    {"ok": False, "reason": "providers_failed",
     "error": "web search failed: ddgs: RuntimeError: сеть недоступна"},
    ensure_ascii=False,
)
found, error = run(B.web_search_async("кариес", 2, timeout=20))
check("вызывающий видит отказ поиска словами",
      found == [] and error and "сеть недоступна" in error, f"{found!r} {error!r}")
os.environ["STOMCHAT_HELPER_REPLY"] = json.dumps({"ok": True, "results": []})
found, error = run(B.web_search_async("кариес", 2, timeout=20))
check("«ничего не найдено» приходит без ошибки", found == [] and error is None, f"{found!r} {error!r}")

print("\n[6] Пустой ответ модели отличим от развалившегося каскада")
fake_gemini = types.ModuleType("gemini_client")


class _Resp:
    def __init__(self, text):
        self.text = text


_saved_gemini = sys.modules.get("gemini_client")
sys.modules["gemini_client"] = fake_gemini

fake_gemini.generate_text = lambda prompt, context, timeout=None: None
check("каскад развалился -> None", B._generate_gemini_text_sync("p", {}) is None)
fake_gemini.generate_text = lambda prompt, context, timeout=None: _Resp("")
check("модель ответила пустотой -> пустая строка", B._generate_gemini_text_sync("p", {}) == "")
fake_gemini.generate_text = lambda prompt, context, timeout=None: _Resp("ответ")
check("обычный ответ проходит как есть", B._generate_gemini_text_sync("p", {}) == "ответ")
if _saved_gemini is None:
    sys.modules.pop("gemini_client", None)
else:
    sys.modules["gemini_client"] = _saved_gemini

_real_gen = B._generate_gemini_text_sync
B._generate_gemini_text_sync = lambda prompt, context, timeout=None: None
collapsed, _ = run_main("gemini-text", {"prompt": "p", "context": {}})
B._generate_gemini_text_sync = lambda prompt, context, timeout=None: ""
empty, _ = run_main("gemini-text", {"prompt": "p", "context": {}})
B._generate_gemini_text_sync = lambda prompt, context, timeout=None: "готовый текст"
good, _ = run_main("gemini-text", {"prompt": "p", "context": {}})
B._generate_gemini_text_sync = _real_gen
check("оба отказа — не успех", collapsed.get("ok") is False and empty.get("ok") is False,
      f"{collapsed} {empty}")
check("отказы РАЗЛИЧИМЫ по причине",
      collapsed.get("reason") != empty.get("reason")
      and collapsed.get("reason") == "cascade_exhausted"
      and empty.get("reason") == "empty_text",
      f"{collapsed} {empty}")
check("отказы различимы и словами", (collapsed.get("error") or "") != (empty.get("error") or ""),
      f"{collapsed} {empty}")
check("успех не задет", good.get("ok") is True and good.get("text") == "готовый текст", str(good))

print("\n[7] Отметка идущей сводки переживает короткий вызов")


def status():
    try:
        return json.load(io.open(runtime_guard.SUMMARY_STATUS_PATH, encoding="utf-8"))
    except Exception:
        return {}


def write_status_as(payload, pid):
    """Запись «как будто из другого процесса»: pid ставим сами."""
    body = dict(payload)
    body["pid"] = pid
    body.setdefault("utc", runtime_guard.utc_now_text())
    with io.open(runtime_guard.SUMMARY_STATUS_PATH, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False)


CHILD_PID = os.getpid() + 10000  # чужой pid: у подпроцесса он всегда не наш


def child_like_tool(clear_pid):
    """
    Подставной вызов, повторяющий поведение настоящего ребёнка: он пишет в общий
    одноместный файл свой kind, а в конце gemini_client уже внутри ребёнка
    снимает флаг — то есть гасит чужую отметку, не зная о ней.
    """
    async def tool(action, payload, timeout=None):
        context = (payload or {}).get("context") or {}
        kind = context.get("kind")
        write_status_as({"active": True, "kind": kind, "stage": "gemini_request"}, CHILD_PID)
        if kind not in runtime_guard.SUMMARY_KINDS:
            write_status_as({"active": False, "stage": f"{kind}_done"}, clear_pid)
        return {"ok": True, "text": "короткий ответ"}, None
    return tool


B._run_json_tool = child_like_tool(CHILD_PID)
write_status_as({"active": True, "kind": "daily", "stage": "gemini_generation_start"}, os.getpid())
started_utc = status().get("utc")
time.sleep(1.1)  # чтобы новый utc гарантированно отличался (точность — секунды)
run(B.generate_gemini_text_async("p", {"kind": "pm_chat"}, timeout=5))
after = status()
check("отметка дайджеста не потеряна коротким вызовом",
      after.get("active") is True and after.get("kind") == "daily", str(after))
check("в отметке сохранено исходное время — сторож увидит настоящий возраст",
      after.get("restored_from_utc") == started_utc and after.get("utc") != started_utc, str(after))

write_status_as({"active": True, "kind": "weekly", "stage": "telegraph"}, os.getpid())
run(B.generate_gemini_text_async("p", {"kind": "llama_triage"}, timeout=5))
check("недельная сводка охраняется так же", status().get("kind") == "weekly", str(status()))

# Сводка успела закончиться сама: гасит её ТОЛЬКО свой процесс (summarizer), и
# по pid это отличимо. Воскресить её отметку нельзя — снимать её было бы некому,
# и через полчаса сторож перезапустил бы процесс впустую.
write_status_as({"active": True, "kind": "daily", "stage": "gemini_generation_start"}, os.getpid())
B._run_json_tool = child_like_tool(os.getpid())
run(B.generate_gemini_text_async("p", {"kind": "pm_chat"}, timeout=5))
check("законченная сводка не воскресает", status().get("active") is False, str(status()))

B._run_json_tool = child_like_tool(CHILD_PID)
write_status_as({"active": True, "kind": "daily", "stage": "gemini_generation_start"}, os.getpid())
run(B.generate_gemini_text_async("p", {"kind": "daily"}, timeout=5))
check("сама сводка свой флаг не снимает — конвейер ещё идёт",
      status().get("active") is True and status().get("kind") == "daily", str(status()))

write_status_as({"active": True, "kind": "pm_chat", "stage": "gemini_request"}, os.getpid())
run(B.generate_gemini_text_async("p", {"kind": "pm_chat"}, timeout=5))
check("обычный вызов по-прежнему снимает свой флаг", status().get("active") is False, str(status()))
run(B.generate_gemini_text_async("p", None, timeout=5))
check("контекст None не роняет finally", status().get("active") is False, str(status()))

print("\n[8] Сквозной прогон НАСТОЯЩЕГО дочернего процесса")
# Всё выше гоняет подставного ребёнка. Здесь запускается сам blocking_tools.py —
# неизвестным действием, чтобы не дойти ни до сети, ни до gemini_client. Проверка
# на то, что переписанный обмен по пайпам работает и с настоящим ребёнком:
# stdin доставлен, stdout прочитан, код выхода 2 разобран, ответ извлечён.
B._run_json_tool = _real_tool  # в [7] он был подменён на подставного ребёнка
B.__file__ = REAL_BLOCKING_TOOLS
COLLECTOR.records.clear()
payload, error = run(B._run_json_tool("__проверка__", {"prompt": "x" * 50000}, timeout=25))
B.__file__ = HELPER_PATH
check("настоящий ребёнок отвечает по протоколу",
      payload is None and error and "unknown action" in error, f"got {payload!r} {error!r}")
check("крупный payload доставлен без затыка пайпа", error and "__проверка__" in error, f"got {error!r}")

print("\n[9] Изоляция прогона")
check("файл статуса уведён во временный каталог",
      "stomchat_subproc_" in runtime_guard.SUMMARY_STATUS_PATH, runtime_guard.SUMMARY_STATUS_PATH)
check("журнал уведён во временный каталог",
      "stomchat_subproc_" in os.environ.get("STOMCHAT_LOG_PATH", ""), os.environ.get("STOMCHAT_LOG_PATH"))
check("боевые файлы рядом не тронуты",
      not os.path.exists(os.path.join(os.getcwd(), "bot_summary_status.json.tmp")),
      "остался временный файл записи статуса")

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
