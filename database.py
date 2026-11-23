import asyncio
import os
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import timezone
import time

import atexit

import html

from typing import Any, Dict, List, Optional, Tuple, Union



import config

import db_worker


logger = logging.getLogger(__name__)
# Экспортируем _DB_EXECUTOR из db_worker для обратной совместимости с внешними модулями и тестами
def __getattr__(name: str):
    if name == "_DB_EXECUTOR":
        return db_worker.get_db_executor()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__():
    return sorted(list(globals().keys()) + ["_DB_EXECUTOR"])

SAVE_RETRY_ATTEMPTS = 3
SAVE_RETRY_DELAY_SECONDS = 0.25

# Пути баз, для которых режим журнала уже выставлен в этом процессе (делегировано db_worker)
_WAL_READY = db_worker._WAL_READY_PATHS


def _connect():
    return db_worker.connect_db(config.DB_PATH, timeout=30)


def checkpoint_wal(mode: str = "TRUNCATE", max_retries: int = 5, retry_delay: float = 0.25) -> bool:
    """Сбрасывает WAL-журнал в основной файл базы данных (TRUNCATE или PASSIVE).
    
    Для ручного бекапа/синхронизации узлов используется TRUNCATE.
    Для периодического фонового сброса во время работы используется PASSIVE,
    чтобы не блокировать активные читающие и пишущие транзакции Telegram-сообщений.
    Защищен от дедлоков и сбоев блокировок: перехватывает sqlite3.OperationalError и повторяет с паузой.
    """
    valid_modes = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
    mode_upper = mode.upper() if mode and mode.upper() in valid_modes else "TRUNCATE"
    for attempt in range(max_retries):
        try:
            with _connection() as db:
                row = db.execute(f"PRAGMA wal_checkpoint({mode_upper})").fetchone()
                if row:
                    busy, log_pages, ckpt_pages = row[0], row[1], row[2]
                    if busy == 0:
                        logger.debug("WAL checkpoint (%s) успешен: log=%d, checkpointed=%d", mode_upper, log_pages, ckpt_pages)
                        return True
                    logger.warning(
                        "WAL checkpoint (%s) busy (попытка %d/%d): log=%d, checkpointed=%d. Ожидание...",
                        mode_upper, attempt + 1, max_retries, log_pages, ckpt_pages
                    )
                else:
                    return True
        except sqlite3.OperationalError as exc:
            logger.warning("WAL checkpoint (%s) lock contention (попытка %d/%d): %s", mode_upper, attempt + 1, max_retries, exc)
        except Exception as exc:
            logger.warning("WAL checkpoint (%s) error (попытка %d/%d): %s", mode_upper, attempt + 1, max_retries, exc)
        if attempt + 1 < max_retries:
            time.sleep(retry_delay * (attempt + 1))
    if mode_upper == "PASSIVE":
        logger.debug("WAL checkpoint (PASSIVE) отложен из-за активных читателей")
        return False
    logger.error("WAL checkpoint (%s) не завершился после %d попыток из-за блокировок", mode_upper, max_retries)
    return False


def close_db(checkpoint: bool = True) -> bool:
    """Корректное закрытие базы данных с соблюдением SQLite WAL Checkpoint Law (RULE 12):
    1. Автоматический PRAGMA wal_checkpoint(TRUNCATE) для сброса WAL-журнала в основной файл базы.
    2. Освобождение кэша страниц PRAGMA shrink_memory.
    3. Остановка рабочего пула транзакций _DB_EXECUTOR.
    """
    success = True
    if checkpoint:
        try:
            success = checkpoint_wal(mode="TRUNCATE", max_retries=5)
        except Exception as exc:
            logger.warning("close_db: checkpoint_wal failed: %s", exc)
            success = False
    try:
        shrink_memory_sync()
    except Exception:
        pass
    try:
        db_worker.shutdown_db_worker(wait=True)
    except Exception:
        pass
    return success


