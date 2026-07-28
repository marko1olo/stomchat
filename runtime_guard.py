import faulthandler
import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler


LOG_PATH = "bot.log"
HEARTBEAT_PATH = "bot_heartbeat.json"
SUMMARY_STATUS_PATH = "bot_summary_status.json"
WATCHDOG_DUMP_PATH = "bot_watchdog_dump.txt"
HEARTBEAT_INTERVAL_SECONDS = 30
WATCHDOG_STALE_SECONDS = 300

_last_heartbeat_monotonic = time.monotonic()
_watchdog_stop = threading.Event()
_watchdog_thread = None


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
    try:
        with open(SUMMARY_STATUS_PATH, "r", encoding="utf-8") as status_file:
            return json.load(status_file)
    except (OSError, json.JSONDecodeError):
        return {}


# Виды работ, которые сторож саммари обязан охранять: только у них зависание
# означает потерянный отчёт всему чату врачей.
SUMMARY_KINDS = frozenset({"daily", "weekly"})


def release_generation_status(kind=None, reason=None):
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

    Возвращает True, если флаг снят.
    """
    if kind in SUMMARY_KINDS:
        return False
    current = read_summary_status()
    if current.get("active") and current.get("kind") in SUMMARY_KINDS:
        return False
    clear_summary_status(reason or f"{kind or 'generation'}_done")
    return True


def dump_runtime_state(reason):
    try:
        with open(WATCHDOG_DUMP_PATH, "a", encoding="utf-8") as dump_file:
            dump_file.write("\n=== STOMCHAT RUNTIME DUMP ===\n")
            dump_file.write(f"utc={utc_now_text()} pid={os.getpid()} reason={reason}\n")
            faulthandler.dump_traceback(file=dump_file, all_threads=True)
            dump_file.write("=== END DUMP ===\n")
            dump_file.flush()
    except Exception:
        pass


def _watchdog_loop():
    while not _watchdog_stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        age = time.monotonic() - _last_heartbeat_monotonic
        if age > WATCHDOG_STALE_SECONDS:
            try:
                with open(WATCHDOG_DUMP_PATH, "a", encoding="utf-8") as dump_file:
                    dump_file.write("\n=== STOMCHAT WATCHDOG EXIT ===\n")
                    dump_file.write(
                        f"utc={utc_now_text()} pid={os.getpid()} reason=event_loop_heartbeat_stale_{age:.1f}s\n"
                    )
                    faulthandler.dump_traceback(file=dump_file, all_threads=True)
                    dump_file.write("=== END WATCHDOG EXIT ===\n")
                    dump_file.flush()
            except Exception:
                pass
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
