"""
Схема и запросы database.py: индексы, ключи чатов, границы окон.

Что здесь проверяется и почему это не абстракция.

  * Индекса на messages.reply_to_msg_id не было, а ответы читаются по родителю:
    на каждое сообщение-ответ под медиа-постом идёт COUNT(*) по этой колонке и
    выборка ветки (msg_id = ? OR reply_to_msg_id = ?). Пока проиндексирована
    одна колонка из двух, OR-план по индексам недостижим — только полный скан.
  * ORDER BY date без добора по msg_id: колонка хранит СЕКУНДЫ, и на копии
    боевой базы 2 727 сообщений из 32 883 делят секунду с другим. Граница LIMIT
    попадала внутрь такой группы в 5.5% окон, выбор внутри секунды был
    произволен — окно теряло более раннюю реплику и держало более позднюю.
  * bot_sent_messages ключевалась одним msg_id, хотя бот работает в двух чатах
    и номера уникальны только внутри чата: INSERT OR REPLACE молча стирал учёт
    сообщения другого чата, и /wipe там уже не мог его удалить.
  * Уникальность закладок игнорировала chat_id при наличии колонки chat_id.
  * Выборка неразобранных снимков была ограничена тремя днями, поэтому
    обещание «непоставленное подберёт recover_pending_media_analysis» не
    работало: на копии боевой базы 745 снимков без описания и НОЛЬ из них в
    окне.

Боевые файлы не открываются на запись: база — во временном каталоге, копия
боевой базы тоже. Журнал уведён через STOMCHAT_LOG_PATH до импорта модулей.

Запуск: python test_fix_db.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_REPO = os.path.dirname(os.path.abspath(__file__))
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_fixdb_")

# Журнал — во временный каталог ДО импорта модулей проекта: runtime_guard
# выбирает файл по имени точки входа один раз при импорте, и без этой строки
# запуск из другого каталога мог бы дописать боевой bot.log.
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")
# .env лежит рядом с config.py; переход в каталог репозитория делает запуск
# независимым от того, откуда вызвали python.
os.chdir(_REPO)

import config

config.DB_PATH = os.path.join(_TMPDIR, "fixdb.db")

import database

PASS, FAIL = [], []

MAIN_CHAT = -1001820467444
TEST_CHAT = -1009999888777
PM_CHAT = 555000111
USER = 777


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def note(text):
    print(f"         {text}")


def plan_of(path, sql, args=()):
    db = sqlite3.connect(path)
    try:
        return " | ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args))
    finally:
        db.close()


def bench_ms(path, sql, args=(), reps=30):
    db = sqlite3.connect(path)
    try:
        db.execute(sql, args).fetchall()
        start = time.perf_counter()
        for _ in range(reps):
            db.execute(sql, args).fetchall()
        return (time.perf_counter() - start) / reps * 1000
    finally:
        db.close()


def index_columns(path, index_name):
    db = sqlite3.connect(path)
    try:
        return [row[2] for row in db.execute(f"PRAGMA index_info('{index_name}')")]
    finally:
        db.close()


def unique_key_columns(path, table):
    """Колонки уникальных индексов таблицы — так виден фактический ключ."""
    db = sqlite3.connect(path)
    try:
        keys = []
        for row in db.execute(f"PRAGMA index_list('{table}')"):
            if row[2]:
                keys.append([info[2] for info in db.execute(f"PRAGMA index_info('{row[1]}')")])
        return keys
    finally:
        db.close()


def object_sql(path, name):
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        return (row[0] if row and row[0] else "")
    finally:
        db.close()


def names_of(path, kind):
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", (kind,))}
    finally:
        db.close()


def fresh_db(name):
    """Новая пустая база во временном каталоге; database.py читает config.DB_PATH."""
    path = os.path.join(_TMPDIR, name)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    config.DB_PATH = path
    return path


REPLY_COUNT_SQL = "SELECT COUNT(*) FROM messages WHERE reply_to_msg_id = ?"
THREAD_SQL = (
    "SELECT sender_name, text, msg_id, reply_to_msg_id FROM messages "
    "WHERE msg_id = ? OR reply_to_msg_id = ? ORDER BY date ASC"
)
WIPE_SQL = "SELECT msg_id, chat_id FROM bot_sent_messages WHERE chat_id = ? ORDER BY id DESC LIMIT 10"


async def section_indexes():
    print("\n[1] init_db на чистой базе создаёт индексы и ключи, на которых стоят запросы")
    path = fresh_db("schema.db")
    await database.init_db()

    indexes = names_of(path, "index")
    check("индекс messages.reply_to_msg_id", "idx_reply_to" in indexes, f"есть {sorted(indexes)}")
    check("индекс bot_sent_messages.chat_id", "idx_bot_sent_chat" in indexes, f"есть {sorted(indexes)}")
    check("прежние индексы не потеряны", {"idx_date", "idx_sender", "idx_bookmark_user"} <= indexes,
          f"есть {sorted(indexes)}")
    check("idx_reply_to стоит именно на reply_to_msg_id",
          index_columns(path, "idx_reply_to") == ["reply_to_msg_id"],
          f"got {index_columns(path, 'idx_reply_to')}")
    check("idx_bot_sent_chat начинается с chat_id",
          index_columns(path, "idx_bot_sent_chat")[:1] == ["chat_id"],
          f"got {index_columns(path, 'idx_bot_sent_chat')}")

    keys = unique_key_columns(path, "bot_sent_messages")
    check("учёт исходящих ключуется парой (msg_id, chat_id)",
          ["msg_id", "chat_id"] in keys and ["msg_id"] not in keys, f"got {keys}")
    check("уникальность закладок учитывает chat_id",
          "chat_id" in object_sql(path, "idx_bookmark_unique"),
          f"got {object_sql(path, 'idx_bookmark_unique')}")
    check("промежуточная таблица пересборки не остаётся",
          "bot_sent_messages_rebuild" not in names_of(path, "table"))

    print("\n[2] Три запуска init_db подряд ничего не ломают")
    failure = None
    try:
        await database.init_db()
        await database.init_db()
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    check("повторные запуски без ошибок", failure is None, f"got {failure}")
    keys = unique_key_columns(path, "bot_sent_messages")
    check("ключ пары не откатился к msg_id", ["msg_id"] not in keys, f"got {keys}")
    check("уникальный индекс закладок на месте",
          "idx_bookmark_unique" in names_of(path, "index"))


async def section_reply_index():
    print("\n[3] План запросов по ветке ответов: скан против поиска по индексу")
    path = fresh_db("replies.db")
    await database.init_db()

    base = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    parent_id = 1000
    await database.save_message(parent_id, 5, "Врач", "vrach", "снимок до лечения",
                                base, has_media=True, media_type="photo")
    for i in range(1, 3000):
        await database.save_message(
            1000 + i, 5 + (i % 40), f"Врач{i % 40}", None, f"реплика {i}",
            base + timedelta(seconds=i),
            reply_to_msg_id=parent_id if i % 3 == 0 else None,
        )

    before_path = os.path.join(_TMPDIR, "replies_noindex.db")
    shutil.copyfile(path, before_path)
    drop = sqlite3.connect(before_path)
    drop.execute("DROP INDEX idx_reply_to")
    drop.commit()
    drop.close()

    before_count = plan_of(before_path, REPLY_COUNT_SQL, (parent_id,))
    after_count = plan_of(path, REPLY_COUNT_SQL, (parent_id,))
    before_thread = plan_of(before_path, THREAD_SQL, (parent_id, parent_id))
    after_thread = plan_of(path, THREAD_SQL, (parent_id, parent_id))
    note(f"COUNT до:    {before_count}  ({bench_ms(before_path, REPLY_COUNT_SQL, (parent_id,)):.3f} мс)")
    note(f"COUNT после: {after_count}  ({bench_ms(path, REPLY_COUNT_SQL, (parent_id,)):.3f} мс)")
    note(f"ветка до:    {before_thread}  ({bench_ms(before_path, THREAD_SQL, (parent_id, parent_id)):.3f} мс)")
    note(f"ветка после: {after_thread}  ({bench_ms(path, THREAD_SQL, (parent_id, parent_id)):.3f} мс)")

    check("без индекса COUNT по ответам это полный скан", "SCAN messages" in before_count,
          f"got {before_count}")
    check("с индексом COUNT ищет по idx_reply_to",
          "SEARCH" in after_count and "idx_reply_to" in after_count, f"got {after_count}")
    check("без индекса выборка ветки сканирует таблицу", "SCAN messages" in before_thread,
          f"got {before_thread}")
    check("с индексом выборка ветки идёт OR-планом по двум индексам",
          "MULTI-INDEX OR" in after_thread and "SCAN messages" not in after_thread,
          f"got {after_thread}")

    rows = sqlite3.connect(path).execute(REPLY_COUNT_SQL, (parent_id,)).fetchone()[0]
    check("индекс не изменил результат запроса", rows == 999, f"got {rows}")


async def section_window_ties():
    print("\n[4] Окно последних сообщений: одна секунда — не повод терять реплику")
    path = fresh_db("ties.db")
    await database.init_db()

    base = datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc)
    for i in range(20):
        await database.save_message(400 + i, 5, "Врач", None, f"фон {i}",
                                    base + timedelta(seconds=i))

    # Шесть реплик в ОДНУ секунду, записанные не по возрастанию номера: именно
    # так приходит живой чат (Telethon отдаёт события пачкой, догоняющая
    # синхронизация — вообще в своём порядке). Порядок вставки специально не
    # совпадает с порядком msg_id, иначе дефект не виден: без добора по msg_id
    # SQLite отдаёт строки в порядке rowid, то есть в порядке ЗАПИСИ.
    tie_second = base + timedelta(seconds=100)
    for msg_id in (503, 500, 505, 501, 504, 502):
        await database.save_message(msg_id, 5, "Врач", None, f"реплика {msg_id}", tie_second)

    window = await database.get_last_n_messages(limit=3)
    ids = [row[0] for row in window]
    check("на границе окна остаются три ПОСЛЕДНИЕ реплики секунды", ids == [503, 504, 505],
          f"got {ids}")

    window = await database.get_last_n_messages(limit=6)
    ids = [row[0] for row in window]
    check("всё содержимое секунды идёт по возрастанию номера",
          ids == [500, 501, 502, 503, 504, 505], f"got {ids}")

    window = await database.get_last_n_messages(limit=4)
    ids = [row[0] for row in window]
    check("окно на 4 не пропускает 502 и не тянет 501", ids == [502, 503, 504, 505], f"got {ids}")

    window = await database.get_last_n_messages(limit=8)
    ids = [row[0] for row in window]
    check("сообщения до спорной секунды идут первыми и в порядке",
          ids[:2] == [418, 419] and ids[2:] == [500, 501, 502, 503, 504, 505], f"got {ids}")

    # Добор из прошлого в дневной сводке режется тем же LIMIT.
    old = await database.get_messages_for_daily_summary(
        base + timedelta(seconds=200), base + timedelta(seconds=300), min_count=3)
    ids = [row[0] for row in old]
    check("добор дневной сводки тоже режет по (date, msg_id)", ids == [503, 504, 505], f"got {ids}")

    portrait = await database.get_user_recent_group_messages(5, limit=3)
    check("выборка для портрета врача упорядочена детерминированно",
          portrait == ["реплика 503", "реплика 504", "реплика 505"], f"got {portrait}")


async def section_bot_sent_two_chats():
    print("\n[5] Учёт исходящих: одинаковый номер в двух чатах — две записи")
    path = fresh_db("botsent.db")
    await database.init_db()

    await database.save_bot_sent_message(4821, MAIN_CHAT)
    await database.save_bot_sent_message(4821, TEST_CHAT)
    await database.save_bot_sent_message(4822, MAIN_CHAT)

    main_ids = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=MAIN_CHAT)}
    test_ids = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=TEST_CHAT)}
    check("сообщение основного чата не стёрто записью тестового", 4821 in main_ids, f"got {main_ids}")
    check("сообщение тестового чата тоже учтено", test_ids == {4821}, f"got {test_ids}")

    await database.save_bot_sent_message(4821, MAIN_CHAT)
    main_rows = [r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=MAIN_CHAT)]
    check("повторная регистрация той же пары не даёт дубль",
          main_rows.count(4821) == 1, f"got {main_rows}")

    await database.remove_bot_sent_message(4821, chat_id=TEST_CHAT)
    main_ids = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=MAIN_CHAT)}
    test_ids = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=TEST_CHAT)}
    check("снятие с учёта в одном чате не задело другой",
          4821 in main_ids and not test_ids, f"main {main_ids} test {test_ids}")

    print("\n[6] /wipe выбирает свои сообщения по индексу, а не сканом")
    db = sqlite3.connect(path)
    db.executemany("INSERT OR REPLACE INTO bot_sent_messages (msg_id, chat_id) VALUES (?, ?)",
                   [(9000 + i, PM_CHAT + (i % 200)) for i in range(4000)])
    db.commit()
    db.close()
    after = plan_of(path, WIPE_SQL, (PM_CHAT,))
    note(f"выборка /wipe: {after}  ({bench_ms(path, WIPE_SQL, (PM_CHAT,)):.3f} мс)")
    check("выборка по чату использует idx_bot_sent_chat",
          "idx_bot_sent_chat" in after and "SCAN bot_sent_messages" not in after, f"got {after}")


async def section_bot_sent_migration():
    print("\n[7] Миграция боевой формы учёта исходящих: данные и порядок сохранены")
    path = fresh_db("legacy_botsent.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE bot_sent_messages "
               "(id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER UNIQUE, chat_id INTEGER)")
    db.executemany("INSERT INTO bot_sent_messages (id, msg_id, chat_id) VALUES (?, ?, ?)",
                   [(1, 4821, MAIN_CHAT), (2, 4822, MAIN_CHAT), (3, 7777, PM_CHAT)])
    db.commit()
    db.close()
    check("до миграции ключ — один msg_id",
          ["msg_id"] in unique_key_columns(path, "bot_sent_messages"),
          f"got {unique_key_columns(path, 'bot_sent_messages')}")

    failure = None
    try:
        await database.init_db()
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    check("init_db на такой базе доходит до конца", failure is None, f"got {failure}")

    keys = unique_key_columns(path, "bot_sent_messages")
    check("после миграции ключ — пара",
          ["msg_id", "chat_id"] in keys and ["msg_id"] not in keys, f"got {keys}")

    db = sqlite3.connect(path)
    rows = db.execute("SELECT id, msg_id, chat_id FROM bot_sent_messages ORDER BY id").fetchall()
    db.close()
    check("все три записи перенесены",
          rows == [(1, 4821, MAIN_CHAT), (2, 4822, MAIN_CHAT), (3, 7777, PM_CHAT)], f"got {rows}")
    check("id не перенумерованы: /wipe берёт последние по id",
          [r[0] for r in rows] == [1, 2, 3], f"got {rows}")
    check("промежуточная таблица удалена",
          "bot_sent_messages_rebuild" not in names_of(path, "table"))

    await database.save_bot_sent_message(4821, TEST_CHAT)
    main_ids = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=MAIN_CHAT)}
    check("после миграции запись из второго чата не вытесняет первый",
          4821 in main_ids, f"got {main_ids}")
    db = sqlite3.connect(path)
    new_id = db.execute("SELECT id FROM bot_sent_messages WHERE msg_id = 4821 AND chat_id = ?",
                        (TEST_CHAT,)).fetchone()
    db.close()
    check("новая запись получает id больше перенесённых", new_id and new_id[0] > 3, f"got {new_id}")

    await database.init_db()
    db = sqlite3.connect(path)
    total = db.execute("SELECT COUNT(*) FROM bot_sent_messages").fetchone()[0]
    db.close()
    check("повторный init_db не пересобирает таблицу снова и не теряет строки",
          total == 4, f"got {total}")


async def section_bookmarks():
    print("\n[8] Закладки: ключ с chat_id и постраничный вывод")
    path = fresh_db("bookmarks.db")
    await database.init_db()

    saved_at = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    await database.save_clinical_bookmark(USER, 4821, MAIN_CHAT, "Иванов", "вертипреп",
                                          False, "", saved_at)
    await database.save_clinical_bookmark(USER, 4821, TEST_CHAT, "Петров", "другой пост",
                                          False, "", saved_at)
    rows = await database.get_clinical_bookmarks(USER)
    chats = sorted(r[1] for r in rows)
    check("один номер из двух чатов — две разные закладки",
          len(rows) == 2 and chats == sorted([MAIN_CHAT, TEST_CHAT]), f"got {rows}")

    await database.save_clinical_bookmark(USER, 4821, MAIN_CHAT, "Иванов", "вертипреп",
                                          False, "", saved_at)
    rows = await database.get_clinical_bookmarks(USER)
    check("повторный /save на тот же пост дубля не даёт", len(rows) == 2, f"got {len(rows)}")

    # 25 закладок в ОДНУ секунду: так выходит, когда врач листает архив и
    # сохраняет серию постов подряд.
    for i in range(25):
        await database.save_clinical_bookmark(USER, 5000 + i, MAIN_CHAT, f"Автор{i}",
                                              f"закладка {i}", False, "", saved_at)
    total = await database.count_clinical_bookmarks(USER)
    check("счётчик закладок считает всё", total == 27, f"got {total}")

    page1 = await database.get_clinical_bookmarks(USER, limit=10)
    page2 = await database.get_clinical_bookmarks(USER, limit=10, offset=10)
    page3 = await database.get_clinical_bookmarks(USER, limit=10, offset=20)
    check("страница отдаёт ровно 10 строк", len(page1) == 10 and len(page2) == 10, f"{len(page1)}/{len(page2)}")
    check("последняя страница — остаток", len(page3) == 7, f"got {len(page3)}")
    # Сравнивать по одному msg_id нельзя: ключ закладки — пара (msg_id, chat_id),
    # и двумя проверками выше этот же тест утверждает, что пост #4821 основного
    # чата и #4821 тестового — две РАЗНЫЕ закладки. По одному номеру они
    # выглядели дублем, и проверка падала на верном коде.
    seen = [(r[0], r[1]) for r in page1 + page2 + page3]
    check("страницы не повторяют и не пропускают закладки",
          len(set(seen)) == 27 == len(seen), f"got {len(seen)} строк, уникальных {len(set(seen))}")

    everything = await database.get_clinical_bookmarks(USER)
    check("без limit поведение прежнее — весь архив", len(everything) == 27, f"got {len(everything)}")
    check("порядок постраничного вывода совпадает с полным списком",
          [(r[0], r[1]) for r in everything] == seen, "страницы разошлись с полным списком")

    found = await database.get_clinical_bookmarks(USER, query="Автор7")
    check("поиск по автору работает и с новым ORDER BY", len(found) == 1, f"got {found}")
    check("счётчик найденного совпадает с поиском",
          await database.count_clinical_bookmarks(USER, query="Автор7") == 1)
    found_page = await database.get_clinical_bookmarks(USER, query="закладка", limit=5)
    check("поиск тоже умеет страницы", len(found_page) == 5, f"got {len(found_page)}")
    check("счётчик поиска видит все совпадения",
          await database.count_clinical_bookmarks(USER, query="закладка") == 25,
          f"got {await database.count_clinical_bookmarks(USER, query='закладка')}")

    print("\n[9] Миграция уникальности закладок на существующих данных")
    path = fresh_db("legacy_bookmarks.db")
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE clinical_bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, saved_by_user_id INTEGER, msg_id INTEGER,
        chat_id INTEGER, sender_name TEXT, text TEXT, has_media BOOLEAN,
        media_description TEXT, date TEXT)""")
    # Уникального индекса здесь НЕ создаём: смысл проверки — база из времён,
    # когда уникальности не было вовсе и дубли накапливались свободно.
    # Прежняя версия установки создавала индекс (saved_by_user_id, msg_id) и
    # тут же пыталась вставить три одинаковых дубля — то есть падала на своём
    # же ограничении, ещё не дойдя до проверки миграции.
    db.execute("INSERT INTO clinical_bookmarks (saved_by_user_id, msg_id, chat_id, text, date) "
               "VALUES (?, ?, ?, 'старая закладка', '2026-05-01 10:00:00')",
               (USER, 4821, MAIN_CHAT))
    # Дубли из времён, когда уникальности не было вовсе, и chat_id ещё не писался.
    for _ in range(3):
        db.execute("INSERT INTO clinical_bookmarks (saved_by_user_id, msg_id, text, date) "
                   "VALUES (1, 42, 'дубль', '2026-04-01 10:00:00')")
    db.commit()
    db.close()

    await database.init_db()
    check("старое двухколоночное определение индекса заменено",
          "chat_id" in object_sql(path, "idx_bookmark_unique"),
          f"got {object_sql(path, 'idx_bookmark_unique')}")
    db = sqlite3.connect(path)
    kept = db.execute("SELECT COUNT(*) FROM clinical_bookmarks WHERE msg_id = 4821").fetchone()[0]
    dups = db.execute("SELECT COUNT(*) FROM clinical_bookmarks WHERE msg_id = 42").fetchone()[0]
    db.close()
    check("сохранённая закладка миграцию переживает", kept == 1, f"got {kept}")
    check("дубли без chat_id по-прежнему схлопываются в одну", dups == 1, f"got {dups}")

    await database.save_clinical_bookmark(USER, 4821, TEST_CHAT, "Петров", "тот же номер",
                                          False, "", datetime(2026, 7, 2, tzinfo=timezone.utc))
    rows = await database.get_clinical_bookmarks(USER)
    check("после миграции закладка из второго чата сохраняется", len(rows) == 2, f"got {rows}")


