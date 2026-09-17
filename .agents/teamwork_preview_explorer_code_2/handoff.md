# Handoff Report — Codebase Architecture & Red Teaming Survey

**Agent:** `teamwork_preview_explorer_code_2`  
**Parent:** `orchestrator_6` (`6c2dc5ab-edd6-4b46-ba53-af48fdfe521f`)  
**Directory:** `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_code_2`  
**Handoff Type:** Hard (Survey & Investigation Milestone Complete)

---

## 1. Observation

### Obs 1: Concurrency & Message Dispatch Mechanism
- In `main.py:2185-2221`:
  ```python
  runtime_guard.create_task(run_assistant_safe(), name=f"assistant_{msg_id}")
  ```
  Every incoming group message spawns an independent, concurrent `asyncio.Task` without any per-thread or per-user `asyncio.Lock` or queue (in contrast to private messages where `_pm_user_lock` is used at `main.py:2347`).
- In `assistant.py:2615-2983`:
  `check_and_trigger_assistant` runs DB fetches, context assembly, and the async LLM triage `check_dialogue_continuation_triage` (`assistant.py:2782`) BEFORE reaching the dialogue debounce check at lines 2976-2983:
  ```python
  if is_dialogue and sender_id:
      thread_root_id = reply_to_msg_id or msg_id
      dialogue_cd = check_user_cooldown(event.chat_id, thread_root_id, "dialogue_thread", seconds=25)
      if dialogue_cd > 0:
          return False
  ```
- In `stomat_bot.db` records for incident messages 177390 & 177392:
  - Msg 177388 (13:26:53): Alexey sends "По уступу сидит хорошо", `reply_to_msg_id=None`.
  - Msg 177389 (13:27:12, +19s): Alexey sends "Там где стрелка это винир", `reply_to_msg_id=None`.
  - For msg 177388, `thread_root_id` was calculated as `177388`.
  - For msg 177389, `thread_root_id` was calculated as `177389`.
  - Both keys in `USER_COOLDOWNS` were distinct (`(chat_id, 177388, "dialogue_thread")` vs `(chat_id, 177389, "dialogue_thread")`), allowing both tasks to execute concurrently and send replies at 13:27:14 (msg 177390) and 13:27:32 (msg 177392), just 18 seconds apart.

### Obs 2: Anesthetic Dosage Rules & Pediatric Ceiling
- Rule 12.1 in `assistant.py:3111-3117` and `3170-3176` defines dosage rules in prompt text:
  - Articaine 4%: adults 7 mg/kg <= 500 mg; children <= 5 mg/kg, rounding strictly down.
  - Mepivacaine 3%: 4.4 mg/kg <= 400 mg.
  - Lidocaine 2%: adults 7 mg/kg <= 500 mg; children 4.4 mg/kg <= 500 mg.
- Programmatic function `calculate_anesthesia_instant(text)` is implemented at `assistant.py:653`, but it is ONLY called for `/calc` in private messages (`assistant.py:4381, 4644`). In group chat (`check_and_trigger_assistant`), it is NEVER invoked.
- When tested with `calculate_anesthesia_instant("артикаин ребенок 12 кг")`:
  - 12 kg * 5 mg/kg = 60 mg. 1 carpule of 1.7 ml 4% articaine contains 68 mg.
  - Output says: "Точное значение: 60 / 68 = 0.88 карпул. Безопасный максимум: до 0 карпул (0 мг)".
  - There is no clinical clarification of partial carpule volume (<1.5 ml) or alert that articaine is contraindicated for children under 4 years old (<15 kg).

### Obs 3: Prompt Injection & Sanitization
- In `assistant.py:3075-3077`:
  ```python
  <user_dialogue>
  {chr(10).join(context_msgs)}
  </user_dialogue>
  ```
  And in `fetch_dynamic_chat_context` (`assistant.py:2605`):
  `line = f"[Сообщение #{r_msg_id}{rep_str}] {sender_label}: {msg_text}"`
  Raw message text is interpolated into XML tags without escaping `<` or `>` or sanitizing closing tags `</user_dialogue>`.
- In non-dialogue mode (`assistant.py:3140-3141`), there are no XML tags at all.
- Rule 14.1 (`assistant.py:3119-3121, 3178-3180`) instructs against jailbreaks and narcotic prescription requests, but there is no deterministic pre-LLM regex filter.

### Obs 4: Multimodal Hybrid Pipeline & Visual Artifacts
- `vision.py:226` mandates:
  `"КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО отмахиваться фразами вроде «это пиксели с экрана, дай КЛКТ» или требовать другой снимок вместо анализа."`
  This forces the vision model to guess pathology even on blurred or high-glare images.
- In dialogue chain #5 (177380 -> 177392), surface specular glare on polished ceramic was hallucinated as a "ступенька" (ledge) and had to be retracted by the bot in follow-up.

### Obs 5: Denial-of-Service & API Cascade Limits
- In `gemini_client.py:424`:
  A single 500, 502, 503, or 504 error triggers `ban_model(model_name, MODEL_BAN_SECONDS)` where `MODEL_BAN_SECONDS = 1200` (20 minutes).
- In `blocking_tools.py:557-578`:
  `_pace_gemini_calls()` spaces the *start* of subprocesses by ~2.5 seconds, but does not bound the *concurrency* of simultaneously running LLM subprocesses.

