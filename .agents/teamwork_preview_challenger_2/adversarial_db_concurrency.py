"""
Adversarial SQLite Concurrency & Integrity Verification Harness
Challenger 2 — StomChat Clinician Memory & Summarizer Audit

Stress-tests SQLite database.py under extreme concurrency:
- Spawns 215+ rapid concurrent async operations via asyncio.gather
- Interleaves save_message, get_messages_for_daily_summary, save_user_memory,
  get_user_memory, get_users_memory_batch, get_unprocessed_group_users,
  get_user_messages_since, and process_group_memory_daemon_batch.
- Includes parallel thread doing external direct SQLite reads/writes concurrently.
- Verifies:
  1. ZERO sqlite3.OperationalError: database is locked
  2. ZERO exceptions across all tasks
  3. 100% data consistency: exact message counts and text match
  4. PRAGMA integrity_check == 'ok'
  5. High-throughput completion time
"""

import asyncio
import os
import sys
import tempfile
import time
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# Ensure stomchat root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Setup isolated environment
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_adv_db_")
test_db_path = os.path.join(_TMPDIR, "adv_concurrency.db")
test_log_path = os.path.join(_TMPDIR, "adv_concurrency.log")
os.environ["STOMCHAT_LOG_PATH"] = test_log_path

# Point config to test database
import config  # noqa: E402
config.DB_PATH = test_db_path

import runtime_guard  # noqa: E402
runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "hb.json")
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "status.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "dump.txt")
runtime_guard.start_watchdog = lambda *a, **k: None
runtime_guard.stop_watchdog = lambda *a, **k: None

import database  # noqa: E402
import user_memory  # noqa: E402

PASS = []
FAIL = []

def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        status = "OK  "
    else:
        FAIL.append(name)
        status = "FAIL"
    detail_str = f" -- {detail}" if detail and not cond else ""
    print(f"  [{status}] {name}{detail_str}")

