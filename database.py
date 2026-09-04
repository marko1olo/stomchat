import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timezone

import config


logger = logging.getLogger(__name__)
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stomchat-db")

SAVE_RETRY_ATTEMPTS = 3
SAVE_RETRY_DELAY_SECONDS = 0.25

# Пути баз, для которых режим журнала уже выставлен в этом процессе.
#
# journal_mode = WAL — свойство ФАЙЛА, а не соединения: он записан в заголовок
# базы и переживает и закрытие соединения, и перезапуск процесса. Установка его
# на каждом открытии брала блокировку и писала в файл впустую. Замер на копии
# боевой базы, 40 открытий: 1.936 мс с установкой против 0.735 мс без неё —
# накладные 1.2 мс, в 2.6 раза, и платятся они на КАЖДОМ обращении к базе,
# включая save_message на каждом входящем сообщении. Все обращения к базе
# сериализованы через _DB_EXECUTOR с одним потоком, так что это прямая задержка
# всей работы с базой, а не параллельная.
#
# Ключ — путь, а не флаг: тесты подменяют config.DB_PATH на временные файлы, и
# каждая новая база должна получить WAL один раз. Полагаться на init_db нельзя:
# не всякий путь кода её зовёт.
_WAL_READY = set()


def _connect():
    db = sqlite3.connect(config.DB_PATH, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    if config.DB_PATH not in _WAL_READY:
        db.execute("PRAGMA journal_mode = WAL")
        _WAL_READY.add(config.DB_PATH)
    return db
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

    Сюда приходят значения двух видов, и раньше они трактовались одинаково:
      * tz-aware UTC от Telethon (save_message) — записывались верно;
      * НАИВНЫЕ локальные границы окон от планировщика (datetime.now()) —
        сравнивались с UTC-строками как есть.
    При смещении хоста UTC+4 запрошенное "вчера 20:00 — сейчас" фактически
    начиналось с сегодняшних 00:00 локального времени, и вечерние часы —
    самые активные в этом чате — не попадали ни в один дайджест: ни во
    вчерашний, ни в сегодняшний. Терялись безвозвратно.

    astimezone() наивное значение считает локальным, а tz-aware корректно
    переводит, поэтому одна ветка покрывает оба случая.
    """
    return dt.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


async def _run_db(operation):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, operation)


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
                    date TEXT
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
                except sqlite3.OperationalError:
                    pass

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

    return await _run_db(operation)


async def get_messages_for_daily_summary(start_time, end_time, min_count=100):
    def operation():
        with _connection() as db:
            period_messages = db.execute(
                """
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url
                FROM messages
                WHERE date >= ? AND date <= ?
                ORDER BY date ASC
                """,
                (_date_text(start_time), _date_text(end_time)),
            ).fetchall()

            total_msgs = list(period_messages)
            if len(total_msgs) < min_count:
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
                    SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url
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
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url
                FROM messages
                WHERE date >= ? AND date <= ?
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
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url
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
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url
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
        placeholders = ",".join("?" for _ in clean_ids)
        with _connection() as db:
            rows = db.execute(
                f"SELECT msg_id, sender_name, text FROM messages WHERE msg_id IN ({placeholders})",
                clean_ids,
            ).fetchall()
            return {row[0]: (row[1], row[2]) for row in rows}

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


async def update_media_description(msg_id, description):
    def operation():
        with _connection() as db:
            db.execute(
                "UPDATE messages SET media_description = ? WHERE msg_id = ?",
                (description, msg_id),
            )

    return await _run_db(operation)


async def update_media_remote_url(msg_id, url):
    def operation():
        with _connection() as db:
            db.execute(
                "UPDATE messages SET media_remote_url = ? WHERE msg_id = ?",
                (url, msg_id),
            )

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


async def get_media_description(msg_id):
    def operation():
        with _connection() as db:
            row = db.execute("SELECT media_description FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
            return row[0] if row else None
    return await _run_db(operation)


async def save_clinical_bookmark(saved_by_user_id, msg_id, chat_id, sender_name, text, has_media, media_description, date):
    def operation():
        with _connection() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO clinical_bookmarks
                (saved_by_user_id, msg_id, chat_id, sender_name, text, has_media, media_description, date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (saved_by_user_id, msg_id, chat_id, sender_name, text, has_media, media_description, _date_text(date) if hasattr(date, 'strftime') else str(date)),
            )
    return await _run_db(operation)


async def get_clinical_bookmarks(saved_by_user_id, query=None, limit=None, offset=0):
    """
    Закладки врача, свежие сверху.

    ORDER BY получил добор по id. Дата закладки — секунды, а сохраняют пачкой,
    отвечая на серию постов: реплики одной секунды выстраивались произвольно, и
    при постраничном выводе (/bookmarks листает по 10) одна и та же закладка
    могла показаться на двух страницах подряд, а соседняя — ни на одной. Порядок
    внутри секунды по id совпадает с порядком сохранения.

    limit/offset добавлены для постраничного вывода: сейчас вызывающая сторона
    забирает ВСЕ закладки пользователя и режет их в Python, то есть на каждый
    /bookmarks поднимает из базы весь личный архив ради 10 строк. По умолчанию
    limit=None — поведение прежнее, чтобы правка не требовала одновременного
    изменения вызова.
    """
    def operation():
        with _connection() as db:
            # LIMIT -1 — способ SQLite сказать «без ограничения», он же
            # обязателен, если нужен OFFSET: голый OFFSET синтаксис не
            # принимает.
            window = "LIMIT ? OFFSET ?"
            bounds = (-1 if limit is None else int(limit), int(offset or 0))
            if query:
                # Поиск идёт и по автору. Раньше искали только в тексте и в
                # описании снимка, поэтому «тот пост Иванова» найти было нельзя
                # — а по автору коллеги вспоминают сохранённое ничуть не реже,
                # чем по словам.
                like = f"%{query}%"
                return db.execute(
                    f"""
                    SELECT msg_id, chat_id, sender_name, text, media_description, date
                    FROM clinical_bookmarks
                    WHERE saved_by_user_id = ?
                      AND (text LIKE ? OR media_description LIKE ? OR sender_name LIKE ?)
                    ORDER BY date DESC, id DESC
                    {window}
                    """,
                    (saved_by_user_id, like, like, like) + bounds,
                ).fetchall()
            else:
                return db.execute(
                    f"""
                    SELECT msg_id, chat_id, sender_name, text, media_description, date
                    FROM clinical_bookmarks
                    WHERE saved_by_user_id = ?
                    ORDER BY date DESC, id DESC
                    {window}
                    """,
                    (saved_by_user_id,) + bounds,
                ).fetchall()
    return await _run_db(operation)


async def count_clinical_bookmarks(saved_by_user_id, query=None):
    """
    Сколько всего закладок подходит под условие.

    Нужно вместе с limit/offset: /bookmarks печатает «страница X из Y» и без
    общего числа страниц вызывающая сторона снова вынуждена тянуть все строки.
    Условие ОБЯЗАНО повторять get_clinical_bookmarks — иначе счётчик страниц
    разойдётся с содержимым.
    """
    def operation():
        with _connection() as db:
            if query:
                like = f"%{query}%"
                row = db.execute(
                    """
                    SELECT COUNT(*) FROM clinical_bookmarks
                    WHERE saved_by_user_id = ?
                      AND (text LIKE ? OR media_description LIKE ? OR sender_name LIKE ?)
                    """,
                    (saved_by_user_id, like, like, like),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM clinical_bookmarks WHERE saved_by_user_id = ?",
                    (saved_by_user_id,),
                ).fetchone()
            return row[0] if row else 0
    return await _run_db(operation)


async def set_user_interactive_state(user_id, state_type, current_step, case_id, history):
    def operation():
        with _connection() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO user_interactive_states
                (user_id, state_type, current_step, case_id, history)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, state_type, current_step, case_id, history),
            )
    return await _run_db(operation)


async def get_user_interactive_state(user_id):
    def operation():
        with _connection() as db:
            row = db.execute(
                "SELECT state_type, current_step, case_id, history FROM user_interactive_states WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                return {
                    "state_type": row[0],
                    "current_step": row[1],
                    "case_id": row[2],
                    "history": row[3]
                }
            return None
    return await _run_db(operation)


async def clear_user_interactive_state(user_id):
    def operation():
        with _connection() as db:
            db.execute(
                "DELETE FROM user_interactive_states WHERE user_id = ?",
                (user_id,),
            )
    return await _run_db(operation)


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


async def save_pm_message(user_id, sender_name, text):
    def operation():
        with _connection() as db:
            db.execute(
                "INSERT INTO pm_messages (user_id, sender_name, text) VALUES (?, ?, ?)",
                (user_id, sender_name, text)
            )
    return await _run_db(operation)


async def get_last_pm_messages(user_id, limit=25):
    def operation():
        with _connection() as db:
            cursor = db.execute(
                "SELECT sender_name, text FROM pm_messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit)
            )
            rows = cursor.fetchall()
            return [{"sender_name": row[0], "text": row[1]} for row in reversed(rows)]
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
    # Лимит объема на одного пользователя в БД: строго до 64 КБ для ЛС и до 8 КБ для беседы
    if clinical_summary and len(clinical_summary) > 64000:
        clinical_summary = clinical_summary[:64000]
    if group_summary and len(group_summary) > 8000:
        group_summary = group_summary[:8000]
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
                new_pm_cnt = pm_message_count if pm_message_count is not None else (existing[5] or 0)
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


async def get_unprocessed_group_users(min_new_messages=3, limit=10):
    """
    Возвращает список пользователей, у которых в messages накопились новые сообщения
    для обновления памяти беседы (group_summary).
    """
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT m.sender_id, m.sender_name, m.sender_username,
                       COUNT(m.msg_id) as cnt, MAX(m.msg_id) as max_id
                FROM messages m
                LEFT JOIN user_memories um ON um.user_id = m.sender_id
                WHERE m.sender_id IS NOT NULL AND m.sender_id != 0
                  AND m.text IS NOT NULL AND LENGTH(TRIM(m.text)) > 15
                  AND m.msg_id > COALESCE(um.last_group_analyzed_id, 0)
                GROUP BY m.sender_id
                HAVING cnt >= ?
                ORDER BY max_id DESC
                LIMIT ?
                """,
                (min_new_messages, limit)
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



