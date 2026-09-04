# Handoff Report — Explorer Survey 2 (Summarizer & Profile Integration)

## 1. Observation
1. **Pipelines in `summarizer.py`**:
   - Daily digest: `process_summary_batch(messages, client, chat_id, topic_id=None, msg_count=0, cached_message=None, delivery_hook=None)` (`summarizer.py:561`).
   - Weekly digest: `process_weekly_batch(messages, client, chat_id, topic_id=None, delivery_hook=None, cached_message=None)` (`summarizer.py:962`).
   - Schedulers calling them: `main.py:660, 712` (daily) and `main.py:761, 798` (weekly).
2. **Message Selection & Author Identification**:
   - `database.py:131-145`: Table `messages` has `sender_id INTEGER`, `sender_name TEXT`, `sender_username TEXT`.
   - `database.py:353` and `404`: `get_messages_for_daily_summary` and `get_messages_for_range` only select 8 columns:
     `SELECT msg_id, sender_name, sender_username, text, media_description, date, reply_to_msg_id, media_remote_url FROM messages`
   - `summarizer.py:614` and `1031`: Unpacks exactly 8 elements:
     `m_id, name, username, text, m_desc, date, reply_id, m_url = msg`
   - `test_fix_weekly.py:152-164`: `make_messages()` provides 8-tuples.
3. **Current Rubric «ЭКСПЕРТ ДНЯ»**:
   - `summarizer.py:716-718`:
     `9.🌟 ЭКСПЕРТ ДНЯ \n Выбери врача из чата, который давал самый полезные советы или протокол. Напиши его имя и за что награжден. При выборе эксперта отдавай приоритет тем, чьи сообщения вызвали одобрение коллег или на чьи советы другие врачи отвечали благодарностью. Эксперт — это тот, чьи слова можно вставить в учебник, или тот, кому коллеги сказали "спасибо" за совет`
   - Weekly `summarizer.py:1156-1158`:
     `## 🌟 ДОСКА ПОЧЕТА (ГЕРОИ НЕДЕЛИ)\n (ЗАПРЕЩЕНО писать просто список имен. Перечисли врачей, которые на этой неделе генерировали контент. Для каждого участника ОБЯЗАТЕЛЬНО укажи его конкретную заслугу или ценный вклад...)`
   - Neither rubric has any clinical profile data, specialty, or equipment awareness.
4. **User Profile Formatting**:
   - `user_memory.py:150-200`: `async def format_users_chunk_context(user_ids: List[int]) -> str` formats doctor profiles using `database.get_users_memory_batch(unique_ids)` into a header block with `• {doc_label}: {'; '.join(desc_parts)}`.
5. **Budgets & Test Regular Expressions**:
   - Output length budgets: `WEEKLY_HTML_LIMIT = 9500`, `DAILY_CHAR_BUDGET = 8500`, `WEEKLY_CHAR_BUDGET = 8300` (`summarizer.py:27, 47, 53`).
   - Input prompt check in `test_digest_formatting.py:228-230`:
     `daily_numbers = {int(n) for n in re.findall(r"\{DAILY_CHAR_BUDGET\}|(\d{4,5})\s*символ", daily_prompt) if n}`
     `check("в дневном промпте нет зашитых цифр длины", not daily_numbers)`
   - Input prompt check in `test_fix_weekly.py:254-260`:
     `numbers = {int(n) for n in re.findall(r"(\d{4,5})\s*символ", prompt)}`
     `check("объём в промпте — одна и та же цифра во всех упоминаниях", len(numbers) == 1)`
     `check("цифра взята из константы, а не зашита", numbers == {S.WEEKLY_CHAR_BUDGET})`
6. **Side Effects & Mock Points**:
   - Telegram send/pin: `client.send_message`, `client.get_messages`, `client.pin_message` (`summarizer.py:181, 213, 255`).
   - LLM: `_generate_text_singleflight` (`summarizer.py:270`).
   - Telegraph: `create_telegraph_page_async` (`summarizer.py:817, 1235`).
   - Status file: `runtime_guard.write_summary_status` writes `bot_summary_status.json`.