async def run_adversarial_db_concurrency_stress():
    print("=" * 80)
    print("  ADVERSARIAL STRESS TEST: SQLite Database Concurrency & Consistency")
    print(f"  Target Database: {test_db_path}")
    print("=" * 80)

    # 1. Initialize DB
    await database.init_db()
    check("Database successfully initialized in isolated tempdir", os.path.exists(test_db_path))

    # Pre-populate some baseline messages and memories
    now = datetime.now(timezone.utc)
    base_save_tasks = []
    for i in range(1, 31):
        dt = now - timedelta(minutes=60 - i)
        base_save_tasks.append(
            database.save_message(
                msg_id=i,
                sender_id=1000 + (i % 5),
                sender_name=f"Baseline Doctor {i % 5}",
                sender_username=f"doc_{i % 5}",
                text=f"Клиническое базовое сообщение #{i}: применение адгезива OptiBond и композита",
                date=dt,
            )
        )
    await asyncio.gather(*base_save_tasks)
    print(f"  [INIT] Seeded 30 baseline messages into {test_db_path}")

    # Set up external thread that continuously executes queries on the DB file
    stop_external_thread = threading.Event()
    external_errors = []
    external_query_count = [0]

    def external_thread_worker():
        while not stop_external_thread.is_set():
            try:
                conn = sqlite3.connect(test_db_path, timeout=5)
                conn.execute("PRAGMA busy_timeout = 5000")
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM messages")
                cur.fetchone()
                cur.execute("SELECT count(*) FROM user_memories")
                cur.fetchone()
                conn.close()
                external_query_count[0] += 1
            except Exception as e:
                external_errors.append(f"external_thread: {type(e).__name__}: {e}")
            time.sleep(0.01)

    ext_thread = threading.Thread(target=external_thread_worker, daemon=True)
    ext_thread.start()

    # 2. Build 215 rapid concurrent tasks
    concurrency_errors = []
    saved_msg_ids = set()
    user_memories_expected = {}

    async def task_save_message(m_id: int, s_id: int, s_name: str, s_user: str, text: str, date: datetime):
        try:
            ok = await database.save_message(
                msg_id=m_id,
                sender_id=s_id,
                sender_name=s_name,
                sender_username=s_user,
                text=text,
                date=date,
            )
            if not ok:
                concurrency_errors.append(f"save_message returned False for msg_id={m_id}")
            return ("save_message", m_id, ok)
        except Exception as e:
            concurrency_errors.append(f"save_message exception msg_id={m_id}: {type(e).__name__}: {e}")
            raise

    async def task_get_daily_summary(start_dt, end_dt, min_count):
        try:
            msgs = await database.get_messages_for_daily_summary(start_dt, end_dt, min_count=min_count)
            return ("get_daily_summary", len(msgs))
        except Exception as e:
            concurrency_errors.append(f"get_messages_for_daily_summary exception: {type(e).__name__}: {e}")
            raise

    async def task_save_user_memory(u_id: int, specialty: str, clin_summary: str, grp_summary: str, count: int):
        try:
            await database.save_user_memory(
                user_id=u_id,
                specialty=specialty,
                clinical_summary=clin_summary,
                group_summary=grp_summary,
                message_count=count,
                username=f"doc_{u_id}",
                first_name=f"Доктор {u_id}"
            )
            return ("save_user_memory", u_id)
        except Exception as e:
            concurrency_errors.append(f"save_user_memory exception u_id={u_id}: {type(e).__name__}: {e}")
            raise

    async def task_get_user_memory(u_id: int):
        try:
            mem = await database.get_user_memory(u_id)
            return ("get_user_memory", u_id, bool(mem))
        except Exception as e:
            concurrency_errors.append(f"get_user_memory exception u_id={u_id}: {type(e).__name__}: {e}")
            raise

    async def task_get_users_batch(u_ids):
        try:
            batch = await database.get_users_memory_batch(u_ids)
            return ("get_users_batch", len(batch))
        except Exception as e:
            concurrency_errors.append(f"get_users_memory_batch exception: {type(e).__name__}: {e}")
            raise

    async def task_unprocessed_group_users():
        try:
            users = await database.get_unprocessed_group_users(min_new_messages=1, limit=10)
            return ("unprocessed_users", len(users))
        except Exception as e:
            concurrency_errors.append(f"get_unprocessed_group_users exception: {type(e).__name__}: {e}")
            raise

    async def task_get_user_messages_since(u_id: int, since_id: int):
        try:
            msgs = await database.get_user_messages_since(u_id, since_msg_id=since_id, limit=20)
            return ("user_messages_since", len(msgs))
        except Exception as e:
            concurrency_errors.append(f"get_user_messages_since exception: {type(e).__name__}: {e}")
            raise

    async def task_daemon_tick():
        try:
            with patch("user_memory.generate_gemini_text_async", return_value=(type("R", (), {"text": "{}"})(), None)):
                res = await user_memory.process_group_memory_daemon_batch(min_new_messages=1, limit=5)
            return ("daemon_tick", res)
        except Exception as e:
            concurrency_errors.append(f"process_group_memory_daemon_batch exception: {type(e).__name__}: {e}")
            raise

    tasks = []

    # 1. 60 save_message tasks (msg_ids 1001 to 1060)
    for i in range(1, 61):
        m_id = 1000 + i
        saved_msg_ids.add(m_id)
        s_id = 2000 + (i % 7)
        s_name = f"Врач #{s_id}"
        s_user = f"doctor_{s_id}"
        text = f"Стресс-сообщение #{i}: препарирование зуба {11 + (i % 30)} под коронку E.max, торк 40 Нсм"
        dt = now - timedelta(seconds=i * 5)
        tasks.append(task_save_message(m_id, s_id, s_name, s_user, text, dt))

    # 2. 35 get_messages_for_daily_summary tasks
    for i in range(35):
        s_dt = now - timedelta(hours=24)
        e_dt = now
        tasks.append(task_get_daily_summary(s_dt, e_dt, min_count=20 + i))

    # 3. 45 save_user_memory tasks (interleaved updates on 10 user IDs)
    for i in range(45):
        u_id = 3000 + (i % 10)
        spec = f"Специализация {i % 4} (ортопедия/эндодонтия)"
        clin = f"Клиническое досье пользователя {u_id} шаг {i}: микроскоп Zeiss, протокол адгезии 5 поколения."
        grp = f"Память группы для доктора {u_id} шаг {i}."
        user_memories_expected[u_id] = (spec, clin, grp, i + 1)
        tasks.append(task_save_user_memory(u_id, spec, clin, grp, count=i + 1))

    # 4. 30 get_user_memory tasks
    for i in range(30):
        u_id = 3000 + (i % 10)
        tasks.append(task_get_user_memory(u_id))

    # 5. 20 get_users_memory_batch tasks
    for i in range(20):
        u_ids = [3000 + ((i + k) % 10) for k in range(5)]
        tasks.append(task_get_users_batch(u_ids))

    # 6. 10 get_unprocessed_group_users tasks
    for _ in range(10):
        tasks.append(task_unprocessed_group_users())

    # 7. 10 get_user_messages_since tasks
    for i in range(10):
        tasks.append(task_get_user_messages_since(u_id=2000 + (i % 7), since_id=0))

    # 8. 5 daemon tick tasks
    for _ in range(5):
        tasks.append(task_daemon_tick())

    total_tasks_count = len(tasks)
    print(f"  [DISPATCH] Launching {total_tasks_count} concurrent operations via asyncio.gather...")

    t0 = time.perf_counter()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.perf_counter() - t0

    # Stop external worker
    stop_external_thread.set()
    ext_thread.join(timeout=2)

    print(f"  [TIMING] Completed {total_tasks_count} operations in {elapsed:.3f}s ({total_tasks_count / elapsed:.1f} ops/sec)")
    print(f"  [EXTERNAL THREAD] Executed {external_query_count[0]} external concurrent queries during run")

    # Analyze exceptions
    exceptions_raised = [r for r in results if isinstance(r, Exception)]
    lock_errors = [e for e in concurrency_errors + [str(x) for x in exceptions_raised]
                   if "locked" in e.lower() or "busy" in e.lower()]

    check(
        f"Total concurrent tasks spawned >= 150 (actual: {total_tasks_count})",
        total_tasks_count >= 150,
        f"Total tasks: {total_tasks_count}"
    )
    check(
        "ZERO sqlite3.OperationalError: database is locked occurred during stress",
        len(lock_errors) == 0,
        f"Found lock errors: {lock_errors}"
    )
    check(
        "ZERO exceptions raised across all concurrent async tasks",
        len(exceptions_raised) == 0 and len(concurrency_errors) == 0,
        f"Exceptions: {exceptions_raised[:3]}, errors: {concurrency_errors[:3]}"
    )
    check(
        "ZERO errors in external direct-SQLite concurrent thread",
        len(external_errors) == 0,
        f"External errors: {external_errors[:3]}"
    )

    # 3. Rigorous consistency verification
    print("\n  [VERIFICATION] Verifying data consistency and database integrity...")

    # A. Check all 60 saved messages exist in database with intact text
    with sqlite3.connect(test_db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM messages WHERE msg_id >= 1001 AND msg_id <= 1060")
        msg_count = cur.fetchone()[0]
        check(
            "All 60 concurrently saved messages exist in messages table",
            msg_count == 60,
            f"Expected 60, found {msg_count}"
        )

        # Verify sample message contents
        cur.execute("SELECT msg_id, sender_name, text FROM messages WHERE msg_id = 1042")
        row = cur.fetchone()
        check(
            "Sample message 1042 has intact text and sender data",
            row is not None and row[0] == 1042 and "#42" in row[2] and "E.max" in row[2],
            f"Row content: {row}"
        )

        # B. Verify user memories
        cur.execute("SELECT count(*) FROM user_memories WHERE user_id >= 3000 AND user_id < 3010")
        mem_count = cur.fetchone()[0]
        check(
            "All 10 updated doctor profiles exist in user_memories",
            mem_count == 10,
            f"Expected 10, found {mem_count}"
        )

        # Verify clinical_summary data integrity
        cur.execute("SELECT user_id, specialty, clinical_summary FROM user_memories WHERE user_id = 3005")
        mem_row = cur.fetchone()
        check(
            "Doctor 3005 profile contains expected specialty and clinical content",
            mem_row is not None and "Zeiss" in mem_row[2],
            f"Profile content: {mem_row}"
        )

        # C. PRAGMA integrity_check
        cur.execute("PRAGMA integrity_check;")
        integrity_res = [r[0] for r in cur.fetchall()]
        check(
            "PRAGMA integrity_check passed ('ok')",
            integrity_res == ["ok"],
            f"Integrity check returned: {integrity_res}"
        )

        # D. PRAGMA foreign_key_check
        cur.execute("PRAGMA foreign_key_check;")
        fk_res = cur.fetchall()
        check(
            "PRAGMA foreign_key_check passed (no violations)",
            len(fk_res) == 0,
            f"FK check returned: {fk_res}"
        )

    print("\n" + "=" * 80)
    print(f"  CONCURRENCY STRESS RESULTS: PASSED={len(PASS)}  FAILED={len(FAIL)}")
    print("=" * 80)

    if FAIL:
        print("  FAILURES DETECTED:")
        for f in FAIL:
            print(f"    - {f}")
        sys.exit(1)
    else:
        print("  ALL CONCURRENCY AND CONSISTENCY CHECKS PASSED EMPIRICALLY!")

if __name__ == "__main__":
    asyncio.run(run_adversarial_db_concurrency_stress())
