# Adversarial Verification & Stress Test Handoff Report

**Agent**: Challenger 1 (Adversarial Empirical Verifier)  
**Role**: critic, specialist  
**Working Directory**: `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_1`  
**Date**: 2026-09-04T18:10:00+04:00  
**Verdict**: **REJECT** (Conditional on fixing `is_trivial_message` filter patterns)

---

## 1. Observation

Direct empirical evidence obtained by writing and executing the adversarial stress test harness:
`python .agents\teamwork_preview_challenger_1\adversarial_stress_test.py`

### 1.1 Test Execution Results
Execution of the empirical test suite yielded **60 PASSED** and **6 FAILED** out of 66 checks:
```
======================================================================
ADVERSARIAL STRESS TEST RESULTS: 60/66 PASSED (1.06s)
======================================================================

FAILED CHECKS (6):
  - T3.3_emojis_with_spaces: ('👍  👏  ❤️') returned is_trivial=False
  - T3.3_10_emojis_trivial: 10 emojis ('👍👍👍👍👍👍👍👍👍👍') returned is_trivial=False
  - T3.3_spaced_emojis_trivial: Spaced emojis ('👍 👍 👍 👍 👍 👍 ') returned is_trivial=False
  - T3.4_dobry_den_kollega_trivial: 'Добрый день, коллега' returned is_trivial=False
  - T3.4_dobroe_utro_kollega_trivial: 'Доброе утро, коллега' returned is_trivial=False
  - T3.4_dobry_vecher_vsem_trivial: 'Добрый вечер всем!' returned is_trivial=False
```

### 1.2 Verbatim Code Under Investigation (`user_memory.py`)
Lines 159–185 of `user_memory.py`:
```python
# Паттерны тривиальных сообщений (приветствия, благодарности, команды)
_TRIVIAL_USER_PATTERNS = {
    "привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер",
    "спасибо", "благодарю", "понял", "ясно", "ок", "хорошо", "ладно", "до встречи",
    "пока", "/start", "/help", "/wipe", "/style", "/quiz", "/calc", "/bookmarks", "/stats"
}


def is_trivial_message(text: str) -> bool:
    """Проверяет, является ли сообщение тривиальным (приветствие, спасибки, команды)."""
    if not text:
        return True
    cleaned = text.strip().lower()
    if len(cleaned) < 8:
        return True
    if cleaned in _TRIVIAL_USER_PATTERNS:
        return True
    words = [w.strip("!.,?:;)") for w in cleaned.split()]
    if not words:
        return True
    if all(w in _TRIVIAL_USER_PATTERNS or w in ("большое", "огромное", "очень", "вам", "тебе", "всем", "бот", "коллега", "день", "утро", "вечер") for w in words):
        return True
    if cleaned.startswith(("/", "!", "спасибо", "привет", "здравствуй")):
        if len(words) <= 3:
            return True
    return False
```

Lines 359–384 of `user_memory.py`:
```python
    if not user_id or is_trivial_message(user_message):
        return

    now = time.time()
    last_ts = _LAST_PM_UPDATE_TS.get(user_id, 0.0)
    if now - last_ts < _PM_MEMORY_COOLDOWN:
        logger.debug(f"PM clinician memory update for {user_id} throttled by cooldown.")
        return
    _LAST_PM_UPDATE_TS[user_id] = now

    try:
        mem = await get_clinician_memory(user_id)
        current_spec = mem.get("specialty", "")
        current_summary = mem.get("clinical_summary", "")
        current_pm_count = mem.get("pm_message_count", 0) + 1

        # Проверяем, наступил ли интервал обновления (раз в PM_UPDATE_EVERY_N_MESSAGES реплик)
        # либо это первое сообщение или объемный клинический кейс
        is_first_time = not current_summary
        is_interval = (current_pm_count % PM_UPDATE_EVERY_N_MESSAGES == 0)
        has_rich_case = len(user_message) > 250 or any(w in user_message.lower() for w in (
            "снимок", "рентген", "клкт", "пациент", "кейс", "bopt", "имплант", "канал", "протокол"
        ))

        if not (is_first_time or is_interval or has_rich_case):
            # Просто инкрементируем счетчик сообщений без вызова дорогой LLM
            await database.save_user_memory(
                user_id=user_id,
                pm_message_count=current_pm_count,
                username=username,
                first_name=first_name
            )
            return
```

