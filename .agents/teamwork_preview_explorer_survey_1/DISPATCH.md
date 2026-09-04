## 2026-09-04T13:41:32Z

You are Explorer Survey 1 (Memory & Assistant).
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_1
The authoritative user request is at: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md
Your mission:
Survey the codebase focusing on user_memory.py and assistant.py to map all technical details for:
- R1: E2E clinical interaction simulation for doctor memory in PM (8-12 replies, compaction every 4 messages, no duplicate sentences, structured sections) and group conversation (active doctors filter, daemon tick, group_summary <= 8KB).

CRITICAL CONSTRAINTS:
1. СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
2. Cooldown 2.5-3 секунды между обращениями к LLM API.
3. Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path.
4. You are read-only / exploratory: investigate files, do not modify production code.

Specifically analyze:
1. user_memory.py:
   - Data structures in SQLite (user_memories, group_memory, etc.).
   - Trigger conditions: when is memory updated? How does message counter work? Is there an acknowledgement check (e.g. ignoring 'спасибо', 'ок')?
   - Compaction logic: how does LLM update and compact dossier? Are duplicate sentences prevented? How are structured sections formatted?
   - format_user_memory_context / format_users_chunk_context / get_user_memory functions.
   - Group memory daemon: how does it poll for new messages? How does it filter active doctors? How is the 8KB limit on group_summary enforced? How does it behave if there are no new messages?
2. assistant.py:
   - Where and how is user_memory called during PM chat processing and group chat processing?
   - Context injection limits and prompt formatting.

Write your findings to: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_1\survey_memory_report.md
Also write handoff.md in your working directory and notify the orchestrator via send_message.
