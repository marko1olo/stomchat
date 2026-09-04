# BRIEFING — 2026-09-04T13:48:00Z

## Mission
Survey summarizer.py and user_memory.py integration for R2: clinical profiles in daily/weekly digests, "ЭКСПЕРТ ДНЯ" selection, and strict <= 2000 chars budget injection.

## 🔒 My Identity
- Archetype: explorer
- Roles: survey, analysis, synthesis
- Working directory: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_2
- Original parent: 2eadec10-c0ef-4c69-9101-916f4567ad8a
- Milestone: survey_summarizer_profile_integration

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
- Cooldown 2.5-3 секунды между обращениями к LLM API
- Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path
- No sugarcoating, no sycophantic behaviour

## Current Parent
- Conversation ID: 2eadec10-c0ef-4c69-9101-916f4567ad8a
- Updated: 2026-09-04T13:48:00Z

## Investigation State
- **Explored paths**: summarizer.py, user_memory.py, database.py, assistant.py, main.py, test_user_memory.py, test_digest_formatting.py, test_fix_weekly.py, test_prompt_context.py, test_budget_nesting.py, test_fix_pm.py, test_startup_boot.py
- **Key findings**:
  - `database.py` queries (`get_messages_for_daily_summary`, `get_messages_for_range`) must be extended to select `sender_id`.
  - `summarizer.py` message unpacking must be upgraded to slice `msg[:8]` with `msg[8]` for `sender_id` to preserve compatibility with 8-tuple tests.
  - Active participants can be ranked by message frequency count in the period.
  - `user_memory.format_users_chunk_context` can format top active user profiles with `max_chars=2000`.
  - CRITICAL PITFALL: Do not put literal strings like `2000 символов` in the prompt; `test_digest_formatting.py` and `test_fix_weekly.py` regex `(\d{4,5})\s*символ` will fail.
  - Mock map established for `client` (`send_message`, `get_messages`, `pin_message`), `_generate_text_singleflight`, `create_telegraph_page_async`, and isolated SQLite DB.
  - Pre-existing ruff lint errors documented (4 in summarizer.py, 21 in assistant.py).
- **Unexplored areas**: None for survey scope. Complete technical roadmap delivered.

## Key Decisions Made
- Deliver detailed survey report in `survey_summarizer_report.md` and 5-component handoff in `handoff.md`.
- Enforce <= 2000 char budget strictly via code-level parameter `max_chars=2000` and slice guard in `summarizer.py`, without mentioning numbers in prompt text.

## Artifact Index
- c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_2\survey_summarizer_report.md — Detailed technical survey report
- c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_2\handoff.md — 5-component handoff report
