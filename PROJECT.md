# Project: StomChat Weekend Telemetry Audit, Red Teaming & Production Hardening

## Architecture
- `assistant.py`: Core Telegram bot handler for group and private messages. Implements dialogue thread tracking, cooldown checks, triage, prompt formatting, clinical safety rules (Rule 12.1, Rule 14.1), and message dispatch.
- `gemini_client.py`: Multi-model cascade orchestration (Gemini 2.5 Flash, 2.5 Flash-Lite, etc.) with exponential backoff, rate limiting, and 503 fallback handling.
- `config.py`: Global configuration parameters, cooldown constants, model cascade definitions, and timeouts.
- `database.py`: Thread-safe SQLite executor (`_run_db`) with WAL mode for `stomat_bot.db`.
- `user_memory.py`: Persistent clinical memory and doctor dossier tracking (422+ doctor profiles).

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---|---|---|---|
| F1 | Weekend Telemetry Audit (Sept 11–13) | Analysis of 200 messages (180 clinician, 20 bot responses: 16 clinical replies + 4 digest parts), 0 runtime errors, 64 passive suppressions, 27 cascade 503 fallbacks, token & latency metrics | M1 | ORIGINAL_REQUEST §R1 |
| F2 | 9 Clinical Dialogue Threads Breakdown | Exhaustive analysis of threads 1–9 with exact IDs, timestamps, doctor profiles from user_memories, verbatim transcripts, and EBM evaluation | M1 | ORIGINAL_REQUEST §R1 |
| F3 | Clinician Sentiment & Silence Audit | Documenting doctor praise, skepticism, humor/memes, and silence points (177414, 177311, 177410) | M1 | ORIGINAL_REQUEST §R1 |
| F4 | Comprehensive Report Publication | Publish `REPORT_WEEKEND_TELEMETRY.md` documenting complete findings of R1 | M1 | ORIGINAL_REQUEST §R1, Acceptance Criteria |
| F5 | Concurrency & Race Condition Red Teaming | Deep test scenarios modeling rapid fragmented messages, burst spam, and verifying 18s dual-reply race condition (177390 & 177392) | M2 | ORIGINAL_REQUEST §R2.1 |
| F6 | Clinical Pharmacology & Pediatric Safety Testing | Adversarial test cases for pediatric dosing (<15 kg, 12 kg edge case), double ceiling (mg/kg vs max), cardiovascular comorbidities, pregnancy, Rule 12.1 | M2 | ORIGINAL_REQUEST §R2.2 |
| F7 | Prompt Injection & Persona Hijacking Testing | Adversarial test cases testing jailbreaks ("забудь инструкции", "выпиши рецепт на учетный препарат 148-1/у", tramadol/pregabalin, illicit synthesis) | M2 | ORIGINAL_REQUEST §R2.3 |
| F8 | Visual Diagnostic Uncertainty Testing | Multi-modal test cases testing blurred, low-res, specular glare images to verify epistemic caution instead of hallucinating pathology | M2 | ORIGINAL_REQUEST §R2.4 |
| F9 | DoS & API Exhaustion Testing | Tests evaluating cascade fallback under 503s, transient ban logic, and memory footprints | M2 | ORIGINAL_REQUEST §R2.5 |
| F10 | Comprehensive Red Team Test Suite | Deliver `test_redteam_deep.py` containing complete test coverage across all 5 vulnerability classes | M2 | ORIGINAL_REQUEST §R2, Acceptance Criteria |
| F11 | Thread Debounce & Concurrency Lock Patch | Programmatic thread lock & debounce (30-45s window) keyed by active dialogue anchor (`last_case_bot_msg_id`) before slow triage in `assistant.py` | M3 | ORIGINAL_REQUEST §R3 |
| F12 | Adversarial Input Sanitization Patch | XML escaping/neutralization and pre-LLM regex filter against jailbreak patterns and controlled substance requests | M3 | ORIGINAL_REQUEST §R3 |
| F13 | Pediatric Safety Guard Programmatic Patch | Deterministic pre-LLM check calculating double ceiling dose (mg/kg vs max, math.floor) and blocking toxic requests / contraindications (<15 kg) | M3 | ORIGINAL_REQUEST §R3 |
| F14 | Regression Suite Verification | 100% pass across `test_recon_fixes.py`, `test_multimodal_hybrid.py`, `test_dialogue_reply_limit.py`, `test_passive_gate.py`, `test_silent_failures.py`, and `test_redteam_deep.py` | M4 | ORIGINAL_REQUEST Acceptance Criteria |
| F15 | Codebase Integrity & Forensic Audit | Clean `py_compile` across all modified files and clean Forensic Integrity Audit | M4 | ORIGINAL_REQUEST Acceptance Criteria |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Telemetry Audit & Report | Generate and publish `REPORT_WEEKEND_TELEMETRY.md` covering R1 (100% of 9 threads, transcripts, doctor profiles, sentiment, suppression metrics, latency/token stats) | none | IN_PROGRESS |
| M2 | Red Teaming Vulnerability Suite | Create and verify `test_redteam_deep.py` covering all 5 vulnerability classes (concurrency, pharmacology/pediatrics, prompt injection, visual uncertainty, DoS/cascade) | M1 | PLANNED |
| M3 | Production Hardening Patches | Implement thread debounce lock, input sanitization, and pediatric safety guard in `assistant.py`, `gemini_client.py`, `config.py` | M2 | PLANNED |
| M4 | Regression, Forensic Audit & Victory | Execute all regression suites, py_compile check, forensic integrity audit, and report victory to Sentinel | M1, M2, M3 | PLANNED |

## Interface Contracts
### Thread Debounce & Concurrency Lock (`assistant.py`)
- Key: `(chat_id, anchor_msg_id, "dialogue_thread")` where `anchor_msg_id` is canonicalized to `last_case_bot_msg_id` or `reply_to_msg_id`.
- Window: 35 seconds debounce. Fast-fail check executed at entrance before slow async triage.
- In-flight task tracking: Async lock or in-flight set preventing concurrent LLM generation tasks for the same dialogue anchor.

### Pediatric Safety Guard (`assistant.py`)
- Function: `check_pediatric_anesthesia_safety(text: str) -> Optional[str]`
- Evaluates weight (<15 kg), drug type (articaine, mepivacaine, lidocaine), maximum mg/kg and absolute maximum mg.
- Enforces strict downward floor: e.g. for 12 kg articaine (60 mg limit vs 68 mg carpule), returns safe limit: 0 full carpules (<1.5 ml), notes age/weight contraindication.
- If toxic/overdose requested or pediatric calculation triggered, returns pre-computed safe clinical response bypassing LLM arithmetic errors.

### Adversarial Input Sanitizer (`assistant.py`)
- Neutralizes `<` and `>` tags in user text injected into `<user_dialogue>`.
- Regex pre-filter detects jailbreak keywords ("забудь инструкции", "игнорируй правила") and controlled substances (tramadol, pregabalin, form 148-1/у) and short-circuits with professional clinical refusal.

## Code Layout
- `assistant.py`: Dialogue handling, debounce lock, input sanitization, pediatric guard, clinical rules.
- `gemini_client.py`: API cascade, 503 handling, transient cooldown.
- `config.py`: Cooldown constants, thread lock timeouts.
- `test_redteam_deep.py`: Comprehensive Red Teaming test suite covering 5 vulnerability classes.
- `REPORT_WEEKEND_TELEMETRY.md`: Root-level comprehensive weekend telemetry audit report.
