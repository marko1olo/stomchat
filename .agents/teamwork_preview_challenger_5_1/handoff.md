# Adversarial Challenge Report: Mathematical Models, Edge Cases & Safety Verification

**Auditor:** Adversarial Challenger 1 (Mathematical Models & Edge-Case Stress Verifier)  
**Date:** September 8, 2026  
**Working Directory:** `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_5_1`  
**Parent Orchestrator ID:** `dbf85257-c028-4cb2-88f2-d96c00e70a01`  
**Target Deliverable Evaluated:** `c:\Users\danat\Desktop\stomchat\REPORT_CHAT_BALANCE_AND_LOGS.md`  
**Overall Verdict:** **`REQUEST_CHANGES`**

---

## 1. Observation

Direct empirical observations, executed verification code outputs, and verbatim codebase inspections:

### Obs 1: Arithmetic Discrepancies Between Table 4.1.3 and Section 4.1.1 / Diff 2 Formula
- **Report Section 4.1.1 Formulation:**
  $$T_{\text{cooldown}}(V, H) = \text{clamp}\left( T_{\text{base}} \cdot \left( \frac{V_{\text{target}}}{V_{\text{eff}} + \epsilon} \right)^\alpha \cdot K_{\text{diurnal}}(H), \; T_{\min}, \; T_{\max} \right)$$
  Where $T_{\text{base}} = 60$, $V_{\text{target}} = 30$, $V_{\text{eff}} = \max(V, 5)$, $\epsilon = 1.0$, $\alpha = 0.40$, $K \in \{0.85, 1.00, 1.60\}$.
- **Report Diff 2 (`assistant.py:564-580`):**
  ```python
  v_eff = max(velocity, 5)
  f_vel = (30.0 / (v_eff + 1.0)) ** 0.40
  ...
  raw_cd = config.PASSIVE_COOLDOWN_BASE_MINUTES * f_vel * f_time
  cd_minutes = int(max(config.PASSIVE_COOLDOWN_MIN_MINUTES,
                       min(raw_cd, config.PASSIVE_COOLDOWN_MAX_MINUTES)))
  ```
- **Empirical Execution Result (`test_stress_math_and_cooldown.py`):**
  ```
  V=  5 | Day: claimed=108, got= 97 (diff=-11) | Eve: claimed=127, got=114 (diff=-13) | Night: claimed=180, got=180 (diff=+0)
  V= 15 | Day: claimed= 67, got= 65 (diff=-2)  | Eve: claimed= 79, got= 77 (diff=-2)  | Night: claimed=126, got=123 (diff=-3)
  V= 30 | Day: claimed= 51, got= 50 (diff=-1)  | Eve: claimed= 60, got= 59 (diff=-1)  | Night: claimed= 96, got= 94 (diff=-2)
  V= 60 | Day: claimed= 45, got= 45 (diff=+0)  | Eve: claimed= 46, got= 45 (diff=-1)  | Night: claimed= 74, got= 72 (diff=-2)
  V=120 | Day: claimed= 45, got= 45 (diff=+0)  | Eve: claimed= 45, got= 45 (diff=+0)  | Night: claimed= 56, got= 54 (diff=-2)
  V=300 | Day: claimed= 45, got= 45 (diff=+0)  | Eve: claimed= 45, got= 45 (diff=+0)  | Night: claimed= 45, got= 45 (diff=+0)
  Total velocity rows with arithmetic discrepancies: 5 of 6
  ```
- **Root Cause:** The table in Section 4.1.3 was calculated setting $\epsilon = 0$ ($(30 / V)^{0.4}$), while both the formula in Section 4.1.1 and Diff 2 use $\epsilon = 1.0$ ($(30 / (V + 1))^{0.4}$). At low velocities ($V = 5$), dividing by $6.0$ instead of $5.0$ shifts the cooldown downwards by 11 to 13 minutes.

### Obs 2: Dead Code & Unupdated Call Sites in Diff 2 (`passive_gate_block_reason_async`)
- In `REPORT_CHAT_BALANCE_AND_LOGS.md` lines 585–617, Diff 2 defines:
  `async def passive_gate_block_reason_async(state: dict) -> str | None:`
- In `assistant.py`, lines 1173–1191 define:
  `def passive_gate_block_reason(state):` (synchronous, using `full = timedelta(minutes=PASSIVE_COOLDOWN_MINUTES)` where `PASSIVE_COOLDOWN_MINUTES = 120`).
