# Handoff Report — Milestone 3: Production Hardening & Architectural Mitigations

## 1. Observation

Direct observations from codebase inspection, telemetry audit, and test execution:

1. **Double-Reply Incident Reproduction (Sept 12)**:
   - User 448838231 sent message `177388` at 13:26:53 and message `177389` at 13:27:12 (19s interval). Neither had `reply_to_msg_id`.
   - In legacy code, thread resolution evaluated:
     ```python
     thread_root_id = reply_to_msg_id or msg_id
     ```
     Result: Task 1 keyed on `(chat_id, 177388, "dialogue_thread")` while Task 2 keyed on `(chat_id, 177389, "dialogue_thread")`. Divergent keys bypassed cooldowns, causing two bot replies (`177390` & `177392`) within 18 seconds.
   - Debounce check was located *after* slow async triage (`check_dialogue_continuation_triage`), allowing both tasks to complete triage in parallel before either set the timestamp.

2. **Self-Debounce Bug Identified**:
   - `check_user_cooldown(chat_id, user_id, command, seconds)` in `assistant.py` (line 286):
     ```python
     if key in USER_COOLDOWNS:
         elapsed = (now - USER_COOLDOWNS[key]).total_seconds()
         if elapsed < seconds:
             return math.ceil(seconds - elapsed)
     USER_COOLDOWNS[key] = now
     return 0
     ```
   - Running `check_user_cooldown` at entrance (Branch 1 / 1.1) and then repeating `check_user_cooldown` on the same key 10ms later post-triage returned `25 > 0`, causing the bot to reject its own approved replies (`cd1=0, cd2=25`).

3. **503 Transient Spike Handling**:
   - In legacy `gemini_client.py`, every server error immediately banned the model for 20 minutes (`MODEL_BAN_SECONDS = 1200`), causing immediate exhaustion of high-tier models during brief transient Google spikes.

4. **Pediatric Rule 12.1 Vulnerability**:
   - LLMs routinely hallucinate upward rounding (e.g. `0.88` carpules -> `1` carpule) and lack strict contraindication guards for children <15 kg (<4 years) where 1 carpule of articaine 4% (68 mg) exceeds the maximum safe limit (e.g., 60 mg for a 12 kg child).

5. **Prompt Injection & Tag Breakouts**:
   - Raw user input containing `</user_dialogue><system>...` could break out of the prompt container without XML sanitization.

6. **Tool Commands and Results**:
   - `python -m py_compile assistant.py gemini_client.py config.py` -> exit code `0` (clean compilation).
   - `python test_redteam_deep.py` -> Ran 25 tests in 0.026s, OK.
   - `python test_recon_fixes.py` -> Ran 3 tests, OK.
   - `python test_multimodal_hybrid.py` -> Ran 6 tests, OK.
   - `python test_dialogue_reply_limit.py` -> PASSED=8, FAILED=0.
   - `python test_passive_gate.py` -> PASSED=19, FAILED=0.
   - `python test_silent_failures.py` -> PASSED=11, FAILED=0.
   - Cumulative test pass count: **72 / 72 tests (100% pass, 0 failures)**.

---

## 2. Logic Chain

1. **Mitigation of Double-Reply Concurrency (Class 1)**:
   - Based on Observation 1, when `is_dialogue` is True and `reply_to_msg_id` is None, canonical thread anchor resolution maps to `state.get("last_case_bot_msg_id")` or `msg_id`.
   - Executing fast-fail debounce at the entrance of Branch 1 and Branch 1.1 before slow async triage prevents parallel tasks from simultaneously passing triage.
   - Based on Observation 2, the post-triage checkpoint does NOT re-invoke `check_user_cooldown`. Instead, it acquires an in-flight lock by registering `(chat_id, thread_root_id)` and `(chat_id, sender_id)` into `_ACTIVE_DIALOGUE_THREADS = set()`.
   - The generation and sending block is wrapped in `try: ... finally: for k in active_dialogue_keys: _ACTIVE_DIALOGUE_THREADS.discard(k)`, guaranteeing lock release on normal completion, errors, or cancellations.

