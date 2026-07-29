"""
Проверки диагностируемости: молчаливые отказы на пути врача обязаны оставлять
в журнале строку, по которой инцидент можно найти.

Проверяется ПОВЕДЕНИЕ, а не текст исходников: к корневому логгеру подвешен
обработчик-сборщик, боевой код запускается по-настоящему (включая настоящий
дочерний процесс и настоящий поток сторожа), и утверждения делаются о
перехваченных записях — их уровне, содержимом и количестве. Поиск по исходнику
на logger.warning ничего бы не доказал: строка может быть недостижима, стоять
ниже порога корневого логгера или не доезжать до файла.

Сеть не трогается ни разу: дочерним процессом запускается либо безобидный
помощник, либо сам blocking_tools.py — невалидным вводом и неизвестным
действием, до gemini_client управление не доходит. Провайдеры поиска подменены
заглушками в sys.modules.

Боевые файлы не трогаются: журнал, файл статуса, heartbeat и дамп сторожа
уведены во временный каталог до импорта runtime_guard.

Запуск: python test_observability.py
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import types

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_observe_")

# STOMCHAT_LOG_PATH ставится ДО импорта runtime_guard: LOG_PATH там вычисляется на
# импорте, и без подмены прогон целился бы в общий bot_test.log.
LOG_FILE = os.path.join(_TMPDIR, "bot_observe.log")
os.environ["STOMCHAT_LOG_PATH"] = LOG_FILE

import runtime_guard  # noqa: E402

runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")
runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "bot_heartbeat.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "bot_watchdog_dump.txt")

import blocking_tools as B  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# --- записи журнала ловим в память, а не в файл ------------------------------
#
# configure_logging() сносит все обработчики корня, поэтому сборщик вешается
# ПОСЛЕ него: проверяется настоящая боевая настройка уровня, а не удобная.
class Collector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records = []

    def emit(self, record):
        self.records.append(record)


runtime_guard.configure_logging()
COLLECTOR = Collector()
logging.getLogger().addHandler(COLLECTOR)


def fresh():
    """Чистый сборщик и сброшенные окна ограничителя перед каждым сценарием."""
    COLLECTOR.records.clear()
    runtime_guard.reset_throttle()


def texts():
    return [r.getMessage() for r in COLLECTOR.records]


def find(fragment, min_level=logging.WARNING):
    """Записи не ниже min_level, содержащие фрагмент."""
    return [r for r in COLLECTOR.records
            if fragment in r.getMessage() and r.levelno >= min_level]


def run(coro):
    B._GEMINI_PACE_LOCK = asyncio.Lock()
    B._LAST_GEMINI_CALL_START = 0.0
    return asyncio.run(coro)


def write_helper(name, body):
    path = os.path.join(_TMPDIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(textwrap.dedent(body))
    return path


def run_child(source, argv=(), stdin_bytes=b"", timeout=90):
    """Запустить настоящий отдельный процесс и вернуть (код, stdout, stderr)."""
    path = write_helper(f"child_{abs(hash(source)) % 10 ** 8}.py", source)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", path, *argv],
        input=stdin_bytes,
        capture_output=True,
        timeout=timeout,
    )
    return (proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


print("=" * 70)
print("ДИАГНОСТИРУЕМОСТЬ: молчаливые отказы на пути врача")
print("=" * 70)

# ============================================================================
# [1] Порог корневого логгера
#
# ЗАМЕР: во всех журналах на диске (bot.log, bot.log.1, bot_test.log,
# distiller.log, bot_supervisor.log — 131 219 строк) записей DEBUG ровно 0.
# По коду порог задан один раз: runtime_guard.py root.setLevel(logging.INFO),
# ни одного setLevel(DEBUG) в боевых модулях нет.
#
# СЦЕНАРИЙ ОПЕРАТОРА: врач говорит, что бот молчит. Разработчик уверен, что
# «там есть логирование» — и оно там есть, только на DEBUG, то есть в журнал не
# попадает никогда. Проверка закрепляет порог поведением: диагностика, посаженная
# на DEBUG, невидима, и это факт, а не предположение.
# ============================================================================
print("\n[1] Порог корневого логгера: DEBUG в журнал не попадает")
fresh()
check("корень настроен на INFO", logging.getLogger().level == logging.INFO,
      f"level={logging.getLogger().level}")

probe = logging.getLogger("проба_порога")
probe.debug("эта строка не должна дойти")
check("logger.debug не доезжает до обработчика", texts() == [], str(texts()))
probe.warning("эта строка должна дойти")
check("logger.warning доезжает", len(COLLECTOR.records) == 1, str(texts()))
check("isEnabledFor(DEBUG) отвечает False", not probe.isEnabledFor(logging.DEBUG))

# ============================================================================
# [2] Сторож убивает процесс — и объясняет это в журнале
#
# ЗАМЕР: в bot_watchdog_dump.txt 15 записей WATCHDOG EXIT
# (reason=event_loop_heartbeat_stale_300.0..330.0s, 2026-05-18 .. 06-21).
# В bot.log, bot.log.1, bot_test.log и bot_supervisor.log про них 0 строк.
# Кода выхода 78 нет даже в надзорном журнале start.bat: там -1 x7, 1 x5,
# 0 x4, -1073741819 x1 при 52 запусках и 17 записях о завершении.
#
# СЦЕНАРИЙ ОПЕРАТОРА: 2026-05-19 13:45:46 журнал обрывается на извлечении кадра
# из видео, следующая строка — 2026-05-20 19:03:30. Бот лежал 29 часов, врачи
# писали в пустоту. Отличить убийство сторожем от отключения света, ручной
# остановки и падения интерпретатора по журналу было НЕВОЗМОЖНО: все 15 случаев
# выглядели одинаково — обрыв, тишина, «Инициализация базы данных».
# ============================================================================
print("\n[2] Сторож: строка в журнал перед os._exit(78)")
fresh()
runtime_guard._log_watchdog_exit(317.5)
found = find("watchdog exit")
check("строка о выходе сторожа появилась", len(found) == 1, str(texts()))
if found:
    rec, msg = found[0], found[0].getMessage()
    check("уровень CRITICAL", rec.levelno == logging.CRITICAL, logging.getLevelName(rec.levelno))
    check("возраст heartbeat в строке", "317.5" in msg, msg)
    check("порог в строке", str(runtime_guard.WATCHDOG_STALE_SECONDS) in msg, msg)
    check("pid в строке", f"pid={os.getpid()}" in msg, msg)
    check("код выхода в строке", "78" in msg, msg)
    check("путь дампа в строке", "bot_watchdog_dump" in msg, msg)

fresh()
runtime_guard._log_watchdog_exit(301.0, "PermissionError: файл занят")
found = find("watchdog exit")
check("отказ записи дампа попадает в ту же строку",
      bool(found) and "ДАМП НЕ ЗАПИСАН" in found[0].getMessage()
      and "PermissionError" in found[0].getMessage(),
      str(texts()))

# ============================================================================
# [3] Тот же путь целиком, в настоящем процессе
#
# Блок [2] звал функцию записи напрямую. Здесь поднимается настоящий поток
# сторожа в настоящем отдельном процессе, который действительно умирает через
# os._exit(78), и проверяется, что строка доехала до ФАЙЛА журнала. Это прямой
# ответ на замер: раньше файл оставался пуст, теперь в нём причина.
# ============================================================================
print("\n[3] Настоящий процесс: сторож убивает и оставляет запись в файле")
watchdog_log = os.path.join(_TMPDIR, "watchdog_real.log")
watchdog_dump = os.path.join(_TMPDIR, "watchdog_real_dump.txt")
code, out, err = run_child(f'''
    import os, sys, time
    os.environ["STOMCHAT_LOG_PATH"] = {watchdog_log!r}
    sys.path.insert(0, {os.getcwd()!r})
    import runtime_guard
    runtime_guard.WATCHDOG_DUMP_PATH = {watchdog_dump!r}
    runtime_guard.HEARTBEAT_PATH = os.path.join({_TMPDIR!r}, "hb_real.json")
    runtime_guard.configure_logging()
    runtime_guard.HEARTBEAT_INTERVAL_SECONDS = 0.2
    runtime_guard.WATCHDOG_STALE_SECONDS = 0.1
    runtime_guard.start_watchdog()
    time.sleep(30)
    print("НЕ УБИТ")
''')
check("процесс убит именно кодом 78", code == 78, f"код={code} out={out.strip()!r}")
written = open(watchdog_log, encoding="utf-8").read() if os.path.exists(watchdog_log) else ""
check("причина смерти лежит в файле журнала", "watchdog exit" in written, repr(written[-300:]))
check("уровень в файле — CRITICAL", "CRITICAL" in written, repr(written[-300:]))
check("возраст heartbeat в файле", "не отвечает" in written, repr(written[-300:]))
check("дамп сторожа тоже записан",
      os.path.exists(watchdog_dump)
      and "WATCHDOG EXIT" in open(watchdog_dump, encoding="utf-8").read())

# ============================================================================
# [4] Выход не зависит от журнала — наблюдаемость не меняет управление
#
# Сторож срабатывает тогда, когда главный поток застрял, — в том числе он мог
# застрять ВНУТРИ записи в журнал, держа замок обработчика. Тогда logger.critical
# из потока сторожа встал бы на этом замке навсегда, и зависание на 5 минут
# превратилось бы в вечное: процесс не убит, не перезапущен, врач ждёт всегда.
# Это было бы ХУЖЕ исходной ошибки — молчаливого, но быстрого выхода. Здесь
# замок обработчика захватывается и не отпускается никогда.
# ============================================================================
print("\n[4] Заклиненный журнал не мешает сторожу убить процесс")
deadlock_log = os.path.join(_TMPDIR, "deadlock.log")
started = time.monotonic()
code, out, err = run_child(f'''
    import os, sys, time, logging
    os.environ["STOMCHAT_LOG_PATH"] = {deadlock_log!r}
    sys.path.insert(0, {os.getcwd()!r})
    import runtime_guard
    runtime_guard.WATCHDOG_DUMP_PATH = os.path.join({_TMPDIR!r}, "deadlock_dump.txt")
    runtime_guard.HEARTBEAT_PATH = os.path.join({_TMPDIR!r}, "hb_deadlock.json")
    runtime_guard.configure_logging()
    # Замок обработчика захвачен и не будет отпущен: имитация главного потока,
    # застрявшего посреди записи в журнал.
    logging.getLogger().handlers[0].acquire()
    runtime_guard.HEARTBEAT_INTERVAL_SECONDS = 0.2
    runtime_guard.WATCHDOG_STALE_SECONDS = 0.1
    runtime_guard.WATCHDOG_LOG_GRACE_SECONDS = 1.0
    runtime_guard.start_watchdog()
    time.sleep(30)
    print("НЕ УБИТ")
''')
elapsed = time.monotonic() - started
check("процесс умер кодом 78 несмотря на заклиненный журнал", code == 78,
      f"код={code} out={out.strip()!r}")
check("умер быстро, а не по таймауту прогона", elapsed < 25, f"{elapsed:.1f} c")

# ============================================================================
# [5] Писатель дампа больше не гасит свой отказ
#
# ЗАМЕР: занятость файла — ровно та причина, по которой на этой машине ломается
# запись heartbeat (main.py прямо называет антивирус и индексатор; в bot.log
# 648 записей «Heartbeat write failed»). Совпадение занятого файла дампа с
# падением фоновой задачи оставляло разбор НИГДЕ: ни в дампе, ни в журнале.
#
# СЦЕНАРИЙ ОПЕРАТОРА: врач прислал снимок, ответа нет. Оператор идёт в дамп —
# дампа нет; идёт в журнал — про дамп ничего. Дальше идти некуда.
# ============================================================================
print("\n[5] Отказ записи дампа состояния виден в журнале")
fresh()
busy_dir = os.path.join(_TMPDIR, "занято_каталогом")
os.makedirs(busy_dir, exist_ok=True)
saved_dump = runtime_guard.WATCHDOG_DUMP_PATH
runtime_guard.WATCHDOG_DUMP_PATH = busy_dir  # open(..., "a") на каталоге падает
returned = runtime_guard.dump_runtime_state("проверка_отказа_записи")
runtime_guard.WATCHDOG_DUMP_PATH = saved_dump
found = find("дамп состояния НЕ записан")
check("отказ записи дампа записан в журнал", len(found) == 1, str(texts()))
if found:
    msg = found[0].getMessage()
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке причина отказа (тип исключения)",
          any(t in msg for t in ("PermissionError", "IsADirectoryError", "OSError")), msg)
    check("в строке повод дампа", "проверка_отказа_записи" in msg, msg)
check("управление не изменилось: исключение не поднято, возврат None", returned is None)

# ============================================================================
# [6] Нечитаемый файл статуса сводки
#
# ЗАМЕР: read_summary_status стоит на пути каждого ответа врачу
# (release_generation_status) и на пути сторожа сводки (main.py:435). Возврат {}
# означал для обоих «никакой сводки не идёт»: флаг гасится, сторож разоружается.
#
# СЦЕНАРИЙ ОПЕРАТОРА: дневной дайджест не пришёл в чат врачей. Сторож сводки его
# зависание не поймал, потому что читал битый файл и считал, что сводки нет.
# В журнале — ни одной строки про файл статуса.
#
# Отдельно проверяется, что ОТСУТСТВИЕ файла (штатное состояние до первого
# clear_summary_status на старте, main.py:2142) записи НЕ порождает: иначе
# правка залила бы журнал шумом на каждом вызове и обесценила бы себя.
# ============================================================================
print("\n[6] Битый файл статуса виден, отсутствующий — молчит")
fresh()
with open(runtime_guard.SUMMARY_STATUS_PATH, "w", encoding="utf-8") as handle:
    handle.write("{это не JSON")
status = runtime_guard.read_summary_status()
found = find("статус сводки не прочитан")
check("битый JSON записан в журнал", len(found) == 1, str(texts()))
if found:
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке тип отказа", "JSONDecodeError" in found[0].getMessage(),
          found[0].getMessage())
    check("в строке путь файла", "bot_summary_status" in found[0].getMessage(),
          found[0].getMessage())
check("управление не изменилось: по-прежнему {}", status == {}, repr(status))

fresh()
os.remove(runtime_guard.SUMMARY_STATUS_PATH)
status = runtime_guard.read_summary_status()
check("отсутствие файла записи НЕ порождает", texts() == [], str(texts()))
check("и по-прежнему возвращает {}", status == {}, repr(status))

# ============================================================================
# [7] Ограничитель частоты: одна строка на инцидент, а не на сообщение
#
# ЗАМЕР: отказ провайдера наблюдался постоянным — 45 записей
# «400 FAILED_PRECONDITION User location is not supported» по всем четырём
# ключам, 44 записи «recent delivery scan failed», 16 записей Groq 413. Триаж
# вызывается на КАЖДОЕ сообщение группы. Без ограничителя новая честная строка
# на 749 врачах вытеснила бы из bot.log (ротация 5 МБ) всю историю работы —
# то есть диагностика уничтожила бы диагностику.
#
# СЦЕНАРИЙ ОПЕРАТОРА: открыл bot.log, чтобы разобрать вчерашний инцидент, а там
# 20 000 одинаковых строк за последний час и больше ничего.
# ============================================================================
print("\n[7] Ограничитель частоты: сжимает, но не скрывает")
fresh()
with open(runtime_guard.SUMMARY_STATUS_PATH, "w", encoding="utf-8") as handle:
    handle.write("{это не JSON")
for _ in range(50):
    runtime_guard.read_summary_status()
found = find("статус сводки не прочитан")
check("50 одинаковых отказов дали одну строку", len(found) == 1, f"строк={len(found)}")

saved_window = runtime_guard.THROTTLE_WINDOW_SECONDS
runtime_guard.THROTTLE_WINDOW_SECONDS = 0.0
runtime_guard.read_summary_status()
runtime_guard.THROTTLE_WINDOW_SECONDS = saved_window
found = find("статус сводки не прочитан")
check("подавленные не потеряны, а посчитаны",
      len(found) == 2 and "подавлено похожих: 49" in found[1].getMessage(),
      found[1].getMessage() if len(found) > 1 else str(texts()))
os.remove(runtime_guard.SUMMARY_STATUS_PATH)

fresh()
check("разные ключи друг друга не подавляют",
      runtime_guard.throttled_log(logging.WARNING, "ключ_A", "первый")
      and runtime_guard.throttled_log(logging.WARNING, "ключ_Б", "второй")
      and not runtime_guard.throttled_log(logging.WARNING, "ключ_A", "снова первый"),
      str(texts()))

# ============================================================================
# [8] Падение дочернего процесса: тип и трейсбек
#
# ЗАМЕР: str() у ConnectionResetError, TimeoutError, IndexError и
# asyncio.CancelledError равен пустой строке. Верхний обработчик ребёнка отдавал
# наружу ровно str(exc), родитель на пустом поле подставлял «<action> failed», и
# в bot.log попадала запись без причины — таких там 12 из 1393 ERROR/WARNING.
# Трейсбек не попадал никуда: stdout занят JSON-протоколом, в stderr его не писали.
#
# СЦЕНАРИЙ ОПЕРАТОРА: врач спросил про коффердам, бот молчит. В журнале
# «gemini-text failed» — ни типа исключения, ни файла, ни строки, ни модели,
# ни ключа. Разбирать нечем.
# ============================================================================
print("\n[8] Падение ребёнка: тип и трейсбек доезжают")
check("пустой str() больше не даёт пустую причину",
      B._describe_exception(ConnectionResetError()) == "ConnectionResetError",
      B._describe_exception(ConnectionResetError()))
check("непустой str() сохраняет и тип, и текст",
      B._describe_exception(ValueError("плохой кадр")) == "ValueError: плохой кадр",
      B._describe_exception(ValueError("плохой кадр")))
for exc in (TimeoutError(), IndexError(), asyncio.CancelledError()):
    check(f"{type(exc).__name__} опознан по типу",
          B._describe_exception(exc) == type(exc).__name__, B._describe_exception(exc))

# настоящий дочерний процесс: невалидный JSON на stdin ломает _read_stdin_json
# ДО разбора действия, то есть до gemini_client и до сети управление не доходит
code, out, err = run_child(f'''
    import sys
    sys.path.insert(0, {os.getcwd()!r})
    import blocking_tools
    blocking_tools._main()
''', argv=["gemini-text"], stdin_bytes="{это не json".encode("utf-8"))
check("ребёнок вышел кодом 1", code == 1, f"код={code}")
payload = json.loads([l for l in out.splitlines() if l.strip().startswith("{")][-1])
check("в поле error есть ТИП исключения",
      payload.get("error", "").startswith("JSONDecodeError"), repr(payload))
check("трейсбек ушёл в stderr, откуда его забирает родитель",
      "Traceback" in err and "JSONDecodeError" in err, repr(err[-300:]))
check("в stderr названо упавшее действие",
      "дочерний процесс упал action=gemini-text" in err, repr(err[-300:]))
check("уровень строки ребёнка — ERROR (родитель разберёт его по префиксу)",
      "ERROR" in err, repr(err[:200]))

# ============================================================================
# [9] Успешный выход ребёнка не считается падением
#
# _json_exit бросает SystemExit из каждой ветки, в том числе успешной. Если
# верхний обработчик посчитает его отказом, журнал будет писать «ребёнок упал»
# на КАЖДЫЙ нормальный ответ врачу — новая диагностика станет шумом.
# ============================================================================
print("\n[9] Штатный выход ребёнка не пишется как падение")
code, out, err = run_child(f'''
    import sys
    sys.path.insert(0, {os.getcwd()!r})
    import blocking_tools
    blocking_tools._main()
''', argv=["неизвестное-действие"], stdin_bytes=b"{}")
check("неизвестное действие — код 2", code == 2, f"код={code}")
check("про падение не написано ничего", "дочерний процесс упал" not in err, repr(err))
check("трейсбека нет", "Traceback" not in err, repr(err))

# ============================================================================
# [10] Каждый неудавшийся вызов подпроцесса виден, даже если вызывающий
#      выбросил строку ошибки
#
# ЗАМЕР: вызывающие регулярно выбрасывают error. assistant.py:104-106 отдаёт
# врачу готовую фразу «Недостаточно сообщений…» и не пишет ничего;
# assistant.py:135-137 возвращает False молча; assistant.py:1213-1214
# сворачивает причину в _unavailable. Отказ доезжает до врача, но не до журнала.
#
# СЦЕНАРИЙ ОПЕРАТОРА: врач говорит, что бот молчит — по журналу невозможно
# понять, почему: за эту минуту в bot.log нет ни одной строки об отказе.
# Ниже идёт НАСТОЯЩИЙ обмен с дочерним процессом, вызывающий ведёт себя как
# assistant.py (строку ошибки выбрасывает), и запись всё равно обязана быть.
# ============================================================================
print("\n[10] Отказ подпроцесса виден даже при выброшенной строке ошибки")

HELPER = write_helper("helper_child.py", '''
    import json, os, sys, time
    action = sys.argv[1]
    mode = os.environ.get("HELPER_MODE", "not_ok")
    if mode == "instant_exit":
        # Умираем, НЕ читая stdin: запись задания в пайп обязана провалиться.
        os._exit(0)
    sys.stdin.buffer.read()
    if mode == "not_ok":
        sys.stderr.write("ERROR gemini_client каскад развалился\\n")
        sys.stderr.flush()
        print(json.dumps({"ok": False, "reason": "cascade_exhausted",
                          "error": "ни одна модель не ответила"}))
    elif mode == "secret":
        # Длинный хвост нужен, чтобы проверка обрезки была не декоративной:
        # без _ERROR_TEXT_LIMIT такая ошибка уехала бы в журнал целиком.
        print(json.dumps({"ok": False, "error":
              "401 Unauthorized https://x/v1?key=AIzaSyC0ffeeDamBOPT1234567890abcdefQ "
              + "подробности провайдера " * 300}))
    elif mode == "no_json":
        sys.stderr.write("ERROR ребёнок умер, не отдав JSON\\n")
        sys.stderr.flush()
        sys.exit(3)
    elif mode == "overflow":
        # Одна строка длиннее лимита потока asyncio (64 КБ): _read_lines обязан
        # её потерять, посчитать и доложить одной сводной записью.
        sys.stderr.write("X" * 200000 + "\\n")
        sys.stderr.write("ERROR последняя внятная строка\\n")
        sys.stderr.flush()
        print(json.dumps({"ok": True, "text": "ответ"}))
    elif mode == "hang":
        time.sleep(120)
    elif mode == "ok":
        print(json.dumps({"ok": True, "text": "коффердам обязателен"}))
    sys.stdout.flush()
''')
REAL_BT = os.path.abspath(B.__file__)
B.__file__ = HELPER

fresh()
os.environ["HELPER_MODE"] = "not_ok"
payload, error = run(B._run_json_tool("gemini-text", {"prompt": "коффердам?"}, timeout=20))
if error:
    # ровно то, что делает assistant.py:105-106 — строка ошибки выброшена
    _ = "Недостаточно сообщений в общей группе для анализа клинического профиля."
found = find("подпроцесс не дал ответа")
check("отказ подпроцесса записан", len(found) == 1, str(texts()))
if found:
    msg = found[0].getMessage()
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке действие", "action=gemini-text" in msg, msg)
    check("в строке pid подпроцесса", "pid=" in msg, msg)
    check("в строке вид отказа", "cascade_exhausted" in msg, msg)
    check("в строке причина от ребёнка", "ни одна модель не ответила" in msg, msg)
check("журнал ребёнка перелит отдельными строками",
      any("[gemini-text]" in t and "каскад развалился" in t for t in texts()), str(texts()))
check("управление не изменилось: та же пара (None, причина)",
      payload is None and error == "ни одна модель не ответила", f"{payload!r} {error!r}")

fresh()
os.environ["HELPER_MODE"] = "no_json"
payload, error = run(B._run_json_tool("whisper-transcribe", {"file_path": "x.ogg"}, timeout=20))
found = find("подпроцесс не дал ответа")
check("ребёнок без JSON тоже записан", len(found) == 1, str(texts()))
if found:
    check("вид отказа — no_json", "вид=no_json" in found[0].getMessage(), found[0].getMessage())
check("управление не изменилось", payload is None and error, f"{payload!r} {error!r}")

fresh()
os.environ["HELPER_MODE"] = "hang"
started = time.monotonic()
payload, error = run(B._run_json_tool("gemini-text", {"prompt": "x"}, timeout=1))
hang_elapsed = time.monotonic() - started
found = find("подпроцесс не дал ответа")
check("убийство по таймауту записано", len(found) == 1, str(texts()))
if found:
    check("вид отказа — timeout", "вид=timeout" in found[0].getMessage(), found[0].getMessage())
check("управление не изменилось: вернулся таймаут, а не исключение",
      payload is None and error and "timeout" in error, f"{payload!r} {error!r}")
check("ребёнок действительно убит, а не дожидался 120 c", hang_elapsed < 40, f"{hang_elapsed:.1f} c")

fresh()
os.environ["HELPER_MODE"] = "ok"
payload, error = run(B._run_json_tool("gemini-text", {"prompt": "x"}, timeout=20))
check("успешный вызов не порождает WARNING",
      payload and error is None
      and not [r for r in COLLECTOR.records if r.levelno >= logging.WARNING],
      f"{payload!r} {error!r} {texts()}")

# ============================================================================
# [10a] Задание не доставлено подпроцессу
#
# Ребёнок умер, не дочитав промпт. Дальше родитель не находит JSON и возвращает
# «... failed with code N» — а это ДРУГОЙ отказ: не дошло задание против «модель
# не ответила». Различить их по журналу было нельзя.
#
# СЦЕНАРИЙ ОПЕРАТОРА: врач задал длинный вопрос со снимком, бот молчит. По
# журналу видно только «ребёнок умер», и непонятно, успел ли он вообще получить
# вопрос — то есть неясно, виноват провайдер или запуск подпроцесса.
#
# Отказ вносится точечно: настоящий дочерний процесс, настоящие _feed_stdin,
# _read_lines и релей, сломан ровно drain() на пайпе stdin. Приводить это
# «естественно» (ребёнок умирает, не читая) нельзя — замер показал гонку:
# транспорт Proactor на Windows успевает принять и 4 МБ, и запись не падает,
# то есть проверка выходила плавающей. Точечная поломка детерминирована.
# ============================================================================
print("\n[10a] Недоставленное задание записано отдельно от «нет ответа»")
fresh()
B.__file__ = HELPER
os.environ["HELPER_MODE"] = "ok"
saved_exec = B.asyncio.create_subprocess_exec


class BrokenStdin:
    """stdin, который принимает запись, но не может её доставить."""

    def __init__(self, real):
        self._real = real

    def write(self, data):
        return None

    async def drain(self):
        raise ConnectionResetError("ребёнок закрыл пайп")

    def close(self):
        # Настоящее закрытие обязательно: иначе ребёнок навсегда встанет на read().
        self._real.close()


async def _exec_broken_stdin(*args, **kwargs):
    proc = await saved_exec(*args, **kwargs)
    proc.stdin = BrokenStdin(proc.stdin)
    return proc


B.asyncio.create_subprocess_exec = _exec_broken_stdin
try:
    payload, error = run(B._run_json_tool("gemini-text",
                                         {"prompt": "СЕКРЕТНЫЙ_ВОПРОС_ВРАЧА"}, timeout=20))
finally:
    B.asyncio.create_subprocess_exec = saved_exec
    os.environ.pop("HELPER_MODE", None)
found = find("задание не доставлено подпроцессу")
check("недоставленное задание записано", len(found) == 1, str(texts())[:300])
if found:
    msg = found[0].getMessage()
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке действие и pid", "action=gemini-text" in msg and "pid=" in msg, msg)
    check("в строке размер задания, а не сам вопрос врача",
          "байт=" in msg and "СЕКРЕТНЫЙ_ВОПРОС_ВРАЧА" not in msg, msg)
    check("в строке тип отказа",
          any(t in msg for t in ("ConnectionResetError", "BrokenPipeError", "OSError")), msg)
check("управление не изменилось: отказ записи задания не подменил результат вызова",
      payload is not None and error is None, f"{payload!r} {error!r}")
check("вопрос врача не попал в журнал ни одной строкой",
      not any("СЕКРЕТНЫЙ_ВОПРОС_ВРАЧА" in t for t in texts()), str(texts())[:300])

# ============================================================================
# [10b] Хвост журнала убитого ребёнка не дочитан
#
# Это последний дренаж убитого по таймауту процесса: его самые последние строки
# перед зависанием — ровно то, что и нужно разбирать. Молчание здесь означало,
# что хвост исчезал, а оператор не знал даже, что чего-то не хватает.
#
# СЦЕНАРИЙ ОПЕРАТОРА: разбирает зависание ответа, видит журнал ребёнка,
# обрывающийся на середине, и считает, что ребёнок просто замолчал.
# ============================================================================
print("\n[10b] Провал дренажа хвоста журнала записан")
fresh()
os.environ["HELPER_MODE"] = "hang"
saved_wait_for = B.asyncio.wait_for


async def _wait_for_boom(awaitable, timeout=None):
    """Ломаем ровно proc.communicate(), не трогая остальные ожидания."""
    if getattr(getattr(awaitable, "cr_code", None), "co_name", "") == "communicate":
        awaitable.close()
        raise OSError("пайп уже закрыт")
    return await saved_wait_for(awaitable, timeout)


B.asyncio.wait_for = _wait_for_boom
try:
    payload, error = run(B._run_json_tool("whisper-transcribe", {"file_path": "x.ogg"}, timeout=1))
finally:
    B.asyncio.wait_for = saved_wait_for
    os.environ.pop("HELPER_MODE", None)
found = find("хвост журнала подпроцесса не дочитан")
check("провал дренажа хвоста записан", len(found) == 1, str(texts())[:300])
if found:
    msg = found[0].getMessage()
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке действие и pid", "action=whisper-transcribe" in msg and "pid=" in msg, msg)
    check("в строке тип отказа", "OSError" in msg, msg)
check("управление не изменилось: вернулся таймаут, а не исключение",
      payload is None and error and "timeout" in error, f"{payload!r} {error!r}")

# ============================================================================
# [11] Потери журнала ребёнка агрегируются, а не пишутся в цикле
#
# _read_lines ловит ValueError в теле while True, и на одной переполненной строке
# ветка срабатывает многократно. Строка журнала на каждый оборот залила бы
# bot.log (ротация 5 МБ) быстрее, чем оператор успел бы его открыть, поэтому
# потери копятся и уезжают ОДНОЙ сводной записью.
#
# СЦЕНАРИЙ ОПЕРАТОРА: разбирает зависание распознавания, видит журнал ребёнка
# с дырой и не знает, что часть строк потеряна — думает, что ребёнок молчал.
#
# Привод настоящий: ребёнок пишет в stderr строку на 200 КБ, лимит потока
# asyncio — 64 КБ.
# ============================================================================
print("\n[11] Потерянные строки журнала ребёнка посчитаны одной записью")
fresh()
os.environ["HELPER_MODE"] = "overflow"
payload, error = run(B._run_json_tool("whisper-transcribe", {"file_path": "x.ogg"}, timeout=25))
found = find("журнал подпроцесса потерян частично")
check("потеря строк журнала доложена", len(found) == 1, str(texts())[:400])
if found:
    msg = found[0].getMessage()
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке действие и pid", "action=whisper-transcribe" in msg and "pid=" in msg, msg)
    check("в строке названо число потерянных строк", " строк длиннее лимита" in msg, msg)
check("уцелевшая часть журнала ребёнка всё равно перелита",
      any("последняя внятная строка" in t for t in texts()), str(texts())[:400])
check("управление не изменилось: ответ ребёнка разобран",
      payload and payload.get("text") == "ответ" and error is None, f"{payload!r} {error!r}")
check("сводная запись ровно одна, а не по одной на оборот цикла",
      len([r for r in COLLECTOR.records
           if "журнал подпроцесса потерян частично" in r.getMessage()]) == 1,
      str(len(COLLECTOR.records)))

fresh()
os.environ["HELPER_MODE"] = "ok"
run(B._run_json_tool("gemini-text", {"prompt": "x"}, timeout=20))
check("без переполнения сводной записи нет",
      not find("журнал подпроцесса потерян частично"), str(texts()))

# ============================================================================
# [12] Секреты в журнал не попадают
#
# ЗАМЕР: по всем 131 219 строкам журналов на диске совпадений с формами ключей
# (AIza…, sk_/gsk_…, tvly-…, digits:hash, ?key=…, Bearer …) — 0. Сегодня ключи
# не текут, и новая диагностика не имеет права стать первой течью: она впервые
# печатает str() чужих исключений, а google отдаёт ключ в query string. Формат
# маски совпадает с существующим (gemini_client печатает 5 последних символов),
# чтобы записи можно было сопоставлять по ключу.
# ============================================================================
print("\n[12] Секреты маскируются, хвост оставлен для сопоставления")
SECRETS = [
    "AIzaSyC0ffeeDamBOPT1234567890abcdefQ",
    "gsk_aBcDeFgH1234567890IjKlMnOp",
    "tvly-K1234567890abcdef",
]
for secret in SECRETS:
    masked = B._redact(f"ошибка провайдера: {secret} отклонён")
    check(f"ключ вида {secret[:4]}… не печатается целиком", secret not in masked, masked)
    check(f"хвост ключа {secret[:4]}… сохранён", secret[-5:] in masked, masked)

masked = B._redact("GET https://generativelanguage.googleapis.com/v1?key=SUPERSECRETVALUE12345 -> 400")
check("ключ из query string замаскирован", "SUPERSECRETVALUE12345" not in masked, masked)
check("сам URL для разбора остался", "generativelanguage" in masked, masked)
masked = B._redact("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
check("Bearer-токен замаскирован", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in masked, masked)

fresh()
os.environ["HELPER_MODE"] = "secret"
payload, error = run(B._run_json_tool("gemini-text", {"prompt": "x"}, timeout=20))
emitted = "\n".join(texts())
check("ключ, приехавший от ребёнка, в журнал не попал",
      "AIzaSyC0ffeeDamBOPT1234567890abcdefQ" not in emitted, emitted)
check("но отказ записан и сопоставим по хвосту ключа",
      bool(find("подпроцесс не дал ответа")) and "efQ" in emitted, emitted)
check("длинный чужой текст обрезан", all(len(t) < 1200 for t in texts()),
      str([len(t) for t in texts()]))

B.__file__ = REAL_BT
os.environ.pop("HELPER_MODE", None)

# ============================================================================
# [13] Уборка за убитым ребёнком: утечка диска перестала быть молчаливой
#
# ЗАМЕР: убитый по таймауту процесс до своей уборки не доходит, и wav 16 кГц
# моно остаётся рядом с исходником навсегда — около 2 МБ на минуту голосового.
# В журнале причины заполнения диска не было ни одной строки.
#
# СЦЕНАРИЙ ОПЕРАТОРА: на диске кончилось место, temp_media чистится, а рядом с
# голосовыми лежат сотни *_converted.wav, и в журнале про них ничего.
# ============================================================================
print("\n[13] Неудалённый конвертат голосового виден в журнале")
fresh()
voice = os.path.join(_TMPDIR, "голосовое.ogg")
wav = os.path.join(_TMPDIR, "голосовое_converted.wav")
open(voice, "wb").close()
os.makedirs(wav, exist_ok=True)  # каталог вместо файла: os.remove упадёт
returned = B._remove_converted_wav(voice)
found = find("конвертат голосового не удалён")
check("отказ удаления записан", len(found) == 1, str(texts()))
if found:
    msg = found[0].getMessage()
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке путь", "_converted.wav" in msg, msg)
    check("в строке тип отказа",
          any(t in msg for t in ("PermissionError", "IsADirectoryError", "OSError")), msg)
check("управление не изменилось: исключение не поднято", returned is None)
os.rmdir(wav)

# ============================================================================
# [14] Отказ снятия дерева процессов: сироты ffmpeg перестали быть невидимыми
#
# ЗАМЕР: whisper-transcribe запускает ffmpeg; при провале taskkill /T внук
# остаётся жить без родителя, без таймаута и без читателя результата. В журнале
# об этом не было ничего — сирот приходилось искать в диспетчере задач.
# ============================================================================
print("\n[14] Провал снятия дерева процессов записан")
fresh()


class FakeProc:
    """Процесс, у которого убийство дерева падает, а kill() работает."""

    def __init__(self):
        self.pid = 4242
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True


async def _drive_kill_failure():
    proc = FakeProc()
    saved_exec = B.asyncio.create_subprocess_exec
    saved_killpg = getattr(B.os, "killpg", None)

    # Текст отказа умышленно огромный: он проверяет и обрезку в
    # _describe_exception. Драйвер операционной системы может вернуть простыню,
    # и целиком она в журнал попадать не должна.
    long_tail = " деталь" * 400

    async def boom(*a, **k):
        raise OSError("taskkill недоступен" + long_tail)

    def boom_sync(*a, **k):
        raise OSError("killpg недоступен" + long_tail)

    B.asyncio.create_subprocess_exec = boom
    if saved_killpg is not None:
        B.os.killpg = boom_sync
    try:
        await B._kill_process_tree(proc)
    finally:
        B.asyncio.create_subprocess_exec = saved_exec
        if saved_killpg is not None:
            B.os.killpg = saved_killpg
    return proc


proc = run(_drive_kill_failure())
found = find("дерево процессов не снято")
check("провал снятия дерева записан", len(found) == 1, str(texts()))
if found:
    msg = found[0].getMessage()
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке pid", "pid=4242" in msg, msg)
    check("в строке тип отказа", "OSError" in msg, msg)
    check("в строке названы сироты", "ffmpeg" in msg, msg)
    check("простыня от драйвера обрезана", len(msg) < 600, f"длина {len(msg)}")
check("управление не изменилось: страховочный kill() всё равно вызван", proc.killed)

# ============================================================================
# [15] Отметка сводки: отказ снимка, отказ восстановления, отказ снятия флага
#
# ЗАМЕР: файл статуса один на процесс и одноместный, ребёнок затирает чужую
# отметку своей. Успешное восстановление писалось (WARNING «восстановлена
# затёртая отметка сводки»), а отказ — нет: в журнале был виден только хороший
# исход. Отказ означает, что сторож сводки слеп до конца дайджеста.
#
# СЦЕНАРИЙ ОПЕРАТОРА: недельный дайджест не пришёл, сторож молчал, в журнале
# про отметку сводки — только успехи.
# ============================================================================
print("\n[15] Отказы вокруг отметки сводки записаны")
fresh()
saved_read = runtime_guard.read_summary_status


def _read_boom(*a, **k):
    raise OSError("файл статуса занят другим процессом")


runtime_guard.read_summary_status = _read_boom
snapshot = B._foreign_summary_status({"kind": "llama_triage"})
runtime_guard.read_summary_status = saved_read
found = find("снимок чужой отметки сводки не сделан")
check("отказ снимка отметки записан", len(found) == 1, str(texts()))
if found:
    check("в строке тип отказа", "OSError" in found[0].getMessage(), found[0].getMessage())
check("управление не изменилось: None, без исключения", snapshot is None, repr(snapshot))

fresh()
saved_status_path = runtime_guard.SUMMARY_STATUS_PATH
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "нет_такого_каталога", "s.json")
restored = B._restore_foreign_summary_status({"kind": "weekly", "stage": "gemini_attempt",
                                              "utc": "2026-07-28T20:00:00+00:00", "active": True})
runtime_guard.SUMMARY_STATUS_PATH = saved_status_path
found = find("затёртая отметка сводки НЕ восстановлена")
check("отказ восстановления записан", len(found) == 1, str(texts()))
if found:
    msg = found[0].getMessage()
    check("в строке вид сводки", "kind=weekly" in msg, msg)
    check("в строке тип отказа",
          any(t in msg for t in ("FileNotFoundError", "OSError", "NotADirectoryError")), msg)
check("управление не изменилось: False, без исключения", restored is False)

fresh()
saved_release = runtime_guard.release_generation_status


def _release_boom(*a, **k):
    raise OSError("файл статуса занят")


runtime_guard.release_generation_status = _release_boom
os.environ["HELPER_MODE"] = "ok"
B.__file__ = HELPER
payload, error = run(B.generate_gemini_text_async("вопрос врача", {"kind": "llama_triage"},
                                                 timeout=20))
runtime_guard.release_generation_status = saved_release
B.__file__ = REAL_BT
os.environ.pop("HELPER_MODE", None)
found = find("флаг генерации не снят")
check("отказ снятия флага записан", len(found) == 1, str(texts()))
if found:
    msg = found[0].getMessage()
    check("уровень не ниже WARNING", found[0].levelno >= logging.WARNING,
          logging.getLevelName(found[0].levelno))
    check("в строке вид генерации", "kind=llama_triage" in msg, msg)
    check("в строке тип отказа", "OSError" in msg, msg)
check("управление не изменилось: ответ врачу доехал, несмотря на отказ уборки",
      payload is not None and getattr(payload, "text", None) == "коффердам обязателен",
      f"{payload!r} {error!r}")

# ============================================================================
# [16] Отказ провайдера поиска перестал быть невидимым
#
# ЗАМЕР по коду: список errors доезжает до вызывающего ТОЛЬКО когда пусты оба
# провайдера — при успехе ddgs ветка отдаёт ok: True и errors выбрасывает. То
# есть «tavily не отвечает уже месяц, поиск втихую держится на ddgs» не видно
# нигде: ни во враче, ни в журнале, ни в коде вызывающего.
#
# СЦЕНАРИЙ ОПЕРАТОРА: качество ответов просело, потому что половина поисков идёт
# по резервному провайдеру, и узнать об этом было нельзя.
#
# Провайдеры подменены заглушками в sys.modules: сети нет.
# ============================================================================
print("\n[16] Отказ провайдера веб-поиска записан")
fresh()
fake_config = types.ModuleType("config")
fake_config.SEARCH_PROVIDER = "tavily"
fake_config.TAVILY_API_KEY = "tvly-нетакойключ"
fake_tavily = types.ModuleType("tavily")


class _Tavily:
    def __init__(self, api_key=None):
        pass

    def search(self, **kwargs):
        raise RuntimeError("tavily: 503 Service Unavailable")


fake_tavily.TavilyClient = _Tavily
fake_ddgs = types.ModuleType("ddgs")


class _DDGS:
    def __enter__(self):
        raise RuntimeError("ddgs: ratelimit 429")

    def __exit__(self, *a):
        return False


fake_ddgs.DDGS = _DDGS
saved_modules = {name: sys.modules.get(name) for name in ("config", "tavily", "ddgs")}
sys.modules.update({"config": fake_config, "tavily": fake_tavily, "ddgs": fake_ddgs})
try:
    results, errors = B._web_search_sync("коффердам изоляция", 2)
finally:
    for name, module in saved_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module

tavily_found = find("провайдер tavily отказал")
ddgs_found = find("провайдер ddgs отказал")
check("отказ tavily записан", len(tavily_found) == 1, str(texts()))
check("отказ ddgs записан", len(ddgs_found) == 1, str(texts()))
if tavily_found:
    check("в строке tavily есть тип и текст отказа",
          "RuntimeError" in tavily_found[0].getMessage()
          and "503" in tavily_found[0].getMessage(), tavily_found[0].getMessage())
if ddgs_found:
    check("в строке ddgs есть тип и текст отказа",
          "RuntimeError" in ddgs_found[0].getMessage()
          and "429" in ddgs_found[0].getMessage(), ddgs_found[0].getMessage())
check("управление не изменилось: пустой список и оба отказа в errors",
      results == [] and len(errors) == 2, f"{results!r} {errors!r}")

# ============================================================================
# [17] Ни одна новая запись на пути врача не сидит ниже WARNING
#
# Прямое следствие замера из блока [1]: 0 записей DEBUG на 131 219 строк, корень
# на INFO. Диагностика на DEBUG или INFO не поднимет grep по ERROR/WARNING,
# которым оператор и разбирает инциденты. Проверка не ищет строки в исходнике —
# она прогоняет приводы и смотрит на уровни фактически выпущенных записей.
# ============================================================================
print("\n[17] Все выпущенные записи — не ниже WARNING")
fresh()
runtime_guard._log_watchdog_exit(300.0)
saved_dump = runtime_guard.WATCHDOG_DUMP_PATH
runtime_guard.WATCHDOG_DUMP_PATH = busy_dir
runtime_guard.dump_runtime_state("проверка_уровней")
runtime_guard.WATCHDOG_DUMP_PATH = saved_dump
with open(runtime_guard.SUMMARY_STATUS_PATH, "w", encoding="utf-8") as handle:
    handle.write("{битый")
runtime_guard.read_summary_status()
os.remove(runtime_guard.SUMMARY_STATUS_PATH)
B._remove_converted_wav(None)  # пустой путь — тихий штатный выход
below = [f"{logging.getLevelName(r.levelno)}: {r.getMessage()[:50]}"
         for r in COLLECTOR.records if r.levelno < logging.WARNING]
check("среди выпущенных записей нет ни одной ниже WARNING", not below, str(below))
check("выпущено ровно три записи по трём приводам", len(COLLECTOR.records) == 3, str(texts()))
check("ни одного вызова logger.debug в обоих файлах",
      ".debug(" not in (open("runtime_guard.py", encoding="utf-8").read()
                        + open("blocking_tools.py", encoding="utf-8").read()),
      "найден вызов debug")

# ============================================================================
# [18] Изоляция прогона
# ============================================================================
print("\n[18] Изоляция прогона")
check("журнал уведён во временный каталог", _TMPDIR in os.environ.get("STOMCHAT_LOG_PATH", ""),
      os.environ.get("STOMCHAT_LOG_PATH"))
check("файл статуса уведён во временный каталог", _TMPDIR in runtime_guard.SUMMARY_STATUS_PATH,
      runtime_guard.SUMMARY_STATUS_PATH)
check("дамп сторожа уведён во временный каталог", _TMPDIR in runtime_guard.WATCHDOG_DUMP_PATH,
      runtime_guard.WATCHDOG_DUMP_PATH)
check("боевой файл статуса рядом не тронут",
      not os.path.exists(os.path.join(os.getcwd(), "bot_summary_status.json.tmp")))
check("B.__file__ возвращён на настоящий blocking_tools",
      os.path.basename(B.__file__) == "blocking_tools.py", B.__file__)
check("подмены sys.modules сняты",
      not isinstance(sys.modules.get("tavily"), types.ModuleType)
      or getattr(sys.modules.get("tavily"), "__file__", None) is not None
      or "tavily" not in sys.modules,
      str(sys.modules.get("tavily")))

logging.getLogger().removeHandler(COLLECTOR)
for handler in list(logging.getLogger().handlers):
    try:
        handler.close()
    except Exception:
        pass
logging.getLogger().handlers.clear()
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'=' * 70}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
