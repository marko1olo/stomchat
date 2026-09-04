## 2026-09-04T13:48:54Z
You are Worker M1 (user_memory.py).
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m1
The authoritative user request is at: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md
The project blueprint is at: c:\Users\danat\Desktop\stomchat\PROJECT.md
The survey report is at: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_1\survey_memory_report.md

CRITICAL CONSTRAINTS & RULES:
1. СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
2. Cooldown 2.5-3 секунды между обращениями к LLM API.
3. Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path.
4. EXCLUSIVE WRITE OWNERSHIP: You exclusively own `c:\Users\danat\Desktop\stomchat\user_memory.py`. Do not touch any other production files.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

YOUR ASSIGNMENT (Milestone M1):
1. In `user_memory.py`, add programmatic sentence deduplication to `clinical_summary`:
   - In `update_clinician_memory_async`, when LLM rewrites the dossier into `clinical_summary`, pass the resulting text through a deduplication helper that strips and normalizes sentences/bullet items while keeping section headers (`Специализация:`, `Арсенал и оснащение:`, `Клинические протоколы:`, `Кейсы:`), ensuring that no identical or duplicate sentences/facts appear across or within sections.
2. In `format_users_chunk_context(user_ids: List[int], max_chars: Optional[int] = 2000) -> str`:
   - Add `max_chars: Optional[int] = 2000` parameter.
   - When building the context string of doctor dossiers, if appending the next doctor profile would cause the total length to exceed `max_chars`, stop appending and return the accumulated string. This guarantees strict budget compliance without mid-sentence truncation.
3. Add a clean test reset helper:
   - Provide `reset_pm_memory_cooldown(user_id: Optional[int] = None)` in `user_memory.py` so that tests can clear `_LAST_PM_UPDATE_TS` when simulating multi-turn dialogues without needing to wait 15 seconds between turns.
4. Verify that trivial message filter (`is_trivial_message`) and group daemon logic (8KB limit, 0 LLM calls on idle ticks) remain fully functional and clean.
5. Verification:
   - Run tests: `python test_user_memory.py`
   - Run linter: `python -m ruff check user_memory.py`
   Ensure 100% tests pass and 0 linter errors.
6. Write your handoff report to `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m1\handoff.md` and notify me via send_message.
