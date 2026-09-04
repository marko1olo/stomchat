## 2026-09-04T13:48:54Z
You are Test Writer M5 (E2E Integration & Stress Suite).
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_test_writer_m5
The authoritative user request is at: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md
The project blueprint is at: c:\Users\danat\Desktop\stomchat\PROJECT.md
Survey reports are in `.agents/teamwork_preview_explorer_survey_*`

CRITICAL CONSTRAINTS & RULES:
1. СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
   All tests and simulations MUST run strictly on an isolated temporary SQLite database (tempfile / temp DB path) with mocked network calls (FakeClient / AsyncMock) and mocked LLM calls!
2. Cooldown 2.5-3 секунды between real LLM API calls, but all automated tests should mock LLM calls or simulate responses deterministically.
3. Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path.
4. EXCLUSIVE WRITE OWNERSHIP: You exclusively own `c:\Users\danat\Desktop\stomchat\test_memory_e2e_integration.py` and `c:\Users\danat\Desktop\stomchat\TEST_INFRA.md`.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All test implementations must be genuine. DO NOT create dummy tests that trivially pass (e.g. `assert True`). Write rigorous, comprehensive assertion checks. A teamwork_preview_auditor will independently verify your work.

YOUR ASSIGNMENT (Milestone M5):
1. Create `c:\Users\danat\Desktop\stomchat\TEST_INFRA.md` documenting:
   - Test architecture and philosophy (opaque-box, requirement-driven, 4 tiers).
   - Test runner command and pass/fail semantics.
   - Isolation strategy: temporary SQLite DB via tempfile, fake Telegram client with zero network access, deterministic LLM simulation.
2. Implement `c:\Users\danat\Desktop\stomchat\test_memory_e2e_integration.py` covering ALL requirements from ORIGINAL_REQUEST.md:
   - Scenario 1 (R1 PM Simulation): Multi-turn clinical interaction (8-12 replies: doctor specialty 'ортодонт / терапевт', dental microscope equipment 'Leica M320', adhesion protocols 'OptiBond FL / самопротравливающий праймер', clinical case 'разбор зуба 3.6 эндодонтия и восстановление коронковой части').
     - Verify compaction occurs every 4 messages.
     - Verify resulting `clinical_summary` contains structured sections: Специализация, Арсенал и оснащение, Клинические протоколы, Кейсы.
     - Verify no duplicate sentences exist in `clinical_summary`.
   - Scenario 2 (R1 Trivial Messages): Single-word / conversational acknowledgements ('спасибо', 'ок', '/help') cause 0 LLM calls and do NOT increment `pm_message_count`.
   - Scenario 3 (R1 Group Memory Daemon):
     - Populate group messages from multiple senders (some active with >=3 messages >15 chars, some inactive or trivial).
     - Trigger daemon tick: verify only active authors are processed.
     - Verify `group_summary` is strictly <= 8000 bytes (8KB limit).
     - Run idle daemon tick when no new messages: verify 0 LLM calls.
   - Scenario 4 (R2 Summarizer Integration & Expert of the Day):
     - Inject active authors with clinical dossiers into temporary DB.
     - Verify `format_users_chunk_context` retrieves the dossiers adhering to `max_chars=2000`.
     - Verify summarizer daily prompt incorporates the doctor profiles.
     - Verify "ЭКСПЕРТ ДНЯ" selection logic is driven by clinical status and experience.
     - Verify context budget injection <= 2000 chars.
   - Scenario 5 (R3 SQLite Concurrency Stress Test):
     - Run 100 concurrent asynchronous tasks on isolated SQLite DB: simultaneous PM write, profile read, background update, and group daemon execution.
     - Assert 0 `sqlite3.OperationalError: database is locked` errors, and verify all transactions commit cleanly.
   - Scenario 6 (R4 Regression Suite Runner):
     - Invoke existing test suites (`test_user_memory.py`, `test_budget_nesting.py`, `test_fix_pm.py`, `test_startup_boot.py`) and assert 100% PASSED.
3. Verification:
   - Run `python test_memory_e2e_integration.py`
   - Run `python -m ruff check test_memory_e2e_integration.py`
4. Write handoff.md in `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_test_writer_m5\handoff.md` and notify me via send_message.
