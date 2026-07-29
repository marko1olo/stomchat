"""
Подъём на боевой схеме: init_db против базы, в которой лежит только messages.

Именно так выглядит живая база: на 27.07.2026 в stomat_bot.db существует ровно
одна таблица — messages с 32 883 строками. Все остальные (закладки, состояния
симулятора, история ЛС, профили, учёт исходящих) создаются при старте. Значит
на следующем запуске исполняется миграция, которую до сих пор никто не
проверял, — а её отказ означает бота, который не работает после рестарта.

Проверка идёт на СИНТЕТИЧЕСКОЙ базе с боевой формой схемы, боевой файл не
открывается.

Запуск: python test_schema_migration.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_schema_")
config.DB_PATH = os.path.join(_TMPDIR, "legacy.db")

import database

PASS, FAIL = [], []

EXPECTED_TABLES = {
    "messages", "clinical_bookmarks", "user_interactive_states",
    "bot_sent_messages", "pm_messages", "user_profiles",
}
EXPECTED_INDEXES = {"idx_date", "idx_sender", "idx_bookmark_user", "idx_pm_user"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def build_legacy_db(path, rows=50):
    """База в том виде, в каком она сейчас в бою: одна таблица messages."""
    db = sqlite3.connect(path)
    db.execute(
        """
        CREATE TABLE messages (
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
    for i in range(rows):
        db.execute(
            "INSERT INTO messages (msg_id, sender_id, sender_name, text, date) VALUES (?,?,?,?,?)",
            (1000 + i, 555, "Врач", f"сообщение {i}", "2026-06-20 12:00:00"),
        )
    db.commit()
    db.close()


def objects(path, kind):
    db = sqlite3.connect(path)
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'", (kind,))}
    db.close()
    return names


async def run():
    path = config.DB_PATH
    build_legacy_db(path, rows=50)

    print("\n[1] Исходное состояние повторяет боевое")
    check("до миграции есть только messages", objects(path, "table") == {"messages"},
          f"got {objects(path, 'table')}")

    print("\n[2] init_db на такой базе доходит до конца")
    failure = None
    try:
        await database.init_db()
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    check("миграция не упала", failure is None, f"got {failure}")

    print("\n[3] Создано всё, от чего зависят команды бота")
    tables = objects(path, "table")
    for table in sorted(EXPECTED_TABLES):
        check(f"таблица {table}", table in tables, f"есть только {sorted(tables)}")

    print("\n[4] Индексы на месте")
    indexes = objects(path, "index")
    for index in sorted(EXPECTED_INDEXES):
        check(f"индекс {index}", index in indexes, f"есть только {sorted(indexes)}")

    print("\n[5] Данные не потеряны и не переписаны")
    db = sqlite3.connect(path)
    count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    sample = db.execute("SELECT text FROM messages WHERE msg_id = 1007").fetchone()
    db.close()
    check("все 50 строк на месте", count == 50, f"got {count}")
    check("текст не изменился", sample and sample[0] == "сообщение 7", f"got {sample}")

    print("\n[6] Повторный запуск идемпотентен")
    failure = None
    try:
        await database.init_db()
        await database.init_db()
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    check("три запуска подряд без ошибок", failure is None, f"got {failure}")
    db = sqlite3.connect(path)
    check("строки не размножились", db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 50)
    db.close()

    print("\n[7] После миграции работают операции, которым нужны новые таблицы")
    await database.save_bot_sent_message(9001, -1001234567890)
    check("учёт исходящих пишется",
          9001 in {r[0] for r in await database.get_last_bot_sent_messages(count=5)})

    await database.save_pm_message(777, "User", "вопрос в личке")
    history = await database.get_last_pm_messages(777, limit=5)
    check("история ЛС пишется и читается", len(history) == 1, f"got {history}")

    await database.set_user_interactive_state(777, "case", 1, "dynamic", "[]")
    state = await database.get_user_interactive_state(777)
    check("состояние симулятора пишется и читается",
          state is not None and state["current_step"] == 1, f"got {state}")

    await database.save_clinical_bookmark(
        saved_by_user_id=777, msg_id=1007, chat_id=-1001234567890,
        sender_name="Врач", text="сообщение 7", has_media=False,
        media_description="", date=datetime(2026, 6, 20, tzinfo=timezone.utc),
    )
    bookmarks = await database.get_clinical_bookmarks(777)
    check("закладка сохраняется", len(bookmarks) == 1, f"got {bookmarks}")

    print("\n[8] Уникальность закладок обеспечена после миграции")
    await database.save_clinical_bookmark(
        saved_by_user_id=777, msg_id=1007, chat_id=-1001234567890,
        sender_name="Врач", text="сообщение 7", has_media=False,
        media_description="", date=datetime(2026, 6, 20, tzinfo=timezone.utc),
    )
    bookmarks = await database.get_clinical_bookmarks(777)
    check("повторный /save не создаёт дубль", len(bookmarks) == 1, f"got {len(bookmarks)}")

    print("\n[9] Уже накопленные дубли закладок схлопываются при миграции")
    dup_path = os.path.join(_TMPDIR, "dups.db")
    build_legacy_db(dup_path, rows=5)
    db = sqlite3.connect(dup_path)
    db.execute(
        """CREATE TABLE clinical_bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, saved_by_user_id INTEGER, msg_id INTEGER,
            chat_id INTEGER, sender_name TEXT, text TEXT, has_media BOOLEAN,
            media_description TEXT, date TEXT)"""
    )
    for _ in range(3):
        db.execute(
            "INSERT INTO clinical_bookmarks (saved_by_user_id, msg_id, text) VALUES (1, 42, 'дубль')"
        )
    db.commit()
    db.close()

    config.DB_PATH = dup_path
    await database.init_db()
    db = sqlite3.connect(dup_path)
    left = db.execute("SELECT COUNT(*) FROM clinical_bookmarks WHERE msg_id = 42").fetchone()[0]
    has_unique = "idx_bookmark_unique" in {
        r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    db.close()
    check("из трёх дублей осталась одна запись", left == 1, f"got {left}")
    check("уникальный индекс поставлен", has_unique)


try:
    asyncio.run(run())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
