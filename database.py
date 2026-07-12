import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import config


logger = logging.getLogger(__name__)
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stomchat-db")

def _connect():
    db = sqlite3.connect(config.DB_PATH, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA journal_mode = WAL")
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
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _run_db(operation):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, operation)


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

            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_sent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id INTEGER UNIQUE,
                    chat_id INTEGER
                )
                """
            )

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

            try:
                db.execute("ALTER TABLE messages ADD COLUMN media_remote_url TEXT")
                logger.info("database schema migrated: added media_remote_url")
            except sqlite3.OperationalError:
                pass

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
                needed = min_count - len(total_msgs)
                old_messages = db.execute(
                    """
                    SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url
                    FROM messages
                    WHERE date < ?
                    ORDER BY date DESC
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


async def get_last_n_messages(limit=300):
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url
                FROM messages
                ORDER BY date DESC
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

    try:
        return await _run_db(operation)
    except Exception:
        logger.exception("database save_message failed msg_id=%s", msg_id)
        return None


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


async def delete_messages_by_ids(msg_ids):
    clean_ids = sorted({int(msg_id) for msg_id in msg_ids if msg_id})
    if not clean_ids:
        return

    def operation():
        with _connection() as db:
            db.executemany(
                "DELETE FROM messages WHERE msg_id = ?",
                [(m_id,) for m_id in clean_ids],
            )

    return await _run_db(operation)


async def update_media_description(msg_id, description):
    def operation():
        with _connection() as db:
            db.execute(
                "UPDATE messages SET media_description = ? WHERE msg_id = ?",
                (description, msg_id),
            )

    return await _run_db(operation)


async def get_pending_media_message_ids(limit=5):
    def operation():
        with _connection() as db:
            rows = db.execute(
                """
                SELECT msg_id, text, media_type
                FROM messages
                WHERE has_media = 1
                  AND (media_description IS NULL OR media_description = '')
                  AND date >= datetime('now', '-3 days')
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


async def get_clinical_bookmarks(saved_by_user_id, query=None):
    def operation():
        with _connection() as db:
            if query:
                return db.execute(
                    """
                    SELECT msg_id, chat_id, sender_name, text, media_description, date
                    FROM clinical_bookmarks
                    WHERE saved_by_user_id = ? AND (text LIKE ? OR media_description LIKE ?)
                    ORDER BY date DESC
                    """,
                    (saved_by_user_id, f"%{query}%", f"%{query}%"),
                ).fetchall()
            else:
                return db.execute(
                    """
                    SELECT msg_id, chat_id, sender_name, text, media_description, date
                    FROM clinical_bookmarks
                    WHERE saved_by_user_id = ?
                    ORDER BY date DESC
                    """,
                    (saved_by_user_id,),
                ).fetchall()
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


async def get_last_bot_sent_messages(count=10):
    def operation():
        with _connection() as db:
            cursor = db.execute(
                "SELECT msg_id, chat_id FROM bot_sent_messages ORDER BY id DESC LIMIT ?",
                (count,)
            )
            return cursor.fetchall()
    return await _run_db(operation)


async def remove_bot_sent_message(msg_id):
    def operation():
        with _connection() as db:
            db.execute("DELETE FROM bot_sent_messages WHERE msg_id = ?", (msg_id,))
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
                ORDER BY date DESC LIMIT ?
                """,
                (user_id, limit)
            ).fetchall()
            return [r[0] for r in rows[::-1]]
    return await _run_db(operation)


async def get_active_pm_users(days_limit=30):
    def operation():
        with _connection() as db:
            # Выбираем уникальных пользователей, которые писали боту в ЛС за последние N дней
            rows = db.execute(
                """
                SELECT DISTINCT user_id FROM pm_messages 
                WHERE date >= datetime('now', ?)
                """,
                (f"-{days_limit} days",)
            ).fetchall()
            return [r[0] for r in rows]
    return await _run_db(operation)

