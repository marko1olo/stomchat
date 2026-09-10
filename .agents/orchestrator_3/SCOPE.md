# Scope: Chat Balance and Logs Audit

## Architecture
- Runtime logs: `bot.log`, `bot.log.1`, `bot.log.2`, `bot_supervisor.log`
- Persistent state: `assistant_state.json`
- SQLite databases: `stomat_bot.db` (messages, pm_messages, user_memories, bot_sent_messages) and `stomat_archive.db`
- Core assistant logic: `assistant.py`, `database.py`

## Feature Inventory & Requirements Mapping
| # | Requirement | Source | Milestone | Assigned Agent | Status |
|---|---|---|---|---|---|
| 1 | R1: Bot Activity vs Silence Audit (Logs & Runtime) | ORIGINAL_REQUEST §R1 | M1 | teamwork_preview_explorer_logs_2 | Planned |
| 2 | R2: User Messages & Sentiment Analysis (Databases) | ORIGINAL_REQUEST §R2 | M2 | teamwork_preview_explorer_db_2 | Planned |
| 3 | R3: Code Triage Architecture & Rebalancing | ORIGINAL_REQUEST §R3 | M3 | teamwork_preview_explorer_code_1 | Done (analysis_code.md ready) |
| 4 | Final Deliverable: REPORT_CHAT_BALANCE_AND_LOGS.md | Acceptance Criteria | M4 | teamwork_preview_worker_report_1 | Planned |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| 1 | Log & Runtime Audit | Complete parsing of all bot logs & assistant_state.json | None | IN_PROGRESS |
| 2 | Database & Sentiment Audit | SQL analysis of active, archive, and PM datasets | None | IN_PROGRESS |
| 3 | Code Triage & Dynamic Models | Architectural analysis and formulas | None | DONE |
| 4 | Synthesis & Deliverable Compilation | Compile REPORT_CHAT_BALANCE_AND_LOGS.md | M1, M2, M3 | PENDING |
| 5 | Review & Forensic Audit Gate | Reviewers, Challenger & Forensic Auditor verification | M4 | PENDING |
