## 2026-09-04T13:41:33Z
You are Explorer Survey 3 (Database, Concurrency & Tests).
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_3
The authoritative user request is at: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md
Your mission:
Survey the codebase focusing on database.py, SQLite concurrency, and existing test suite for:
- R3: Stress-testing parallel access to SQLite on isolated DB (concurrent PM write, profile read, background update, group daemon; 0 database is locked errors, verify _run_db).
- R4: Regression test suite (100% pass for test_user_memory.py, test_budget_nesting.py, test_fix_pm.py, test_startup_boot.py), create new test_memory_e2e_integration.py covering all requirements, ruff check on touched files (user_memory.py, summarizer.py, database.py, assistant.py) with 0 errors.

CRITICAL CONSTRAINTS:
1. СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
2. Cooldown 2.5-3 секунды между обращениями к LLM API.
3. Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path.
4. You are read-only / exploratory: investigate files, do not modify production code.
