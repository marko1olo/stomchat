# Worker M3 Context

- Role: teamwork_preview_worker
- Identity: teamwork_preview_worker_m3
- Working Directory: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_worker_m3
- Authoritative Request: c:\Users\danat\Desktop\stomchat\.agents\ORIGINAL_REQUEST.md (latest section: ## 2026-09-13T11:26:13Z)
- Mission: Implement production hardening patches across assistant.py, gemini_client.py, config.py:
  1. Thread Debounce / Concurrency Lock (canonical thread anchor, fast-fail entrance check, in-flight registry, 35s window).
  2. Pediatric Safety Guard (programmatic pre-check calculating double ceiling dose with math.floor, <15kg contraindication alert).
  3. Adversarial Input Sanitization (neutralize XML tags, pre-LLM regex filter against jailbreak strings and controlled substance prescriptions).
  4. Cascading & 503 transient cooldown adjustments in gemini_client.py.
  5. Verify py_compile clean and existing regression tests pass.