Lines 300–306 of `user_memory.py`:
```python
            profile_text = ""
            if grp_sum:
                # Берем до 300 символов самой сути профиля для чанка
                profile_text = grp_sum[:300].strip()
                if len(grp_sum) > 300:
                    profile_text += "..."
```

---

## 2. Logic Chain

1. **Failure of Multi-Emoji Messages**:
   - `len("👍👍👍👍👍👍👍👍👍👍") == 10 >= 8`.
   - The string is not in `_TRIVIAL_USER_PATTERNS`.
   - Splitting into words yields `['👍👍👍👍👍👍👍👍👍👍']`. None of the emojis are in the whitelist tuple.
   - The string does not start with `"/"`, `"!"`, `"спасибо"`, `"привет"`, or `"здравствуй"`.
   - `is_trivial_message` returns `False`.
   - Therefore, a user sending emojis or thumbs-up reactions (length >= 8) bypasses the trivial filter, increments `pm_message_count`, and will trigger an LLM rewrite if `current_pm_count % 4 == 0` or if `not current_summary`.

2. **Failure of Standard Polite Greetings with Address**:
   - When a doctor writes `"Добрый день, коллега"`, `"Доброе утро, коллега"`, or `"Добрый вечер всем!"`:
   - `len >= 8`. The full text is not in `_TRIVIAL_USER_PATTERNS` because `_TRIVIAL_USER_PATTERNS` only has `"добрый день"`, `"доброе утро"`, etc. without address.
   - When split, `words = ["добрый", "день", "коллега"]`.
   - While `"день"` and `"коллега"` are in the whitelist tuple `("большое", "огромное", ..., "день", "утро", "вечер", "коллега")`, the word `"добрый"` (and `"доброе"`) is NOT in `_TRIVIAL_USER_PATTERNS` (which stores multi-word phrases) and NOT in the whitelist tuple!
   - Line 181 `cleaned.startswith(...)` checks only for `("/", "!", "спасибо", "привет", "здравствуй")` and omits `"добр"`.
   - Consequently, `all(...)` fails on `"добрый"`, and `is_trivial_message` returns `False`.
   - This directly breaks the Acceptance Criterion from `ORIGINAL_REQUEST.md`:
     > "Односложные реплики («спасибо», «ок») не вызывают холостых вызовов LLM и не увеличивают счетчик цикла актуализации."
     When a new doctor initiates private messaging with `"Добрый день, коллега!"`, `is_first_time` evaluates to `True`, triggering a wasteful Gemini call to synthesize a "clinical dossier" from a greeting.

3. **Robustness of Sentence Deduplication (`deduplicate_clinical_summary`)**:
   - 14 out of 14 adversarial tests passed.
   - Inputs with 50 identical sentences collapsed to a single line.
   - Duplicates across multiple sections were eliminated while keeping all 4 required headers (`Специализация:`, `Арсенал и оснащение:`, `Клинические протоколы:`, `Кейсы:`).
   - Subtle variations (casing, bullets `•`, `-`, `*`, numbered items `1.`, `2)`, quotes `«»`, `""`, trailing punctuation `.`, `!`, `?`, `;`, whitespace) were normalized and collapsed.
   - Markdown headers (`**Специализация:**`, `### Кейсы:`) were recognized and preserved.
   - Empty/None/whitespace inputs returned safe empty strings.

4. **Robustness of Context Budgeting (`format_users_chunk_context`)**:
   - 11 out of 11 boundary tests passed.
   - Empty user lists return `""`.
   - 100 users are cleanly deduplicated and capped to the top 20 candidates.
   - At the chunk boundary, adding doctor profiles stops before `max_chars=2000` is exceeded. The candidate check `len(candidate) > max_chars: break` drops the entire next doctor profile rather than slicing it mid-sentence.
   - When `max_chars < len(header)` (~275 chars), it returns `""` safely without corrupted partial headers.
   - Caveat identified: In `user_memory.py:303`, `profile_text = grp_sum[:300].strip()` slices individual doctor group dossiers at 300 characters without aligning to sentence or word boundaries.

---

## 3. Caveats