- Call sites in `assistant.py`:
  - Line 2709: `block_reason = passive_gate_block_reason(state)`
  - Line 2789: `passive_cooldown_active = passive_gate_block_reason(load_state()) is not None`
- Neither line 2709 nor line 2789 is modified by Diff 2, nor does Diff 2 update or replace `passive_gate_block_reason`.
- If applied as written, `passive_gate_block_reason_async` has **0 callers**, and the bot will continue invoking the synchronous 120-minute gate at both lines.

### Obs 3: Volume Bypass Gate Desynchronization (`last_case_bot_msg_id`)
- Diff 2 (lines 596–603) implements the volume gate bypass as:
  ```python
  if since_sent >= timedelta(minutes=config.PASSIVE_COOLDOWN_MIN_MINUTES):
      ref_msg_id = state.get("last_case_bot_msg_id") or 0
      if ref_msg_id:
          msgs_since = await query_db_async(
              "SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < 90000000",
              (ref_msg_id,)
          )
          cnt = msgs_since[0][0] if msgs_since else 0
          if cnt >= config.PASSIVE_VOLUME_GATE_MSGS:
              return None
  ```
- In `assistant.py:3074`, successful text runs call:
  `record_passive_success(pending_thread_id)`
  Notice that `author_id` and `msg_id` are NOT passed!
- Inside `record_passive_success` (`assistant.py:1364–1367`):
  ```python
  if author_id is not None:
      state["last_case_author_id"] = author_id
      state["last_case_bot_msg_id"] = msg_id
      state["last_case_time"] = now_iso
  ```
  Therefore, on standard passive text responses, `state["last_case_bot_msg_id"]` is **never updated**.
- If `last_case_bot_msg_id` is missing or 0, the volume bypass gate will NEVER fire. If it holds an ID from a media case sent days earlier, `msgs_since` will immediately return thousands of messages, bypassing the cooldown on EVERY message after 45 minutes.

### Obs 4: Flawed Dialogue Staleness Logic in Diff 3 (`is_direct_quote_reply`)
- Diff 3 replaces code inside Section 1 (`assistant.py:2559`: `if reply_to_msg_id and BOT_ID:`):
  ```python
  # Differentiate explicit quote-reply from ambient follow-up
  is_direct_quote_reply = bool(reply_to_msg_id)
  max_stale_limit = (
      config.DIALOGUE_MAX_STALE_DIRECT_REPLY 
      if is_direct_quote_reply 
      else config.DIALOGUE_MAX_STALE_SEQUENTIAL
  )
  ```
- Because this code is nested inside `if reply_to_msg_id and BOT_ID:`, `reply_to_msg_id` is guaranteed to be truthy. Therefore, `is_direct_quote_reply` is **always True**, and `DIALOGUE_MAX_STALE_SEQUENTIAL` is **dead code**.
- Section 1.1 (`assistant.py:2670–2704`: `if not is_dialogue and not reply_to_msg_id:`), which actually handles sequential follow-ups from the case author without the Reply button, is **untouched** by Diff 3 and remains hardcoded at `if count_since <= 5:`.
- In addition, when Doctor A replies to Doctor B in a thread where the bot commented earlier (`found_bot_in_chain = True`, but `is_parent_bot = False`), `bool(reply_to_msg_id)` is True. Diff 3 mistakenly classifies this human-to-human reply as a direct bot reply and grants it 25 messages of staleness tolerance, causing the bot to hijack human conversations.

### Obs 5: Dialogue Window Vulnerability to Spam & Fast Clinical Bursts
- Simulation output from `test_stress_dialogue_freshness.py`:
  - **Fast clinical bursts:** When doctors post 26 messages over 4 minutes, Diff 3 (`count_since <= 25`) drops the clinician's follow-up despite only 4 minutes having elapsed (`elapsed_min = 4.0m`).
  - **Spam bursts:** 50 stickers/flood messages in 30 seconds instantly increments `count_since` to 50, cutting off all active doctor consultations with the bot.
  - **Topical drift on slow nights:** In quiet periods (10 messages over 85 minutes), Diff 3 allows the bot to intervene 85 minutes later on a conversation that has long since moved on to non-clinical topics.

### Obs 6: Critical Safety Violation in Diff 5 (Unvalidated Unsolicited Messages)
- In `REPORT_CHAT_BALANCE_AND_LOGS.md` lines 739–743 (Diff 5):
  ```python
  elif not is_dialogue and "cascade exhausted" in reason_lower:
      # Resilient fallback: primary model draft succeeded; secondary validator timed out/503
      logger.warning("Validator cascade exhausted during passive reply. Permitting fallback delivery under warning.")
      quality_ok = True
  ```
