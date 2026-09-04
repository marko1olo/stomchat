# StomChat Test Infrastructure & Verification Architecture

## 1. Architecture & Testing Philosophy

The StomChat test infrastructure follows an **opaque-box, requirement-driven** methodology. Tests validate observable system behaviors, interface contracts, and data invariants rather than private implementation details. Every test suite traces directly to authoritative requirements in `ORIGINAL_REQUEST.md` (R1–R4) and the project specification `PROJECT.md` (F1–F10).

### Four-Tier Test Taxonomy

```
+-------------------------------------------------------------------------+
| Tier 4: End-to-End Integration & Concurrency Stress                     |
| (test_memory_e2e_integration.py)                                        |
| Multi-turn PM dialogue, group daemon pipeline, summarizer integration,  |
| 100+ concurrent async SQLite tasks, full regression orchestration       |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
| Tier 3: Lifecycle, Bootstrapping & Handler Wiring                       |
| (test_startup_boot.py, test_fix_pm.py)                                  |
| Full bot bootstrap without network, Telegram handler registration,      |
| state file transitions, watchdog isolation                              |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
| Tier 2: Subsystem & Formatting Budgets                                  |
| (test_budget_nesting.py, test_digest_formatting.py, test_fix_weekly.py) |
| Prompt budgets, character clamps (<=2000, <=8500), regex assertions     |
+-------------------------------------------------------------------------+
                                    ^
+-------------------------------------------------------------------------+
| Tier 1: Unit & Component Isolation                                      |
| (test_user_memory.py, test_vocab.py, etc.)                              |
| DB schema, CRUD operations, trivial message heuristics, clamps          |
+-------------------------------------------------------------------------+
```

1. **Tier 1: Unit & Component Isolation**  
   Validates primitive logic, schema creation, database CRUD helpers, string clamping (64KB PM, 8KB group), and message triviality filters (`is_trivial_message`).
2. **Tier 2: Subsystem & Formatting Budgets**  
   Verifies context assembly, nesting budgets, HTML character limits, and prompt template validity across daily/weekly summarizers and assistant dialogue trees.
3. **Tier 3: Lifecycle, Bootstrapping & Handler Wiring**  
   Ensures the entire bot can instantiate its handlers, database pools, and background tasks cleanly, verifying cooldown arithmetic and state recovery without external side-effects.
4. **Tier 4: End-to-End Integration & Concurrency Stress**  
   Exercises real multi-step workflows end-to-end: multi-turn clinician PM progression, group memory daemon batching, summarizer profile grounding ("ЭКСПЕРТ ДНЯ"), and high-concurrency SQLite stress testing under async workloads.

---

## 2. Test Runner Commands & Pass/Fail Semantics

### Test Runner Commands

The project utilizes standalone script runners executed directly with Python:

| Scope | Command | Description |
|---|---|---|
| **E2E & Stress Suite** | `python test_memory_e2e_integration.py` | Complete R1–R4 integration and concurrency validation |
| **User Memory Unit Suite** | `python test_user_memory.py` | Clinician profile memory schema and CRUD checks |
| **Budget & Formatting Suite** | `python test_budget_nesting.py` | Prompt character budgets and nesting constraints |
| **Private Messaging Fixes** | `python test_fix_pm.py` | Cooldown precision, history limits, /case handling |
| **Bot Startup & Lifecycle** | `python test_startup_boot.py` | Full system bootstrapping and handler registration |
| **All Test Suites** | `python run_all_tests.py` | Master subprocess runner with integrity auditing |
| **Linter Verification** | `python -m ruff check <files>` | Code style, syntax, and typing error audit |

> **Note on Pytest CLI**: In this environment, `python -m pytest` is disabled due to a broken site-packages entry point (`ModuleNotFoundError: No module named '_hypothesis_pytestplugin'`). All suites are self-contained executable Python test runners.

### Pass/Fail Output Semantics

All test suites follow standardized console reporting semantics:
- Each check logs status with `check(name, cond, detail)`:
  - Success: `  [OK  ] <test_name>`
  - Failure: `  [FAIL] <test_name> -- <detail>`
- At completion, the runner outputs a summary block:
  ```text
  ==============================================================
  PASSED: 18   FAILED: 0
  Duration: 1.2s
  ```
