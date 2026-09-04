# Handoff Report — Worker M1 (user_memory.py)

**Date**: 2026-09-04  
**Agent**: Worker M1 (`teamwork_preview_worker_m1`)  
**Milestone**: M1 (Core Memory & Deduplication)  
**Handoff Type**: Hard (Task Complete)

---

## 1. Observation

### Codebase State & Changes
- Target file: `c:\Users\danat\Desktop\stomchat\user_memory.py` (exclusive write ownership maintained).
- `user_memory.py:47-58`: Added `reset_pm_memory_cooldown(user_id: Optional[int] = None)` which clears `_LAST_PM_UPDATE_TS` globally (when `user_id is None`) or pops a specific user entry.
- `user_memory.py:61-160`: Implemented `_SECTION_HEADER_RE`, `_normalize_item`, `_split_into_sentences`, and `deduplicate_clinical_summary(summary: str) -> str`. It normalizes bullet markers (`- `, `• `, `* `, numbering), punctuation (`.`, `,`, `;`), whitespace, and lowercases text to detect duplicates, while explicitly recognizing and keeping section headers (`Специализация:`, `Арсенал и оснащение:`, `Клинические протоколы:`, `Кейсы:` and their canonical variations). Duplicate sentences and bullet items are eliminated both within sections and across sections.
- `user_memory.py:264-332`: Updated `format_users_chunk_context(user_ids: List[int], max_chars: Optional[int] = 2000) -> str` to accept `max_chars` (defaulting to 2000). The loop iterates in order of `unique_ids` and tests `candidate = f"{header}\n" + "\n".join(selected_notes + [note])`. If `len(candidate) > max_chars`, it stops appending without truncating any doctor's profile mid-sentence.
- `user_memory.py:422-427`: Updated the LLM instruction prompt in `update_clinician_memory_async` to explicitly structure rewritten summaries into the 4 canonical sections (`Специализация:`, `Арсенал и оснащение:`, `Клинические протоколы:`, `Кейсы:`).
- `user_memory.py:464`: Routed `final_summary` through `deduplicate_clinical_summary(final_summary)` prior to 64KB length bounding and saving to SQLite.

### Verification Commands and Outputs
1. `python test_user_memory.py`:
   ```
   [1] Инициализация БД с таблицей user_memories: [OK]
   ...
   ==============================================================
   PASSED: 35   FAILED: 0
   ```
2. `python -m ruff check user_memory.py`:
   ```
   All checks passed!
   ```
3. `python .agents\teamwork_preview_worker_m1\test_m1_comprehensive.py`:
   ```
   --- [M1.1] Deduplication of clinical_summary ---
   [OK] Специализация: сохранена
   [OK] Арсенал и оснащение: сохранено
   [OK] Клинические протоколы: сохранено
   [OK] Кейсы: сохранено
   [OK] Carl Zeiss ровно 1 раз (удален дубль внутри и межсекционный дубль в Протоколах)
   [OK] OptiBond FL ровно 1 раз
   ...
   --- [M1.2] format_users_chunk_context with max_chars budgeting ---
   [OK] Контекст по умолчанию укладывается в 2000 символов
   [OK] Контекст 650 символов строго <= 650
   [OK] Доктор-5 не влез и опущен целиком (нет mid-sentence truncation)
   ...
   --- [M1.3] reset_pm_memory_cooldown helper ---
   [OK] Сброс для 3001 удалил только 3001
   [OK] Сброс без аргументов очистил всех
   ...
   --- [M1.4] update_clinician_memory_async with LLM deduplication E2E ---
   [OK] Специализация обновлена в БД
   [OK] Carl Zeiss в БД строго 1 раз
   [OK] OptiBond FL в БД строго 1 раз
   [OK] зуба 3.6 в БД строго 1 раз
   ...
   ==============================================================
   PASSED: 40   FAILED: 0
   ```