### Obs 6: Regression Test Suite Verification
All existing test commands were executed and passed cleanly:
- `python test_recon_fixes.py` -> 3 tests OK (0.003s)
- `python test_multimodal_hybrid.py` -> 6 tests OK (0.048s)
- `python test_dialogue_reply_limit.py` -> 8 checks OK (0.050s)
- `python test_passive_gate.py` -> 19 checks OK (0.120s)
- `python test_silent_failures.py` -> 11 checks OK (0.150s)
- `python test_redteam_deep.py` -> 5 tests OK (0.018s)

---

## 2. Logic Chain

1. **Double-Reply Vulnerability (Obs 1):**  
   Because `main.py` launches concurrent tasks without a mutex, and `thread_root_id = reply_to_msg_id or msg_id` falls back to `msg_id` when doctors post without hitting "Reply", sequential follow-ups generate distinct cooldown keys. Because the debounce check occurs AFTER slow async triage, two tasks run simultaneously and post within 18 seconds.
   *Inference:* The debounce key must be canonicalized to the active dialogue anchor (`last_case_bot_msg_id`), placed at the entrance before triage, and backed by an in-flight task registry.

2. **Pediatric Toxic Overdose Risk (Obs 2):**  
   Because `calculate_anesthesia_instant` is never called in group chats, dosage queries are 100% reliant on LLM arithmetic. For a 12 kg child, 60 mg / 68 mg per carpule yields 0.88 carpules. LLMs frequently round 0.88 to 1 carpule (overdose) or fail to enforce the <15 kg age/weight contraindication.
   *Inference:* A deterministic pre-check (`Pediatric Safety Guard`) must compute double ceiling with `math.floor` and inject exact boundaries or direct responses before LLM invocation.

3. **Prompt Injection & Persona Hijacking (Obs 3):**  
   Because raw user messages are concatenated into `<user_dialogue>` without escaping, closing tag breakouts and fake system prompts can reach the LLM. Relying purely on prompt Rule 14.1 wastes tokens and risks adversarial bypass.
   *Inference:* Programmatic pre-screening for jailbreak strings and controlled substance prescriptions (`148-1/у`, tramadol, pregabalin) must short-circuit adversarial requests in 0 ms.

4. **Visual Diagnostic Uncertainty (Obs 4):**  
   Because the vision prompt forbids expressing pixel/resolution limitations, specular reflections on glaze/polish are hallucinated as marginal defects (as seen in dialogue #5).
   *Inference:* Vision and assistant prompts must mandate epistemic uncertainty calibration for optical artifacts and glare.

5. **Cascade API Exhaustion (Obs 5):**  
   Because a single 503 error bans a model for 20 minutes across all processes, transient Google Cloud spikes cause cascade collapse down to Groq or total failure.
   *Inference:* Model bans should be progressive (60s for transient 503, escalating to 20m only on persistent failure), and subprocess concurrency should be capped via a semaphore.

---

## 3. Caveats

- **Read-only survey:** No source code was modified during this phase in accordance with instructions.
- **Production log availability:** Audit was based on `stomat_bot.db`, `bot.log`, and source code. No live Telegram API requests were made.
- **No external API probing:** Consistent with global safety rules, no external API keys were stressed or spammed.

---

## 4. Conclusion

The StomChat architecture is robust in core dialogue flow and RAG indexing, but has 3 critical vulnerabilities that require immediate code hardening:
1. **Thread Race Condition:** Duplicate replies happen on sequential messages without Reply buttons due to `msg_id` fallback in cooldown keys.
2. **Pediatric Pharmacology:** Lack of programmatic pre-checks allows potential LLM dosage rounding errors for children <15 kg.
3. **Model Ban & Concurrency Cascade:** 20-minute bans on transient 503s risk taking the entire Gemini pool offline under burst traffic.

Detailed architectural recommendations and code specifications are documented in:
`c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_code_2\report_code.md`

---

## 5. Verification Method

To independently verify all findings and test suite integrity:

1. **Verify regression test suite:**
   ```powershell
   python test_recon_fixes.py
   python test_multimodal_hybrid.py
   python test_dialogue_reply_limit.py
   python test_passive_gate.py
   python test_silent_failures.py
   python test_redteam_deep.py
   ```
   *Expected result:* All 6 commands exit with code 0.

2. **Verify double-reply database evidence:**
   ```powershell
   python -c "import sys, sqlite3; sys.stdout.reconfigure(encoding='utf-8'); conn = sqlite3.connect('stomat_bot.db'); cur = conn.cursor(); cur.execute('SELECT msg_id, date, sender_name, reply_to_msg_id, text FROM messages WHERE msg_id IN (177388, 177389, 177390, 177392)'); [print(row) for row in cur.fetchall()]"
   ```
   *Expected result:* Shows 177390 and 177392 sent 18 seconds apart responding to 177388 and 177389.

3. **Verify pediatric calculation edge case (<15 kg):**
   ```powershell
   python -c "import sys, assistant; sys.stdout.reconfigure(encoding='utf-8'); print(assistant.calculate_anesthesia_instant('артикаин ребенок 12 кг'))"
   ```
   *Expected result:* Shows 60 mg limit vs 68 mg carpule, resulting in 0 full carpules.
