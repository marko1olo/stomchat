## 2026-09-04T14:04:41Z

You are Challenger 2 (Adversarial Database Concurrency & Pipeline Verifier).
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_2
The authoritative user request is at: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md
The project blueprint is at: c:\Users\danat\Desktop\stomchat\PROJECT.md
The test ready document is at: c:\Users\danat\Desktop\stomchat\TEST_READY.md

CRITICAL CONSTRAINTS & RULES:
1. СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
   All adversarial testing must use isolated temporary DBs and mocked network calls!
2. Cooldown 2.5-3 секунды между обращениями к LLM API.
3. Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path.

YOUR ASSIGNMENT:
Adversarially stress-test SQLite concurrency and summarizer pipeline integrity:
1. Write and execute an adversarial concurrency script in your working directory:
   - Spawn 150+ rapid concurrent async operations against database.py on an isolated temp DB (interleaved save_message, get_messages_for_daily_summary, save_user_memory, get_user_memory, and group daemon queries).
   - Verify that ZERO sqlite3.OperationalError: database is locked occur and all transactions remain fully consistent.
2. Adversarially verify summarizer.py:
   - Pass mixed batches of 8-element tuples, 9-element tuples, and corrupt/None sender_ids to process_summary_batch. Assert no ValueError: too many values to unpack or crashes occur.
   - Verify prompt construction does not trigger prompt regex guards in existing tests.
3. Report your findings, empirical test execution results, and verdict (APPROVE or REJECT) in your handoff report.
4. Write your handoff report to c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_2\handoff.md and notify orchestrator via send_message.
