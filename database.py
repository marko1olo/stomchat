import logging
import sqlite3

import config


logger = logging.getLogger(__name__)


def _connect():
    db = sqlite3.connect(config.DB_PATH, timeout=30)
    db.execute("PRAGMA busy_timeout = 30000")
    return db


def _date_text(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def init_db():
    with _connect() as db:
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

        try:
            db.execute("ALTER TABLE messages ADD COLUMN media_remote_url TEXT")
            logger.info("database schema migrated: added media_remote_url")
        except sqlite3.OperationalError:
            pass


async def get_messages_for_daily_summary(start_time, end_time, min_count=100):
    with _connect() as db:
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


async def get_messages_for_range(start_dt, end_dt):
    with _connect() as db:
        return db.execute(
            """
            SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url
            FROM messages
            WHERE date >= ? AND date <= ?
            ORDER BY date ASC
            """,
            (_date_text(start_dt), _date_text(end_dt)),
        ).fetchall()


async def get_last_n_messages(limit=300):
    with _connect() as db:
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
    try:
        with _connect() as db:
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
    except Exception:
        logger.exception("database save_message failed msg_id=%s", msg_id)


async def get_unsummarized_count():
    with _connect() as db:
        row = db.execute("SELECT COUNT(*) FROM messages WHERE is_summarized = 0").fetchone()
        return row[0] if row else 0


async def get_messages_for_summary():
    with _connect() as db:
        return db.execute(
            """
            SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id
            FROM messages
            WHERE is_summarized = 0
            ORDER BY date ASC
            """
        ).fetchall()


async def get_last_msg_id():
    with _connect() as db:
        row = db.execute("SELECT MAX(msg_id) FROM messages").fetchone()
        return row[0] if row and row[0] else 0


async def get_text_by_id(msg_id):
    with _connect() as db:
        return db.execute(
            "SELECT sender_name, text FROM messages WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()


async def get_texts_by_ids(msg_ids):
    clean_ids = sorted({int(msg_id) for msg_id in msg_ids if msg_id})
    if not clean_ids:
        return {}

    placeholders = ",".join("?" for _ in clean_ids)
    with _connect() as db:
        rows = db.execute(
            f"SELECT msg_id, sender_name, text FROM messages WHERE msg_id IN ({placeholders})",
            clean_ids,
        ).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}


async def mark_messages_as_summarized(msg_ids):
    with _connect() as db:
        db.executemany(
            "UPDATE messages SET is_summarized = 1 WHERE msg_id = ?",
            [(m_id,) for m_id in msg_ids],
        )


async def delete_messages_by_ids(msg_ids):
    clean_ids = sorted({int(msg_id) for msg_id in msg_ids if msg_id})
    if not clean_ids:
        return

    with _connect() as db:
        db.executemany(
            "DELETE FROM messages WHERE msg_id = ?",
            [(m_id,) for m_id in clean_ids],
        )


async def update_media_description(msg_id, description):
    with _connect() as db:
        db.execute(
            "UPDATE messages SET media_description = ? WHERE msg_id = ?",
            (description, msg_id),
        )


async def get_messages_for_period(hours):
    with _connect() as db:
        return db.execute(
            """
            SELECT sender_name, text, media_description, date
            FROM messages
            WHERE date >= datetime('now', ?)
            ORDER BY date ASC
            """,
            (f"-{hours} hours",),
        ).fetchall()