4. Regression suite:
   - `python test_budget_nesting.py`: PASSED: 29, FAILED: 0
   - `python test_fix_pm.py`: PASSED: 29, FAILED: 0
   - `python test_startup_boot.py`: PASSED: 51, FAILED: 0

---

## 2. Logic Chain

1. **Sentence Deduplication in `clinical_summary`**:
   - Upstream LLM responses can repeat equipment, materials, or facts across different conversation turns (e.g. mentioning Carl Zeiss in both arsenal and protocols/cases, or repeating a bullet).
   - By creating `deduplicate_clinical_summary` and calling it on `final_summary` before `database.save_user_memory`, duplicate sentences/bullets are filtered out before persistence.
   - Preserving matched section headers (`Специализация:`, `Арсенал и оснащение:`, `Клинические протоколы:`, `Кейсы:`) guarantees that the dossier keeps its 4-part structure while stripping duplicate content.

2. **Context Budget Enforcement (`max_chars: Optional[int] = 2000`)**:
   - `summarizer.py` requires injecting doctor profiles into daily/weekly summaries under a strict <= 2000 character limit to prevent compressing digest sections.
   - In `format_users_chunk_context`, rather than slicing the accumulated text at an arbitrary index (which would cut words and sentences in half), each profile note is checked before being appended. If the next candidate note exceeds `max_chars`, appending stops immediately.
   - Iterating over `unique_ids` preserves caller priority order (e.g., top active discussion authors first).

3. **Multi-Turn Test Reset Helper (`reset_pm_memory_cooldown`)**:
   - A 15-second throttle (`_PM_MEMORY_COOLDOWN`) prevents rapid API quota exhaustion in production.
   - In tests and E2E simulation suites (such as `test_memory_e2e_integration.py`), waiting 15s between 8-12 dialogue turns would make test runs unnacceptably slow.
   - `reset_pm_memory_cooldown` allows test suites to instantly reset the in-memory timestamp for specific or all users without mutating production cooldown constants or sleeping.

4. **Preservation of Trivial Message Filter & Group Daemon**:
   - `is_trivial_message` and daemon limits (8KB) remain intact and verified. Idle ticks make 0 calls to LLM when no new messages exist.

---

## 3. Caveats

- `deduplicate_clinical_summary` operates at the sentence and bullet level. If an LLM paraphrases the exact same fact using completely different vocabulary, semantic deduplication would require embeddings or LLM post-processing. However, programmatic sentence/bullet deduplication with whitespace, case, punctuation, and bullet normalization handles the typical LLM repetition patterns (identical bullets, repeated protocol names, repeated case notes).
- No other production files were touched, adhering to exclusive write ownership of `user_memory.py`.

---

## 4. Conclusion

Milestone M1 is complete:
- Programmatic sentence deduplication for `clinical_summary` is fully implemented and active in `update_clinician_memory_async`.
- `format_users_chunk_context` supports `max_chars: Optional[int] = 2000` with whole-profile budget compliance and no mid-sentence truncation.
- `reset_pm_memory_cooldown` is available for test suites.
- All existing tests and the new comprehensive M1 suite pass 100% (184/184 total assertions passed across suites).
- Linter `ruff check user_memory.py` reports 0 errors.

---

## 5. Verification Method

To independently verify the changes:
1. Run existing memory tests:
   ```bash
   python test_user_memory.py
   ```
2. Run M1 comprehensive verification suite:
   ```bash
   python .agents\teamwork_preview_worker_m1\test_m1_comprehensive.py
   ```
3. Run linter check:
   ```bash
   python -m ruff check user_memory.py
   ```
4. Run full regression suite:
   ```bash
   python test_budget_nesting.py
   python test_fix_pm.py
   python test_startup_boot.py
   ```
Invalidation conditions:
- Any test failure in `test_user_memory.py` or `test_m1_comprehensive.py`.
- Any ruff check violation in `user_memory.py`.
- `format_users_chunk_context` producing output exceeding `max_chars`.
- `clinical_summary` retaining duplicated sentences or bullets.
