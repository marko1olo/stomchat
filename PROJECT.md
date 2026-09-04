# Project: StomChat Clinician Memory & Summarizer Audit & Integration

## Architecture
- `user_memory.py`: Manages two-tier doctor memory (PM memory up to 64KB, group memory up to 8KB). Compaction every 4 messages, deduplication, trivial message filtering, active group participants daemon.
- `database.py`: Single-threaded serialized SQLite runner (`_run_db` on `_DB_EXECUTOR`), WAL mode, busy timeout 30s. Tables `user_memories` and `messages`.
- `summarizer.py`: Daily & weekly digest generator. Extracts active participants, injects clinical context via `format_users_chunk_context(max_chars=2000)`, instructs LLM for "ЭКСПЕРТ ДНЯ" based on clinical profile.
- `assistant.py`: Core bot assistant handler. Connects PM and group message events with memory retrieval and updates.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---|---|---|---|
| F1 | PM Clinical Memory Compaction | 8-12 turn clinical dialogue simulation, compaction every 4 messages, structured sections, programmatic sentence deduplication | M1 | ORIGINAL_REQUEST §R1 |
| F2 | Trivial Message Filter | Ignore single-word / acknowledgements ('спасибо', 'ок') with 0 LLM calls and no counter advance | M1 | ORIGINAL_REQUEST §R1, Acceptance Criteria |
| F3 | Group Memory Daemon Logic | Active doctors filter, group_summary <= 8KB, 0 LLM calls on idle ticks | M1 | ORIGINAL_REQUEST §R1, Acceptance Criteria |
| F4 | Database Sender ID Extraction | Include `sender_id` in `get_messages_for_daily_summary` and `get_messages_for_range` while preserving backward compatibility | M2 | Explorer Survey 2 & 3 |
| F5 | Summarizer Profile Integration | Fetch profiles of top active daily/weekly authors via `format_users_chunk_context` | M3 | ORIGINAL_REQUEST §R2 |
| F6 | "ЭКСПЕРТ ДНЯ" Clinical Selection | Prompt rubric evaluation based on doctor clinical profile, specialty, equipment, protocols | M3 | ORIGINAL_REQUEST §R2 |
| F7 | Strict Profile Budget (<=2000 chars) | Strict <= 2000 chars context injection enforced in Python, avoiding prompt text regex collisions | M3 | ORIGINAL_REQUEST §R2, Acceptance Criteria |
| F8 | SQLite Concurrency Stress Verification | Parallel async execution (PM write, profile read, background update, daemon tick) with 0 locked errors | M2, M5 | ORIGINAL_REQUEST §R3 |
| F9 | Regression Test Suite (100% Pass) | 100% pass for test_user_memory.py, test_budget_nesting.py, test_fix_pm.py, test_startup_boot.py | M5 | ORIGINAL_REQUEST §R4 |
| F10 | Comprehensive Integration Test Suite | New `test_memory_e2e_integration.py` validating all R1-R4 scenarios | M5 | ORIGINAL_REQUEST §R4 |
| F11 | Linter Cleanliness | 0 errors on `ruff check user_memory.py summarizer.py database.py assistant.py` | M4 | ORIGINAL_REQUEST §R4, Acceptance Criteria |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Core Memory & Deduplication | Enhance `user_memory.py` with programmatic sentence deduplication, `max_chars` parameter in `format_users_chunk_context`, test cooldown helper | none | DONE |
| M2 | Database Queries & Concurrency | Add `sender_id` to message queries in `database.py`, verify `_run_db` isolation | none | DONE |
| M3 | Summarizer Clinical Integration | Integrate profiles into `summarizer.py`, <= 2000 chars budget, "ЭКСПЕРТ ДНЯ" prompt update | M1, M2 | DONE |
| M4 | Linter Cleanliness | Fix 4 errors in `summarizer.py` and 21 errors in `assistant.py` for 0 ruff errors | M1, M3 | DONE |
| M5 | E2E Integration Suite & Stress Tests | Implement `test_memory_e2e_integration.py`, run 100% regression suite | M1, M2, M3, M4 | IN_PROGRESS |

## Interface Contracts
### `user_memory.py` ↔ `summarizer.py`
- Function: `format_users_chunk_context(user_ids: List[int], max_chars: Optional[int] = 2000) -> str`
- Input: List of integer Telegram user IDs; max_chars integer cap (default 2000).
- Output: Formatted string of user profiles within max_chars, each block containing specialty, equipment, protocols, cases. Returns empty string if no profiles found.

### `database.py` ↔ `summarizer.py`
- Function: `get_messages_for_daily_summary(...) -> List[Tuple]`
- Tuple format: `(msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url, sender_id)`
- Consumer unpacking: `m_id, name, username, text, m_desc, date, reply_id, m_url = msg[:8]`, `sender_id = msg[8] if len(msg) > 8 else None`.

## Code Layout
- `user_memory.py`: Clinician profile management, compaction, deduplication, group daemon.
- `database.py`: SQLite schema, indexes, query helpers, thread pool executor.
- `summarizer.py`: Daily / weekly summaries, expert of the day selection, telegraph publishing.
- `assistant.py`: Private message and group message orchestrator.
- `test_memory_e2e_integration.py`: End-to-end integration and stress test runner.
