import faulthandler
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler


def _default_log_path():
    """
    Куда писать журнал. Боевой bot.log тесты трогать не должны.

    configure_logging() вызывается на уровне модуля в main.py, поэтому ЛЮБОЙ
    тест, импортирующий main, начинал писать в журнал бота. Замер на 28 июля
    2026 на этой машине: в bot.log лежало 1629 строк работы бота и 1005 строк
    тестовой выдумки — несуществующие чаты, выдуманные врачи и строки ERROR,
    которых в работе не было. Именно этот файл читают, когда разбираются, почему
    бот вёл себя не так.

    Хуже того, журнал ротируется по 5 МБ. Один прогон набора добавляет около
    0.2 МБ, то есть полтора десятка прогонов вытесняют историю работы целиком.

    Имя точки входа — надёжный признак: тесты запускаются как test_*.py.
    Переменная окружения оставлена для случаев, когда нужно указать путь явно.
    """
    override = os.getenv("STOMCHAT_LOG_PATH")
    if override:
        return override
    try:
        import sys
        entry = os.path.basename(sys.argv[0] or "")
    except Exception:
        entry = ""
    if entry.startswith("test_") and entry.endswith(".py"):
        return "bot_test.log"
    return "bot.log"


LOG_PATH = _default_log_path()
HEARTBEAT_PATH = "bot_heartbeat.json"
SUMMARY_STATUS_PATH = "bot_summary_status.json"
WATCHDOG_DUMP_PATH = "bot_watchdog_dump.txt"
HEARTBEAT_INTERVAL_SECONDS = 30
WATCHDOG_STALE_SECONDS = 300

# Сколько сторож даёт себе на запись дампа и строки в журнал перед os._exit(78).
# См. _arm_hard_exit: выход не имеет права зависеть от того, удалась ли запись.
WATCHDOG_LOG_GRACE_SECONDS = 5.0

_last_heartbeat_monotonic = time.monotonic()
_watchdog_stop = threading.Event()
_watchdog_thread = None

# --- ограничитель частоты записей -------------------------------------------
#
# Одна строка на инцидент, а не на сообщение врача.
#
# Отказы, которые лечатся этими правками, повторяются на КАЖДОМ обращении:
# файл статуса держит антивирус, ключи Gemini гео-заблокированы (замер по
# bot.log: 45 записей FAILED_PRECONDITION подряд по всем четырём ключам).
# Честная строка без ограничителя превращается в тысячи, а bot.log ротируется
# по 5 МБ (configure_logging) — то есть шум не просто мешает, он ВЫТЕСНЯЕТ из
# журнала историю работы бота, ровно ту, которую потом читают.
#
# Ограничитель считает, а не выбрасывает: число подавленных уезжает в следующую
# строку, и масштаб отказа оператору виден.
THROTTLE_WINDOW_SECONDS = 60.0
_throttle_lock = threading.Lock()
_throttle_state = {}


def throttled_log(level, key, message, *args, logger=None):
    """
    Записать не чаще одного раза в THROTTLE_WINDOW_SECONDS на ключ.

    Замок нужен по-настоящему: сюда заходят и петля событий, и поток сторожа.
    Сама запись делается ВНЕ замка — logging берёт свой замок на обработчике, и
    держать два замка вложенно на горячем пути незачем.

    logger передаётся вызывающим, чтобы имя в журнале осталось его собственным:
    оператор ищет отказы подпроцессов по `blocking_tools`, и подмена имени на
    `runtime_guard` сломала бы и поиск, и разбор по логгерам.
    """
    now = time.monotonic()
    with _throttle_lock:
        last, suppressed = _throttle_state.get(key, (None, 0))
        if last is not None and now - last < THROTTLE_WINDOW_SECONDS:
            _throttle_state[key] = (last, suppressed + 1)
            return False
        _throttle_state[key] = (now, 0)
    if suppressed:
        message += " [подавлено похожих: %d за %.0f c]"
        args = args + (suppressed, now - last)
    (logger or logging.getLogger(__name__)).log(level, message, *args)
    return True


