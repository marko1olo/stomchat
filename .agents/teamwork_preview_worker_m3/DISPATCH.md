## 2026-09-04T13:57:03Z
You are Worker M3 (summarizer.py).
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m3
The authoritative user request is at: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md
The project blueprint is at: c:\Users\danat\Desktop\stomchat\PROJECT.md
The survey report is at: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_2\survey_summarizer_report.md
Test Writer handoff is at: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_test_writer_m5\handoff.md

CRITICAL CONSTRAINTS & RULES:
1. СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
2. Cooldown 2.5-3 секунды между обращениями к LLM API.
3. Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path.
4. EXCLUSIVE WRITE OWNERSHIP: You exclusively own `c:\Users\danat\Desktop\stomchat\summarizer.py`. Do not touch any other production files.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

YOUR ASSIGNMENT (Milestone M3):
1. Support both 8-tuple and 9-tuple message unpacking (backward compatibility):
   In `process_summary_batch` (around line 614) and `process_weekly_batch` (around line 1031) in `summarizer.py`:
   Change unpacking from rigid 8-tuple unpack to:
   ```python
   m_id, name, username, text, m_desc, date, reply_id, m_url = msg[:8]
   sender_id = msg[8] if len(msg) > 8 else None
   ```
2. Active authors identification & Clinical Profiles injection:
   - Collect unique active senders (`sender_id`) from the filtered discussion messages (ranked by frequency of substantive messages, ignoring None or 0).
   - Fetch their clinical profiles asynchronously via `user_memory.format_users_chunk_context(active_user_ids, max_chars=2000)`.
   - Inject this clinical profile context into the daily summary generation prompt (and weekly prompt where appropriate) under a clearly identified section.
3. Strict Budget Compliance (<= 2000 chars):
   - The context budget is strictly enforced by `max_chars=2000` in Python.
   - CRITICAL WARNING: DO NOT put literal text like "2000 символов" into the prompt string itself! Existing tests in `test_digest_formatting.py` and `test_fix_weekly.py` scan prompt text with regex `re.findall(r"(\d{4,5})\s*символ", prompt)`. Ensure the prompt instructions do not trigger regex false positives.
4. "ЭКСПЕРТ ДНЯ" Prompt Rubric:
   - In `DAILY_SUMMARY_PROMPT`, update the instructions for the "ЭКСПЕРТ ДНЯ" rubric:
     Direct the model to choose the expert based on the doctor's verified clinical profile, specialty, equipment/microscope, and protocols from the clinical dossiers, rather than random conversational phrases.
5. Fix 4 pre-existing E701 lint errors in `summarizer.py` (lines 567, 972, 1248, 1307: `if topic_id: ...` -> split into 2 lines).
6. Verification:
   - Run: `python test_user_memory.py`
   - Run: `python test_budget_nesting.py`
   - Run: `python test_fix_pm.py`
   - Run: `python test_startup_boot.py`
   - Run: `python test_digest_window.py`
   - Run: `python test_memory_e2e_integration.py`
   - Run: `python -m ruff check summarizer.py`
   Ensure 100% tests pass and 0 linter errors.
7. Write your handoff report to `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m3\handoff.md` and notify me via send_message.