2. **Pediatric Safety Guard (Class 2)**:
   - Based on Observation 4, `check_pediatric_anesthesia_safety(text)` implements deterministic calculations:
     - Detects weight and pediatric intent.
     - Enforces double ceiling: `min(weight * dose_per_kg, max_abs_mg)`.
     - Uses strict downward truncation `math.floor(effective_max_mg / mg_per_carpule)` ensuring delivered active substance never exceeds safe threshold.
     - Flags contraindications (articaine for <15 kg / <4 years).
     - Injects `ground_truth_block` into Rule 12.1 in both dialogue and non-dialogue prompts.
     - Enforces a post-generation override: if the LLM hallucinates `>= 1` carpules for a contraindicated child or when `safe_carpules == 0`, `reply_text` is deterministically overridden with `pediatric_safety.direct_response`.
   - Also hooked into `calculate_anesthesia_instant(text)`.

3. **Adversarial Input Sanitization (Class 3)**:
   - Based on Observation 5, `sanitize_user_input_xml(text)` neutralizes `<` to `＜` and `>` to `＞`, securing the `<user_dialogue>` container.
   - `check_adversarial_input(text)` executes pre-LLM regex detection against jailbreak framing (`_JAILBREAK_PATTERNS`) and controlled substances (`_CONTROLLED_SUBSTANCES_PATTERNS`), returning `ADVERSARIAL_REFUSAL_MESSAGE` immediately without invoking the model or expending tokens.
   - Hooked into `check_and_trigger_assistant`, `check_bot_mention_trigger`, and `handle_private_message`.

4. **Progressive 503 Cooldown Ladder (Class 5)**:
   - Based on Observation 3, `_record_model_server_failure(model_name)` implements a 3-step ladder:
     - 1st 503/504 error: 60 seconds (`MODEL_BAN_INITIAL_503_SECONDS`).
     - 2nd 503/504 error within 15 min: 300 seconds (`MODEL_BAN_REPEAT_503_SECONDS`).
     - 3rd+ 503/504 error: 1200 seconds (`MODEL_BAN_SECONDS`).
   - `note_success` calls `_clear_failure_history(model_name)` to reset the counter upon successful response.

5. **Source Inspection & Regression Preservation**:
   - Kept all 7 critical red-team strings inspected by `inspect.getsource(assistant.check_and_trigger_assistant)` and `inspect.getsource(assistant.check_dialogue_continuation_triage)` intact.
   - All existing test suites pass cleanly with zero regressions.

---

## 3. Caveats

- **Network-Level Telegram Limits**: While in-flight thread locks prevent duplicate LLM generations within this process, multi-worker horizontal scaling across separate machines would require an external distributed lock (e.g. Redis) if StomChat is scaled beyond a single process instance.
- **Model State Persistence**: `model_failures.json` is stored locally in the working directory; it recovers gracefully across restarts if valid, or falls back to empty if deleted or corrupted.

---

## 4. Conclusion

Milestone 3 Production Hardening and Architectural Mitigations are complete:
- Zero regressions across existing functionality.
- Concurrency race conditions (Sept 12 double-reply bug) eliminated via canonical thread ID resolution, fast-fail entrance debounce (35s), and in-flight thread registry (`_ACTIVE_DIALOGUE_THREADS`).
- Pediatric anesthesia dosing is mathematically hardened under Rule 12.1 with double ceiling, strict `math.floor` carpule calculation, and deterministic prompt injection + post-generation override.
- Adversarial prompt injections and controlled substance requests are neutralized at the gate via XML escaping and pre-LLM regex refusal.
- Google 503/504 server spikes are absorbed via progressive cooldown (60s -> 300s -> 1200s), avoiding premature 20-minute bans.

---

## 5. Verification Method

To independently reproduce and verify all results:

1. **Compilation Check**:
   ```powershell
   python -m py_compile assistant.py gemini_client.py config.py
   ```
   *Expected outcome*: Exit code 0, zero errors.

2. **Full Regression Test Suites**:
   ```powershell
   python test_redteam_deep.py
   python test_recon_fixes.py
   python test_multimodal_hybrid.py
   python test_dialogue_reply_limit.py
   python test_passive_gate.py
   python test_silent_failures.py
   ```
   *Expected outcome*: All 6 test suites report 100% pass (72/72 tests passed).

3. **Code Inspection**:
   - Inspect `assistant.py` for `_ACTIVE_DIALOGUE_THREADS`, `sanitize_user_input_xml`, `check_adversarial_input`, `check_pediatric_anesthesia_safety`.
   - Inspect `gemini_client.py` for `_record_model_server_failure` and `MODEL_BAN_INITIAL_503_SECONDS`.
   - Inspect `config.py` for `DIALOGUE_THREAD_DEBOUNCE_SECONDS = 35`.