1. The test suite tested Russian and English text inputs; multi-byte Asian alphabets or mixed unicode confusable characters were not tested.
2. The SQLite concurrency stress test was executed and verified via `test_memory_e2e_integration.py` (Scenario 5, 100 parallel tasks, 0 locked errors), but not pushed beyond 500 concurrent connections.
3. The LLM responses in testing were mocked to adhere to the strict zero-prod / zero-real-LLM quota rules. Real LLM model drift on JSON output formatting was not evaluated.

---

## 4. Conclusion & Challenge Report

### Challenge Summary
**Overall Risk Assessment**: **MEDIUM-HIGH** (Memory compaction and deduplication are highly robust, but trivial message filtering contains clear bypass paths that cause false-positive memory updates and wasted LLM calls on greetings and emoji sequences).

### Challenges

#### [High] Challenge 1: Greeting with Recipient Bypasses Trivial Message Filter
- **Assumption challenged**: `is_trivial_message` filters out all non-clinical greetings and acknowledgements.
- **Attack scenario**: User sends `"Добрый день, коллега"` or `"Доброе утро, коллега"`.
- **Blast radius**: `is_trivial_message` returns `False`. The message increments `pm_message_count`. For a new user, it triggers an immediate LLM dossier generation. For existing users, it desynchronizes the 4-message compaction cycle.
- **Mitigation**: In `user_memory.py`:
  1. Add `"добрый"`, `"доброе"`, `"доброго"` to the whitelist tuple on line 179:
     ```python
     ("большое", "огромное", "очень", "вам", "тебе", "всем", "бот", "коллега", "день", "утро", "вечер", "добрый", "доброе", "доброго")
     ```
  2. Add `"добр"` to `cleaned.startswith(...)` on line 181:
     ```python
     if cleaned.startswith(("/", "!", "спасибо", "привет", "здравствуй", "добр")):
         if len(words) <= 3:
             return True
     ```

#### [Medium] Challenge 2: Long Emoji Strings Bypass Filter
- **Assumption challenged**: Emojis are treated as trivial non-clinical messages.
- **Attack scenario**: User sends `"👍👍👍👍👍👍👍👍👍👍"` or `"👍  👏  ❤️"`.
- **Blast radius**: Classified as clinical message; advances message counter and can trigger LLM compaction.
- **Mitigation**: Strip emojis or check if all non-whitespace characters are emojis/punctuation:
  ```python
  import unicodedata
  # If text contains no alphanumeric characters (letters/digits in Cyrillic/Latin):
  if not any(c.isalnum() for c in cleaned):
      return True
  ```

#### [Low] Challenge 3: Hard Character Slice in `format_users_chunk_context` Inner Excerpt
- **Assumption challenged**: Doctor profile excerpts never truncate mid-sentence.
- **Attack scenario**: Doctor has a `group_summary` of 500 characters with a sentence ending at char 280 and next sentence ending at 350.
- **Blast radius**: `profile_text = grp_sum[:300].strip() + "..."` cuts in the middle of a word or sentence.
- **Mitigation**: Use `_split_into_sentences` or find the last punctuation mark before 300 characters.

### Final Verdict
**REJECT**  
The work in M1–M5 cannot be approved until Challenge 1 and Challenge 2 are addressed in `user_memory.py` so that common doctor greetings with titles/recipients and emoji reactions do not trigger unnecessary LLM compaction cycles.

---

## 5. Verification Method

To independently verify these findings:

1. **Run Adversarial Stress Test**:
   ```powershell
   python .agents\teamwork_preview_challenger_1\adversarial_stress_test.py
   ```
   *Expected result*: Exit code 1 with exactly the 6 failed checks demonstrated in Section 1.1.

2. **Run Baseline E2E Suite**:
   ```powershell
   python test_memory_e2e_integration.py
   ```
   *Expected result*: Exit code 0, 70/70 standard checks pass (confirming regression safety, but highlighting the gap in edge-case coverage).

3. **Inspect Failure Reproduction Lines in Python**:
   ```python
   import user_memory
   assert user_memory.is_trivial_message("Добрый день, коллега") is True  # Fails! Returns False
   assert user_memory.is_trivial_message("👍" * 10) is True  # Fails! Returns False
   ```