7. **Ruff Linters**:
   - `python -m ruff check user_memory.py summarizer.py database.py assistant.py` showed 25 errors:
     - `user_memory.py`: 0 errors
     - `database.py`: 0 errors
     - `summarizer.py`: 4 E701 colon errors (lines 567, 972, 1248, 1307)
     - `assistant.py`: 21 errors (E402, E701, F841, E712, F401).

## 2. Logic Chain
1. From Observation 2, `database.py` stores `sender_id` in `messages`, but doesn't return it in `get_messages_for_daily_summary` and `get_messages_for_range`. Adding `sender_id` as the 9th column allows direct participant tracking without extra queries.
2. However, from Observation 2, `summarizer.py` and `test_fix_weekly.py` unpack 8 columns. Therefore, `summarizer.py` must use slicing `msg[:8]` and `sender_id = msg[8] if len(msg) > 8 else None` to avoid breaking 8-tuple callers.
3. From Observation 3, the current «ЭКСПЕРТ ДНЯ» and «ДОСКА ПОЧЕТА» rubrics evaluate only chat text.
4. From Observation 4, `format_users_chunk_context` can be called for the top active `sender_id` values from the message batch.
5. From Observation 5, `test_digest_formatting.py` and `test_fix_weekly.py` fail if any 4-digit number followed by "символ" (such as "2000 символов") appears in the prompt. Therefore, the `<= 2000` chars limit MUST be enforced entirely in Python code (`MAX_USERS_CONTEXT_CHARS = 2000` and `max_chars=2000`), never putting the literal string `2000 символов` into prompt templates.
6. From Observation 6, safe E2E testing of `summarizer.py` requires stubbing `client` (`send_message`, `get_messages`, `pin_message`), `_generate_text_singleflight`, `create_telegraph_page_async`, and isolating the DB path.
7. From Observation 7, fixing the 4 E701 line colons in `summarizer.py` brings `summarizer.py` to 0 ruff errors.

## 3. Caveats
- `assistant.py` has 21 pre-existing ruff errors. While `summarizer.py`, `user_memory.py`, and `database.py` can be clean (0 errors), the prompt acceptance criteria lists `ruff check user_memory.py summarizer.py database.py assistant.py`. The implementer must either clean the 21 errors in `assistant.py` or note them as pre-existing debt.
- We did not modify production code (read-only constraint respected).

## 4. Conclusion
Integration of clinical profiles into `summarizer.py` (R2) is fully feasible and architecturally mapped:
- Pass `sender_id` in `database.py` message queries.
- Safely unpack 8- or 9-tuples in `summarizer.py`.
- Select top active authors by message frequency and call `user_memory.format_users_chunk_context(top_user_ids, max_chars=2000)`.
- Inject `{users_chunk_context}` into daily and weekly prompts and update the «ЭКСПЕРТ ДНЯ» rubric to evaluate clinical specialization, equipment, and verified experience.
- Do NOT mention literal numbers ("2000 символов") in prompt text to prevent regex failures in regression tests.
- Comprehensive technical details, snippets, and mock requirements are recorded in `survey_summarizer_report.md`.

## 5. Verification Method
1. Regression tests:
   - `python test_user_memory.py` (35 checks passed)
   - `python test_budget_nesting.py` (29 checks passed)
   - `python test_fix_pm.py` (29 checks passed)
   - `python test_startup_boot.py` (51 checks passed)
   - `python test_digest_formatting.py` (61 checks passed)
   - `python test_fix_weekly.py` (70 checks passed)
2. Lint check:
   - `python -m ruff check summarizer.py user_memory.py database.py`
3. Invalidation conditions:
   - If `test_fix_weekly.py` fails on `len(numbers) == 1`, a literal number was placed in the weekly prompt.
   - If `process_summary_batch` raises `ValueError: too many values to unpack`, an 8-tuple unpack was used instead of `msg[:8]`.
