# Comprehensive Survey Report: Database, SQLite Concurrency & Test Suite (R3 & R4)

**Date**: 2026-09-04  
**Author**: Explorer Survey 3 (Database, Concurrency & Tests Specialist)  
**Target Modules**: `database.py`, `user_memory.py`, `summarizer.py`, `assistant.py`, test suite  
**Authoritative Request**: `.agents/ORIGINAL_REQUEST.md` (Requirements R3, R4)  

---

## Executive Summary

1. **Database & Concurrency Architecture (`database.py`)**:
   - Single-threaded executor pattern (`_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stomchat-db")`) handles all `_run_db` operations.
   - All 40+ public database access functions in `database.py` are strictly asynchronous coroutines returning `await _run_db(operation)`.
   - Connection management via `_connect()` and context manager `_connection()` applies `PRAGMA busy_timeout = 30000`, `timeout=30`, and `PRAGMA journal_mode = WAL` (cached in `_WAL_READY` to eliminate 1.2 ms connection overhead).
   - **Concurrency Safety Verdict**: Because `_DB_EXECUTOR` has `max_workers=1`, intra-process operations never execute concurrently on the SQLite engine, preventing deferred transaction upgrade deadlocks. Heavy parallel coroutines (PM writes, profile reads, background memory updates, daemon runs) safely serialize in the thread pool queue without blocking the asyncio event loop or encountering `sqlite3.OperationalError: database is locked`.
   - Long-running operations (such as LLM generation in `user_memory.update_clinician_memory_async` and `process_group_memory_daemon_batch`) perform LLM calls *outside* `_run_db`, releasing database access while waiting for API responses.

2. **Existing Test Suite Baseline (R4)**:
   - Verified 100% pass across all 4 mandatory regression test files:
     - `test_user_memory.py`: **35 PASSED, 0 FAILED** (0.3s)
     - `test_budget_nesting.py`: **29 PASSED, 0 FAILED** (1.3s)
     - `test_fix_pm.py`: **29 PASSED, 0 FAILED** (0.8s)
     - `test_startup_boot.py`: **51 PASSED, 0 FAILED** (6.7s)
     - Total: **144 checks passed, 0 failures**.
   - Test framework discovery: The project uses standalone script-based test runners with `check(name, cond, detail)` outputting `[OK  ]` / `[FAIL]` and `run_all_tests.py` orchestrator. Standard `pytest` discovery is blocked by an environment-level broken entry point (`_hypothesis_pytestplugin`).

3. **Linter Baseline (`ruff check`)**:
   - `user_memory.py`: **0 errors** (CLEAN)
   - `database.py`: **0 errors** (CLEAN)
   - `summarizer.py`: **4 errors** (all `E701 Multiple statements on one line (colon)` on `if topic_id: send_params[...]`)
   - `assistant.py`: **21 errors** (8 `E402`, 1 `F401`, 1 `E712`, 2 `F841`, 6 `E701`)
   - Total existing errors: **25 errors**. Required target: **0 errors** across all 4 files.

4. **Integration & Design Specifications**:
   - Specification for `test_memory_e2e_integration.py` designed to cover R1 (PM 8-12 message progression, group daemon, trivial message filtering), R2 (summarizer context injection, 2000 char budget, Expert of the Day grounding), R3 (stress test with 100+ concurrent operations, 0 locked errors), and R4 (zero regression).

---

## 1. Deep Dive: `database.py` Architecture & Concurrency

### 1.1 Connection Lifecycle & SQLite PRAGMAs

File: `database.py`, lines 12–50:

```python
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stomchat-db")
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
```

#### Key Technical Observations:
1. **WAL Mode Optimization**:
   - WAL (Write-Ahead Logging) is a persistent database header property.
   - Re-executing `PRAGMA journal_mode = WAL` on every connection acquisition incurs disk I/O and lock overhead (1.2 ms per call, 2.6x slower).
   - `_WAL_READY: set` caches paths that already have WAL enabled within the process.
   - When tests redirect `config.DB_PATH` to a new temporary database, the new path is recognized and WAL is set once.