async def close_db_async(checkpoint: bool = True) -> bool:
    """Асинхронное закрытие базы данных с выполнением wal_checkpoint(TRUNCATE)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: close_db(checkpoint=checkpoint))


def _atexit_wal_checkpoint():
    """Автоматический сброс WAL при завершении процесса."""
    try:
        checkpoint_wal(mode="TRUNCATE", max_retries=3, retry_delay=0.1)
    except Exception:
        pass


atexit.register(_atexit_wal_checkpoint)


def verify_database_invariants() -> Dict[str, Any]:
    """Проверяет соблюдение ключевых инвариантов надежности базы данных (RULE 12 и Micro-VPS):
    1. journal_mode == 'wal'
    2. synchronous == 1 (NORMAL)
    3. busy_timeout >= 30000 (30 сек)
    4. cache_size == -4000 (4 МБ)
    5. целостность PRAGMA quick_check
    """
    with _connection() as db:
        j_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        sync_mode = db.execute("PRAGMA synchronous").fetchone()[0]
        b_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
        c_size = db.execute("PRAGMA cache_size").fetchone()[0]
        integrity = db.execute("PRAGMA quick_check").fetchone()[0]
        return {
            "journal_mode": str(j_mode).lower(),
            "synchronous": sync_mode,
            "busy_timeout": b_timeout,
            "cache_size": c_size,
            "quick_check": integrity,
            "is_wal": str(j_mode).lower() == "wal",
            "is_normal_sync": sync_mode in (1, "1", "NORMAL"),
            "is_safe": str(j_mode).lower() == "wal" and integrity == "ok"
        }


async def verify_database_invariants_async() -> Dict[str, Any]:
    """Асинхронная проверка инвариантов надежности базы данных."""
    return await _run_db(verify_database_invariants)
    logger.error("WAL checkpoint (%s) не завершился после %d попыток из-за блокировок", mode_upper, max_retries)
    return False


async def checkpoint_wal_async(mode: str = "TRUNCATE", max_retries: int = 5, retry_delay: float = 0.25) -> bool:
    """Асинхронный сброс WAL через рабочий поток базы данных с контролем статуса busy."""
    return await _run_db(lambda: checkpoint_wal(mode=mode, max_retries=max_retries, retry_delay=retry_delay))

def shrink_memory_sync() -> bool:
    """Выполняет PRAGMA shrink_memory для освобождения неиспользуемых страниц кэша SQLite в ОС."""
    try:
        with _connection() as db:
            db.execute("PRAGMA shrink_memory;")
        return True
    except Exception as exc:
        logger.debug("PRAGMA shrink_memory failed: %s", exc)
        return False

@contextmanager
def _connection():
    db = _connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def _date_text(dt):
    """
    Приводит любой datetime к строке в UTC — именно UTC лежит в колонке `date`.
    Если уже передана строка, возвращает её как есть.
    """
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


async def _run_db(operation):
    return await db_worker.run_in_db_worker(operation)


async def query_db_async(query_sql, params=()):
    """Выполняет произвольный SQL-запрос асинхронно через рабочий пул БД."""
    def operation():
        with _connection() as db:
            c = db.cursor()
            c.execute(query_sql, params)
            return c.fetchall()
    return await _run_db(operation)


def _bot_sent_messages_is_legacy(db):
    """
    True, если учёт исходящих ещё ключуется ОДНИМ msg_id.

    Смотрим фактические уникальные индексы, а не текст CREATE TABLE: боевая база
    создавалась кодом, где msg_id объявлен UNIQUE на уровне колонки, а такой
    индекс (sqlite_autoindex_*) нельзя ни удалить, ни переопределить — таблицу
    приходится пересобирать. Проверка по индексам ещё и идемпотентна: после
    пересборки уникальный индекс покрывает пару (msg_id, chat_id), и миграция
    больше не срабатывает.
    """
    for row in db.execute("PRAGMA index_list('bot_sent_messages')"):
        index_name, is_unique = row[1], row[2]
        if not is_unique:
            continue
        columns = [info[2] for info in db.execute(f"PRAGMA index_info('{index_name}')")]
        if columns == ["msg_id"]:
            return True
    return False


def _rebuild_bot_sent_messages(db):
    """
    Переключает учёт исходящих на ключ (msg_id, chat_id) с переносом данных.

    id переносится как есть, потому что /wipe берёт последние сообщения бота
    через ORDER BY id DESC: пересборка со сквозной перенумерацией перемешала бы
    порядок отправки и удаляла бы не то, что админ ожидает увидеть удалённым.
    Конфликтов при переносе быть не может — старый ключ строже нового.
    DROP ... IF EXISTS на промежуточной таблице оставлен ради самолечения:
    если процесс упал между CREATE и RENAME, следующий старт начинает заново.
    """
    db.execute("DROP TABLE IF EXISTS bot_sent_messages_rebuild")
    db.execute(
        """
        CREATE TABLE bot_sent_messages_rebuild (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_id INTEGER,
            chat_id INTEGER,
            UNIQUE(msg_id, chat_id)
        )
        """
    )
    db.execute(
        "INSERT INTO bot_sent_messages_rebuild (id, msg_id, chat_id) "
        "SELECT id, msg_id, chat_id FROM bot_sent_messages"
    )
    db.execute("DROP TABLE bot_sent_messages")
    db.execute("ALTER TABLE bot_sent_messages_rebuild RENAME TO bot_sent_messages")


async def init_db():
    def operation():
        with _connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id INTEGER UNIQUE,
                    reply_to_msg_id INTEGER,
                    sender_id INTEGER,
                    sender_name TEXT,
                    sender_username TEXT,
                    text TEXT,
                    date TIMESTAMP,
                    has_media BOOLEAN,
                    media_type TEXT,
                    media_description TEXT,
                    media_remote_url TEXT,
                    is_summarized BOOLEAN DEFAULT 0
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_date ON messages(date)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sender ON messages(sender_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender_msg ON messages(sender_id, msg_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_messages_date_msg ON messages(date, msg_id)")
            # Ответы в чате читаются по родителю, а не по себе: на КАЖДОЕ
            # сообщение-ответ под медиа-постом assistant.py делает
            # COUNT(*) WHERE reply_to_msg_id = ?, а затем выборку всей ветки
            # WHERE msg_id = ? OR reply_to_msg_id = ?. Замер EXPLAIN на копии
            # боевой базы (32 883 строки, 20 499 из них ответы):
            #   до  — SCAN messages / SCAN messages USING INDEX idx_date,
            #         5.7 мс на COUNT и 6.9 мс на ветку;
            #   после — SEARCH ... USING COVERING INDEX idx_reply_to и
            #         MULTI-INDEX OR, 0.006 мс и 0.063 мс (в 950 и 109 раз).
            # OR-план по двум индексам вообще недостижим, пока проиндексирована
            # только одна из двух колонок, поэтому одна вторая половина условия
            # обесценивала уникальный индекс на msg_id.
            db.execute("CREATE INDEX IF NOT EXISTS idx_reply_to ON messages(reply_to_msg_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_is_summarized ON messages(is_summarized, date)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_pending_media ON messages(msg_id) WHERE has_media = 1 AND (media_description IS NULL OR media_description = '')")

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS clinical_bookmarks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    saved_by_user_id INTEGER,
                    msg_id INTEGER,
                    chat_id INTEGER,
                    sender_name TEXT,
                    text TEXT,
                    has_media BOOLEAN,
                    media_description TEXT,
                    date TEXT,
                    tags TEXT DEFAULT ''
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_bookmark_user ON clinical_bookmarks(saved_by_user_id)")
            # Закладки сохраняются через INSERT OR IGNORE, но подавлять было
            # нечего: UNIQUE-констрейнта в схеме нет, и повторный /save на тот
            # же пост давал дубль в списке. Сначала схлопываем уже накопленные
            # дубли (оставляя самую раннюю запись), затем ставим индекс —
            # иначе CREATE UNIQUE INDEX упадёт на существующих данных.
            #
            # В ключ входит и chat_id: id сообщений уникальны только внутри
            # чата, поэтому пост #4821 основного чата и #4821 тестового — два
            # разных поста, а ключ (saved_by_user_id, msg_id) объявлял их одной
            # закладкой. Последствия были обоюдные: второй /save молча не
            # сохранялся (INSERT OR IGNORE), а дедупликация ниже удаляла уже
            # сохранённое. То же касается закладок на статьи энциклопедии — у
            # них синтетический отрицательный msg_id и chat_id личного чата.
            # IFNULL нужен, потому что в NULL-значениях SQLite считает строки
            # различными: старые закладки без chat_id перестали бы
            # дедуплицироваться вообще.
            # DROP перед CREATE обязателен: индекс с этим именем в боевой базе
            # уже стоит, а CREATE ... IF NOT EXISTS сверяет только имя и молча
            # оставил бы прежнее двухколоночное определение.
            try:
                idx_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_bookmark_unique'").fetchone()
                if not idx_exists:
                    db.execute("DROP INDEX IF EXISTS idx_bookmark_unique")
                    db.execute(
                        """
                        DELETE FROM clinical_bookmarks
                        WHERE id NOT IN (
                            SELECT MIN(id) FROM clinical_bookmarks
                            GROUP BY saved_by_user_id, msg_id, IFNULL(chat_id, 0)
                        )
                        """
                    )
                    db.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookmark_unique "
                        "ON clinical_bookmarks(saved_by_user_id, msg_id, IFNULL(chat_id, 0))"
                    )
            except Exception as e:
                logger.warning(f"Could not enforce bookmark uniqueness: {e}")

            # Автомиграция тегов для clinical_bookmarks
            try:
                bm_cols = [info[1] for info in db.execute("PRAGMA table_info(clinical_bookmarks)").fetchall()]
                if "tags" not in bm_cols:
                    db.execute("ALTER TABLE clinical_bookmarks ADD COLUMN tags TEXT DEFAULT ''")
                    logger.info("database schema migrated: added tags to clinical_bookmarks")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    logger.warning("clinical_bookmarks migration tags error: %s", exc)
            db.execute("CREATE INDEX IF NOT EXISTS idx_bookmark_tags ON clinical_bookmarks(saved_by_user_id, tags)")

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_interactive_states (
                    user_id INTEGER PRIMARY KEY,
                    state_type TEXT,
                    current_step INTEGER,
                    case_id TEXT,
                    history TEXT
                )
                """
            )

            # Ключ — ПАРА (msg_id, chat_id). Бот работает минимум в двух чатах
            # (основной и тестовый) плюс личные переписки, а id сообщений
            # уникальны лишь внутри чата. При UNIQUE на одном msg_id
            # save_bot_sent_message (INSERT OR REPLACE) на совпадении номера
            # молча УДАЛЯЛ учётную строку другого чата: /wipe там переставал
            # видеть собственное сообщение и не мог его убрать — навсегда, потому
            # что заново оно уже не регистрируется.
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_sent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id INTEGER,
                    chat_id INTEGER,
                    UNIQUE(msg_id, chat_id)
                )
                """
            )
            # Боевая таблица создана старым определением, где UNIQUE висит на
            # колонке msg_id; CREATE TABLE IF NOT EXISTS её не меняет, поэтому
            # ключ переносится пересборкой. Ошибку глушим намеренно: учёт
            # исходящих нужен только /wipe, и неудачная миграция не должна
            # ронять старт бота целиком.
            try:
                if _bot_sent_messages_is_legacy(db):
                    _rebuild_bot_sent_messages(db)
                    logger.info("database schema migrated: bot_sent_messages keyed by (msg_id, chat_id)")
            except Exception as e:
                logger.warning(f"Could not migrate bot_sent_messages key: {e}")
            # /wipe выбирает WHERE chat_id = ? ORDER BY id DESC LIMIT ?. Замер на
            # синтетической таблице боевой формы (20 000 строк, 300 личных чатов):
            # до — SCAN bot_sent_messages, 0.104 мс; после — SEARCH ... USING
            # INDEX idx_bot_sent_chat, 0.031 мс. Уникальный индекс пары здесь не
            # помогает: chat_id в нём второй колонкой.
            db.execute("CREATE INDEX IF NOT EXISTS idx_bot_sent_chat ON bot_sent_messages(chat_id, id)")

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    sender_name TEXT,
                    text TEXT,
                    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # get_last_pm_messages делает WHERE user_id = ? ORDER BY id DESC —
            # без индекса это полный скан на каждое личное сообщение и на
            # каждого пользователя в почасовом цикле пингов. Таблица не чистится
            # и растёт бессрочно.
            db.execute("CREATE INDEX IF NOT EXISTS idx_pm_user ON pm_messages(user_id, id)")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    parse_mode TEXT DEFAULT 'html',
                    status TEXT DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0,
                    next_retry_at TIMESTAMP,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    sent_at TIMESTAMP
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_bot_outbox_status ON bot_outbox(status, id)")
            db.execute("DROP INDEX IF EXISTS idx_pm_date")
            db.execute("CREATE INDEX IF NOT EXISTS idx_pm_date_user ON pm_messages(date, user_id)")

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id INTEGER PRIMARY KEY,
                    selected_style TEXT DEFAULT 'colleague_friendly',
                    profile_portrait TEXT,
                    last_analyzed_msg_id INTEGER DEFAULT 0
                )
                """
            )

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_memories (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    first_name TEXT DEFAULT '',
                    specialty TEXT DEFAULT '',
                    clinical_summary TEXT DEFAULT '',
                    group_summary TEXT DEFAULT '',
                    facts_json TEXT DEFAULT '[]',
                    message_count INTEGER DEFAULT 0,
                    pm_message_count INTEGER DEFAULT 0,
                    group_message_count INTEGER DEFAULT 0,
                    last_pm_analyzed_id INTEGER DEFAULT 0,
                    last_group_analyzed_id INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_user_memories_updated ON user_memories(last_updated)")

            # Автомиграция колонок для существующих баз данных
            for col_def in (
                "group_summary TEXT DEFAULT ''",
                "pm_message_count INTEGER DEFAULT 0",
                "group_message_count INTEGER DEFAULT 0",
                "last_pm_analyzed_id INTEGER DEFAULT 0",
                "last_group_analyzed_id INTEGER DEFAULT 0",
            ):
                try:
                    db.execute(f"ALTER TABLE user_memories ADD COLUMN {col_def}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        logger.warning("user_memories migration %s НЕ применена: %s: %s",
                                       col_def, type(exc).__name__, exc)

            try:
                db.execute("ALTER TABLE messages ADD COLUMN media_remote_url TEXT")
                logger.info("database schema migrated: added media_remote_url")
            except sqlite3.OperationalError as exc:
                # «duplicate column name» — штатный повторный запуск, молчим.
                # Всё остальное молчать не имеет права: этим же исключением
                # приходит «database is locked» и «no such table», то есть
                # МИГРАЦИЯ НЕ ПРИМЕНИЛАСЬ, и следующая запись в media_remote_url
                # упадёт уже в другом месте, где причину не связать с миграцией.
                if "duplicate column" not in str(exc).lower():
                    logger.warning("database migration media_remote_url НЕ применена: "
                                   "%s: %s", type(exc).__name__, exc)

            # Краткая подпись (1-2 фразы chairside) для figcaption в дайджестах и Telegraph.
            # Длинное media_description остаётся для LLM-контекста; short_caption идёт в UI.
            try:
                db.execute("ALTER TABLE messages ADD COLUMN media_short_caption TEXT")
                logger.info("database schema migrated: added media_short_caption")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    logger.warning("database migration media_short_caption НЕ применена: "
                                   "%s: %s", type(exc).__name__, exc)

                        # Автомиграция колонок для bot_outbox
            for col_def in (
                "retry_count INTEGER DEFAULT 0",
                "next_retry_at TIMESTAMP",
            ):
                try:
                    db.execute(f"ALTER TABLE bot_outbox ADD COLUMN {col_def}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        logger.warning("bot_outbox migration %s НЕ применена: %s: %s",
                                       col_def, type(exc).__name__, exc)

            try:
                db.execute("CREATE INDEX IF NOT EXISTS idx_bot_outbox_pending ON bot_outbox(status, next_retry_at, id)")
            except sqlite3.OperationalError as exc:
                logger.warning("idx_bot_outbox_pending creation error: %s", exc)

            # Таблица автоматически извлеченных и проверенных клинических протоколов сообщества
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS clinical_protocols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT DEFAULT 'Общая стоматология',
                    indication TEXT DEFAULT '',
                    contraindications TEXT DEFAULT '',
                    steps_json TEXT DEFAULT '[]',
                    materials TEXT DEFAULT '',
                    key_nuances TEXT DEFAULT '',
                    author_doctor TEXT DEFAULT '',
                    source_msg_id INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_protocol_category ON clinical_protocols(category)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_protocol_title ON clinical_protocols(title)")



            # Полнотекстовый поиск FTS5 для клинических протоколов

            try:

                db.execute("""

                CREATE VIRTUAL TABLE IF NOT EXISTS clinical_protocols_fts USING fts5(

                    title, category, indication, contraindications, steps_json, materials, key_nuances, author_doctor,

                    content='clinical_protocols',

                    content_rowid='id'

                )

                """)

                db.execute("""

                CREATE TRIGGER IF NOT EXISTS protocols_ai AFTER INSERT ON clinical_protocols BEGIN

                    INSERT INTO clinical_protocols_fts(rowid, title, category, indication, contraindications, steps_json, materials, key_nuances, author_doctor)

                    VALUES (new.id, new.title, new.category, new.indication, new.contraindications, new.steps_json, new.materials, new.key_nuances, new.author_doctor);

                END;

                """)

                db.execute("""

                CREATE TRIGGER IF NOT EXISTS protocols_ad AFTER DELETE ON clinical_protocols BEGIN

                    INSERT INTO clinical_protocols_fts(clinical_protocols_fts, rowid, title, category, indication, contraindications, steps_json, materials, key_nuances, author_doctor)

                    VALUES('delete', old.id, old.title, old.category, old.indication, old.contraindications, old.steps_json, old.materials, old.key_nuances, old.author_doctor);

                END;

                """)

                db.execute("""

                CREATE TRIGGER IF NOT EXISTS protocols_au AFTER UPDATE ON clinical_protocols BEGIN

                    INSERT INTO clinical_protocols_fts(clinical_protocols_fts, rowid, title, category, indication, contraindications, steps_json, materials, key_nuances, author_doctor)

                    VALUES('delete', old.id, old.title, old.category, old.indication, old.contraindications, old.steps_json, old.materials, old.key_nuances, old.author_doctor);

                    INSERT INTO clinical_protocols_fts(rowid, title, category, indication, contraindications, steps_json, materials, key_nuances, author_doctor)

                    VALUES (new.id, new.title, new.category, new.indication, new.contraindications, new.steps_json, new.materials, new.key_nuances, new.author_doctor);

                END;

                """)

            except sqlite3.OperationalError as exc:

                logger.debug("clinical_protocols_fts setup info: %s", exc)

    await _run_db(operation)
    await purge_broken_media_remote_urls()
    try:
        import protocol_extractor
        await protocol_extractor.seed_default_protocols_async()
    except Exception as seed_err:
        logger.debug("Automatic protocol seeding on init: %s", seed_err)
    # Проверка наличия и сидирование протокола VDO
    try:
        def seed_vdo_if_missing():
            with _connection() as db:
                vdo_row = db.execute(
                    "SELECT 1 FROM clinical_protocols WHERE LOWER(title) LIKE '%vdo%' OR LOWER(title) LIKE '%высота прикуса%' LIMIT 1"
                ).fetchone()
                if not vdo_row:
                    vdo_steps = json.dumps([
                        {"step": 1, "name": "Миорелаксация и депрограммирование мышц", "description": "Использование депрограмматора Койса (Kois deprogrammer), Lucia Jig или аппарата TENS в течение 20–30 минут для стирания энграмм мышечной памяти и релаксации латеральных крыловидных мышц."},
                        {"step": 2, "name": "Регистрация центрального соотношения (CR)", "description": "Ведение нижней челюсти в терминальную шарнирную ось (Central Relation) без насильственного давления на подбородок. Фиксация положения жестким окклюзионным регистратором (LuxaBite / Futar D) при разобщении резцов на 1.5–2 мм в области депрограмматора."},
                        {"step": 3, "name": "Определение физиологической VDO", "description": "Оценка высоты покоя (Rest Position): физиологический покой минус 2–3 мм свободного межокклюзионного пространства (Freeway space). Фонетические пробы: произнесение звуков «С», «З» («шестьдесят шесть»), зазор между резцами 1.0–1.5 мм (Closest Speaking Space по Сильверману)."},
                        {"step": 4, "name": "Тестирование на временных реставрациях (Mock-up / Overlay)", "description": "Перенос запланированной VDO в полость рта прямым композитным mock-up или безакриловыми накладками (Overlay) на жевательные зубы на срок 6–12 недель для функциональной адаптации жевательных мышц и суставных головок ВНЧС."},
                        {"step": 5, "name": "Окончательное протезирование", "description": "Постоянные керамические реставрации (виниры, оверлеи e.max, коронки) в найденном и адаптированном положении центрального соотношения с клыковым и резцовым ведением без балансирующих суперконтактов."}
                    ], ensure_ascii=False)
                    db.execute(
                        """
                        INSERT INTO clinical_protocols (
                            title, category, indication, contraindications, steps_json,
                            materials, key_nuances, author_doctor, source_msg_id, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (
                            "Определение и восстановление высоты прикуса (VDO / депрограмматор Койса / Lucia Jig)",
                            "Ортопедия",
                            "Патологическая стираемость зубов (генерализованная форма), потеря межальвеолярной высоты (VDO), мышечно-суставная дисфункция ВНЧС, тотальная эстетическая и функциональная реабилитация.",
                            "Острый артрит ВНЧС в фазе выраженного синовита без предварительной противовоспалительной терапии.",
                            vdo_steps,
                            "Депрограмматор Койса / Lucia Jig, регистратор прикуса LuxaBite / Futar D, лицевая дуга, артикулятор (Artex / SAM), бис-акрил (Protemp 4 / Structur 3), фольга Бауша 8–12 мкм.",
                            "Никогда не увеличивать VDO более чем на 3–4 мм одномоментно без предварительного шинотерапевтического или mock-up тестирования. Центральное соотношение должно быть воспроизводимым и безболезненным.",
                            "John Kois / Эксперты сообщества Docendo Discimus"
                        )
                    )
                    logger.info("Default VDO clinical protocol seeded successfully.")

                # Дополнительное сидирование протоколов по реальным врачебным запросам
                curated_protocols = [
                    (
                        "Купирование гиперестезии дентина и боли от сладкого (Десенситизация и глубокое фторирование)",
                        "Терапия",
                        "Реакция зубов на сладкое, кислое, температурные и механические раздражители при оголении шеек зубов, клиновидных дефектах, эрозиях эмали и после препарирования.",
                        "Необратимый пульпит (спонтанные ночные боли), некроз пульпы.",
                        json.dumps([
                            {"step": 1, "name": "Изоляция и антисептическая обработка", "description": "Изоляция операционного поля ватными валиками или коффердамом, бережное высушивание теплым воздухом."},
                            {"step": 2, "name": "Механическая очистка поверхности", "description": "Очистка шеек зубов безабразивной пастой без фтора (Cleanic / Nupro) резиновой чашечкой на малых оборотах."},
                            {"step": 3, "name": "Аппликация десенситайзера (Gluma / Shield Force Plus)", "description": "Нанесение десенситайзера аппликатором на 30–60 секунд для коагуляции белков в дентинных канальцах и блокирования гидродинамического тока жидкости."},
                            {"step": 4, "name": "Глубокое фторирование эмали и дентина", "description": "Поочередное нанесение компонентов эмаль-герметизирующего ликвида (Tiefenfluorid) с образованием микрокристаллов CaF2 в глубине канальцев."},
                            {"step": 5, "name": "Назначение домашней реминерализующей терапии", "description": "Зубная паста с фторидом 1450–5000 ppm (Duraphat / Sensodyne Pronamel) и реминерализующий гель (Tooth Mousse / GC MI Paste)."}
                        ], ensure_ascii=False),
                        "Десенситайзер Gluma Desensitizer / Tokuyama Shield Force Plus, эмаль-герметизирующий ликвид Tiefenfluorid, лак Duraphat 50 мг/мл (22600 ppm F-), Tooth Mousse.",
                        "Гидродинамическая теория Бреннстрома: боль от сладкого вызвана осмотическим движением жидкости в дентинных трубочках. Устранение проницаемости канальцев снимает боль мгновенно.",
                        "Martin Brännström / Клинические эксперты Docendo Discimus"
                    ),
                    (
                        "Протокол фторпрофилактики и реминерализации эмали (Пасты с фтором 1450–5000 ppm / Duraphat)",
                        "Терапия",
                        "Профилактика кариеса, лечение начального кариеса в стадии белого пятна, гиперестезия эмали, ортодонтическое лечение, высокий кариесогенный риск.",
                        "Эндемический флюороз тяжелой степени (III-IV степень по Дин).",
                        json.dumps([
                            {"step": 1, "name": "Оценка кариесогенного риска (CAMBRA)", "description": "Определение уровня гигиены (индекс OHI-S), наличия очагов деминерализации и диеты пациента."},
                            {"step": 2, "name": "Профессиональная гигиена полости рта", "description": "Снятие минерализованных и мягких зубных отложений ультразвуком и Air-Flow с порошком на основе глицина/эритритола."},
                            {"step": 3, "name": "Профессиональная аппликация фторлака", "description": "Нанесение лака Duraphat (22 600 ppm F-) тонким слоем на высушенные поверхности зубов. Не принимать пищу 2 часа, не чистить зубы жесткой щеткой в день процедуры."},
                            {"step": 4, "name": "Подбор домашней пасты с фтором", "description": "Взрослым: паста с концентрацией фторида 1450 ppm (аминофторид или фторид натрия) дважды в день. При высоком риске: высокодозная паста 5000 ppm (Colgate Duraphat 5000) курсом 3–6 месяцев."},
                            {"step": 5, "name": "Техника «Spit, don't rinse»", "description": "После чистки сплюнуть излишки пасты, но НЕ полоскать рот водой, сохраняя терапевтическую концентрацию фторид-ионов в слюне."}
                        ], ensure_ascii=False),
                        "Фторсодержащие зубные пасты 1450 ppm (Elmex Защита от кариеса, Curaprox Enzycal), Colgate Duraphat 5000 ppm, фторидный лак Duraphat 2.26%, Clinpro Tooth Creme.",
                        "Фторид замещает гидроксильную группу в гидроксиапатите с образованием фторапатита, устойчивого к деминерализации при pH до 4.5 (критический pH гидроксиапатита 5.5).",
                        "EAPD (European Academy of Paediatric Dentistry) / Эксперты Docendo Discimus"
                    ),
                    (
                        "Фонетические пробы и коррекция окклюзии при шепелявости и присвистывании звука «С» (Silverman Closest Speaking Space)",
                        "Гнатология",
                        "Нарушения артикуляции и речи после фиксации коронок, виниров или протезов: шепелявость, присвистывание звуков «С», «З», стук зубов при разговоре, перегрузка ВНЧС.",
                        "Острые воспалительные процессы в полости рта до заживления слизистой.",
                        json.dumps([
                            {"step": 1, "name": "Посадка и фонетическое тестирование", "description": "Пациент сидит строго вертикально без опоры на подголовник. Произнесение счета «шестьдесят шесть», слов «сисси», «миссисипи»."},
                            {"step": 2, "name": "Оценка ближайшего разговорного расстояния по Сильверману", "description": "При звуке «С» резцы не должны контактировать! Норма Closest Speaking Space = 1.0–1.5 мм. Если зубы стучат — VDO завышена."},
                            {"step": 3, "name": "Диагностика шепелявости (Lisping)", "description": "Если пациент шепелявит («шепелявость»): избыточная толщина небной поверхности верхних резцов или базиса протеза в зоне rugae palatinae стесняет кончик языка."},
                            {"step": 4, "name": "Коррекция при шепелявости", "description": "Сошлифовывание небной поверхности реставраций с освобождением резонансного пространства для кончика языка."},
                            {"step": 5, "name": "Коррекция при свисте (Whistling)", "description": "Если звук «С» свистит: воздушный зазор между резцами слишком узок или режущий край нижних резцов расположен чересчур вестибулярно. Пришлифовывание вестибулярного скоса нижних резцов под контролем фольги 8 мкм."}
                        ], ensure_ascii=False),
                        "Артикуляционная фольга Shimstock 8 мкм (Almore / Bausch), бумага 40 мкм, микрометр, алмазные финиры с белой/желтой полосой, керамические полиры.",
                        "Фонетический тест Сильвермана (1953) — самый чувствительный маркер завышения высоты прикуса. Шепелявость — признак дефицита пространства для языка; присвистывание — избытка зазора.",
                        "Meyer M. Silverman / Клинические эксперты Docendo Discimus"
                    ),
                    (
                        "Диагностика и ультразвуковое удаление дентиклей (пульполитов / шаровидных кальцификатов) пульповой камеры",
                        "Эндодонтия",
                        "Кальцификат пульпы (свободный или пристеночный дентикль / «шарик в центре коронки»), блокирующий доступ к устьям корневых каналов при лечении пульпита или периодонтита.",
                        "Глубокая резорбция дна пульповой камеры с риском перфорации фуркации.",
                        json.dumps([
                            {"step": 1, "name": "Визуализация и оценка рентгенограммы", "description": "Прицельный снимок: круглое плотное рентгеноконтрастное образование («шарик в центре коронки») в проекции пульповой камеры. Микроскоп 10–16х."},
                            {"step": 2, "name": "Изоляция и создание прямого доступа", "description": "Коффердам. Формирование эндодонтического доступа с сохранением перицервикального дентина (PCD)."},
                            {"step": 3, "name": "Ультразвуковая мобилизация контура", "description": "Специальная насадка (Start-X #2 или ProUltra #2) БЕЗ водяного охлаждения на мощности 2–3 обходит дентикль по периферии, разрушая точки прикрепления к стенкам камеры."},
                            {"step": 4, "name": "Эвакуация дентикля", "description": "После появления подвижности дентикль аккуратно вымывается струей гипохлорита натрия или извлекается микропинцетом/эндозондом DG16."},
                            {"step": 5, "name": "Финишная ревизия дна пульпарной камеры", "description": "Удаление остаточных кальцинатов 17% раствором ЭДТА с ультразвуковой активацией до четкой визуализации всех устьев каналов."}
                        ], ensure_ascii=False),
                        "Операционный микроскоп, ультразвуковой пьезоскейлер, насадки Start-X #2 / Start-X #3, эндодонтический зонд DG16, 17% раствор ЭДТА, 3% NaOCl.",
                        "Категорически запрещено высверливать дентикль турбинным бором — высокий риск ятрогенной перфорации дна полости зуба (strip/bifurcation perforation). Только ультразвук под микроскопом!",
                        "Yoshi Terauchi / Эксперты сообщества Docendo Discimus"
                    ),
                    (
                        "Протокол оценки и создания феррул-эффекта (Ferrule Effect 1.5–2.0 мм) при восстановлении зубов под коронки",
                        "Ортопедия",
                        "Разрушение коронковой части зуба >50%, планирование восстановления культевой вкладкой или стекловолоконным штифтом с последующим покрытием коронкой.",
                        "Поддесневое разрушение корня глубже 3 мм от гребня кости без возможности экструзии или удлинения.",
                        json.dumps([
                            {"step": 1, "name": "Удаление несостоятельных тканей и оценка феррула", "description": "Полная некрэктомия. Оценка высоты оставшегося дентина над уровнем десны пародонтологическим зондом UNC-15."},
                            {"step": 2, "name": "Критерии надежного феррул-эффекта", "description": "Золотой стандарт: высота здоровых стенок дентина ≥ 1.5–2.0 мм циркулярно (минимум на 75% периметра зуба), толщина стенок ≥ 1.0 мм."},
                            {"step": 3, "name": "Тактика при дефиците феррула", "description": "Если феррул < 1.5 мм: глубокая элевация края (DME) композитом при интактной биологической ширине ИЛИ хирургическое удлинение коронки (остеоэктомия на 3 мм от будущего края реставрации) ИЛИ ортодонтическая экструзия корня."},
                            {"step": 4, "name": "Восстановление культи (Build-up)", "description": "Адгезивный билд-ап композитом двойного отверждения (LuxaCore) со стекловолоконным штифтом или литая культевая вкладка."},
                            {"step": 5, "name": "Препарирование под коронку с охватом феррула", "description": "Край будущей коронки ОБЯЗАН перекрывать границу «культя-корень» минимум на 1.5–2.0 мм здорового дентина (циркулярный эффект обруча бочки — ferrule collar)."}
                        ], ensure_ascii=False),
                        "Пародонтологический зонд UNC-15, бинокуляры, композит LuxaCore Z / Core-It Dual, стекловолоконные штифты (RelyX Fiber Post), алмазные боры с закругленным концом.",
                        "Феррул-эффект обеспечивает устойчивость зуба к изгибающим нагрузкам. Штифт лишь удерживает культю, но НЕ укрепляет корень; только круговой феррул 2 мм предотвращает перелом корня!",
                        "Urs Belser / Pascal Magne / Эксперты сообщества Docendo Discimus"
                    )
                ]
                for p_title, p_cat, p_ind, p_contra, p_steps, p_mat, p_nuances, p_doc in curated_protocols:
                    existing = db.execute("SELECT 1 FROM clinical_protocols WHERE LOWER(title) = LOWER(?) LIMIT 1", (p_title,)).fetchone()
                    if not existing:
                        db.execute(
                            """
                            INSERT INTO clinical_protocols (
                                title, category, indication, contraindications, steps_json,
                                materials, key_nuances, author_doctor, source_msg_id, created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            """,
                            (p_title, p_cat, p_ind, p_contra, p_steps, p_mat, p_nuances, p_doc)
                        )
                try:
                    fts_c = db.execute("SELECT count(*) FROM clinical_protocols_fts").fetchone()
                    proto_c = db.execute("SELECT count(*) FROM clinical_protocols").fetchone()
                    if proto_c and proto_c[0] > 0 and (not fts_c or fts_c[0] == 0):
                        db.execute("INSERT INTO clinical_protocols_fts(clinical_protocols_fts) VALUES('rebuild')")
                except Exception:
                    pass
        await _run_db(seed_vdo_if_missing)
    except Exception as vdo_err:
        logger.debug("VDO protocol seeding: %s", vdo_err)


async def get_messages_for_daily_summary(start_time, end_time, min_count=100, allow_backfill=True):
    def operation():
        with _connection() as db:
            period_messages = db.execute(
                """
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url, sender_id, media_short_caption
                FROM messages
                WHERE date >= ? AND date <= ?
                  AND (has_media = 0 OR (media_description IS NOT NULL AND media_description != '') OR date < datetime('now', '-5 minutes'))
                ORDER BY date ASC
                """,
                (_date_text(start_time), _date_text(end_time)),
            ).fetchall()

            total_msgs = list(period_messages)
            if allow_backfill and len(total_msgs) < min_count:
                # Добор из прошлого — только то, что ещё НЕ уходило в сводку.
                # Без этого условия в тихий день дайджест пересказывал
                # сообществу вчерашний.
                #
                # Замер на локальном снимке базы (144 дня, снимок может
                # отставать от боевого): добор срабатывает на 15 днях и
                # поднимает 495 сообщений, из которых 442 (89%) уже
                # публиковались; в худший день повторами были все 66 из 100.
                # Числа — про масштаб; сам дефект от них не зависит, он в том,
                # что запрос игнорировал флаг, заведённый ровно для этого.
                # Флаг для того и заведён, см. mark_messages_as_summarized:
                # «сообщение, уже ушедшее в сводку, не должно всплыть в
                # следующей ещё раз» — здесь это правило и нарушалось.
                #
                # Добор по msg_id в ORDER BY — по той же причине, что в
                # get_last_n_messages: граница LIMIT падает внутрь одной секунды
                # в 5.5% случаев, и без второго ключа непонятно, какую из реплик
                # секунды взяли, а какую оставили следующему разу.
                needed = min_count - len(total_msgs)
                old_messages = db.execute(
                    """
                    SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url, sender_id, media_short_caption
                    FROM messages
                    WHERE date < ? AND is_summarized = 0
                    ORDER BY date DESC, msg_id DESC
                    LIMIT ?
                    """,
                    (_date_text(start_time), needed),
                ).fetchall()
                total_msgs = old_messages[::-1] + total_msgs

            return total_msgs

    return await _run_db(operation)


async def get_messages_for_range(start_dt, end_dt):
    def operation():
        with _connection() as db:
            return db.execute(
                """
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url, sender_id, media_short_caption
                FROM messages
                WHERE date >= ? AND date <= ?
                  AND (has_media = 0 OR (media_description IS NOT NULL AND media_description != '') OR date < datetime('now', '-5 minutes'))
                ORDER BY date ASC
                """,
                (_date_text(start_dt), _date_text(end_dt)),
            ).fetchall()

    return await _run_db(operation)


async def get_messages_from(start_msg_id, limit=60):
    """
    Сообщения начиная с указанного, по возрастанию msg_id.

    Нужно для /итог в ответ на конкретное сообщение: врач указывает, откуда
    начался разбор, и ждёт сводку именно этой ветки, а не случайных последних
    реплик чата.
    """
    def operation():
        with _connection() as db:
            return db.execute(
                """
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url, sender_id, media_short_caption
                FROM messages
                WHERE msg_id >= ?
                ORDER BY msg_id ASC
                LIMIT ?
                """,
                (start_msg_id, limit),
            ).fetchall()

    return await _run_db(operation)


async def get_last_n_messages(limit=300):
    """
    Последние `limit` сообщений в хронологическом порядке.

    Добор по msg_id в ORDER BY — не косметика. Колонка date хранит секунды, и в
    живом чате секунда содержит по несколько реплик: на копии боевой базы
    2 727 сообщений из 32 883 (8.3%) делят секунду с другим, в самой плотной
    группе их 14. Без второго ключа порядок внутри секунды не определён, а
    граница LIMIT попадает внутрь такой группы в 1 779 окнах из 32 583 (5.5%):
    окно брало более позднюю реплику и отбрасывало более раннюю — ту, на
    которую позднее отвечает. После разворота rows[::-1] порядок внутри секунды
    тоже был произвольным, то есть ответ мог оказаться в контексте ВЫШЕ реплики,
    которой отвечает.

    Стоимости у добора нет: SQLite идёт по idx_date в обратную сторону и
    досортировывает только внутри равных дат («USE TEMP B-TREE FOR LAST TERM OF
    ORDER BY»), замер на той же копии 0.99 мс до и 0.90 мс после.
    """
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url, sender_id, media_short_caption
                FROM messages
                ORDER BY date DESC, msg_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return rows[::-1]

    return await _run_db(operation)


async def save_message(
    msg_id,
    sender_id,
    sender_name,
    sender_username,
    text,
    date,
    reply_to_msg_id=None,
    has_media=False,
    media_type=None,
):
    def operation():
        with _connection() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO messages
                (msg_id, reply_to_msg_id, sender_id, sender_name, sender_username, text, date, has_media, media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    reply_to_msg_id,
                    sender_id,
                    sender_name,
                    sender_username,
                    text,
                    _date_text(date),
                    has_media,
                    media_type,
                ),
            )

    # Потеря записи здесь необратима: sync_history догоняет пропущенное по
    # MAX(msg_id), то есть по верхней границе. Если сообщение N не сохранилось,
    # а N+1 сохранилось — граница уехала вперёд, и N не будет найдено уже
    # никогда. Поэтому пробуем повторно, как это делает runtime_guard при
    # записи heartbeat: на Windows типовая причина отказа временная —
    # антивирус или индексатор, держащий файл.
    for attempt in range(SAVE_RETRY_ATTEMPTS):
        try:
            await _run_db(operation)
            if attempt:
                logger.info("save_message recovered on attempt %s msg_id=%s", attempt + 1, msg_id)
            return True
        except Exception:
            if attempt + 1 < SAVE_RETRY_ATTEMPTS:
                logger.warning(
                    "save_message attempt %s failed msg_id=%s, retrying", attempt + 1, msg_id
                )
                await asyncio.sleep(SAVE_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            logger.exception(
                "MESSAGE LOST: save_message failed after %s attempts msg_id=%s. "
                "sync_history recovers by MAX(msg_id), so a later successful save "
                "hides this gap permanently.",
                SAVE_RETRY_ATTEMPTS, msg_id,
            )
            return False


async def get_unsummarized_count():
    def operation():
        with _connection() as db:
            row = db.execute("SELECT COUNT(*) FROM messages WHERE is_summarized = 0").fetchone()
            return row[0] if row else 0

    return await _run_db(operation)


async def get_messages_for_summary():
    def operation():
        with _connection() as db:
            return db.execute(
                """
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id
                FROM messages
                WHERE is_summarized = 0
                ORDER BY date ASC
                """
            ).fetchall()

    return await _run_db(operation)


async def get_last_msg_id():
    def operation():
        with _connection() as db:
            row = db.execute("SELECT MAX(msg_id) FROM messages").fetchone()
            return row[0] if row and row[0] else 0

    return await _run_db(operation)


async def get_text_by_id(msg_id):
    def operation():
        with _connection() as db:
            return db.execute(
                "SELECT sender_name, text FROM messages WHERE msg_id = ?",
                (msg_id,),
            ).fetchone()

    return await _run_db(operation)


async def get_texts_by_ids(msg_ids):
    clean_ids = sorted({int(msg_id) for msg_id in msg_ids if msg_id})
    if not clean_ids:
        return {}

    def operation():
        result = {}
        with _connection() as db:
            chunk_size = 500
            for i in range(0, len(clean_ids), chunk_size):
                batch = clean_ids[i:i + chunk_size]
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT msg_id, sender_name, text FROM messages WHERE msg_id IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    result[row[0]] = (row[1], row[2])
        return result

    return await _run_db(operation)

async def get_reply_chain_texts(msg_id, max_depth=5):
    def operation():
        with _connection() as db:
            chain = []
            curr_id = msg_id
            for _ in range(max_depth):
                row = db.execute(
                    "SELECT reply_to_msg_id, sender_name, text FROM messages WHERE msg_id = ?",
                    (curr_id,)
                ).fetchone()
                if row:
                    parent_id, sender_name, text = row
                    if text:
                        chain.append(f"{sender_name}: {text}")
                    if parent_id:
                        curr_id = parent_id
                    else:
                        break
                else:
                    break
            return list(reversed(chain))
    return await _run_db(operation)


async def mark_messages_as_summarized(msg_ids):
    def operation():
        with _connection() as db:
            db.executemany(
                "UPDATE messages SET is_summarized = 1 WHERE msg_id = ?",
                [(m_id,) for m_id in msg_ids],
            )

    return await _run_db(operation)


async def update_message_text(msg_id, text):
    """
    Догоняет правку сообщения в Telegram.

    Без этого база годами отдаёт первую редакцию: автор исправил дозировку,
    номер зуба или опечатку в протоколе, а дайджест, цитаты в ответах и
    контекст ассистента продолжают тянуть исходный — уже неверный — текст и
    подают его как факт коллеги.

    is_summarized намеренно НЕ сбрасывается: сообщение, уже ушедшее в сводку,
    не должно всплыть в следующей ещё раз.

    Возвращает число обновлённых строк (0 — сообщения в базе нет).
    """
    def operation():
        with _connection() as db:
            cursor = db.execute(
                "UPDATE messages SET text = ? WHERE msg_id = ?",
                (text, msg_id),
            )
            return cursor.rowcount or 0

    try:
        return await _run_db(operation)
    except Exception:
        logger.exception("database update_message_text failed msg_id=%s", msg_id)
        return 0


async def delete_messages_by_ids(msg_ids, chat_id=None):
    """
    Убирает удалённые в Telegram сообщения из локальной базы.

    ВАЖНО: таблица messages не хранит chat_id — в неё пишется только основной
    чат. Вызывать эту функцию можно ТОЛЬКО для SOURCE_CHAT_ID: id сообщений
    уникальны лишь в пределах чата, и удаление #4821 в тестовом чате снесло бы
    чужую строку с тем же номером из основного.

    Заодно чистит bot_sent_messages, иначе /wipe потом ломится удалять уже
    удалённые сообщения. Эта таблица chat_id хранит, поэтому чистка по нему
    и ограничивается.

    clinical_bookmarks оставляем намеренно: это личный архив врача со снимком
    текста на момент сохранения, и молча стирать сохранённое им — хуже, чем
    хранить копию удалённого поста в профессиональном чате коллег.

    Возвращает (удалено_сообщений, удалено_записей_бота).
    """
    clean_ids = sorted({int(msg_id) for msg_id in msg_ids if msg_id})
    if not clean_ids:
        return (0, 0)

    def operation():
        placeholders = ",".join("?" for _ in clean_ids)
        with _connection() as db:
            removed = db.execute(
                f"DELETE FROM messages WHERE msg_id IN ({placeholders})",
                clean_ids,
            ).rowcount or 0
            if chat_id is None:
                bot_removed = db.execute(
                    f"DELETE FROM bot_sent_messages WHERE msg_id IN ({placeholders})",
                    clean_ids,
                ).rowcount or 0
            else:
                bot_removed = db.execute(
                    f"DELETE FROM bot_sent_messages WHERE chat_id = ? AND msg_id IN ({placeholders})",
                    [chat_id] + clean_ids,
                ).rowcount or 0
            return (removed, bot_removed)

    try:
        return await _run_db(operation)
    except Exception:
        logger.exception("database delete_messages_by_ids failed count=%s", len(clean_ids))
        return (0, 0)


async def update_media_description(msg_id, description, short_caption=None):
    def operation():
        with _connection() as db:
            if short_caption is not None:
                db.execute(
                    "UPDATE messages SET media_description = ?, media_short_caption = ? WHERE msg_id = ?",
                    (description, short_caption, msg_id),
                )
            else:
                db.execute(
                    "UPDATE messages SET media_description = ? WHERE msg_id = ?",
                    (description, msg_id),
                )

    return await _run_db(operation)


def validate_media_file_safe(file_path: Optional[str], min_bytes: int = 100) -> bool:
    """
    Безопасная валидация медиа-файла:
    Проверяет существование, доступность на чтение и ненулевой размер.
    Предотвращает падения и сбои при битых/отсутствующих локальных путях.
    """
    if not file_path or not isinstance(file_path, str):
        return False
    clean_p = file_path.strip().strip("'\"")
    if not clean_p:
        return False
    try:
        if not os.path.exists(clean_p):
            return False
        if not os.path.isfile(clean_p):
            return False
        size = os.path.getsize(clean_p)
        return size >= min_bytes
    except (OSError, PermissionError) as err:
        logger.debug("validate_media_file_safe failed for %s: %s", clean_p, err)
        return False


async def sanitize_broken_media_record(msg_id: int) -> bool:
    """
    Очищает битую/несуществующую медиа-ссылку в базе данных для указанного сообщения,
    предотвращая повторные ошибки и сбои декодеров.
    """
    def operation():
        with _connection() as db:
            cur = db.execute(
                "UPDATE messages SET media_remote_url = NULL, has_media = 0 WHERE msg_id = ?",
                (msg_id,)
            )
            return cur.rowcount > 0
    try:
        return await _run_db(operation)
    except Exception as exc:
        logger.warning("sanitize_broken_media_record msg_id=%s failed: %s", msg_id, exc)
        return False


async def safe_get_media_url(msg_id: int) -> Optional[str]:
    """
    Безопасно возвращает media_remote_url сообщения.
    Если указан локальный путь и файл отсутствует/поврежден — автоматически очищает запись и возвращает None.
    """
    def operation():
        with _connection() as db:
            row = db.execute("SELECT media_remote_url FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
            return row[0] if row else None
    url = await _run_db(operation)
    if not url:
        return None
    if not url.startswith("http://") and not url.startswith("https://"):
        if not validate_media_file_safe(url):
            await sanitize_broken_media_record(msg_id)
            return None
    return url


async def update_media_remote_url(msg_id, url):
    def operation():
        with _connection() as db:
            db.execute(
                "UPDATE messages SET media_remote_url = ? WHERE msg_id = ?",
                (url, msg_id),
            )

    return await _run_db(operation)


async def purge_broken_media_remote_urls():
    """
    Зачищает битые / заблокированные ссылки на CDN (iili.io / freeimage.host),
    где клинические снимки были заблокированы как 'adult content' (403/404).
    Возвращает количество очищенных записей.
    """
    def operation():
        with _connection() as db:
            cursor = db.execute(
                """
                UPDATE messages
                SET media_remote_url = NULL
                WHERE media_remote_url LIKE '%iili.io%'
                   OR media_remote_url LIKE '%freeimage%'
                """
            )
            count = cursor.rowcount or 0
            if count > 0:
                logger.info("purge_broken_media_remote_urls: очищено %s битых ссылок iili.io/freeimage", count)
            return count

    return await _run_db(operation)


async def get_pending_media_message_ids(limit=5):
    """
    Снимки без описания — очередь на разбор для recover_pending_media_analysis.

    Окна «за последние 3 дня» здесь больше нет, и это исправление, а не
    ослабление фильтра. enqueue_media_analysis обещает: непоставленное в очередь
    не пропадает, у него пустое media_description, и его подберёт восстановление
    при следующих запусках. С окном обещание было ложным — снимок, переживший
    трёхдневную паузу (очередь на 128 переполнилась при массовом догоне,
    процесс лежал, ключи Vision кончились), выпадал из выборки НАВСЕГДА.
    Замер на копии боевой базы: 745 снимков без описания, самый старый от
    2026-01-29, самый свежий от 2026-06-17 — в окно попадает 0, то есть функция
    отдавала пустой список при 745 неразобранных.

    Порядок msg_id DESC сохранён намеренно: свежий снимок ещё обсуждают, и он
    важнее архивного. Постоянно недоступные строки очередь не закупоривают —
    воркер помечает провал анализа текстом «[медиа — ошибка анализа]», и
    непустое описание выводит такую строку из выборки.

    Плата за снятие окна: 0.060 мс, пока неразобранное есть, и полный проход по
    индексу msg_id, когда очередь пуста, — 33.6 мс на той же копии. Функция
    вызывается один раз за старт процесса, так что это разовые 33 мс.
    """
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT msg_id, text, media_type
                FROM messages
                WHERE has_media = 1
                  AND (media_description IS NULL OR media_description = '')
                ORDER BY msg_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [(row[0], row[1] or "", row[2]) for row in rows]

    return await _run_db(operation)


async def get_messages_for_period(hours):
    def operation():
        with _connection() as db:
            return db.execute(
                """
                SELECT sender_name, text, media_description, date
                FROM messages
                WHERE date >= datetime('now', ?)
                ORDER BY date ASC
                """,
                (f"-{hours} hours",),
            ).fetchall()

    return await _run_db(operation)


async def check_recent_chat_activity(minutes=5):
    """
    Проверяет активность в чате за последние minutes минут (Activity Guard).
    messages.date хранится СТРОГО В UTC, сравниваем строго с UTC datetime('now').
    """
    def operation():
        with _connection() as db:
            row = db.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE date >= datetime('now', ?)
                """,
                (f"-{minutes} minutes",),
            ).fetchone()
            return row[0] if row else 0

    return await _run_db(operation)


async def get_last_messages(chat_id=None, limit=25):
    """
    Алиас для совместимости с assistant.py и модулями интерактива.
    Возвращает список словарей [{'msg_id': ..., 'sender_name': ..., 'text': ...}].
    """
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT msg_id, sender_name, sender_username, text, date, has_media, media_type
                FROM messages
                ORDER BY date DESC, msg_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "msg_id": r[0],
                    "sender_name": r[1] or "",
                    "sender_username": r[2] or "",
                    "text": r[3] or "",
                    "date": r[4],
                }
                for r in reversed(rows)
            ]

    return await _run_db(operation)




