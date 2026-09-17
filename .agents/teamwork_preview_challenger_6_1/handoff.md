# Empirical Challenger Handoff Report: Concurrency & DoS Exhaustion

**Agent**: teamwork_preview_challenger_6_1
**Roles**: critic, specialist (Empirical Challenger)
**Working Directory**: `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_6_1`
**Target Modules**: `assistant.py`, `gemini_client.py`, `config.py`
**Evaluation Scope**: Concurrency & Race Conditions (Class 1) and DoS / Cascade Exhaustion (Class 5)

---

## 1. Observation

Direct empirical observations and measurements obtained via test execution and codebase inspection:

### O1. In-Flight Thread Registry & Locks (`assistant.py`)
- `_ACTIVE_DIALOGUE_THREADS` is declared at `assistant.py:2621`:
  ```python
  _ACTIVE_DIALOGUE_THREADS = set()
  ```
- Fast-fail entrance guards are implemented at lines 3025-3039 (Direct Reply branch) and 3136-3150 (Sequential Follow-up branch):
  ```python
  # Fast-fail entrance in-flight check:
  if (event.chat_id, resolved_thread_id) in _ACTIVE_DIALOGUE_THREADS or (sender_id and (event.chat_id, sender_id) in _ACTIVE_DIALOGUE_THREADS):
      logger.info(
          f"In-flight dialogue lock: thread {resolved_thread_id} or sender {sender_id} is already generating a reply. Skipping duplicate."
      )
      return False
  ```
- Cooldown debounce is checked before any slow async DB/triage calls at lines 3033-3038 and 3144-3149:
  ```python
  dialogue_cd = check_user_cooldown(event.chat_id, resolved_thread_id, "dialogue_thread", seconds=DIALOGUE_THREAD_DEBOUNCE_SECONDS)
  user_dialogue_cd = check_user_cooldown(event.chat_id, sender_id, "dialogue_sender", seconds=DIALOGUE_THREAD_DEBOUNCE_SECONDS) if sender_id else 0
  if dialogue_cd > 0 or user_dialogue_cd > 0:
      return False
  ```
- Thread and sender locks are registered at lines 3321-3329 and guaranteed released in a `finally` block at lines 3657-3659:
  ```python
  active_dialogue_keys = []
  if is_dialogue:
      thread_root_id = reply_to_msg_id or state.get("last_case_bot_msg_id") or msg_id
      active_dialogue_keys.append((event.chat_id, thread_root_id))
      if sender_id:
          active_dialogue_keys.append((event.chat_id, sender_id))
      for k in active_dialogue_keys:
          _ACTIVE_DIALOGUE_THREADS.add(k)
  try:
      ...
  finally:
      for k in active_dialogue_keys:
          _ACTIVE_DIALOGUE_THREADS.discard(k)
  ```

### O2. Progressive 503 Cooldown Ladder (`gemini_client.py`)
- Constants defined at lines 236-238:
  ```python
  MODEL_BAN_INITIAL_503_SECONDS = 60    # 1-я ошибка 503/504: короткий бан на 60 сек
  MODEL_BAN_REPEAT_503_SECONDS = 300   # Повторная ошибка 503 подряд: 5 минут
  MODEL_BAN_SECONDS = 1200             # 20 минут на устойчивый отказ
  ```
- `_record_model_server_failure` calculates escalating durations at lines 256-290:
  - Count == 1: returns 60s
  - Count == 2: returns 300s
  - Count >= 3: returns 1200s
  - Reset after 15 minutes (900s) if `now - last_time > 900`
- `note_success(provider, api_key, model_name)` calls `_clear_failure_history(model_name)` at lines 428-429, resetting the failure counter to 0 upon healthy reply.
- `note_key_failure` at lines 477-494 regex-matches transient 503/504/unavailable/overload/deadline strings and routes them to `_record_model_server_failure(model_name)` followed by `ban_model(model_name, ban_duration)`.