2. **Timeout Settings**:
   - `sqlite3.connect(..., timeout=30)`: Python-level timeout waiting for SQLite locks.
   - `PRAGMA busy_timeout = 30000`: Sets the SQLite core engine's internal busy handler to 30,000 milliseconds (30 seconds). If another thread/process holds a write lock, SQLite automatically sleeps and retries for up to 30 seconds before failing.

3. **Transaction Management**:
   - `with db:` context manager invokes Python's standard SQLite transaction control.
   - When DML statements (INSERT, UPDATE, DELETE) are executed, a deferred transaction begins.
   - If the block exits cleanly, `db.commit()` is called automatically.
   - If an unhandled exception occurs, `db.rollback()` is called automatically.
   - `db.close()` in `finally:` guarantees connection closure and release of memory / file locks immediately upon completion of the operation.

### 1.2 Execution Model: `_run_db` and Threading

File: `database.py`, lines 70–73:

```python
async def _run_db(operation):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_EXECUTOR, operation)
```

- **Thread Pool Topology**: Exactly 1 worker thread (`max_workers=1`).
- **Scheduling**: Every asynchronous database function delegates its blocking synchronous callable (`operation`) to `_DB_EXECUTOR`.
- **Elimination of Lock Contention**:
  - In standard multi-threaded SQLite, concurrent threads running deferred transactions (`BEGIN DEFERRED`) that read and subsequently write can experience upgrade deadlocks (`SQLITE_BUSY: database is locked`).
  - Because `_DB_EXECUTOR` has `max_workers=1`, all operations are strictly serialized in FIFO order inside a single thread.
  - No two operations within `_run_db` can ever execute simultaneously on the SQLite connection.
  - Concurrently spawned asyncio coroutines (`asyncio.gather(...)`) queue their tasks in `_DB_EXECUTOR`, which completes each in sub-milliseconds without lock contention.

### 1.3 Asynchronous Boundary & LLM Separation

In `user_memory.py`:
- `update_clinician_memory_async`:
  1. Calls `await get_clinician_memory(user_id)` -> runs in `_run_db` (0.2 ms), connection closes.
  2. Runs heuristic filters (`is_trivial_message`, intervals, case rich check).
  3. Awaits `generate_gemini_text_async(...)` -> **I/O bound network coroutine outside `_run_db`**. The DB worker thread is 100% idle and available for other queries during the entire 2–15 seconds LLM roundtrip.
  4. Calls `await database.save_user_memory(...)` -> runs in `_run_db` (0.3 ms), connection closes.
- `process_group_memory_daemon_batch`:
  1. Calls `await database.get_unprocessed_group_users(...)` -> runs in `_run_db`, connection closes.
  2. For each user:
     - `await database.get_user_memory(user_id)` (DB read, closed)
     - `await database.get_user_messages_since(...)` (DB read, closed)
     - `await generate_gemini_text_async(...)` (LLM call, 0 DB lock)
     - `await database.save_user_memory(...)` (DB write, closed)
     - `await asyncio.sleep(2.5)` (cooldown, 0 DB lock)

### 1.4 Clinical Memory Functions in `database.py`