- Contrast with the established safety contract in `assistant.py:2143–2147`:
  *"Политика при НЕДОСТУПНОМ валидаторе: invited=False -> глушим черновик. Молчание незваного бота не стоит ничего, непроверенная клиническая отсебятина стоит дорого."*
- Diff 5 forces `quality_ok = True` for uninvited group messages (`not is_dialogue`) when the validator cascade has crashed or timed out, without executing any deterministic dosage/contraindication regex checks.

### Obs 7: Triage Prompt Over-Triggering & Hallucination/Spam Risks (Diff 4)
- Proposed prompt instructions in Diff 4 (`assistant.py:695-696`):
  *"Либо ответы коллег были неполными, сомнительными, односложными (например, просто "удали", "+", "хз") или мнения разделились, и требуется четкий доказательный EBM-протокол."*
- Empirical evaluation in `test_triage_risk_cases.py`:
  - Routine clinical exchanges (e.g. handpiece speed preferences: 150k vs 200k) trigger unsolicited 4-paragraph lectures because "opinions divided".
  - Professional sarcasm (e.g. "patient wants 10-year warranty on buildup without crown 😂") triggers pedantic lectures on ferrule biomechanics because answers are brief.
  - Lowering `TRIAGE_CONFIDENCE_THRESHOLD` to 0.70 compounds false positive triggers on ambiguous chat chatter.

---

## 2. Logic Chain

1. **Premise 1 (Mathematical Integrity):** A production engineering proposal must have internal consistency between its formal formulas, code implementations, and reference sensitivity tables.
   - *Supported by Obs 1:* Table 4.1.3 was derived with $\epsilon = 0$, while the formula and code enforce $\epsilon = 1.0$. This creates an undocumented $-11$ to $-13$ minute variance at $V = 5$.

2. **Premise 2 (Executable Completeness):** Code diffs provided in an architectural report must be syntactically and semantically complete; introducing a new async helper without updating existing synchronous call sites results in dead code and failed implementation.
   - *Supported by Obs 2:* `passive_gate_block_reason_async` is introduced without updating callers at lines 2709 and 2789 in `assistant.py`. The bot would continue using the old synchronous 120m gate.

3. **Premise 3 (State Variable Correctness):** State-dependent bypass gates must reference variables that are reliably updated by all relevant execution paths.
   - *Supported by Obs 3:* `record_passive_success` does not update `last_case_bot_msg_id` on text runs (line 3074). The volume bypass gate will either never fire or evaluate against a multi-day stale message ID.

4. **Premise 4 (Conversational Context Awareness):** Distinguishing direct quote-replies from ambient chatter requires inspecting whether the message replied to was actually sent by the bot (`is_parent_bot`), not simply whether `reply_to_msg_id` exists.
   - *Supported by Obs 4:* In `assistant.py:2559`, `reply_to_msg_id` is always truthy. Diff 3 mistakenly sets `is_direct_quote_reply = bool(reply_to_msg_id)`, which evaluates to True even when Doctor A replies to Doctor B in a thread where the bot spoke. This causes thread hijacking and fails to update the actual sequential follow-up block at line 2691.

5. **Premise 5 (Adversarial Robustness):** Staleness heuristics must survive adversarial conditions (high-speed bursts and spam attacks) without dropping legitimate users or waking up hours later.
   - *Supported by Obs 5:* A fixed count threshold ($N \le 25$) cuts off doctors during 4-minute active discussions ($N = 26$) and instantly breaks under 30-second sticker floods ($N = 50$). A composite time+count rule is required.

6. **Premise 6 (Clinical Patient Safety):** In medical AI systems operating in groups of 750 practitioners, uninvited (passive) advice must fail closed when the validation cascade is unavailable.
   - *Supported by Obs 6:* Diff 5 completely dismantles the fail-closed invariant for passive replies during validator outages, allowing unvalidated LLM drafts (potentially containing hallucinated dosages or contraindications) to reach production without any deterministic safety filter.

7. **Premise 7 (Conversational Triage Balance):** Prompt instructions that direct an uninvited bot to intervene whenever "colleagues' answers are incomplete or opinions divide" transform the assistant from a helpful advisor into an intrusive, condescending nuisance.
   - *Supported by Obs 7:* Real doctor interactions involve banter, routine equipment preference questions, and brevity. Diff 4 causes severe over-triggering on harmless chat banter.

---

## 3. Caveats

