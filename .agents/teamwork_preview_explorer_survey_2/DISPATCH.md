## 2026-09-04T13:41:32Z

You are Explorer Survey 2 (Summarizer & Profile Integration).
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_2
The authoritative user request is at: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md
Your mission:
Survey the codebase focusing on summarizer.py to map all technical details for:
- R2: Integration of clinical profiles into summarizer.py (daily/weekly digest, "ЭКСПЕРТ ДНЯ" selection based on clinical status and experience, strict budget <= 2000 chars context injection).

CRITICAL CONSTRAINTS:
1. СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
2. Cooldown 2.5-3 секунды между обращениями к LLM API.
3. Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path.
4. You are read-only / exploratory: investigate files, do not modify production code.

Specifically analyze:
1. summarizer.py:
   - Daily and weekly digest generation pipelines (functions, prompts, message gathering).
   - How are participants/authors identified in daily/weekly digests?
   - Current rubric "ЭКСПЕРТ ДНЯ" in prompt templates: does it currently use clinical profiles, or just chat text?
   - How to integrate format_users_chunk_context from user_memory.py to fetch profiles of active participants.
   - Token / character budget: where and how are budgets enforced in summarizer prompts? How to enforce strict <= 2000 chars context injection for doctor profiles without truncating other sections?
   - Any newspaper / digest post-processing or Telegram message sending code that needs safe mocking during testing.

Write your findings to: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_2\survey_summarizer_report.md
Also write handoff.md in your working directory and notify the orchestrator via send_message.
