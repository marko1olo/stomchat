## 2026-09-04T13:57:03Z
You are Worker M4 (assistant.py & Linter Cleanliness).
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m4
The authoritative user request is at: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md
The project blueprint is at: c:\Users\danat\Desktop\stomchat\PROJECT.md
The survey report is at: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_3\survey_database_tests_report.md

CRITICAL CONSTRAINTS & RULES:
1. СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
2. Cooldown 2.5-3 секунды между обращениями к LLM API.
3. Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path.
4. EXCLUSIVE WRITE OWNERSHIP: You exclusively own `c:\Users\danat\Desktop\stomchat\assistant.py`. Do not touch any other production files.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT create dummy fixes. A teamwork_preview_auditor will independently verify your work.

YOUR ASSIGNMENT (Milestone M4):
1. In `assistant.py`, fix all 21 pre-existing ruff lint errors to achieve 0 errors on `python -m ruff check assistant.py`:
   - E402: Module level imports not at top of file (lines 53-85). Note: if certain imports must remain late due to `runtime_guard` bootstrap order, add targeted `# noqa: E402` or clean up the import order properly without breaking boot sequence.
   - F401: Unused imports (e.g. `DENTAL_KEYWORDS` or similar if unused).
   - E712: Comparison to `True` (e.g. change `== True` to `is True` or boolean truthiness).
   - F841: Local variable assigned to but never used (e.g. `last_text_lower`, `address`).
   - E701: Multiple statements on one line (colons).
2. Verification:
   - Run `python test_startup_boot.py` (critical: ensure bot initialization and assistant boot sequence works 100%)
   - Run `python test_fix_pm.py`
   - Run `python test_user_memory.py`
   - Run `python -m ruff check assistant.py`
   Ensure 100% tests pass and 0 ruff errors in `assistant.py`.
3. Write your handoff report to `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m4\handoff.md` and notify me via send_message.