async def section_pending_media():
    print("\n[10] Неразобранные снимки: подбирается всё, а не последние три дня")
    path = fresh_db("media.db")
    await database.init_db()

    now = datetime.now(timezone.utc)
    old_ids = []
    for i, age_days in enumerate((120, 60, 30, 10, 4)):
        msg_id = 200 + i
        old_ids.append(msg_id)
        await database.save_message(msg_id, 5, "Врач", None, f"снимок {age_days} дней назад",
                                    now - timedelta(days=age_days), has_media=True,
                                    media_type="photo")
    await database.save_message(300, 5, "Врач", None, "свежий снимок", now - timedelta(hours=2),
                                has_media=True, media_type="photo")
    await database.save_message(301, 5, "Врач", None, "уже разобран", now - timedelta(days=50),
                                has_media=True, media_type="photo")
    await database.update_media_description(301, "рентген, 36 зуб")
    await database.save_message(302, 5, "Врач", None, "разбор провалился",
                                now - timedelta(days=50), has_media=True, media_type="photo")
    await database.update_media_description(302, "[медиа — ошибка анализа]")
    await database.save_message(303, 5, "Врач", None, "текст без медиа", now - timedelta(days=40))

    pending = await database.get_pending_media_message_ids(limit=20)
    ids = [row[0] for row in pending]
    check("снимок 120-дневной давности без описания попадает в выборку", 200 in ids, f"got {ids}")
    check("подбираются все неразобранные, независимо от возраста",
          set(ids) == set(old_ids) | {300}, f"got {sorted(ids)}")
    check("разобранный снимок не возвращается", 301 not in ids, f"got {ids}")
    check("помеченный неудачей не крутится в очереди вечно", 302 not in ids, f"got {ids}")
    check("сообщение без медиа не попадает", 303 not in ids, f"got {ids}")

    pending = await database.get_pending_media_message_ids(limit=2)
    ids = [row[0] for row in pending]
    check("свежие снимки идут первыми: их ещё обсуждают", ids == [300, 204], f"got {ids}")
    check("текст и тип медиа отдаются вместе с номером",
          pending[0][1] == "свежий снимок" and pending[0][2] == "photo", f"got {pending[0]}")


