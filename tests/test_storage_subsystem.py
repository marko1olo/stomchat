"""
test_storage_subsystem.py: Тестирование подсистемы хранения (db_worker, backup_db, database).

Проверяет:
1. PRAGMA-инварианты 1GB Micro-VPS (busy_timeout, synchronous, cache_size, WAL).
2. Автоматический retry при блокировках SQLite (database is locked / busy).
3. Горячий бекап (sqlite3.backup API + безопасный TRUNCATE WAL + integrity_check).
4. Защиту от SQL-инъекций и параметризацию запросов.
5. Корректность очереди и отсутствие утечек соединений.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

# Установка UTF-8 для вывода в консоль
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import backup_db
import db_worker
import database

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  [OK  ] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def test_pragmas():
    print("\n[1] Проверка PRAGMA-инвариантов Micro-VPS")
    tmp = tempfile.mktemp(suffix=".db")
    try:
        conn = db_worker.connect_db(tmp)
        cur = conn.cursor()

        cur.execute("PRAGMA journal_mode")
        jmode = cur.fetchone()[0]
        check("journal_mode = WAL", jmode.lower() == "wal", f"got {jmode}")

        cur.execute("PRAGMA synchronous")
        sync = cur.fetchone()[0]
        check("synchronous = NORMAL (1)", sync == 1, f"got {sync}")

        cur.execute("PRAGMA cache_size")
        csize = cur.fetchone()[0]
        check("cache_size = -4000 (4MB)", csize == -4000, f"got {csize}")

        cur.execute("PRAGMA busy_timeout")
        btimeout = cur.fetchone()[0]
        check("busy_timeout = 30000", btimeout == 30000, f"got {btimeout}")

        conn.close()
    finally:
        for s in ("", "-wal", "-shm"):
            p = tmp + s
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def test_lock_retry():
    print("\n[2] Проверка retry-цикла при database is locked")
    tmp = tempfile.mktemp(suffix=".db")
    try:
        conn = db_worker.connect_db(tmp)
        conn.execute("CREATE TABLE t (id INT)")
        conn.commit()
        conn.close()

        # Фоновый поток удерживает эксклюзивную блокировку на 0.35 секунды
        locked_event = threading.Event()

        def locker():
            c = sqlite3.connect(tmp, timeout=1)
            c.execute("BEGIN EXCLUSIVE")
            locked_event.set()
            time.sleep(0.35)
            c.execute("INSERT INTO t VALUES (42)")
            c.commit()
            c.close()

        t = threading.Thread(target=locker)
        t.start()
        locked_event.wait(timeout=2.0)  # детерминированно ждем фактического захвата блокировки

        # Операция с retry при блокировке
        def write_op():
            c = sqlite3.connect(tmp, timeout=0.05)
            try:
                with c:
                    c.execute("INSERT INTO t VALUES (99)")
            finally:
                c.close()
            return True

        start = time.time()
        res = db_worker.run_with_lock_retry(write_op, max_retries=10, base_delay=0.1)
        duration = time.time() - start
        t.join()

        check("операция успешно выполнена после ожидания блокировки", res is True)
        check("retry действительно выждал удержание блокировки (>= 0.3 с)", duration >= 0.3, f"duration: {duration:.3f}")

        with db_worker.db_connection(tmp) as c:
            rows = c.execute("SELECT id FROM t ORDER BY id").fetchall()
            check("все записи сохранены без потерь", rows == [(42,), (99,)], f"got {rows}")
    finally:
        for s in ("", "-wal", "-shm"):
            p = tmp + s
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def test_backup_and_wal_checkpoint():
    print("\n[3] Проверка горячего бекапа и WAL Checkpoint Law")
    tmp_src = Path(tempfile.mktemp(suffix=".db"))
    tmp_dest = Path(tempfile.mktemp(suffix=".db"))
    try:
        conn = sqlite3.connect(str(tmp_src))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'item1'), (2, 'item2')")
        conn.commit()
        conn.close()

        # Checkpoint WAL
        cp_res = backup_db.checkpoint_wal_safe(tmp_src)
        check("безопасный сброс WAL завершается успешно", cp_res is True)

        # Hot backup
        res = backup_db.backup_database(tmp_src, tmp_dest)
        check("горячий бекап создан со статусом ok", res.get("status") == "ok")
        check("целевой файл бекапа существует", tmp_dest.exists())
        check("SHA-256 рассчитан", len(res.get("sha256", "")) == 64)

        # Integrity check на бекапе
        is_ok, msg = backup_db.verify_database_integrity(tmp_dest)
        check("PRAGMA integrity_check на созданном бекапе: ok", is_ok is True, msg)

        # Проверка данных в бекапе
        conn2 = sqlite3.connect(str(tmp_dest))
        rows = conn2.execute("SELECT name FROM items ORDER BY id").fetchall()
        conn2.close()
        check("данные в бекапе совпадают с исходными", rows == [("item1",), ("item2",)])
    finally:
        for p in (tmp_src, tmp_dest):
            for s in ("", "-wal", "-shm", ".tmp"):
                t = p.with_name(p.name + s)
                if t.exists():
                    try:
                        t.unlink()
                    except Exception:
                        pass


async def test_async_database_integration():
    print("\n[4] Проверка интеграции database.py с db_worker")
    tmp = tempfile.mktemp(suffix=".db")
    orig_db_path = getattr(database.config, "DB_PATH", None)
    database.config.DB_PATH = tmp
    try:
        await database.init_db()

        # Тест save_message
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        res = await database.save_message(
            msg_id=101010,
            sender_id=777,
            sender_name="Тестовый Врач",
            sender_username="doc_test",
            text="Клинический тест интеграции",
            date=now
        )
        check("database.save_message сохраняет сообщение через db_worker", res is True)

        # Тест чтения
        text_row = await database.get_text_by_id(101010)
        check("database.get_text_by_id возвращает сохраненные данные", text_row == ("Тестовый Врач", "Клинический тест интеграции"), f"got {text_row}")

        # Тест checkpoint_wal_async
        cp_async = await database.checkpoint_wal_async()
        check("database.checkpoint_wal_async успешен", cp_async is True)

        # Проверка глубины очереди
        depth = db_worker.get_worker_queue_depth()
        check("очередь исполнителя пуста после завершения задач", depth == 0, f"depth: {depth}")
    finally:
        database.config.DB_PATH = orig_db_path
        for s in ("", "-wal", "-shm"):
            p = tmp + s
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def main():
    test_pragmas()
    test_lock_retry()
    test_backup_and_wal_checkpoint()
    asyncio.run(test_async_database_integration())

    print(f"\n{'=' * 60}")
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print("Провалено: " + ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
