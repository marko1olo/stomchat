# BRIEFING — 2026-09-04T13:45:00Z

## Mission
Survey user_memory.py and assistant.py to map technical details for R1 (E2E clinical interaction simulation for doctor memory in PM and group chat).

## 🔒 My Identity
- Archetype: explorer
- Roles: survey, analysis
- Working directory: c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_survey_1
- Original parent: 2eadec10-c0ef-4c69-9101-916f4567ad8a
- Milestone: Survey 1 (Memory & Assistant)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement
- СТРОЖАЙШИЙ ЗАПРЕТ: НЕ ОТПРАВЛЯТЬ ТЕСТОВЫЕ СООБЩЕНИЯ В ПРОД, В ТЕЛЕГРАМ-ГРУППУ ИЛИ РЕАЛЬНЫМ ПОЛЬЗОВАТЕЛЯМ!
- Cooldown 2.5-3 секунды между обращениями к LLM API
- Windows terminal escaping safety: never run multiline powershell with variables in terminal parameters; write scripts to scratch files and execute by path
- Do not modify production code

## Current Parent
- Conversation ID: 2eadec10-c0ef-4c69-9101-916f4567ad8a
- Updated: not yet

## Investigation State
- **Explored paths**: `user_memory.py`, `assistant.py`, `database.py`, `test_user_memory.py`, `test_budget_nesting.py`, `test_fix_pm.py`, `test_startup_boot.py`, `main.py`
- **Key findings**:
  - `user_memories` table holds both PM memory (`clinical_summary`, 64KB) and group memory (`group_summary`, 8KB).
  - Trivial messages ('спасибо', 'ок') exit early without incrementing counters or calling LLM.
  - Compaction in PM occurs at `pm_message_count % 4 == 0` or when `has_rich_case` matches dental case keywords.
  - A 15-second cooldown `_LAST_PM_UPDATE_TS` must be bypassed/cleared in rapid E2E simulation tests.
  - Facts deduplicate via Python `not in current_facts`, but sentence deduplication for `clinical_summary` currently relies on LLM prompt instructions (no programmatic deduplicator in Python).
  - Group daemon polls active doctors with >= 3 messages (> 15 chars) since `last_group_analyzed_id`. If none, it returns with 0 LLM calls.
  - Assistant injects PM memory into prompt as `{portrait}` across 3 prompt branches, and group memory as `users_chunk_context` (up to 20 doctors, 300 chars each).
  - All 4 existing regression test suites pass 100%. `ruff check` on `user_memory.py` and `database.py` gives 0 errors.
- **Unexplored areas**: None for this survey milestone.

## Key Decisions Made
- Completed full analysis and verified regression tests and linter state.
- Generated `survey_memory_report.md` and `handoff.md`.

## Artifact Index
- DISPATCH.md — Incoming assignment
- survey_memory_report.md — Technical survey report
- handoff.md — 5-component handoff report
- progress.md — Liveness heartbeat
