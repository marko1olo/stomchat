# Handoff Report: Explorer Survey 1 (Memory & Assistant)

## 1. Observation
- **`user_memories` table schema** (`database.py:300-317`):
  `CREATE TABLE IF NOT EXISTS user_memories (user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '', specialty TEXT DEFAULT '', clinical_summary TEXT DEFAULT '', group_summary TEXT DEFAULT '', facts_json TEXT DEFAULT '[]', message_count INTEGER DEFAULT 0, pm_message_count INTEGER DEFAULT 0, group_message_count INTEGER DEFAULT 0, last_pm_analyzed_id INTEGER DEFAULT 0, last_group_analyzed_id INTEGER DEFAULT 0, last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP);`
  There is no separate `group_memory` table; group memory is saved in `group_summary` in `user_memories`.
- **Trivial message filtering** (`user_memory.py:53-70, 218`):
  `is_trivial_message` flags short (<8 chars) or predefined trivial words (`"спасибо"`, `"ок"`, `"привет"`). At line 218: `if not user_id or is_trivial_message(user_message): return`. Trivial messages exit immediately without incrementing `pm_message_count` or calling LLM.
- **Cooldown mechanism** (`user_memory.py:42, 221-226`):
  `_PM_MEMORY_COOLDOWN = 15.0`. If called within 15 seconds for the same user, it exits without incrementing `pm_message_count` (`return`).
- **Compaction & update condition** (`user_memory.py:234-242`):
  `is_first_time = not current_summary`; `is_interval = (current_pm_count % PM_UPDATE_EVERY_N_MESSAGES == 0)` where `PM_UPDATE_EVERY_N_MESSAGES = 4`; `has_rich_case = len(user_message) > 250 or any(w in user_message.lower() for w in ("снимок", "рентген", "клкт", "пациент", "кейс", "bopt", "имплант", "канал", "протокол"))`. If none are true, it increments `pm_message_count` in DB and returns without LLM.
- **Compaction execution** (`user_memory.py:260-322`):
  LLM is prompted to rewrite and compact the dossier (not append). JSON output is parsed: `specialty`, `rewritten_summary`, `new_facts`. New facts are appended to `current_facts` avoiding exact duplicate string facts (`if f_str and f_str not in current_facts:`), capped at 30. `final_summary` is replaced with `rewritten_summary` and capped at 64,000 characters. There is NO programmatic sentence deduplication function in Python for `rewritten_summary`.
- **Group memory daemon** (`user_memory.py:349-450`, `database.py:1239-1273`):
  `get_unprocessed_group_users` selects users from `messages` joined with `user_memories` with `cnt >= 3`, `LENGTH(TRIM(m.text)) > 15`, `m.msg_id > COALESCE(um.last_group_analyzed_id, 0)`. If empty, lines 361-363 return immediately with 0 LLM calls. The 8KB limit on `group_summary` is enforced at `user_memory.py:430` (`[:8000]`) and `database.py:1153` (`[:8000]`).
- **Assistant integration** (`assistant.py:4708-4709, 4766, 4815, 4838, 4981, 5090, 5103`):
  In PM chat, `clinician_mem` is read via `user_memory.get_clinician_memory(chat_id)` and formatted via `user_memory.format_clinician_memory_prompt(chat_id, clinician_mem)`, then injected into prompts as `{portrait}`. After sending PM response, `update_clinician_memory_async` is spawned in background. In group chat mention, `sender_ids` are formatted via `user_memory.format_users_chunk_context(sender_ids)` and injected into `reply_prompt`.
- **Existing tests execution**:
  - `python test_user_memory.py`: PASSED 35, FAILED 0.
  - `python test_budget_nesting.py`: PASSED 29, FAILED 0.
  - `python test_fix_pm.py`: PASSED 29, FAILED 0.
  - `python test_startup_boot.py`: PASSED 51, FAILED 0.
- **Linter execution**:
  `python -m ruff check user_memory.py database.py`: All checks passed (0 errors).

## 2. Logic Chain
1. From the schema in `database.py:300` and daemon queries in `database.py:1246`, `user_memories` stores both PM and group profiles in separate columns (`clinical_summary` vs `group_summary`), with separate counters (`pm_message_count` vs `group_message_count`) and analyzed message watermarks (`last_pm_analyzed_id` vs `last_group_analyzed_id`).
2. From `user_memory.py:218`, trivial messages exit prior to counter incrementation, satisfying the requirement that "спасибо" / "ок" do not trigger LLM calls or advance the update interval.
3. From `user_memory.py:223`, the 15-second process-level cooldown will drop simulated PM messages unless `_LAST_PM_UPDATE_TS` is cleared or mocked during test execution.
4. From `user_memory.py:237`, messages containing keywords like "протокол" or "кейс" trigger `has_rich_case`, which initiates LLM compaction outside of the 4-message interval. For testing strict compaction every 4 messages, the simulation must be aware of keyword triggers or the trigger logic must be tested intentionally.
5. From `user_memory.py:312`, deduplication of facts is programmatic (`not in current_facts`), but deduplication of sentences in `clinical_summary` relies entirely on LLM instructions. To meet R1 criteria ("без повторов одних и тех же предложений"), the mock LLM responses or post-processing must guarantee unique sentences.
6. From `user_memory.py:361`, when there are no new group messages from users with >= 3 messages since their watermark, `get_unprocessed_group_users` returns an empty list and the daemon exits without calling `generate_gemini_text_async`.

## 3. Caveats
- Production code was strictly not modified (read-only exploratory survey).
- LLM API calls to external services were not performed (mock-only / code analysis).
- In `assistant.py:8042` (`proactive_dm_ping_loop`), legacy `user_profile.get("profile_portrait")` is still queried instead of `user_memory.get_clinician_memory`. This was not changed as it is outside R1, but noted for completeness.

## 4. Conclusion
1. The technical mechanics of `user_memory.py`, `database.py`, and `assistant.py` are fully mapped and ready for E2E simulation development (R1).
2. The exact function names are: `format_clinician_memory_prompt` (PM context), `format_users_chunk_context` (group context), and `get_clinician_memory` / `get_user_memory`.
3. To build the new `test_memory_e2e_integration.py` test suite safely:
   - Use `tempfile` SQLite database with `config.DB_PATH`.
   - Mock `generate_gemini_text_async` and Telegram clients.
   - Clear `_LAST_PM_UPDATE_TS` between simulated steps to prevent cooldown throttling.
   - Simulate 8–12 PM turns, verifying compaction at interval boundaries (every 4 messages), section structure (`Специализация`, `Арсенал`, `Протоколы`, `Кейс`), and absence of duplicate sentences.
   - Simulate group daemon tick, verifying active doctor filtering (>= 3 messages, text > 15 chars), 8KB truncation, and 0 LLM calls on empty tick.

## 5. Verification Method
1. Run existing regression tests:
   - `python test_user_memory.py` (Must output PASSED: 35, FAILED: 0)
   - `python test_budget_nesting.py` (Must output PASSED: 29, FAILED: 0)
   - `python test_fix_pm.py` (Must output PASSED: 29, FAILED: 0)
   - `python test_startup_boot.py` (Must output PASSED: 51, FAILED: 0)
2. Run linter:
   - `python -m ruff check user_memory.py database.py` (Must exit with code 0)
3. Inspect detailed survey report:
   - `view_file` at `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_1\survey_memory_report.md`