### O3. Empirical Stress Execution Results (`test_empirical_challenger_concurrency_dos.py`)
Execution of the 17-test empirical stress battery (`python test_empirical_challenger_concurrency_dos.py`):
```text
test_arrival_delays_spectrum_0_1s_1s_18s_30s_36s ... ok
test_dual_key_debounce_thread_and_sender_coordination ... ok
test_multi_threaded_hammer_on_check_user_cooldown ... ok
test_high_frequency_fault_injection_50_iterations ... ok
test_in_flight_lock_exclusivity ... ok
test_lock_release_on_simulated_cascade_exhaustion_exception ... ok
test_lock_release_on_simulated_task_cancellation ... ok
test_lock_release_on_simulated_telegram_network_error ... ok
test_lock_release_on_simulated_timeout_error ... ok
test_concurrent_tasks_same_dialogue_thread_only_one_proceeds ... ok
test_sequential_follow_up_without_reply_button_concurrency ... ok
test_corrupted_model_failures_json_resilience ... ok
test_multi_model_cooldown_isolation ... ok
test_note_key_failure_integration_with_transient_overload_strings ... ok
test_progressive_ladder_exact_durations_60s_300s_1200s ... ok
test_success_resets_ladder_immediately ... ok
test_window_expiration_after_15_minutes_resets_ladder ... ok

Ran 17 tests in 0.233s
OK
```

### O4. Full Regression Suite Results
1. `python test_redteam_deep.py`:
   - 25/25 tests PASSED in 0.036s.
2. `python test_recon_fixes.py`:
   - 3/3 tests PASSED in 0.007s.
3. `python test_multimodal_hybrid.py`:
   - 6/6 tests PASSED in 0.033s.
4. `python test_dialogue_reply_limit.py`:
   - 8/8 assertions PASSED.
5. `python test_passive_gate.py`:
   - 19/19 assertions PASSED.
6. `python test_silent_failures.py`:
   - 11/11 assertions PASSED.
7. `python -m py_compile assistant.py gemini_client.py config.py test_redteam_deep.py test_empirical_challenger_concurrency_dos.py`:
   - Exited with code 0 (clean compilation).

---

## 2. Logic Chain

1. **Reproduction and Mitigation of the 18-second Dual-Reply Incident (O1, O3)**:
   - *Observation*: On Sept 12 (messages 177388 & 177389), two user messages sent 19 seconds apart without `reply_to_msg_id` triggered two bot responses (177390 & 177392) 18 seconds apart because divergent fallback keys were generated (`reply_to or msg_id`).
   - *Logic*: Canonicalization of the thread key to the active dialogue anchor (`state.get('last_case_bot_msg_id')`) unifies both incoming messages under identical thread identifiers `(chat_id, 177380, "dialogue_thread")`.
   - *Empirical Proof*: In `test_arrival_delays_spectrum_0_1s_1s_18s_30s_36s`, message 1 at t=0s sets cooldown. At t=18s, `check_user_cooldown` returns 17s remaining, immediately blocking the second message. At t=0.1s (cd=35s), t=1.0s (cd=34s), and t=30.0s (cd=5s), incoming messages are strictly rejected. Only after the 35s window expires (t=36.0s) is a new turn permitted (cd=0).

2. **Thread Safety and Lock Exclusivity Under True Parallel Concurrency (O1, O3)**:
   - *Observation*: `test_multi_threaded_hammer_on_check_user_cooldown` fired 100 concurrent OS threads simultaneously through a `threading.Barrier` targeting the same key.
   - *Logic*: In CPython with GIL, `check_user_cooldown` executes atomically without yields, guaranteeing that exactly one caller receives 0 and sets `USER_COOLDOWNS[key] = now`.
   - *Empirical Proof*: Out of 100 simultaneous threads, exactly 1 thread received 0; exactly 99 threads received > 0. In `test_concurrent_tasks_same_dialogue_thread_only_one_proceeds`, 10 concurrent async tasks were evaluated in `check_and_trigger_assistant`. Exactly 1 task generated a response and called `send_message`, while 9 tasks returned `False` immediately.

