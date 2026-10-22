"""
db_worker.py: Асинхронный рабочий пул и управление блокировками SQLite для StomChat и EndoChat.

Инварианты подсистемы:
1. Сериализация операций записи в базу данных через выделенный однопоточный исполнитель (_DB_EXECUTOR).
2. Защита от блокировок: автоматический retry с экспоненциальным backoff при sqlite3.OperationalError (database is locked / busy).
3. Соблюдение ограничений 1GB Micro-VPS:
   - PRAGMA busy_timeout = 30000 (30 сек)
   - PRAGMA synchronous = NORMAL
   - PRAGMA cache_size = -4000 (лимит 4 МБ)
   - PRAGMA journal_mode = WAL (кешируется per-path)
4. Мониторинг глубины очереди пула для защиты от переполнения памяти при всплесках входящих сообщений.
"""

import asyncio
import logging
import random
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional, Set

try:
    import config
except ImportError:
    config = None

logger = logging.getLogger(__name__)

# Порог глубины очереди, при превышении которого логируется предупреждение о беклоге
MAX_QUEUE_BACKLOG = 500

# Параметры повторных попыток при блокировках БД
DB_LOCK_RETRY_ATTEMPTS = 5
DB_LOCK_RETRY_BASE_DELAY = 0.1  # секунды
DB_LOCK_RETRY_MAX_DELAY = 2.0   # секунды

# Пути баз данных, для которых режим журнала WAL уже активирован в текущем процессе
_WAL_READY_PATHS: Set[str] = set()

# Единый однопоточный исполнитель для строгой сериализации транзакций SQLite
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-worker")


def get_default_db_path() -> str:
    """Возвращает актуальный путь к базе данных из config.py или дефолтное имя."""
    if config and hasattr(config, "DB_PATH"):
        return str(config.DB_PATH)
    root = Path(__file__).resolve().parent
    if (root / "endo_bot.db").exists():
        return "endo_bot.db"
    return "stomat_bot.db"


def connect_db(db_path: Optional[str] = None, timeout: float = 30.0) -> sqlite3.Connection:
    """
    Открывает соединение с базой данных SQLite с соблюдением всех инвариантов Micro-VPS:
    - busy_timeout = 30000
    - synchronous = NORMAL
    - cache_size = -4000 (4 МБ)
    - journal_mode = WAL (выставляется один раз на процесс для каждого файла базы)
    """
    path = db_path or get_default_db_path()
    conn = sqlite3.connect(path, timeout=timeout)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -4000")
    if path not in _WAL_READY_PATHS:
        conn.execute("PRAGMA journal_mode = WAL")
        _WAL_READY_PATHS.add(path)
    return conn


@contextmanager
def db_connection(db_path: Optional[str] = None, timeout: float = 30.0):
    """
    Контекстный менеджер безопасного соединения SQLite с авто-коммитом/роллбеком
    и гарантированным закрытием соединения во всех ветках исключений.
    """
    conn = connect_db(db_path, timeout=timeout)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def get_worker_queue_depth() -> int:
    """Возвращает текущее количество задач, ожидающих выполнения в пуле БД."""
    try:
        return _DB_EXECUTOR._work_queue.qsize()
    except Exception:
        return 0


def is_lock_error(exc: Exception) -> bool:
    """Проверяет, является ли исключение ошибкой блокировки SQLite (database is locked / busy)."""
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "locked" in msg or "busy" in msg
    return False


def run_with_lock_retry(
    operation: Callable[[], Any],
    max_retries: int = DB_LOCK_RETRY_ATTEMPTS,
    base_delay: float = DB_LOCK_RETRY_BASE_DELAY
) -> Any:
    """
    Синхронно выполняет операцию с повторными попытками и экспоненциальным backoff
    при возникновении sqlite3.OperationalError (database is locked / busy).
    """
    for attempt in range(max_retries):
        try:
            return operation()
        except Exception as exc:
            if is_lock_error(exc) and attempt + 1 < max_retries:
                delay = min(DB_LOCK_RETRY_MAX_DELAY, base_delay * (2 ** attempt)) + random.uniform(0.01, 0.05)
                logger.warning(
                    "SQLite lock contention (attempt %d/%d): %s. Backing off for %.3fs",
                    attempt + 1, max_retries, exc, delay
                )
                time.sleep(delay)
                continue
            raise


async def run_in_db_worker(
    operation: Callable[[], Any],
    max_retries: int = DB_LOCK_RETRY_ATTEMPTS
) -> Any:
    """
    Асинхронно отправляет операцию в рабочий поток SQLite с контролем глубины очереди
    и автоматическим retry при блокировках.
    """
    depth = get_worker_queue_depth()
    if depth > MAX_QUEUE_BACKLOG:
        logger.warning(
            "DB worker queue backlog is high (%d tasks pending). Possible write spike or lock delay.",
            depth
        )

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _DB_EXECUTOR,
        lambda: run_with_lock_retry(operation, max_retries=max_retries)
    )


def shutdown_db_worker(wait: bool = True):
    """Корректно останавливает рабочий пул БД."""
    _DB_EXECUTOR.shutdown(wait=wait)