async def get_media_description(msg_id):
    def operation():
        with _connection() as db:
            row = db.execute("SELECT media_description FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
            return row[0] if row else None
    return await _run_db(operation)


async def get_media_short_caption(msg_id):
    def operation():
        with _connection() as db:
            row = db.execute("SELECT media_short_caption FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
            return row[0] if row else None
    return await _run_db(operation)



def normalize_bookmark_tags(tags: Union[str, list, tuple, set]) -> str:
    """Приводит список или строку тегов к каноническому формату '#тег1, #тег2'."""
    if not tags:
        return ""
    if isinstance(tags, (list, tuple, set)):
        items = list(tags)
    elif isinstance(tags, str):
        items = [t.strip() for t in re.split(r'[,; ]+', tags) if t.strip()]
    else:
        items = [str(tags)]
    clean_tags = []
    for item in items:
        cleaned = item.strip().lower()
        if not cleaned:
            continue
        if not cleaned.startswith("#"):
            cleaned = "#" + cleaned
        if cleaned not in clean_tags:
            clean_tags.append(cleaned)
    return ", ".join(clean_tags)


def extract_hashtags_from_text(text: str) -> List[str]:
    """Автоматически извлекает хештеги из клинического текста сообщения."""
    if not text:
        return []
    tags = re.findall(r'#([a-zA-Zа-яА-Я0-9_]{2,30})', text)
    return [f"#{t.lower()}" for t in dict.fromkeys(tags)]


async def save_clinical_bookmark(
    saved_by_user_id: int,
    msg_id: int,
    chat_id: int,
    sender_name: str,
    text: str,
    has_media: bool = False,
    media_description: str = "",
    date: Any = None,
    tags: Union[str, list, tuple, set] = "",
):
    """Сохраняет клиническую запись в Избранное / Закладки врача с поддержкой тегов."""
    clean_tags = normalize_bookmark_tags(tags)
    if not clean_tags and text:
        extracted = extract_hashtags_from_text(text)
        if extracted:
            clean_tags = ", ".join(extracted)

    date_val = _date_text(date) if hasattr(date, 'strftime') else str(date if date is not None else time.strftime("%Y-%m-%d %H:%M:%S"))

    def operation():
        with _connection() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO clinical_bookmarks
                (saved_by_user_id, msg_id, chat_id, sender_name, text, has_media, media_description, date, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (saved_by_user_id, msg_id, chat_id, sender_name, text, has_media, media_description, date_val, clean_tags),
            )
    return await _run_db(operation)


async def update_clinical_bookmark_tags(bookmark_id: int, tags: Union[str, list, tuple, set], saved_by_user_id: Optional[int] = None) -> bool:
    """Обновляет теги клинической закладки. При передаче saved_by_user_id проверяет принадлежность врачу (IDOR)."""
    clean_tags = normalize_bookmark_tags(tags)
    def operation():
        with _connection() as db:
            if saved_by_user_id is not None:
                cur = db.execute(
                    "UPDATE clinical_bookmarks SET tags = ? WHERE id = ? AND saved_by_user_id = ?",
                    (clean_tags, bookmark_id, saved_by_user_id)
                )
            else:
                cur = db.execute(
                    "UPDATE clinical_bookmarks SET tags = ? WHERE id = ?",
                    (clean_tags, bookmark_id)
                )
            return cur.rowcount > 0
    return await _run_db(operation)


async def get_clinical_bookmarks(
    saved_by_user_id: int,
    query: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    include_id: bool = False,
    tag: Optional[str] = None,
    include_tags: bool = False,
):
    """Закладки врача, свежие сверху.
    
    Поддерживает фильтрацию по поисковому запросу (query) и тегам (tag).
    Обратная совместимость:
    - include_id=False, include_tags=False -> кортеж из 6 элементов: (msg_id, chat_id, sender_name, text, media_description, date)
    - include_id=True, include_tags=False -> кортеж из 7 элементов: (id, msg_id, chat_id, sender_name, text, media_description, date)
    - include_tags=True -> добавляет поле tags в конец кортежа.
    """
    def operation():
        with _connection() as db:
            window = "LIMIT ? OFFSET ?"
            bounds = (-1 if limit is None else int(limit), int(offset or 0))
            if include_id:
                cols = "id, msg_id, chat_id, sender_name, text, media_description, date" + (", tags" if include_tags else "")
            else:
                cols = "msg_id, chat_id, sender_name, text, media_description, date" + (", tags" if include_tags else "")

            conditions = ["saved_by_user_id = ?"]
            params = [saved_by_user_id]

            if query:
                like_q = f"%{query}%"
                conditions.append("(text LIKE ? OR media_description LIKE ? OR sender_name LIKE ? OR tags LIKE ?)")
                params.extend([like_q, like_q, like_q, like_q])

            if tag:
                clean_tag = tag.strip().lstrip('#').lower()
                like_t = f"%{clean_tag}%"
                conditions.append("(LOWER(tags) LIKE ? OR LOWER(text) LIKE ?)")
                params.extend([like_t, like_t])

            where_clause = " AND ".join(conditions)
            sql = f"""
                SELECT {cols}
                FROM clinical_bookmarks
                WHERE {where_clause}
                ORDER BY date DESC, id DESC
                {window}
            """
            return db.execute(sql, params + list(bounds)).fetchall()
    return await _run_db(operation)


async def get_clinical_bookmark_by_id(bookmark_id: int, saved_by_user_id: Optional[int] = None, include_tags: bool = False):
    """Получить клиническую закладку по ID.
    По умолчанию возвращает 7 элементов для строгой совместимости с распаковкой в assistant.py:
    (id, msg_id, chat_id, sender_name, text, media_description, date).
    При include_tags=True возвращает 8 элементов (+ tags).
    """
    cols = "id, msg_id, chat_id, sender_name, text, media_description, date" + (", tags" if include_tags else "")
    def operation():
        with _connection() as db:
            if saved_by_user_id is not None:
                return db.execute(
                    f"SELECT {cols} FROM clinical_bookmarks WHERE id = ? AND saved_by_user_id = ?",
                    (bookmark_id, saved_by_user_id),
                ).fetchone()
            else:
                return db.execute(
                    f"SELECT {cols} FROM clinical_bookmarks WHERE id = ?",
                    (bookmark_id,),
                ).fetchone()
    return await _run_db(operation)


async def get_clinical_bookmark_dict(bookmark_id: int, saved_by_user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Возвращает закладку в виде удобного словаря со всеми полями, включая tags."""
    row = await get_clinical_bookmark_by_id(bookmark_id, saved_by_user_id=saved_by_user_id, include_tags=True)
    if not row:
        return None
    return {
        "id": row[0],
        "msg_id": row[1],
        "chat_id": row[2],
        "sender_name": row[3],
        "text": row[4],
        "media_description": row[5],
        "date": row[6],
        "tags": row[7] if len(row) > 7 else ""
    }


async def delete_clinical_bookmark(bookmark_id: int, saved_by_user_id: Optional[int] = None) -> bool:
    """Удалить клиническую закладку по id с защитой IDOR."""
    def operation():
        with _connection() as db:
            if saved_by_user_id is not None:
                cursor = db.execute(
                    "DELETE FROM clinical_bookmarks WHERE id = ? AND saved_by_user_id = ?",
                    (bookmark_id, saved_by_user_id),
                )
            else:
                cursor = db.execute(
                    "DELETE FROM clinical_bookmarks WHERE id = ?",
                    (bookmark_id,),
                )
            return cursor.rowcount > 0
    return await _run_db(operation)


async def count_clinical_bookmarks(saved_by_user_id: int, query: Optional[str] = None, tag: Optional[str] = None) -> int:
    """Сколько всего закладок подходит под условие с учетом поискового запроса и фильтрации по тегам."""
    def operation():
        with _connection() as db:
            conditions = ["saved_by_user_id = ?"]
            params = [saved_by_user_id]
            if query:
                like_q = f"%{query}%"
                conditions.append("(text LIKE ? OR media_description LIKE ? OR sender_name LIKE ? OR tags LIKE ?)")
                params.extend([like_q, like_q, like_q, like_q])
            if tag:
                clean_tag = tag.strip().lstrip('#').lower()
                like_t = f"%{clean_tag}%"
                conditions.append("(LOWER(tags) LIKE ? OR LOWER(text) LIKE ?)")
                params.extend([like_t, like_t])
            where_clause = " AND ".join(conditions)
            row = db.execute(f"SELECT COUNT(*) FROM clinical_bookmarks WHERE {where_clause}", params).fetchone()
            return row[0] if row else 0
    return await _run_db(operation)


async def get_user_bookmark_tags(saved_by_user_id: int) -> List[str]:
    """Возвращает отсортированный список всех уникальных тегов закладок конкретного врача."""
    def operation():
        with _connection() as db:
            rows = db.execute(
                "SELECT tags FROM clinical_bookmarks WHERE saved_by_user_id = ? AND tags IS NOT NULL AND tags != ''",
                (saved_by_user_id,)
            ).fetchall()
            tags_set = set()
            for r in rows:
                if not r[0]:
                    continue
                for t in re.split(r'[,; ]+', str(r[0])):
                    t_clean = t.strip()
                    if t_clean:
                        if not t_clean.startswith("#"):
                            t_clean = "#" + t_clean
                        tags_set.add(t_clean.lower())
            return sorted(list(tags_set))
    return await _run_db(operation)


async def search_clinical_bookmarks(saved_by_user_id: int, query: Optional[str] = None, tag: Optional[str] = None, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    """Поиск клинических закладок врача с выдачей структурированных словарей."""
    rows = await get_clinical_bookmarks(
        saved_by_user_id=saved_by_user_id,
        query=query,
        limit=limit,
        offset=offset,
        include_id=True,
        tag=tag,
        include_tags=True,
    )
    return [
        {
            "id": r[0],
            "msg_id": r[1],
            "chat_id": r[2],
            "sender_name": r[3],
            "text": r[4],
            "media_description": r[5],
            "date": r[6],
            "tags": r[7] if len(r) > 7 else ""
        }
        for r in rows
    ]


async def set_user_interactive_state(user_id, state_type, current_step, case_id, history):
    def operation():
        with _connection() as db:
            try:
                db.execute(
                    """
                    INSERT OR REPLACE INTO user_interactive_states
                    (user_id, state_type, current_step, case_id, history)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, state_type, current_step, case_id, history),
                )
            except sqlite3.OperationalError:
                pass
    return await _run_db(operation)


async def get_user_interactive_state(user_id):
    def operation():
        with _connection() as db:
            try:
                row = db.execute(
                    "SELECT state_type, current_step, case_id, history FROM user_interactive_states WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if not row:
                    return None
                state_type, current_step, case_id, history = row
                # Автоочистка зависших сессий (> 1 часа неактивности)
                if state_type == "case" and history:
                    try:
                        h = json.loads(history)
                        if isinstance(h, dict):
                            ts = h.get("last_updated")
                            if ts and (time.time() - float(ts) > 3600):
                                db.execute("DELETE FROM user_interactive_states WHERE user_id = ?", (user_id,))
                                return None
                    except Exception:
                        pass
                return {
                    "state_type": state_type,
                    "current_step": current_step,
                    "case_id": case_id,
                    "history": history
                }
            except sqlite3.OperationalError:
                return None
    return await _run_db(operation)


async def clear_user_interactive_state(user_id):
    def operation():
        with _connection() as db:
            try:
                db.execute(
                    "DELETE FROM user_interactive_states WHERE user_id = ?",
                    (user_id,),
                )
            except sqlite3.OperationalError:
                pass
    return await _run_db(operation)


async def cleanup_stale_interactive_states(max_age_seconds=3600, max_quiz_age_seconds=7200):
    """
    Удаляет зависшие интерактивные сессии симулятора (старше max_age_seconds, по умолчанию 1 час)
    и устаревшие конфигурации квизов (завершенные — через 10 мин, заброшенные — через max_quiz_age_seconds=2 часа, любые — через 24 часа).
    """
    now = time.time()
    def operation():
        with _connection() as db:
            try:
                rows = db.execute("SELECT user_id, state_type, history FROM user_interactive_states").fetchall()
            except sqlite3.OperationalError:
                return 0
            deleted = 0
            for user_id, state_type, hist_raw in rows:
                is_stale = False
                try:
                    if not hist_raw:
                        is_stale = True
                    else:
                        data = json.loads(hist_raw)
                        if state_type == "case":
                            if isinstance(data, dict):
                                ts = data.get("last_updated")
                                if not ts or (now - float(ts) > max_age_seconds):
                                    is_stale = True
                            else:
                                is_stale = True
                        elif state_type == "quiz_config":
                            ts = None
                            is_completed = False
                            votes_count = 0
                            if isinstance(data, dict):
                                ts = data.get("last_updated") or data.get("created_at")
                                is_completed = bool(data.get("is_completed", False))
                                voters = data.get("voters", {})
                                votes_count = len(voters) if isinstance(voters, dict) else 0
                            if not ts:
                                try:
                                    u_int = int(user_id)
                                    if u_int < 0:
                                        ts = abs(u_int) / 1_000_000.0
                                except Exception:
                                    pass

                            # Завершенные квизы удаляем через 10 минут
                            if is_completed:
                                if not ts or (now - float(ts) > 600):
                                    is_stale = True
                            # Квизы без ответов (заброшенные карточки) удаляем через max_quiz_age_seconds (2 часа)
                            elif votes_count == 0 and ts and (now - float(ts) > max_quiz_age_seconds):
                                is_stale = True
                            # Любые квизы старше max(max_quiz_age_seconds, 86400) (24 часа) удаляем
                            elif not ts or (now - float(ts) > max(max_quiz_age_seconds, 86400)):
                                is_stale = True
                        else:
                            is_stale = True
                except Exception:
                    is_stale = True

                if is_stale:
                    try:
                        db.execute("DELETE FROM user_interactive_states WHERE user_id = ?", (user_id,))
                        deleted += 1
                    except sqlite3.OperationalError:
                        pass
            return deleted
    return await _run_db(operation)


async def cleanup_orphaned_quiz_configs(max_age_seconds=7200):
    """
    Немедленная очистка устаревших конфигураций квизов (отрицательные user_id)
    из user_interactive_states для предотвращения утечки строк.
    """
    return await cleanup_stale_interactive_states(max_age_seconds=3600, max_quiz_age_seconds=max_age_seconds)


async def save_bot_sent_message(msg_id, chat_id):
    def operation():
        with _connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO bot_sent_messages (msg_id, chat_id) VALUES (?, ?)",
                (msg_id, chat_id)
            )
    return await _run_db(operation)


async def get_last_bot_sent_messages(count=10, chat_id=None):
    """
    Последние сообщения бота. chat_id=None — выборка по ВСЕМ чатам; для /wipe
    это опасно (заденет личные переписки других врачей), туда следует
    передавать конкретный чат.
    """
    def operation():
        with _connection() as db:
            if chat_id is None:
                cursor = db.execute(
                    "SELECT msg_id, chat_id FROM bot_sent_messages ORDER BY id DESC LIMIT ?",
                    (count,)
                )
            else:
                cursor = db.execute(
                    "SELECT msg_id, chat_id FROM bot_sent_messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                    (chat_id, count)
                )
            return cursor.fetchall()
    return await _run_db(operation)


async def remove_bot_sent_message(msg_id, chat_id=None):
    """
    Снимает сообщение бота с учёта после удаления в Telegram.

    chat_id стоит передавать всегда: с ключом (msg_id, chat_id) строки разных
    чатов сосуществуют, и удаление «по номеру» снимает с учёта чужую — то же
    сообщение основного чата, которое /wipe после этого не увидит.
    chat_id=None оставлен ради совместимости с вызовами, которые чат не знают.
    """
    def operation():
        with _connection() as db:
            if chat_id is None:
                db.execute("DELETE FROM bot_sent_messages WHERE msg_id = ?", (msg_id,))
            else:
                db.execute(
                    "DELETE FROM bot_sent_messages WHERE msg_id = ? AND chat_id = ?",
                    (msg_id, chat_id),
                )
    return await _run_db(operation)


async def is_bot_sent_message(msg_id, chat_id=None):
    """
    Проверяет, было ли сообщение отправлено ботом.
    Быстрый локальный поиск в bot_sent_messages без сетевых вызовов в Telegram.
    """
    if not msg_id:
        return False
    def operation():
        with _connection() as db:
            if chat_id is not None:
                row = db.execute(
                    "SELECT 1 FROM bot_sent_messages WHERE msg_id = ? AND chat_id = ? LIMIT 1",
                    (msg_id, chat_id)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT 1 FROM bot_sent_messages WHERE msg_id = ? LIMIT 1",
                    (msg_id,)
                ).fetchone()
            return bool(row)
    return await _run_db(operation)


def _resolve_known_bot_ids() -> tuple:
    ids = {7886475630, 8140417978, 7971556097, 7475073041, 8775535374}
    try:
        import config
        for k in ("BOT_ID", "STOMCHAT_BOT_ID", "ENDOCHAT_BOT_ID"):
            v = getattr(config, k, None)
            if v and isinstance(v, int):
                ids.add(v)
            elif v and str(v).isdigit():
                ids.add(int(v))
    except Exception:
        pass
    for k in ("BOT_ID", "STOMCHAT_BOT_ID", "ENDOCHAT_BOT_ID"):
        v = os.getenv(k)
        if v and v.isdigit():
            ids.add(int(v))
    return tuple(ids)


def _resolve_known_bot_usernames() -> tuple:
    unames = {'stomchat_bot', 'docendobot', 'endohelp_bot', 'stomat_bot', 'endochatbot', 'endo_bot'}
    try:
        import config
        for k in ("BOT_USERNAME", "STOMCHAT_BOT_USERNAME", "ENDOCHAT_BOT_USERNAME"):
            v = getattr(config, k, None)
            if v:
                unames.add(str(v).lstrip("@").lower())
    except Exception:
        pass
    for k in ("BOT_USERNAME", "STOMCHAT_BOT_USERNAME", "ENDOCHAT_BOT_USERNAME"):
        v = os.getenv(k)
        if v:
            unames.add(v.lstrip("@").lower())
    return tuple(unames)


async def is_bot_message_or_sender(msg_id, bot_id=None, chat_id=None):
    """
    Проверяет, является ли сообщение сообщением бота:
    1) По таблице bot_sent_messages (id всех сообщений, отправленных ботом)
    2) Если передан bot_id (или набор id), дополнительно сверяет sender_id в messages.
    """
    if not msg_id:
        return False
    def operation():
        with _connection() as db:
            if chat_id is not None:
                row = db.execute(
                    "SELECT 1 FROM bot_sent_messages WHERE msg_id = ? AND chat_id = ? LIMIT 1",
                    (msg_id, chat_id)
                ).fetchone()
                if row:
                    return True
            row = db.execute(
                "SELECT 1 FROM bot_sent_messages WHERE msg_id = ? LIMIT 1",
                (msg_id,)
            ).fetchone()
            if row:
                return True
            if bot_id:
                if isinstance(bot_id, (set, list, tuple)):
                    bot_ids = [int(b) for b in bot_id if b is not None]
                    if bot_ids:
                        placeholders = ",".join("?" for _ in bot_ids)
                        m_row = db.execute(
                            f"SELECT 1 FROM messages WHERE msg_id = ? AND sender_id IN ({placeholders}) LIMIT 1",
                            [msg_id] + bot_ids
                        ).fetchone()
                        if m_row:
                            return True
                else:
                    m_row = db.execute(
                        "SELECT 1 FROM messages WHERE msg_id = ? AND sender_id = ? LIMIT 1",
                        (msg_id, bot_id)
                    ).fetchone()
                    if m_row:
                        return True
            # 3) Сверка по имени/юзернейму бота или известным id в таблице messages
            known_ids = _resolve_known_bot_ids()
            known_unames = _resolve_known_bot_usernames()
            ph_ids = ",".join("?" for _ in known_ids)
            ph_unames = ",".join("?" for _ in known_unames)
            bot_meta_row = db.execute(
                f"""
                SELECT 1 FROM messages 
                WHERE msg_id = ? AND (
                    LOWER(IFNULL(sender_username, '')) IN ({ph_unames})
                    OR LOWER(IFNULL(sender_name, '')) LIKE '%bot%'
                    OR LOWER(IFNULL(sender_name, '')) LIKE '%учимся вместе%'
                    OR sender_id IN ({ph_ids})
                ) LIMIT 1
                """,
                [msg_id] + list(known_unames) + list(known_ids)
            ).fetchone()
            if bot_meta_row:
                return True
            return False
    return await _run_db(operation)


async def get_last_bot_activity_timestamp(known_bot_ids=None):
    """
    Возвращает дату (строку вида 'YYYY-MM-DD HH:MM:SS' или None) последней активности бота.
    Сверяет messages (по sender_id бота) и bot_sent_messages.
    """
    def operation():
        with _connection() as db:
            ids = [int(b) for b in known_bot_ids if b] if known_bot_ids is not None else list(_resolve_known_bot_ids())
            latest_dates = []
            if ids:
                placeholders = ",".join("?" for _ in ids)
                row = db.execute(
                    f"SELECT MAX(date) FROM messages WHERE sender_id IN ({placeholders})",
                    ids
                ).fetchone()
                if row and row[0]:
                    latest_dates.append(str(row[0]))
            row = db.execute(
                """SELECT MAX(m.date) FROM messages m
                   JOIN bot_sent_messages b ON m.msg_id = b.msg_id"""
            ).fetchone()
            if row and row[0]:
                latest_dates.append(str(row[0]))
            if latest_dates:
                return max(latest_dates)
            return None
    return await _run_db(operation)


async def has_bot_replied_to_message(msg_id, known_bot_ids=None):
    """
    Проверяет по базе данных, отвечал ли бот на сообщение msg_id:
    1) Ищет сообщение в messages с reply_to_msg_id = msg_id от sender_id бота
    2) Ищет сообщение в messages с reply_to_msg_id = msg_id, чей msg_id числится в bot_sent_messages
    """
    if not msg_id:
        return False
    def operation():
        with _connection() as db:
            ids = [int(b) for b in known_bot_ids if b] if known_bot_ids is not None else list(_resolve_known_bot_ids())
            if ids:
                placeholders = ",".join("?" for _ in ids)
                row = db.execute(
                    f"""SELECT 1 FROM messages
                       WHERE reply_to_msg_id = ?
                         AND (sender_id IN ({placeholders})
                              OR msg_id IN (SELECT msg_id FROM bot_sent_messages))
                       LIMIT 1""",
                    [msg_id] + ids
                ).fetchone()
            else:
                row = db.execute(
                    """SELECT 1 FROM messages
                       WHERE reply_to_msg_id = ?
                         AND msg_id IN (SELECT msg_id FROM bot_sent_messages)
                       LIMIT 1""",
                    (msg_id,)
                ).fetchone()
            return bool(row)
    return await _run_db(operation)


async def get_candidate_messages_for_catchup(since_date_str, known_bot_ids=None, limit=100):
    """
    Возвращает сообщения из messages начиная с since_date_str, исключая сообщения бота,
    в хронологическом порядке (ASC) для обработки пропущенных прямых обращений.
    """
    def operation():
        with _connection() as db:
            ids = [int(b) for b in known_bot_ids if b] if known_bot_ids is not None else list(_resolve_known_bot_ids())
            if ids:
                placeholders = ",".join("?" for _ in ids)
                query = f"""
                    SELECT msg_id, reply_to_msg_id, sender_id, sender_name, sender_username, text, date, has_media, media_type, media_description
                    FROM messages
                    WHERE date >= ?
                      AND (sender_id IS NULL OR sender_id NOT IN ({placeholders}))
                      AND msg_id NOT IN (SELECT msg_id FROM bot_sent_messages)
                    ORDER BY msg_id ASC
                    LIMIT ?
                """
                return db.execute(query, [since_date_str] + ids + [limit]).fetchall()
            else:
                query = """
                    SELECT msg_id, reply_to_msg_id, sender_id, sender_name, sender_username, text, date, has_media, media_type, media_description
                    FROM messages
                    WHERE date >= ?
                      AND msg_id NOT IN (SELECT msg_id FROM bot_sent_messages)
                    ORDER BY msg_id ASC
                    LIMIT ?
                """
                return db.execute(query, (since_date_str, limit)).fetchall()
    return await _run_db(operation)


async def get_recent_messages_for_dedup(chat_id=None, limit=50):
    """
    Выборка последних сообщений из bot_sent_messages + messages для быстрой локальной
    дедупликации без сетевых запросов get_messages в Telegram.
    """
    def operation():
        with _connection() as db:
            seen_ids = set()
            result = []
            if chat_id is not None:
                rows = db.execute(
                    """
                    SELECT b.msg_id, m.text, m.reply_to_msg_id
                    FROM bot_sent_messages b
                    JOIN messages m ON b.msg_id = m.msg_id
                    WHERE b.chat_id = ?
                    ORDER BY b.id DESC
                    LIMIT ?
                    """,
                    (chat_id, limit),
                ).fetchall()
                for r in rows:
                    if r[0] not in seen_ids:
                        seen_ids.add(r[0])
                        result.append((r[0], r[1], r[2]))

            rows_m = db.execute(
                """
                SELECT msg_id, text, reply_to_msg_id
                FROM messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for r in rows_m:
                if r[0] not in seen_ids:
                    seen_ids.add(r[0])
                    result.append((r[0], r[1], r[2]))
            return result
    return await _run_db(operation)


async def save_pm_message(user_id, sender_name, text):
    def operation():
        with _connection() as db:
            cur = db.execute(
                "INSERT INTO pm_messages (user_id, sender_name, text) VALUES (?, ?, ?)",
                (user_id, sender_name, text)
            )
            return cur.lastrowid
    return await _run_db(operation)


async def update_pm_message_text(message_id, new_text):
    """Обновляет текст существующей записи в pm_messages по id (например, обогащение описанием снимка)."""
    def operation():
        with _connection() as db:
            db.execute(
                "UPDATE pm_messages SET text = ? WHERE id = ?",
                (new_text, message_id)
            )
    return await _run_db(operation)


async def get_last_pm_messages(user_id, limit=25):
    def operation():
        with _connection() as db:
            cursor = db.execute(
                "SELECT sender_name, text, date FROM pm_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit)
            )
            rows = cursor.fetchall()
            return [{"sender_name": row[0], "text": row[1], "date": str(row[2]) if row[2] else ""} for row in reversed(rows)]
    return await _run_db(operation)


async def get_user_profile(user_id):
    def operation():
        with _connection() as db:
            row = db.execute(
                "SELECT selected_style, profile_portrait, last_analyzed_msg_id FROM user_profiles WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if row:
                return {
                    "selected_style": row[0],
                    "profile_portrait": row[1],
                    "last_analyzed_msg_id": row[2]
                }
            return {
                "selected_style": "colleague_friendly",
                "profile_portrait": None,
                "last_analyzed_msg_id": 0
            }
    return await _run_db(operation)


async def set_user_style(user_id, style):
    def operation():
        with _connection() as db:
            db.execute(
                """
                INSERT INTO user_profiles (user_id, selected_style)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET selected_style = excluded.selected_style
                """,
                (user_id, style)
            )
    return await _run_db(operation)


async def set_user_portrait(user_id, portrait, last_msg_id):
    if user_id in (7971556097, 8775535374):
        return
    try:
        from user_memory import clean_profile_portrait
        portrait = clean_profile_portrait(portrait)
    except Exception as exc:
        logger.debug("Failed to clean user portrait for user %s: %s", user_id, exc)
    if not portrait:
        return
    def operation():
        with _connection() as db:
            db.execute(
                """
                INSERT INTO user_profiles (user_id, profile_portrait, last_analyzed_msg_id)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET 
                    profile_portrait = excluded.profile_portrait,
                    last_analyzed_msg_id = excluded.last_analyzed_msg_id
                """,
                (user_id, portrait, last_msg_id)
            )
    return await _run_db(operation)


async def get_user_recent_group_messages(user_id, limit=20):
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT text FROM messages
                WHERE sender_id = ? AND text IS NOT NULL AND text != ''
                ORDER BY date DESC, msg_id DESC LIMIT ?
                """,
                (user_id, limit)
            ).fetchall()
            return [r[0] for r in rows[::-1]]
    return await _run_db(operation)


async def get_active_pm_users(days_limit=30):
    def operation():
        with _connection() as db:
            # Выбираем уникальных пользователей, которые РЕАЛЬНО писали боту в ЛС за последние N дней
            rows = db.execute(
                """
                SELECT DISTINCT user_id FROM pm_messages 
                WHERE sender_name = 'User' AND date >= datetime('now', ?)
                  AND user_id < 900000000 AND user_id NOT IN (777, 4242, 555111, 12345)
                """,
                (f"-{days_limit} days",)
            ).fetchall()
            return [r[0] for r in rows]
    return await _run_db(operation)


async def get_user_memory(user_id):
    def operation():
        with _connection() as db:
            row = db.execute(
                """
                SELECT user_id, username, first_name, specialty, clinical_summary, group_summary,
                       facts_json, message_count, pm_message_count, group_message_count,
                       last_pm_analyzed_id, last_group_analyzed_id, last_updated
                FROM user_memories WHERE user_id = ?
                """,
                (user_id,)
            ).fetchone()
            if row:
                return {
                    "user_id": row[0],
                    "username": row[1] or "",
                    "first_name": row[2] or "",
                    "specialty": row[3] or "",
                    "clinical_summary": row[4] or "",
                    "group_summary": row[5] or "",
                    "facts_json": row[6] or "[]",
                    "message_count": row[7] or 0,
                    "pm_message_count": row[8] or 0,
                    "group_message_count": row[9] or 0,
                    "last_pm_analyzed_id": row[10] or 0,
                    "last_group_analyzed_id": row[11] or 0,
                    "last_updated": row[12],
                }
            return {
                "user_id": user_id,
                "username": "",
                "first_name": "",
                "specialty": "",
                "clinical_summary": "",
                "group_summary": "",
                "facts_json": "[]",
                "message_count": 0,
                "pm_message_count": 0,
                "group_message_count": 0,
                "last_pm_analyzed_id": 0,
                "last_group_analyzed_id": 0,
                "last_updated": None,
            }
    return await _run_db(operation)


async def save_user_memory(
    user_id,
    specialty=None,
    clinical_summary=None,
    group_summary=None,
    facts_json=None,
    message_count=None,
    pm_message_count=None,
    group_message_count=None,
    last_pm_analyzed_id=None,
    last_group_analyzed_id=None,
    username=None,
    first_name=None
):
    if user_id in (7971556097, 8775535374):
        logger.warning(f"save_user_memory called for known bot id {user_id}, ignoring")
        return
    # Лимит объема на одного пользователя в БД: строго до 64 КБ для ЛС и до 8 КБ для беседы
    if clinical_summary and len(clinical_summary) > 64000:
        import html_safe
        clinical_summary = html_safe.clip_at_sentence_text(clinical_summary, 64000)
    if group_summary and len(group_summary) > 8000:
        import html_safe
        group_summary = html_safe.clip_at_sentence_text(group_summary, 8000)
    if specialty and len(specialty) > 80:
        specialty = specialty[:80].strip()
    if facts_json:
        try:
            f_list = json.loads(facts_json) if isinstance(facts_json, str) else facts_json
            if isinstance(f_list, list) and len(f_list) > 30:
                facts_json = json.dumps(f_list[-30:], ensure_ascii=False)
        except Exception:
            pass
    def operation():
        with _connection() as db:
            existing = db.execute(
                """
                SELECT specialty, clinical_summary, group_summary, facts_json,
                       message_count, pm_message_count, group_message_count,
                       last_pm_analyzed_id, last_group_analyzed_id, username, first_name
                FROM user_memories WHERE user_id = ?
                """,
                (user_id,)
            ).fetchone()
            if existing:
                new_spec = specialty if specialty is not None else (existing[0] or "")
                new_clin = clinical_summary if clinical_summary is not None else (existing[1] or "")
                new_grp = group_summary if group_summary is not None else (existing[2] or "")
                new_facts = facts_json if facts_json is not None else (existing[3] or "[]")
                new_cnt = message_count if message_count is not None else (existing[4] or 0)
                new_pm_cnt = max(pm_message_count, existing[5] or 0) if pm_message_count is not None else (existing[5] or 0)
                new_grp_cnt = group_message_count if group_message_count is not None else (existing[6] or 0)
                new_pm_id = last_pm_analyzed_id if last_pm_analyzed_id is not None else (existing[7] or 0)
                new_grp_id = last_group_analyzed_id if last_group_analyzed_id is not None else (existing[8] or 0)
                new_un = username if username is not None else (existing[9] or "")
                new_fn = first_name if first_name is not None else (existing[10] or "")
                db.execute(
                    """
                    UPDATE user_memories
                    SET specialty = ?, clinical_summary = ?, group_summary = ?, facts_json = ?,
                        message_count = ?, pm_message_count = ?, group_message_count = ?,
                        last_pm_analyzed_id = ?, last_group_analyzed_id = ?,
                        username = ?, first_name = ?, last_updated = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (new_spec, new_clin, new_grp, new_facts, new_cnt, new_pm_cnt, new_grp_cnt,
                     new_pm_id, new_grp_id, new_un, new_fn, user_id)
                )
            else:
                db.execute(
                    """
                    INSERT INTO user_memories (
                        user_id, specialty, clinical_summary, group_summary, facts_json,
                        message_count, pm_message_count, group_message_count,
                        last_pm_analyzed_id, last_group_analyzed_id, username, first_name, last_updated
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (user_id, specialty or "", clinical_summary or "", group_summary or "", facts_json or "[]",
                     message_count or 1, pm_message_count or 0, group_message_count or 0,
                     last_pm_analyzed_id or 0, last_group_analyzed_id or 0, username or "", first_name or "")
                )
    return await _run_db(operation)


async def get_users_memory_batch(user_ids):
    if not user_ids:
        return {}
    def operation():
        with _connection() as db:
            placeholders = ",".join("?" for _ in user_ids)
            rows = db.execute(
                f"""
                SELECT user_id, username, first_name, specialty, clinical_summary, group_summary,
                       facts_json, message_count, pm_message_count, group_message_count, last_updated
                FROM user_memories WHERE user_id IN ({placeholders})
                """,
                tuple(user_ids)
            ).fetchall()
            res = {}
            for r in rows:
                res[r[0]] = {
                    "user_id": r[0],
                    "username": r[1] or "",
                    "first_name": r[2] or "",
                    "specialty": r[3] or "",
                    "clinical_summary": r[4] or "",
                    "group_summary": r[5] or "",
                    "facts_json": r[6] or "[]",
                    "message_count": r[7] or 0,
                    "pm_message_count": r[8] or 0,
                    "group_message_count": r[9] or 0,
                    "last_updated": r[10],
                }
            return res
    return await _run_db(operation)


async def increment_user_pm_count(user_id: int, username: str = "", first_name: str = "") -> int:
    """
    Атомарно инкрементирует pm_message_count для пользователя в БД.
    Возвращает актуальное значение pm_message_count после инкремента.
    """
    def operation():
        with _connection() as db:
            existing = db.execute(
                "SELECT pm_message_count, message_count FROM user_memories WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if existing is not None:
                new_pm_cnt = (existing[0] or 0) + 1
                new_msg_cnt = (existing[1] or 0) + 1
                db.execute(
                    """
                    UPDATE user_memories
                    SET pm_message_count = ?, message_count = ?,
                        username = CASE WHEN ? != '' THEN ? ELSE username END,
                        first_name = CASE WHEN ? != '' THEN ? ELSE first_name END,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (new_pm_cnt, new_msg_cnt, username, username, first_name, first_name, user_id)
                )
                return new_pm_cnt
            else:
                db.execute(
                    """
                    INSERT INTO user_memories (
                        user_id, username, first_name, pm_message_count, message_count, last_updated
                    )
                    VALUES (?, ?, ?, 1, 1, CURRENT_TIMESTAMP)
                    """,
                    (user_id, username or "", first_name or "")
                )
                return 1
    return await _run_db(operation)


async def get_unprocessed_group_users(min_new_messages=3, limit=20, bot_id=None):
    """
    Возвращает список пользователей, у которых в messages накопились новые сообщения
    для обновления памяти беседы (group_summary).
    Приоритет отдается врачам с наибольшим количеством новых реплик (cnt DESC, max_id DESC).
    Исключает самого бота (bot_id) и отрицательные sender_id (каналы/группы).
    """
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT m.sender_id, m.sender_name, m.sender_username,
                       COUNT(m.msg_id) as cnt, MAX(m.msg_id) as max_id
                FROM messages m
                LEFT JOIN user_memories um ON um.user_id = m.sender_id
                WHERE m.sender_id IS NOT NULL AND m.sender_id > 0
                  AND m.sender_id NOT IN (7971556097, 8775535374)
                  AND (? IS NULL OR m.sender_id != ?)
                  AND m.text IS NOT NULL AND LENGTH(TRIM(m.text)) > 15
                  AND m.msg_id > COALESCE(um.last_group_analyzed_id, 0)
                GROUP BY m.sender_id
                HAVING cnt >= ?
                ORDER BY cnt DESC, max_id DESC
                LIMIT ?
                """,
                (bot_id, bot_id, min_new_messages, limit)
            ).fetchall()
            return [
                {
                    "user_id": r[0],
                    "sender_name": r[1] or "",
                    "username": r[2] or "",
                    "new_msgs_count": r[3],
                    "max_msg_id": r[4],
                    "max_id": r[4],
                }
                for r in rows
            ]
    return await _run_db(operation)


async def get_user_messages_since(user_id, since_msg_id=0, limit=30):
    """Возвращает сообщения пользователя из группы, начиная с since_msg_id."""
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT msg_id, text, date FROM messages
                WHERE sender_id = ? AND msg_id > ? AND text IS NOT NULL AND LENGTH(TRIM(text)) > 10
                ORDER BY msg_id ASC LIMIT ?
                """,
                (user_id, since_msg_id, limit)
            ).fetchall()
            return [{"msg_id": r[0], "text": r[1], "date": r[2]} for r in rows]
    return await _run_db(operation)


async def save_clinical_protocol(
    title,
    category="Общая стоматология",
    indication="",
    contraindications="",
    steps_json=None,
    materials="",
    key_nuances="",
    author_doctor="",
    source_msg_id=0,
    steps=None,
):
    """Сохраняет или актуализирует структурированный клинический протокол в БД."""
    if steps is not None and steps_json is None:
        if isinstance(steps, (list, tuple)):
            steps_json = json.dumps(steps, ensure_ascii=False)
        else:
            steps_json = str(steps)
    if steps_json is None:
        steps_json = "[]"

    if isinstance(materials, (list, tuple)):
        materials = ", ".join(str(m) for m in materials)
    if isinstance(contraindications, (list, tuple)):
        contraindications = "; ".join(str(c) for c in contraindications)

    def operation():
        with _connection() as db:
            existing = db.execute(
                "SELECT id FROM clinical_protocols WHERE LOWER(TRIM(title)) = LOWER(TRIM(?))",
                (title,)
            ).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE clinical_protocols
                    SET category = ?, indication = ?, contraindications = ?,
                        steps_json = ?, materials = ?, key_nuances = ?,
                        author_doctor = CASE WHEN ? != '' THEN ? ELSE author_doctor END,
                        source_msg_id = CASE WHEN ? != 0 THEN ? ELSE source_msg_id END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (category, indication, contraindications, steps_json, materials, key_nuances,
                     author_doctor, author_doctor, source_msg_id, source_msg_id, existing[0])
                )
                return existing[0]
            else:
                cursor = db.execute(
                    """
                    INSERT INTO clinical_protocols (
                        title, category, indication, contraindications, steps_json,
                        materials, key_nuances, author_doctor, source_msg_id, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (title, category, indication, contraindications, steps_json, materials, key_nuances, author_doctor, source_msg_id)
                )
                return cursor.lastrowid
    return await _run_db(operation)


_CLINICAL_PROTOCOL_SYNONYMS = {
    "naocl": ["гипохлорит", "гипохлорита", "ирригац", "апекс", "гипохлоритовая авария", "растворение"],
    "гипохлорит": ["naocl", "ирригац", "апекс", "гипохлоритовая авария"],
    "emax": ["e.max", "дисиликат", "литий-дисиликат", "фиксаци", "цементировк", "цемент", "бондинг"],
    "e.max": ["emax", "дисиликат", "литий-дисиликат", "фиксаци", "цементировк"],
    "фиксация e.max": ["дисиликат", "литий-дисиликат", "variolink", "плавиковая кислота", "силан", "адгезивная фиксация керамики", "e.max", "emax"],
    "анестезия": ["анестетик", "артикаин", "проводников", "инфильтрацион", "убистезин", "лидокаин", "мепивакаин"],
    "анестетик": ["анестезия", "артикаин", "лидокаин", "мепивакаин"],
    "артикаин": ["анестезия", "анестетик", "ультракаин", "септонест", "убистезин"],
    "торк": ["нсм", "н·см", "имплант", "стабильност", "isq", "первичная стабильность"],
    "адгезия": ["бондинг", "травлен", "etching", "силан", "адгез", "фиксация", "фиксаци", "optibond", "clearfil"],
    "bopt": ["vertiprep", "вертипреп", "препарирован", "уступ", "бесуступ"],
    "vertiprep": ["bopt", "вертипреп", "препарирован", "уступ", "knife-edge"],
    "вертипреп": ["vertiprep", "bopt", "препарирован", "уступ"],
    "препарирование": ["препарирован", "уступ", "bopt", "vertiprep", "вертипреп", "коронка", "оверлей", "винир", "knife-edge", "редукция"],
    "препаринг": ["препарирование", "препарирован", "уступ", "bopt", "vertiprep"],
    "фиксация": ["фиксаци", "цементировк", "бондинг", "адгезия", "цемент", "силанизация", "вариолинк", "variolink", "relyx", "panavia", "e.max", "emax"],
    "цементировка": ["фиксация", "фиксаци", "цемент", "бондинг", "адгезия"],
    "апекс": ["апикальн", "верхушка", "апекслокатор", "заапикальн", "гипохлорит", "деструкция", "периодонтит", "wl", "апекса", "апексом"],
    "апикальный": ["апекс", "верхушка", "апекслокатор", "периодонтит"],
    "сломанный инструмент": ["сломанн", "отломок", "байпас", "извлечение", "фрагмент", "протейпер", "файл", "bypass", "broken instrument", "ультразвук"],
    "сломанный": ["сломанный инструмент", "отломок", "байпас", "извлечение", "фрагмент", "файл"],
    "отломок": ["сломанный инструмент", "извлечение", "байпас", "фрагмент", "ультразвук", "файл"],
    "байпас": ["сломанный инструмент", "обход", "извлечение", "отломок", "bypass", "glide path"],
    "vdo": ["высота прикуса", "межальвеолярн", "окклюзи", "депрограмматор", "койс", "jig", "окклюзионная высота", "стираемость", "центральное соотношение"],
    "высота прикуса": ["vdo", "межальвеолярн", "окклюзи", "депрограмматор", "койс"],
    "депрограмматор": ["vdo", "высота прикуса", "койс", "jig", "окклюзия", "миорелаксация"],
    "коффердам": ["раббердам", "рабердам", "кламп", "изоляция"],
    "ирригация": ["naocl", "гипохлорит", "эдта", "edta", "активация", "pui"],
    "сладкое": ["гиперестезия", "десенситайзер", "чувствительность", "фторлак", "кариес", "реминерализация", "gluma", "duraphat", "болит от сладкого", "реакция на сладкое"],
    "паста с фтором": ["фтор", "фторид", "1450 ppm", "5000 ppm", "duraphat", "clinpro", "реминерализац", "фторпрофилактика", "фторлак"],
    "шепелявость": ["звук с", "сильверман", "фонетическ", "окклюзия", "vdo", "высота прикуса", "режущий край", "шепелявит", "присвистывание"],
    "звук с": ["сильверман", "шепелявость", "closest speaking space", "фонетическая проба", "звук «с»", "vdo", "высота прикуса"],
    "звук": ["фонетическ", "сильверман", "звук с", "шепелявость"],
    "шарик в центре коронки": ["дентикл", "пульполит", "кальцификат", "петрификат", "пульповой камеры", "шаровидный кальцификат", "пульпарный камень"],
    "шарик": ["дентикл", "пульполит", "кальцификат", "петрификат"],
    "феррул": ["ferrule", "ферул", "феррул-эффект", "биологическая ширина", "культевая вкладка", "удлинение коронки", "dme", "сохранение стенок"],
    "феррул-эффект": ["ferrule", "феррул", "культевая вкладка", "dme"],
}

_RU_STEM_CUTS = (
    "иями", "ями", "ами", "ями", "иях", "ях", "ах",
    "ия", "ии", "ию", "ией", "ей", "ой", "ом", "ем", "ов", "ев",
    "ный", "ная", "ное", "ные", "ного", "ной", "ных", "ным", "ными",
    "ский", "ская", "ское", "ские", "ского", "ской", "ских",
    "а", "ы", "е", "у", "и", "о", "я"
)

def _clinical_search_stem(word: str) -> str:
    for cut in _RU_STEM_CUTS:
        if word.endswith(cut) and len(word) - len(cut) >= 3:
            return word[:-len(cut)]
    return word


async def get_clinical_protocols(category: Optional[str] = None, search: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Возвращает перечень клинических протоколов с мгновенной фильтрацией по категории и клиническому поисковому запросу.
    Использует гибридный поиск: FTS5 полнотекстовый индекс + расширение клинических синонимов (препарирование, фиксация, апекс, сломанный инструмент, VDO).
    """
    def operation():
        with _connection() as db:
            query = """
                SELECT id, title, category, indication, contraindications,
                       steps_json, materials, key_nuances, author_doctor, source_msg_id, created_at
                FROM clinical_protocols
                WHERE 1=1
            """
            params = []
            if category:
                query += " AND LOWER(category) = LOWER(?)"
                params.append(category)

            if search:
                clean_search = (search or "").lower().strip()
                tokens = [t.strip() for t in re.split(r'[\s,]+', clean_search) if len(t.strip()) >= 2]
                expanded_terms = set(tokens)
                if clean_search:
                    expanded_terms.add(clean_search)

                for phrase, syns in _CLINICAL_PROTOCOL_SYNONYMS.items():
                    if phrase in clean_search or clean_search in phrase:
                        expanded_terms.update(syns)
                        expanded_terms.add(phrase)

                for t in tokens:
                    st = _clinical_search_stem(t)
                    if len(st) >= 3:
                        expanded_terms.add(st)
                    for k, syns in _CLINICAL_PROTOCOL_SYNONYMS.items():
                        if k in t or t in k or (len(st) >= 3 and st in k):
                            expanded_terms.update(syns)
                            expanded_terms.add(k)

                # Попытка поиска через FTS5
                fts_ids = []
                try:
                    fts_tokens = [f'"{t}"' for t in tokens if len(t) >= 2]
                    if fts_tokens:
                        fts_match_query = " OR ".join(fts_tokens)
                        fts_rows = db.execute(
                            "SELECT rowid FROM clinical_protocols_fts WHERE clinical_protocols_fts MATCH ? LIMIT 20",
                            (fts_match_query,)
                        ).fetchall()
                        fts_ids = [r[0] for r in fts_rows]
                except Exception:
                    pass

                if expanded_terms:
                    clauses = []
                    for term in expanded_terms:
                        like_p = f"%{term}%"
                        clauses.append(
                            "(LOWER(title) LIKE ? OR LOWER(materials) LIKE ? OR LOWER(steps_json) LIKE ? "
                            "OR LOWER(indication) LIKE ? OR LOWER(key_nuances) LIKE ? OR LOWER(category) LIKE ?)"
                        )
                        params.extend([like_p] * 6)
                    
                    if fts_ids:
                        ph_fts = ",".join("?" for _ in fts_ids)
                        query += f" AND (id IN ({ph_fts}) OR (" + " OR ".join(clauses) + "))"
                        params = params[:-len(clauses)*6] + fts_ids + params[-len(clauses)*6:]
                    else:
                        query += " AND (" + " OR ".join(clauses) + ")"

                    intent_boosters = []
                    if "сладк" in clean_search:
                        intent_boosters.append("WHEN LOWER(title) LIKE '%сладк%' THEN -3")
                        intent_boosters.append("WHEN LOWER(title) LIKE '%гиперестези%' THEN -2")
                    if "фтор" in clean_search or "паста" in clean_search:
                        intent_boosters.append("WHEN LOWER(title) LIKE '%паст%' AND LOWER(title) LIKE '%фтор%' THEN -3")
                        intent_boosters.append("WHEN LOWER(title) LIKE '%фторпрофилактик%' OR LOWER(title) LIKE '%duraphat%' THEN -2")
                    if "шепеляв" in clean_search:
                        intent_boosters.append("WHEN LOWER(title) LIKE '%шепеляв%' THEN -3")
                    if "звук" in clean_search:
                        intent_boosters.append("WHEN LOWER(title) LIKE '%звук%' AND LOWER(title) NOT LIKE '%ультразвук%' THEN -3")
                        intent_boosters.append("WHEN LOWER(title) LIKE '%сильверман%' OR LOWER(title) LIKE '%фонетическ%' THEN -2")
                    if "шарик" in clean_search:
                        intent_boosters.append("WHEN LOWER(title) LIKE '%дентикл%' OR LOWER(title) LIKE '%шарик%' OR LOWER(title) LIKE '%пульполит%' THEN -3")
                    if "феррул" in clean_search:
                        intent_boosters.append("WHEN LOWER(title) LIKE '%феррул%' OR LOWER(title) LIKE '%ferrule%' THEN -3")
                    if "e.max" in clean_search or "emax" in clean_search:
                        intent_boosters.append("WHEN (LOWER(title) LIKE '%фиксац%' OR LOWER(title) LIKE '%адгезив%') AND (LOWER(title) LIKE '%e.max%' OR LOWER(title) LIKE '%дисиликат%') AND LOWER(title) NOT LIKE '%демонтаж%' THEN -4")
                        intent_boosters.append("WHEN LOWER(title) LIKE '%дисиликат%' OR LOWER(title) LIKE '%e.max%' THEN -3")
                    if "vdo" in clean_search or "высота прикуса" in clean_search:
                        intent_boosters.append("WHEN LOWER(title) LIKE '%vdo%' OR LOWER(title) LIKE '%высота прикуса%' THEN -3")
                    if "сломан" in clean_search or "отломок" in clean_search or "байпас" in clean_search:
                        intent_boosters.append("WHEN LOWER(title) LIKE '%сломан%' OR LOWER(title) LIKE '%отломок%' OR LOWER(title) LIKE '%байпас%' THEN -3")

                    boost_sql = "\n                        ".join(intent_boosters) + "\n                        " if intent_boosters else ""

                    title_cases = " OR ".join(["LOWER(title) LIKE ?"] * len(expanded_terms))
                    mat_cases = " OR ".join(["LOWER(materials) LIKE ?"] * len(expanded_terms))
                    query += (
                        f" ORDER BY CASE "
                        f"{boost_sql}"
                        f"WHEN LOWER(title) LIKE ? THEN 0 "
                        f"WHEN {title_cases} THEN 1 "
                        f"WHEN LOWER(materials) LIKE ? THEN 2 "
                        f"WHEN {mat_cases} THEN 3 "
                        f"ELSE 4 END, id DESC LIMIT ?"
                    )
                    params.append(f"%{clean_search}%")
                    for term in expanded_terms:
                        params.append(f"%{term}%")
                    params.append(f"%{clean_search}%")
                    for term in expanded_terms:
                        params.append(f"%{term}%")
                    params.append(limit)
                else:
                    query += " ORDER BY id DESC LIMIT ?"
                    params.append(limit)
            else:
                query += " ORDER BY id DESC LIMIT ?"
                params.append(limit)

            rows = db.execute(query, params).fetchall()
            return [
                {
                    "id": r[0],
                    "title": r[1],
                    "category": r[2],
                    "indication": r[3],
                    "contraindications": r[4],
                    "steps_json": r[5],
                    "materials": r[6],
                    "key_nuances": r[7],
                    "author_doctor": r[8],
                    "source_msg_id": r[9],
                    "created_at": r[10],
                }
                for r in rows
            ]
    return await _run_db(operation)


async def get_clinical_protocol_by_id(proto_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает один протокол по ID."""
    def operation():
        with _connection() as db:
            row = db.execute(
                """
                SELECT id, title, category, indication, contraindications,
                       steps_json, materials, key_nuances, author_doctor, source_msg_id, created_at
                FROM clinical_protocols WHERE id = ?
                """,
                (proto_id,)
            ).fetchone()
            if not row:
                return None
            return {
                "id": row[0],
                "title": row[1],
                "category": row[2],
                "indication": row[3],
                "contraindications": row[4],
                "steps_json": row[5],
                "materials": row[6],
                "key_nuances": row[7],
                "author_doctor": row[8],
                "source_msg_id": row[9],
                "created_at": row[10],
            }
    return await _run_db(operation)


async def search_clinical_protocols(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Полнотекстовый поиск по протоколам для RAG-подмешивания и пользовательского поиска."""
    return await get_clinical_protocols(search=query, limit=limit)


# ==============================================================================
# Экспорт клинических записей в формате МИС (IDENT, Инфодент, форма 043/у)
# ==============================================================================

def format_protocol_mis(proto: Dict[str, Any]) -> str:
    """Форматирует клинический протокол для 1-тап копирования в МИС (моноширинный блок <code>...</code>)."""
    title = html.escape(str(proto.get("title", "Клинический протокол")).strip())
    category = html.escape(str(proto.get("category", "Стоматология")).strip())
    indication = html.escape(str(proto.get("indication", "")).strip())
    contra = html.escape(str(proto.get("contraindications", "")).strip())
    materials = html.escape(str(proto.get("materials", "")).strip())
    nuances = html.escape(str(proto.get("key_nuances", "")).strip())
    author = html.escape(str(proto.get("author_doctor", "")).strip())

    steps_raw = proto.get("steps_json", "[]")
    try:
        steps_list = json.loads(steps_raw) if isinstance(steps_raw, str) else (steps_raw or [])
    except Exception:
        steps_list = []

    steps_lines = []
    for s in steps_list:
        if isinstance(s, dict):
            s_num = s.get("step", len(steps_lines) + 1)
            s_name = s.get("name", "")
            s_desc = s.get("description", "")
            step_text = f"{s_num}. {s_name}: {s_desc}" if s_name else f"{s_num}. {s_desc}"
            steps_lines.append(html.escape(step_text.strip()))
        else:
            steps_lines.append(f"{len(steps_lines) + 1}. {html.escape(str(s).strip())}")
    steps_formatted = "\n".join(steps_lines) if steps_lines else "По стандартному клиническому протоколу."

    mis_body = (
        f"[ПРОТОКОЛ МИС / ФОРМА 043/У]\n"
        f"НАЗВАНИЕ: {title}\n"
        f"КАТЕГОРИЯ: {category}\n"
        f"ПОКАЗАНИЯ: {indication}\n"
        f"ПРОТИВОПОКАЗАНИЯ: {contra}\n"
        f"МАТЕРИАЛЫ И ОСНАЩЕНИЕ: {materials}\n"
        f"ЭТАПЫ:\n{steps_formatted}\n"
        f"КЛЮЧЕВЫЕ НЮАНСЫ: {nuances}\n"
        f"АВТОР / ИСТОЧНИК: {author}"
    )

    return (
        f"📋 <b>{title}</b>\n"
        f"📁 <i>Категория: {category}</i>\n\n"
        f"💡 <i>1-тап копирование в МИС (IDENT / Инфодент / 043/у):</i>\n"
        f"<code>{mis_body}</code>"
    )


async def export_protocol_mis_async(proto_id: int) -> Optional[str]:
    """Асинхронно получает протокол по ID и возвращает его в формате МИС с блоком <code>...</code>."""
    proto = await get_clinical_protocol_by_id(proto_id)
    if not proto:
        return None
    return format_protocol_mis(proto)


def format_clinical_note_for_mis(
    title: str,
    text: str,
    doctor: str = "",
    date_str: Optional[str] = None,
    tags: Optional[str] = None,
    tooth: Optional[str] = None,
    diagnosis: Optional[str] = None,
    protocol_steps: Optional[List[str]] = None,
) -> str:
    """Создает готовую клиническую запись для медицинской карты (форма 043/у, IDENT, Инфодент) в теге <code>...</code>."""
    clean_title = html.escape(str(title).strip())
    clean_text = html.escape(str(text).strip())
    clean_doctor = html.escape(str(doctor or "Лечащий врач").strip())
    clean_date = html.escape(str(date_str or time.strftime("%Y-%m-%d %H:%M")).strip())
    clean_tooth = html.escape(str(tooth or "Не указан").strip())
    clean_diag = html.escape(str(diagnosis or "По клинической картине").strip())
    clean_tags = html.escape(str(tags or "").strip())

    parts = [
        "[КЛИНИЧЕСКАЯ ЗАПИСЬ В МИС (IDENT / ИНФОДЕНТ / 043/У)]",
        f"ДАТА: {clean_date}",
        f"ВРАЧ: {clean_doctor}",
        f"ЗУБ: {clean_tooth}",
        f"ДИАГНОЗ: {clean_diag}",
    ]
    if clean_tags:
        parts.append(f"ТЕГИ: {clean_tags}")
    parts.append(f"ДНЕВНИК ПРИЕМА / ХОД ЛЕЧЕНИЯ:\n{clean_text}")

    if protocol_steps:
        st_lines = []
        for i, s in enumerate(protocol_steps, 1):
            st_lines.append(f"{i}. {html.escape(str(s).strip())}")
        parts.append("ЭТАПЫ:\n" + "\n".join(st_lines))

    mis_block = "\n".join(parts)
    return (
        f"📋 <b>{clean_title}</b>\n"
        f"💡 <i>1-тап копирование в медицинскую карту МИС (IDENT / Инфодент / 043/у):</i>\n\n"
        f"<code>{mis_block}</code>"
    )


async def export_bookmark_to_mis_async(bookmark_id: int, saved_by_user_id: Optional[int] = None) -> Optional[str]:
    """Асинхронно форматирует сохраненную клиническую закладку врача для вставки в один клик в МИС."""
    bm_dict = await get_clinical_bookmark_dict(bookmark_id, saved_by_user_id=saved_by_user_id)
    if not bm_dict:
        return None
    return format_clinical_note_for_mis(
        title=f"Клиническая закладка #{bm_dict['id']} ({bm_dict['sender_name']})",
        text=bm_dict["text"],
        doctor=bm_dict["sender_name"],
        date_str=str(bm_dict["date"]),
        tags=bm_dict.get("tags", "")
    )


async def search_clinical_notes(query: str, user_id: Optional[int] = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Мгновенный объединенный поиск клинических заметок, протоколов и закладок по ключевым словам.
    Возвращает структурированный перечень найденных клинических материалов со сниппетами и готовыми МИС-блоками.
    Приоритет отдается личным сохраненным закладкам врача, дополненным протоколами сообщества.
    """
    results = []
    clean_q = (query or "").strip()
    if not clean_q:
        return results

    # 1. Персональные закладки врача (наивысший приоритет)
    if user_id is not None:
        bms = await search_clinical_bookmarks(saved_by_user_id=user_id, query=clean_q, limit=limit)
        for b in bms:
            results.append({
                "source": "bookmark",
                "id": b["id"],
                "title": f"Закладка: {b['sender_name']}",
                "category": "Избранное",
                "text": b["text"],
                "author": b["sender_name"],
                "tags": b.get("tags", ""),
                "mis_export": format_clinical_note_for_mis(
                    title=f"Закладка #{b['id']}",
                    text=b["text"],
                    doctor=b["sender_name"],
                    date_str=str(b["date"]),
                    tags=b.get("tags", "")
                )
            })

    # 2. Клинические протоколы сообщества
    protos = await search_clinical_protocols(clean_q, limit=limit)
    for p in protos:
        results.append({
            "source": "protocol",
            "id": p["id"],
            "title": p["title"],
            "category": p["category"],
            "text": p.get("indication", "") + " " + p.get("key_nuances", ""),
            "author": p.get("author_doctor", ""),
            "mis_export": format_protocol_mis(p)
        })

    return results[:limit]

async def enqueue_outbox_message(chat_id: int, text: str, parse_mode: str = "html") -> int:
    """Ставит исходящее сообщение в очередь bot_outbox для гарантированной отправки активным ботом."""
    def operation():
        with _connection() as db:
            cur = db.execute(
                """
                INSERT INTO bot_outbox (chat_id, text, parse_mode, status, created_at)
                VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP)
                """,
                (chat_id, text, parse_mode)
            )
            return cur.lastrowid
    return await _run_db(operation)


async def get_pending_outbox_messages(limit: int = 5):
    """Выбирает ожидающие отправки сообщения из bot_outbox с учетом таймера retry."""
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT id, chat_id, text, parse_mode, retry_count, created_at
                FROM bot_outbox
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                ORDER BY id ASC
                LIMIT ?
                """,
                (limit,)
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "chat_id": r[1],
                    "text": r[2],
                    "parse_mode": r[3] or "html",
                    "retry_count": r[4] or 0,
                    "created_at": r[5],
                }
                for r in rows
            ]
    return await _run_db(operation)


async def mark_outbox_retry(outbox_id: int, retry_count: int, backoff_seconds: int, error_message: str):
    """Обновляет счетчик повторов и время следующей попытки для сообщения outbox при транзиентном сбое."""
    def operation():
        with _connection() as db:
            db.execute(
                """
                UPDATE bot_outbox
                SET retry_count = ?,
                    next_retry_at = datetime('now', '+' || ? || ' seconds'),
                    error_message = ?
                WHERE id = ?
                """,
                (retry_count, int(backoff_seconds), str(error_message)[:1000], outbox_id)
            )
    return await _run_db(operation)


async def mark_outbox_sent(outbox_id: int):
    """Помечает сообщение в bot_outbox как успешно доставленное."""
    def operation():
        with _connection() as db:
            db.execute(
                """
                UPDATE bot_outbox
                SET status = 'sent', sent_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (outbox_id,)
            )
    return await _run_db(operation)


async def mark_outbox_failed(outbox_id: int, error_message: str):
    """Помечает сообщение в bot_outbox как ошибочное с фиксацией причины."""
    def operation():
        with _connection() as db:
            db.execute(
                """
                UPDATE bot_outbox
                SET status = 'failed', error_message = ?
                WHERE id = ?
                """,
                (str(error_message)[:1000], outbox_id)
            )
    return await _run_db(operation)