| Function | Lines | Type | Purpose & Concurrency Profile |
|---|---|---|---|
| `save_pm_message` | 986–993 | Write | Inserts into `pm_messages`. Indexed on `(user_id, id)`. Execution time < 0.2 ms. |
| `get_last_pm_messages` | 996–1005 | Read | Selects last N messages for user. Execution time < 0.2 ms. |
| `get_user_profile` | 1008–1026 | Read | Fetches style & portrait from `user_profiles`. |
| `set_user_portrait` | 1043–1056 | Write | Upserts portrait into `user_profiles`. |
| `get_user_memory` | 1089–1132 | Read | Fetches complete dossier from `user_memories`. Returns clean defaults if row missing. |
| `save_user_memory` | 1135–1204 | Write | Upserts `user_memories` with 64 KB (PM) and 8 KB (group) string clamping. |
| `get_users_memory_batch` | 1206–1236 | Read | Batch query with `IN (?, ?, ...)` for up to 20 doctor IDs. Avoids N+1 query loops. |
| `get_unprocessed_group_users` | 1239–1273 | Read | Aggregates active doctors with `cnt >= min_new_messages` and `msg_id > last_group_analyzed_id`. |
| `get_user_messages_since` | 1276–1290 | Read | Fetches messages since `since_msg_id` for background daemon processing. |

---

## 2. Test Environment & Suite Architecture

### 2.1 Test Runner & Environment Discovery

- **Python Runtime**: Python 3.13.7 (64-bit), executable at:  
  `C:\Users\danat\AppData\Local\Programs\Python\Python313\python.exe`
- **Pytest Status**:
  - `pytest 9.0.3` installed, but direct invocation `python -m pytest` aborts due to:
    `ModuleNotFoundError: No module named '_hypothesis_pytestplugin'` in global site-packages metadata.
  - **Canonical Project Test Architecture**: The codebase uses self-contained runner scripts and `run_all_tests.py`.
- **`run_all_tests.py` Architecture**:
  - Runs each test script in an isolated subprocess with `TEST_TIMEOUT_SECONDS = 600`.
  - Configures temporary `config.py` from `config.example.py`.
  - Takes MD5 snapshots of 19 guarded files (`GUARDED`) including databases and runtime files before and after runs.
  - Parses `  [OK  ]` and `  [FAIL]` from stdout.
  - Direct execution `python test_<name>.py` is the primary and fastest testing mechanism.

### 2.2 Baseline Verification of Mandatory Test Files (R4)

All four mandatory test suites were executed independently and confirmed 100% passing:

```
[1] test_user_memory.py
==============================================================
PASSED: 35   FAILED: 0
Duration: 0.3s

[2] test_budget_nesting.py
==============================================================
PASSED: 29   FAILED: 0
Duration: 1.3s

[3] test_fix_pm.py
==============================================================
PASSED: 29   FAILED: 0
Duration: 0.8s

[4] test_startup_boot.py
==============================================================
PASSED: 51   FAILED: 0
Duration: 6.7s
```

**Total Baseline Passed**: 144 checks, 0 failed.

### 2.3 Isolation Techniques Observed Across Existing Tests

| Requirement | How Implemented in Tests |
|---|---|
| **Isolated SQLite DB** | Creates `_TMPDIR = tempfile.mkdtemp(prefix="stomchat_...")`, sets `config.DB_PATH = os.path.join(_TMPDIR, "test.db")`. For tests requiring existing data (`test_startup_boot.py`), copies `stomat_bot.db*` into `_TMPDIR`. |
| **No Telegram Network Calls** | Replaces Telegram client with `FakeBot` / `MagicMock` with `AsyncMock` for `send_message`, `edit_message`, `get_me`. |
| **No LLM API Quota Burn** | Replaces `blocking_tools.generate_gemini_text_async` or `assistant.generate_gemini_text_async` with deterministic stub returning `type("R", (), {"text": "..."})(), None`. |
| **Guard Runtime Files** | Sets `runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "hb.json")`, `runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "status.json")`, `os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")`. |
| **Thread Pool Cleanup** | Explicitly calls `database._DB_EXECUTOR.shutdown(wait=True)` in `finally:` block (when running as standalone process). |

---

## 3. Detailed Analysis of Linter Errors (`ruff check`)

Running `python -m ruff check user_memory.py summarizer.py database.py assistant.py` identified **25 errors**:

### 3.1 `user_memory.py` & `database.py`
- `user_memory.py`: **0 errors** (CLEAN)
- `database.py`: **0 errors** (CLEAN)

### 3.2 `summarizer.py` (4 errors)
All 4 errors are `E701 Multiple statements on one line (colon)`:
1. Line 567: `if topic_id: send_params['reply_to'] = topic_id`
2. Line 972: `if topic_id: send_params['reply_to'] = topic_id`
3. Line 1248: `if topic_id: send_params['reply_to'] = topic_id`
4. Line 1307: `if topic_id: send_params['reply_to'] = topic_id`

*Fix required by implementer*: Expand to standard two-line if statements:
```python
if topic_id:
    send_params['reply_to'] = topic_id
```

### 3.3 `assistant.py` (21 errors)

| Line | Rule | Detail | Fix Rationale |
|---|---|---|---|
| 21, 22, 26, 31, 32, 33, 43 | `E402` | Module level import not at top of file | Move imports up or add `# noqa: E402` if intentional order dependency. |
| 927 | `E402` | Module level import not at top of file | `from dental_vocab import DENTAL_KEYWORDS...` |
| 927 | `F401` | `dental_vocab.DENTAL_KEYWORDS` imported but unused | Remove `DENTAL_KEYWORDS` from import list on line 927. |
| 2366 | `E712` | Comparison to `True` (`parent_rows[0][0] == True`) | Replace with `bool(parent_rows[0][0])` or `parent_rows[0][0] == 1`. |
| 2420 | `F841` | Unused local variable `last_text_lower` | Remove assignment or use. |
| 3386–3387 | `E701` | Multiple statements on one line (colon) | Split `try: await ... except Exception: pass` across multiple lines. |
| 3708–3712 | `E701` | Multiple statements on one line (colon) | Split inline `try: ... except: pass` blocks across multiple lines. |
| 5097 | `F841` | Unused local variable `address` | Remove assignment `address = ...` (line 5097). |

---

## 4. Requirements & Architecture for `test_memory_e2e_integration.py`

To satisfy R1, R2, R3, and R4, the new integration test file must implement the following 6 comprehensive test sections:

### Section 1: Isolated Environment & DB Bootstrapping
- Create `_TMPDIR = tempfile.mkdtemp(prefix="stomchat_e2e_")`.
- Point `config.DB_PATH = os.path.join(_TMPDIR, "e2e_test.db")`.
- Divert all log and runtime guard files to `_TMPDIR`.
- Call `await database.init_db()`.
- Validate schema: tables `messages`, `user_memories`, `user_profiles`, `pm_messages`, `clinical_bookmarks` and all corresponding indexes created.

### Section 2: R1 E2E Clinical Memory Accumulation in PM (8–12 messages)
- Simulate realistic clinical doctor dialogue across 10 sequential turns:
  1. Turn 1: Doctor introduces specialty (Orthodontist / Endodontist) and microscope usage.
  2. Turn 2: Trivial message ("Спасибо, коллега!") -> verify skipped (`is_trivial_message` True, LLM not called).
  3. Turn 3: Technical inquiry about OptiBond FL and adhesion protocols.
  4. Turn 4: Case presentation (tooth 3.6 pulpitis, MB2 canal anatomy) -> triggers 4th message cycle.
  5. Verify LLM rewrite was called, structure retained, `clinical_summary` contains specialty, microscope, protocols, and tooth 3.6 case without duplication.
  6. Turn 5–8: Additional turns with protocol update.
  7. Turn 9: Trivial message ("ок, понял") -> verify skipped.
  8. Turn 10: Verify total memory length strictly adheres to `<= 64000` chars (`PM_USER_MEMORY_LIMIT`).

### Section 3: R1 E2E Group Memory Daemon & Active Author Processing
- Populate `messages` table with realistic chat messages:
  - Doctor A (Dr. Smirnov): 5 detailed messages about implant protocols (Astra Tech, Straumann).
  - Doctor B (Dr. Petrova): 4 detailed messages about pediatric sedation.
  - Doctor C: 1 short trivial message ("привет всем").