def reset_throttle():
    """Сбросить окна ограничителя. Нужно проверкам, чтобы не влиять друг на друга."""
    with _throttle_lock:
        _throttle_state.clear()


def configure_logging():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.setLevel(logging.INFO)

    if os.getenv("STOMCHAT_CONSOLE_LOG") == "1":
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    logging.getLogger("telethon").setLevel(logging.ERROR)
    # Исключение из глушения: именно этот логгер печатает «Sleeping for Ns
    # (until ...) on <request>» при FloodWait. Telethon по умолчанию ПЕРЕЖИДАЕТ
    # флуд внутри вызова, и на уровне ERROR этой строки не видно — из-за чего
    # флуд в этом проекте оказался неизмерим в принципе: на 109 798 строк
    # bot.log.1 нашлось три flood-записи, и все три транспортные (HTTP 429), то
    # есть RPC-уровень не был виден ни разу. Без этой строки нельзя ни
    # подтвердить, ни опровергнуть, что бот попадает под ограничение частоты.
    logging.getLogger("telethon.client.users").setLevel(logging.INFO)


def utc_now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_heartbeat(reason):
    global _last_heartbeat_monotonic
    _last_heartbeat_monotonic = time.monotonic()

    payload = {
        "utc": utc_now_text(),
        "pid": os.getpid(),
        "reason": reason,
        "stale_after_seconds": WATCHDOG_STALE_SECONDS,
    }
    tmp_path = HEARTBEAT_PATH + ".tmp"
    for attempt in range(5):
        try:
            with open(tmp_path, "w", encoding="utf-8") as heartbeat_file:
                json.dump(payload, heartbeat_file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, HEARTBEAT_PATH)
            break
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.1)


def write_summary_status(status):
    payload = dict(status)
    payload["utc"] = utc_now_text()
    payload["pid"] = os.getpid()
    tmp_path = SUMMARY_STATUS_PATH + ".tmp"
    for attempt in range(5):
        try:
            with open(tmp_path, "w", encoding="utf-8") as status_file:
                json.dump(payload, status_file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, SUMMARY_STATUS_PATH)
            break
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.1)


def clear_summary_status(reason="idle"):
    write_summary_status({"active": False, "stage": reason})


def read_summary_status():
    """
    Разбор статуса или {}. Пустой словарь НЕ означает «сводки нет» — он же
    возвращается, когда файл не прочитан, и это два разных мира.

    Кто на этом стоит: release_generation_status (:172) решает по нему, гасить
    ли флаг генерации, а main.summary_watchdog_task (main.py:435) — ловить ли
    зависание дайджеста. Нечитаемый файл выглядит для обоих как «никакой сводки
    не идёт»: флаг гасится, сторож сводки разоружается, и зависший дайджест всего
    чата врачей теряется молча. Именно эту потерю разбирает докстрока
    release_generation_status. Возврат {} остаётся — управление не меняем,
    добавляем только запись.
    """
    try:
        with open(SUMMARY_STATUS_PATH, "r", encoding="utf-8") as status_file:
            return json.load(status_file)
    except FileNotFoundError:
        # До первого clear_summary_status("startup") (main.py:2142) файла нет.
        # Это штатное состояние, а не отказ: запись здесь была бы шумом на
        # каждый вызов и обесценила бы настоящие строки ниже.
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        # WARNING, а не DEBUG: замер по 131 219 строкам всех журналов на диске —
        # 0 записей DEBUG, корень стоит на INFO (:72 ниже). DEBUG здесь означал
        # бы отсутствие строки.
        throttled_log(
            logging.WARNING,
            "read_summary_status_failed",
            "статус сводки не прочитан path=%s: %s — сторож сводки на это время"
            " ослеп, зависание дайджеста не будет поймано",
            SUMMARY_STATUS_PATH,
            f"{type(exc).__name__}: {exc}",
        )
        return {}


# Виды работ, которые сторож саммари обязан охранять: только у них зависание
# означает потерянный отчёт всему чату врачей.
SUMMARY_KINDS = frozenset({"daily", "weekly"})


