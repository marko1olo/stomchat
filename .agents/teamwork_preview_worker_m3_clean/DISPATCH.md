## 2026-09-13T11:52:23Z
You are teamwork_preview_worker_m3_clean.
Your working directory is: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m3_clean
Your parent is: orchestrator_6 (conversation ID: 6c2dc5ab-edd6-4b46-ba53-af48fdfe521f)

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

MANDATORY: Read c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md (specifically the latest section starting with ## 2026-09-13T11:26:13Z) before starting any work.

MISSION (Milestone 3: Production Hardening & Architectural Mitigations):
Implement targeted, high-reliability production code patches across:
1. `assistant.py`
2. `gemini_client.py`
3. `config.py`

Refer to the architectural specifications and root-cause analysis in:
`c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_code_2\report_code.md`

REQUIRED IMPLEMENTATIONS:
1. Concurrency Debounce & In-Flight Thread Lock (Mitigates 18s double-reply race condition 177390 & 177392):
   - In `assistant.py`:
     - Canonical thread ID calculation: when `is_dialogue` is True and `reply_to_msg_id` is None, canonicalize the thread key to the active dialogue anchor (`state.get("last_case_bot_msg_id")`) instead of falling back to raw `msg_id`.
     - Fast-fail entrance debounce: execute debounce checks on `(chat_id, resolved_thread_id)` and `(chat_id, sender_id)` at the very beginning of dialogue handling BEFORE the slow async LLM triage (`check_dialogue_continuation_triage`).
     - In-flight task registry (`_ACTIVE_DIALOGUE_THREADS = set()`): track ongoing dialogue generations so that rapid incoming messages cannot launch duplicate parallel LLM generations for the same thread. Ensure safe cleanup via try/finally.
   - In `config.py`:
     - Define `DIALOGUE_THREAD_DEBOUNCE_SECONDS = 35` (configurable 30-45s window).
2. Pediatric Safety Guard (Rule 12.1 Programmatic Pre-Check):
   - In `assistant.py`:
     - Implement deterministic pre-LLM check function `check_pediatric_anesthesia_safety(text: str)`:
       - Detects anesthetic calculation intent (articaine, mepivacaine, lidocaine) + pediatric indicators (weight <15 kg, ребенок, малыш, etc.).
       - Enforces double ceiling: min(weight * dose_per_kg, max_abs_dose).
       - Enforces strict downward floor for carpules (`math.floor(max_dose / carpule_dose)`).
       - Explicitly flags clinical contraindications (articaine contraindicated for children <4 years / <15 kg; 1 carpule of 68 mg exceeds the 60 mg limit for 12 kg child).
       - Either returns direct safe clinical calculation response or injects ground-truth calculation block into system prompt, guaranteeing zero toxic dosage hallucinations or rounding errors.
3. Adversarial Input Sanitization:
   - In `assistant.py`:
     - Sanitize user message text interpolated into `<user_dialogue>` by neutralizing/escaping XML angle brackets `<` and `>`.
     - Deterministic pre-LLM regex filter against jailbreak patterns ("забудь инструкции", "игнорируй правила", "ты теперь DAN") and controlled substances ("трамадол", "прегабалин", "лирика", "морфин", "фентанил", "148-1/у", "кустарный синтез"). Returns professional refusal without wasting LLM tokens.
4. Cascade 503 & Timeout Resilience:
   - In `gemini_client.py`:
     - Optimize temporary ban behavior so transient 503 errors use progressive cooldown (e.g. 60s initially) rather than an immediate 20-minute ban across the board.

VERIFICATION & TESTING:
- Run `python -m py_compile assistant.py gemini_client.py config.py` to confirm zero syntax errors.
- Run all regression test suites:
  - `python test_recon_fixes.py`
  - `python test_multimodal_hybrid.py`
  - `python test_dialogue_reply_limit.py`
  - `python test_passive_gate.py`
  - `python test_silent_failures.py`
  - `python test_redteam_deep.py`
  All must pass 100% cleanly!

Deliverables:
- Modified files: `assistant.py`, `gemini_client.py`, `config.py`
- Handoff report: `c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m3_clean\handoff.md`

When done, send a message to parent (6c2dc5ab-edd6-4b46-ba53-af48fdfe521f) with detailed diffs and verification results.