- **Exit Code**:
  - `0`: Exactly 100% of checks passed.
  - `1`: One or more checks failed.
- **Non-Facade Integrity Guarantee**: Tests never use trivial `assert True` or mock bypasses that skip verification. All assertions evaluate actual database records, returned strings, call counts, and concurrency outcomes.

---

## 3. Complete Isolation Strategy

### 3.1 Strict Prohibition on Production Network Calls

**CRITICAL RULE**: Under NO circumstances may tests transmit messages to production Telegram channels, groups, or real users.

The isolation strategy enforces three impermeable barriers:

```
[ Test Execution Process ]
       |
       +---> SQLite Access:   Redirected to tempfile.mkdtemp() / isolated DB
       +---> Telegram API:    Mocked via FakeClient / AsyncMock (zero network egress)
       +---> LLM API:         Mocked via deterministic JSON/text generator (zero quota burn)
       +---> Runtime State:   Redirected to temporary files (heartbeat, log, status)
```

### 3.2 Temporary SQLite Isolation (`tempfile`)

1. **Ephemeral Working Directory**:  
   Every test creates an isolated directory using `tempfile.mkdtemp(prefix="stomchat_...")`.
2. **Database Path Redirection**:  
   `config.DB_PATH = os.path.join(_TMPDIR, "test.db")` is configured before initializing `database.py`. The real production database (`stomat_bot.db`) is never opened for write operations during testing.
3. **WAL Mode Compatibility**:  
   The SQLite connection manager (`_connect()`) checks `_WAL_READY` cache to configure WAL mode on the temporary database once, preventing repeated PRAGMA overhead.
4. **Guaranteed Teardown**:  
   In `finally:` blocks, `database._DB_EXECUTOR.shutdown(wait=True)` ensures all worker threads finish and file locks release before `shutil.rmtree(_TMPDIR, ignore_errors=True)` removes the temporary directory.

### 3.3 Telegram Client Mocking (`FakeClient` / `AsyncMock`)

1. Bot clients (`client`, `bot_client`, `user_client`) are replaced with `AsyncMock` or lightweight `FakeClient` classes.
2. Methods `send_message`, `edit_message`, `get_messages`, `pin_message`, and `get_me` record calls in memory for assertion without issuing HTTP requests.
3. Call arguments (e.g. `chat_id`, `text`, `reply_to`) are inspected by test assertions to verify prompt formatting and message delivery logic.

### 3.4 Deterministic LLM Simulation

1. All calls to `blocking_tools.generate_gemini_text_async` and `summarizer._generate_text_singleflight` are intercepted by deterministic mock functions.
2. Responses simulate real clinical output (valid JSON with `specialty`, `rewritten_summary`, `new_facts` for PM compaction; clinical digest HTML for summarizer).
3. Invocation counters verify:
   - Compaction triggers strictly on schedule (e.g. every 4 messages in PM).
   - Trivial messages ('спасибо', 'ок') trigger **0** LLM calls.
   - Idle daemon passes trigger **0** LLM calls.
4. Rate limiting & Cooldown: Zero API quota is consumed, preventing provider bans and network throttling.

### 3.5 Runtime Guard Redirection

State and monitoring files are cleanly isolated into the temporary test directory:
- `runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "hb.json")`
- `runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "status.json")`
- `runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "dump.txt")`
- `os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")`
- Watchdogs (`runtime_guard.start_watchdog`) are stubbed out to prevent premature process termination via `os._exit`.

---

## 4. Concurrency & Stress Architecture (R3)

SQLite uses file-level locking. To eliminate `sqlite3.OperationalError: database is locked`:
1. `database.py` dispatches all operations through `_run_db` onto a single-threaded executor (`_DB_EXECUTOR = ThreadPoolExecutor(max_workers=1)`).
2. Asynchronous coroutines queue operations in FIFO order; each operation opens a connection, executes in a transaction (`with db:`), and closes the connection in `finally:`.
3. LLM API calls occur **outside** `_run_db`, ensuring database transactions hold locks for sub-millisecond durations.
4. The concurrency stress test in `test_memory_e2e_integration.py` verifies this invariant by executing 100 simultaneous async tasks (PM writes, profile reads, background updates, daemon runs) with zero lock errors and 100% data consistency.