3. **Leak-Proof Lock Release Guarantee (O1, O3)**:
   - *Observation*: Locks in `_ACTIVE_DIALOGUE_THREADS` are added right before the main try block and discarded in the matching `finally` block (`assistant.py:3657-3659`).
   - *Logic*: Python's `try...finally` construct guarantees that `finally` executes regardless of whether execution completes normally or raises `asyncio.TimeoutError`, `RuntimeError`, `asyncio.CancelledError`, `MemoryError`, or `ConnectionError`. `set.discard()` is idempotent and never raises `KeyError`.
   - *Empirical Proof*: Tests `test_lock_release_on_simulated_timeout_error`, `test_lock_release_on_simulated_cascade_exhaustion_exception`, `test_lock_release_on_simulated_task_cancellation`, `test_lock_release_on_simulated_telegram_network_error`, and `test_high_frequency_fault_injection_50_iterations` verified that after any exception or high-frequency fault burst, `len(_ACTIVE_DIALOGUE_THREADS)` returned to exactly 0.

4. **Progressive 503 Escalation and DoS Protection (O2, O3)**:
   - *Observation*: Transient Google/Groq overload errors trigger `_record_model_server_failure(model_name)`.
   - *Logic*: Rapid consecutive failures within 15 minutes indicate persistent upstream outages, requiring exponential backoff to prevent API quota burning and log flooding.
   - *Empirical Proof*: `test_progressive_ladder_exact_durations_60s_300s_1200s` demonstrated exact step intervals (60s -> 300s -> 1200s -> 1200s plateau). `test_success_resets_ladder_immediately` confirmed that a single successful inference via `note_success` resets the model history back to step 1 (60s). `test_window_expiration_after_15_minutes_resets_ladder` proved that spaced failures (>900s) do not accumulate into 20-minute bans. `test_multi_model_cooldown_isolation` verified that failures on Model A do not penalize Model B. `test_corrupted_model_failures_json_resilience` proved resilience against malformed disk JSON.

---

## 3. Caveats

- **External OS Process Restarts**: `_ACTIVE_DIALOGUE_THREADS` and `USER_COOLDOWNS` reside in Python memory. If the host machine reboots or the supervisor kills the process with SIGKILL (bypassing normal shutdown), in-memory locks and debounces reset. However, `model_failures.json`, `banned_models.json`, and `key_cooldowns.json` are persisted to disk via atomic tempfile rename, so model and key cooldowns survive process restarts.
- **Clock Drift**: Cooldowns assume monotonic forward system time. Large backward NTP clock jumps would extend cooldowns until the clock catches up, which is standard behavior for datetime-based cooldown guards.
- No caveats found that undermine runtime safety or correctness.

---

## 4. Conclusion

All empirical stress tests and edge-case challenge harnesses for Concurrency & Race Conditions (Class 1) and DoS / Cascade Exhaustion (Class 5) have passed with 100% success rate. The dual-reply vulnerability (18s window) has been empirically proven mitigated via canonical thread anchor resolution and dual-key debounce. Parallel generation tasks for the same dialogue thread are strictly blocked (exactly 1 proceeds, all others debounced). Lock release in `finally` blocks is leak-proof under all failure modes. The progressive 503 cooldown ladder (60s -> 300s -> 1200s) operates exactly to specification.

**Verdict: APPROVE**

---

## 5. Verification Method

To independently reproduce and verify all findings:

1. **Run the dedicated empirical challenger stress test suite**:
   ```powershell
   python test_empirical_challenger_concurrency_dos.py
   ```
   *Expected output*: `Ran 17 tests in ~0.2s ... OK`

2. **Run the complete Red Team deep verification suite**:
   ```powershell
   python test_redteam_deep.py
   ```
   *Expected output*: `Ran 25 tests in ~0.04s ... OK`

3. **Run all core regression test suites**:
   ```powershell
   python test_recon_fixes.py
   python test_multimodal_hybrid.py
   python test_dialogue_reply_limit.py
   python test_passive_gate.py
   python test_silent_failures.py
   ```
   *Expected output*: All suites pass with 0 failures.

4. **Verify bytecode compilation**:
   ```powershell
   python -m py_compile assistant.py gemini_client.py config.py test_redteam_deep.py test_empirical_challenger_concurrency_dos.py
   ```
   *Expected output*: Exit code 0, 0 output errors.