def release_generation_status(kind=None, reason=None, extra=None):
    """
    Снять флаг «идёт генерация», не погасив чужую работу.

    Файл статуса один на процесс, а пишут в него все: и дайджест, и любой ответ
    ассистента (триаж, рецензент, ответ в ЛС). Безусловный сброс в конце
    короткого ответа стирал отметку дайджеста, который в этот момент ещё
    считался, — и сторож терял способность увидеть его зависание. Поэтому
    обычная генерация не трогает флаг, пока активна сводка.

    Две разные охраны, и путать их нельзя:
      * своя работа — сводка. Конец её вызова к LLM ещё не конец конвейера:
        дальше публикация и отправка в чат, зависание там ловит тот же сторож.
        Флаг снимает владелец конвейера в самом конце (summarizer), не мы;
      * чужая работа — сводка идёт, а закончился посторонний ответ. Тем более
        не трогаем.

    Снятие флага и СОХРАНЕНИЕ разбора — разные вещи, и раньше первое стирало
    второе. Каскад моделей записывал в статус stage=all_exhausted вместе с
    причиной и текстом ошибки провайдера, после чего сразу снимал флаг — и
    файл превращался в «active: false, stage: pm_chat_done». Оператор, открывший
    статус после того, как бот не ответил, не находил там ни причины, ни
    ошибки. Поэтому extra позволяет погасить флаг И оставить разбор в том же
    файле: сторож обезоружен, диагностика на месте.

    Возвращает True, если флаг снят.
    """
    if kind in SUMMARY_KINDS:
        return False
    current = read_summary_status()
    if current.get("active") and current.get("kind") in SUMMARY_KINDS:
        return False
    payload = {"active": False, "stage": reason or f"{kind or 'generation'}_done"}
    if extra:
        payload.update(extra)
    write_summary_status(payload)
    return True


def dump_runtime_state(reason):
    try:
        with open(WATCHDOG_DUMP_PATH, "a", encoding="utf-8") as dump_file:
            dump_file.write("\n=== STOMCHAT RUNTIME DUMP ===\n")
            dump_file.write(f"utc={utc_now_text()} pid={os.getpid()} reason={reason}\n")
            faulthandler.dump_traceback(file=dump_file, all_threads=True)
            dump_file.write("=== END DUMP ===\n")
            dump_file.flush()
    except Exception as exc:
        # Писатель дампа гасил свой отказ молча, и это была вторая половина той же
        # слепоты: занятость файла — ровно та причина, по которой на этой машине
        # ломается запись heartbeat (main.py:417-421 про антивирус/индексатор).
        # Совпадение занятого файла с падением фоновой задачи оставляло разбор
        # НИГДЕ: ни в дампе, ни в журнале. Возврат остаётся тихим (управление не
        # меняем), но факт потери дампа теперь записан.
        throttled_log(
            logging.WARNING,
            "dump_runtime_state_failed",
            "дамп состояния НЕ записан reason=%s path=%s: %s",
            reason,
            WATCHDOG_DUMP_PATH,
            f"{type(exc).__name__}: {exc}",
        )


def _arm_hard_exit(code, grace_seconds):
    """
    Назначить выход, который случится независимо от того, удалась ли запись.

    Зачем: logging берёт замок на обработчике, а сторож срабатывает как раз
    тогда, когда главный поток застрял — в том числе он мог застрять ВНУТРИ
    записи в журнал, держа этот замок. Тогда logger.critical из потока сторожа
    встал бы на замке навсегда, и зависание на 5 минут превратилось бы в вечное:
    процесс не убит, не перезапущен, врач ждёт бесконечно. Это было бы хуже
    исходной ошибки — молчаливого, но БЫСТРОГО выхода.

    Поэтому os._exit(78) назначается ДО попытки записи. Это не смена поведения:
    выход с кодом 78 и был единственным исходом этой ветки, страховка лишь
    гарантирует его при любом состоянии журнала и файла дампа.
    """
    def _hard_exit():
        time.sleep(grace_seconds)
        os._exit(code)

    threading.Thread(target=_hard_exit, name="stomchat-hard-exit", daemon=True).start()


