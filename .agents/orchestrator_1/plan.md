# Master Plan — StomChat Memory & Summarizer Audit & Integration

## Objectives
Execute requirements R1, R2, R3, R4 from ORIGINAL_REQUEST.md:
1. **R1**: E2E Clinical Interaction Simulation for doctor memory in PM (8-12 replies, compaction every 4 messages, no duplicate sentences, structured sections) and group conversation (active doctors filter, daemon tick, group_summary <= 8KB).
2. **R2**: Integration of clinical profiles into summarizer.py (daily/weekly digest, "ЭКСПЕРТ ДНЯ" selection based on clinical status and experience, strict budget <= 2000 chars context injection).
3. **R3**: Stress-testing parallel access to SQLite on isolated DB (concurrent PM write, profile read, background update, group daemon; 0 database is locked errors, verify _run_db).
4. **R4**: Regression test suite (100% pass for test_user_memory.py, test_budget_nesting.py, test_fix_pm.py, test_startup_boot.py), create new test_memory_e2e_integration.py covering all requirements, ruff check on touched files (user_memory.py, summarizer.py, database.py, assistant.py) with 0 errors.

## Execution Strategy
- **Phase 0: Survey**:
  - Spawn 3 Explorers in parallel to map:
    - Explorer 1: user_memory.py and assistant.py (PM memory update, compaction, formatting, deduplication, group daemon).
    - Explorer 2: summarizer.py (digest generation, prompt structure, expert of the day, format_users_chunk_context, character budgeting <= 2000 chars).
    - Explorer 3: database.py, SQLite concurrency, _run_db, locking risks, and existing test suite structure (test_user_memory.py, test_budget_nesting.py, test_fix_pm.py, test_startup_boot.py).
- **Phase 1: Architecture & Decomposition**:
  - Synthesize explorer findings into `PROJECT.md` with Feature Inventory, Milestones, and Interface Contracts.
  - Setup Dual Track: E2E Testing Track (test writer) + Implementation Track.
- **Phase 2: Milestone Execution**:
  - Milestone 1: Memory quality & compaction mechanics (R1) - user_memory.py & assistant.py.
  - Milestone 2: Summarizer integration & Expert of the Day (R2) - summarizer.py.
  - Milestone 3: Database concurrency & safe isolation (R3) - database.py & lock verification.
  - Milestone 4: Full E2E & Regression test suite (R4) - test_memory_e2e_integration.py, ruff check.
- **Phase 3: Final Verification & Audit**:
  - Reviewers, Challengers, and Forensic Auditor verification.