- Run `process_group_memory_daemon_batch(min_new_messages=3, limit=5)` with mocked LLM:
  - Verify Doctor A and Doctor B are processed.
  - Verify Doctor C is omitted (`cnt < 3` and short message).
  - Verify `group_summary` is populated and `<= 8000` chars (`GROUP_USER_MEMORY_LIMIT`).
  - Verify `last_group_analyzed_id` is updated to the max message ID.
- Run daemon a second time:
  - Verify 0 active candidates returned (`get_unprocessed_group_users` returns empty list).
  - Verify 0 calls made to LLM (idle daemon pass).

### Section 4: R2 Integration of Clinical Profiles in `summarizer.py`
- Seed chat messages for a 24-hour daily digest window.
- Verify `format_users_chunk_context` extracts active author profiles:
  - Formatted block contains `=== НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ (ИЗ БЕСЕДЫ) ===`.
  - Doctor labels and clinical specializations are present.
  - Total injected context budget strictly clamped to `<= 2000` characters.
- Verify prompt for `generate_daily_summary`:
  - Contains candidate clinical profiles.
  - Rubric `9.🌟 ЭКСПЕРТ ДНЯ` receives grounded context (prioritizing doctor with verified clinical experience in the discussed topic).

### Section 5: R3 SQLite Concurrency Stress Testing
- Launch heavy concurrent workload simultaneously via `asyncio.gather`:
  - 30 concurrent `database.save_pm_message` calls across multiple user IDs.
  - 30 concurrent `database.get_user_memory` and `database.get_user_profile` reads.
  - 20 concurrent `database.save_user_memory` updates.
  - 5 concurrent `process_group_memory_daemon_batch` passes.
  - 15 concurrent `database.get_users_memory_batch` calls.
- Total concurrent operations: **100 operations**.
- Assertions:
  - All operations return without exception.
  - **Zero** `sqlite3.OperationalError: database is locked` errors.
  - Database row counts match expected totals exactly (`pm_messages` count = 30, etc.).
  - Transactional integrity verified.

### Section 6: API Safety & Clean Teardown
- Confirm 0 external HTTP requests to Telegram or LLM providers occurred.
- Shut down `database._DB_EXECUTOR.shutdown(wait=True)`.
- Clean up `_TMPDIR`.
- Print final results table with `[OK  ]` count and exit with code 0.

---

## 5. Implementation Action Plan for Subsequent Agents

1. **Implementer Step 1 (`summarizer.py` & R2)**:
   - In `summarizer.py`: import `user_memory`.
   - In `generate_daily_summary` (and weekly if applicable):
     - Identify top active author IDs from the messages list.
     - Call `await user_memory.format_users_chunk_context(top_author_ids)`.
     - Clamp formatted user context to max 2000 characters.
     - Inject context into the prompt near `9.🌟 ЭКСПЕРТ ДНЯ`.
   - Fix the 4 `E701` syntax errors in `summarizer.py`.

2. **Implementer Step 2 (`assistant.py` Lint & Clean)**:
   - Resolve the 21 `ruff` errors in `assistant.py` (`E402`, `F401`, `E712`, `F841`, `E701`).

3. **Implementer Step 3 (Write `test_memory_e2e_integration.py`)**:
   - Write the full e2e test suite following Section 4 specifications.
   - Run `python test_memory_e2e_integration.py` to verify 100% pass.
   - Run the full regression suite (`test_user_memory.py`, `test_budget_nesting.py`, `test_fix_pm.py`, `test_startup_boot.py`).
   - Run `python -m ruff check user_memory.py summarizer.py database.py assistant.py` -> verify **0 errors**.

---
*Report completed by Explorer Survey 3.*