async def section_production_copy():
    print("\n[11] init_db на КОПИИ боевой базы: данные целы, план запросов меняется")
    source = os.path.join(_REPO, "stomat_bot.db")
    if not os.path.exists(source):
        note("stomat_bot.db рядом нет — раздел пропущен (боевой файл не создаём)")
        return

    path = os.path.join(_TMPDIR, "prod_copy.db")
    shutil.copyfile(source, path)
    config.DB_PATH = path

    db = sqlite3.connect(path)
    before_rows = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    before_digest = db.execute(
        "SELECT SUM(msg_id), COUNT(text), MIN(date), MAX(date) FROM messages").fetchone()
    before_sample = db.execute(
        "SELECT msg_id, sender_name, text, date FROM messages ORDER BY msg_id LIMIT 5").fetchall()
    before_pending = db.execute(
        "SELECT COUNT(*) FROM messages WHERE has_media = 1 "
        "AND (media_description IS NULL OR media_description = '')").fetchone()[0]
    before_pending_window = db.execute(
        "SELECT COUNT(*) FROM messages WHERE has_media = 1 "
        "AND (media_description IS NULL OR media_description = '') "
        "AND date >= datetime('now', '-3 days')").fetchone()[0]
    hot = db.execute("SELECT reply_to_msg_id, COUNT(*) c FROM messages "
                     "WHERE reply_to_msg_id IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 1").fetchone()
    db.close()
    parent_id = hot[0] if hot else 0
    note(f"копия: {before_rows} сообщений, самая обсуждаемая ветка #{parent_id} "
         f"({hot[1] if hot else 0} ответов)")

    before_count = plan_of(path, REPLY_COUNT_SQL, (parent_id,))
    before_thread = plan_of(path, THREAD_SQL, (parent_id, parent_id))
    note(f"COUNT до:    {before_count}  ({bench_ms(path, REPLY_COUNT_SQL, (parent_id,)):.3f} мс)")
    note(f"ветка до:    {before_thread}  ({bench_ms(path, THREAD_SQL, (parent_id, parent_id)):.3f} мс)")
    check("боевая копия действительно сканировала таблицу на каждый ответ",
          "SCAN messages" in before_count, f"got {before_count}")

    failure = None
    try:
        await database.init_db()
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    check("init_db на боевой копии доходит до конца", failure is None, f"got {failure}")

    after_count = plan_of(path, REPLY_COUNT_SQL, (parent_id,))
    after_thread = plan_of(path, THREAD_SQL, (parent_id, parent_id))
    note(f"COUNT после: {after_count}  ({bench_ms(path, REPLY_COUNT_SQL, (parent_id,)):.3f} мс)")
    note(f"ветка после: {after_thread}  ({bench_ms(path, THREAD_SQL, (parent_id, parent_id)):.3f} мс)")
    check("на боевой копии COUNT по ответам идёт по индексу",
          "idx_reply_to" in after_count and "SCAN messages" not in after_count, f"got {after_count}")
    check("на боевой копии ветка идёт OR-планом",
          "MULTI-INDEX OR" in after_thread, f"got {after_thread}")

    db = sqlite3.connect(path)
    after_rows = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    after_digest = db.execute(
        "SELECT SUM(msg_id), COUNT(text), MIN(date), MAX(date) FROM messages").fetchone()
    after_sample = db.execute(
        "SELECT msg_id, sender_name, text, date FROM messages ORDER BY msg_id LIMIT 5").fetchall()
    db.close()
    check("ни одно сообщение не потеряно", after_rows == before_rows,
          f"было {before_rows}, стало {after_rows}")
    check("содержимое не переписано", after_digest == before_digest,
          f"было {before_digest}, стало {after_digest}")
    check("первые сообщения базы совпадают до символа", after_sample == before_sample)

    pending = await database.get_pending_media_message_ids(limit=5)
    note(f"снимков без описания в копии: {before_pending}, из них в окне трёх дней: "
         f"{before_pending_window}")
    check("на боевых данных окно трёх дней действительно отдавало пустоту",
          before_pending_window == 0 and before_pending > 0,
          f"pending {before_pending}, в окне {before_pending_window}")
    check("после правки выборка неразобранных не пуста", len(pending) > 0, f"got {pending}")

    window = await database.get_last_n_messages(limit=300)
    ids = [row[0] for row in window]
    check("окно на боевой копии отдаёт 300 сообщений", len(ids) == 300, f"got {len(ids)}")
    dates = [row[5] for row in window]
    check("окно строго упорядочено по (date, msg_id)",
          list(zip(dates, ids)) == sorted(zip(dates, ids)), "порядок нарушен")
    check("в окне нет повторов", len(set(ids)) == len(ids), f"got {len(set(ids))} из {len(ids)}")

    await database.init_db()
    db = sqlite3.connect(path)
    check("второй init_db на боевой копии данные не тронул",
          db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == before_rows)
    db.close()


async def run():
    await section_indexes()
    await section_reply_index()
    await section_window_ties()
    await section_bot_sent_two_chats()
    await section_bot_sent_migration()
    await section_bookmarks()
    await section_pending_media()
    await section_production_copy()


try:
    asyncio.run(run())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'=' * 62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