- **No Caveats.** All findings were empirically reproduced via standalone test scripts executing against local formulas, database schemas, and codebase inspection. No live network requests were made to production Telegram or external API endpoints.

---

## 4. Conclusion & Required Changes

The comprehensive audit in `REPORT_CHAT_BALANCE_AND_LOGS.md` provides outstanding forensic depth regarding historical logs and clinician sentiment. However, the proposed mathematical diffs and code patches contain **7 critical design and safety flaws** that would cause broken deployments, thread hijacking, spam vulnerability, and potential medical liability.

**Final Verdict:** **`REQUEST_CHANGES`**

### Mandatory Remediations Required:

1. **Reconcile Table 4.1.3 & Formula:**
   - Either adopt $\epsilon = 0$ with $V_{\text{eff}} = \max(V, 5)$ across the formula, code, and table, or update Table 4.1.3 with the exact values produced by $\epsilon = 1.0$ (e.g. $97\text{ min}$ daytime at $V=5$).

2. **Complete the Async Gate Integration in `assistant.py`:**
   - Replace the synchronous `passive_gate_block_reason(state)` entirely or make it a thin synchronous wrapper around cached state.
   - Update call sites at line 2709 and line 2789 to `await passive_gate_block_reason_async(state)`.
   - Update existing tests in `test_passive_gate.py` to support the async signature.

3. **Fix Volume Gate State Tracking:**
   - In `record_passive_success`, explicitly save `state["last_passive_bot_msg_id"] = msg_id` on ALL successful passive runs (including line 3074).
   - In the volume gate query, use `last_passive_bot_msg_id`, and add a sanity cap ensuring the message is not older than 12 hours.

4. **Correct Dialogue Staleness and Target Section 1.1:**
   - In Section 1 (`assistant.py:2559`), use `is_parent_bot` to determine direct quote-reply:
     `max_stale_limit = config.DIALOGUE_MAX_STALE_DIRECT_REPLY if is_parent_bot else 5`
   - In Section 1.1 (`assistant.py:2670–2704`), replace the hardcoded `if count_since <= 5:` with `config.DIALOGUE_MAX_STALE_SEQUENTIAL` (12 msgs).

5. **Implement Spam-Resistant Composite Freshness:**
   - For `is_parent_bot`, approve dialogue continuation if:
     `(count_since <= 25) OR (elapsed_minutes <= 5.0)`
   - Filter out non-alphanumeric flood messages from the `COUNT(*)` calculation.
   - Cap maximum direct reply age at 45 minutes rather than 90 minutes to prevent awkward intrusions after topic shifts.

6. **Restore Fail-Closed Safety in Diff 5:**
   - Delete the fallback override for uninvited replies (`elif not is_dialogue and "cascade exhausted" in reason_lower:`).
   - Uninvited replies MUST fail closed when the validator cascade is exhausted. Fallback delivery should only be permitted for invited replies (`is_dialogue=True` or direct mentions), and only after passing local deterministic regex validation (e.g. lethal dosage checks, banned phrases).

7. **Refine Triage Prompt (Diff 4):**
   - Remove *"или мнения разделились, и требуется четкий доказательный EBM-протокол"*.
   - Explicitly instruct the triage LLM to ignore routine preference discussions (handpiece RPM, brand comparisons), sarcasm, and pricing banter.
   - Retain the confidence threshold at `0.80` rather than dropping to `0.70`.

---

## 5. Verification Method

To independently verify these findings, execute the verification suite in `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_5_1`:

1. **Verify Mathematical Discrepancies:**
   ```powershell
   python -X utf8 c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_5_1\test_stress_math_and_cooldown.py
   ```
   *Expected:* Output demonstrates 5 of 6 velocity rows mismatch Table 4.1.3.

2. **Verify Database Indexes and Query Execution Plans:**
   ```powershell
   python -X utf8 c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_5_1\inspect_db_schema.py
   ```
   *Expected:* Confirms indexes on `messages(date)` and `messages(msg_id)`.

3. **Verify Dialogue Freshness & Spam Vulnerabilities:**
   ```powershell
   python -X utf8 c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_5_1\test_stress_dialogue_freshness.py
   ```
   *Expected:* Confirms failure modes under fast clinical bursts ($N = 26$), sticker floods ($N = 50$), and thread hijacking when replying to humans.

4. **Verify Triage Edge-Case Over-Triggering:**
   ```powershell
   python -X utf8 c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_challenger_5_1\test_triage_risk_cases.py
   ```
   *Expected:* Confirms over-triggering on routine peer exchanges and clinical sarcasm.