def _log_watchdog_exit(age, dump_error=None):
    """
    Единственная строка, по которой убийство сторожем вообще отличимо от
    отключения света, ручной остановки и падения интерпретатора.

    Замер по всем журналам на диске: 15 записей WATCHDOG EXIT в
    bot_watchdog_dump.txt (2026-05-18 .. 06-21) против 0 строк в bot.log,
    bot.log.1, bot_test.log и bot_supervisor.log. Кода выхода 78 нет даже в
    надзорном журнале start.bat. Пример из данных: 2026-05-19 13:45:46 журнал
    обрывается на извлечении кадра, сторож убивает pid 49776 в 13:51:03, а
    следующая строка — 2026-05-20 19:03:30. Бот лежал 29 часов, и установить
    по журналу, почему, было нельзя.

    CRITICAL, а не ERROR: это последнее, что процесс успевает сказать.
    """
    try:
        logging.getLogger(__name__).critical(
            "watchdog exit: цикл событий не отвечает %.1f c (порог %s c),"
            " pid=%s код выхода=78, дамп=%s%s",
            age,
            WATCHDOG_STALE_SECONDS,
            os.getpid(),
            WATCHDOG_DUMP_PATH,
            f", ДАМП НЕ ЗАПИСАН: {dump_error}" if dump_error else "",
        )
        # os._exit не выполняет ни atexit, ни флаш обработчиков. Оговорка честная:
        # у боевого RotatingFileHandler флаш уже делает сам StreamHandler.emit,
        # то есть на сегодняшней конфигурации строка доезжает и без этого цикла —
        # проверкой он не подтверждён как необходимый. Оставлен как страховка на
        # буферизующий обработчик (MemoryHandler, QueueHandler): с ним запись без
        # флаша умерла бы вместе с процессом, а это ровно тот случай, ради
        # которого правка и делается.
        for handler in list(logging.getLogger().handlers):
            try:
                handler.flush()
            except Exception:
                pass
    except Exception:
        # Отказ журнала не имеет права помешать выходу.
        pass


def _watchdog_loop():
    while not _watchdog_stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        age = time.monotonic() - _last_heartbeat_monotonic
        if age > WATCHDOG_STALE_SECONDS:
            _arm_hard_exit(78, WATCHDOG_LOG_GRACE_SECONDS)
            dump_error = None
            try:
                with open(WATCHDOG_DUMP_PATH, "a", encoding="utf-8") as dump_file:
                    dump_file.write("\n=== STOMCHAT WATCHDOG EXIT ===\n")
                    dump_file.write(
                        f"utc={utc_now_text()} pid={os.getpid()} reason=event_loop_heartbeat_stale_{age:.1f}s\n"
                    )
                    faulthandler.dump_traceback(file=dump_file, all_threads=True)
                    dump_file.write("=== END WATCHDOG EXIT ===\n")
                    dump_file.flush()
            except Exception as exc:
                # Раньше здесь стоял pass, и при занятом файле дампа об убийстве
                # не оставалось следа НИГДЕ. Теперь причина отказа записи уезжает
                # в ту же строку журнала.
                dump_error = f"{type(exc).__name__}: {exc}"
            _log_watchdog_exit(age, dump_error)
            os._exit(78)


def start_watchdog():
    global _watchdog_thread
    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    write_heartbeat("watchdog_start")
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop,
        name="stomchat-watchdog",
        daemon=True,
    )
    _watchdog_thread.start()


def stop_watchdog():
    _watchdog_stop.set()


_ACTIVE_TASKS = set()

def create_task(coro, name):
    task = asyncio.create_task(coro, name=name)
    _ACTIVE_TASKS.add(task)
    task.add_done_callback(_ACTIVE_TASKS.discard)
    task.add_done_callback(_log_task_result)
    return task


def _log_task_result(task):
    if task.cancelled():
        logging.getLogger(__name__).warning("background task cancelled name=%s", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        logging.getLogger(__name__).exception(
            "background task crashed name=%s",
            task.get_name(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        dump_runtime_state(f"background_task_crashed_{task.get_name()}")
