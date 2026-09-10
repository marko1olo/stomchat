# Scope: StomChat Chat Balance, Runtime Logs, and SQLite Database Audit

## Architecture
- Runtime Logs: `bot.log`, `bot_supervisor.log`, `assistant_state.json`
- SQLite Databases: `stomat_bot.db` (`messages`, `pm_messages`, `user_memories`, `bot_sent_messages`), `stomat_archive.db`
- Trigger & Triage Codebase: `assistant.py`, `config.py`, `main.py`, and related modules
- Final Deliverable: `REPORT_CHAT_BALANCE_AND_LOGS.md`

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---|---|---|---|
| 1 | R1 Log Trigger & Silence Breakdown | Quantitative stats on triggers vs silences, all suppression causes in logs | M1 | ORIGINAL_REQUEST §R1 |
| 2 | R1 False-Negative Silence Identification | Real log snippets & instances where clinical queries were improperly silenced | M1 | ORIGINAL_REQUEST §R1 |
| 3 | R2 Database Sentiment & Reactions | Clinician reactions in chat & PM (positive, constructive, skeptical, frustrated) | M2 | ORIGINAL_REQUEST §R2 |
| 4 | R2 Clinical Specialty Engagement | Breakdown across endo, implant, surgery, ortho, prosthetics | M2 | ORIGINAL_REQUEST §R2 |
| 5 | R2 Multi-turn Conversation Depth | Desired turns vs actual turns doctors have with bot | M2 | ORIGINAL_REQUEST §R2 |
| 6 | R3 Mathematical Modeling & Rebalancing | Dynamic cooldown vs 120m, freshness window, triage sensitivity, validator tuning | M3 | ORIGINAL_REQUEST §R3 |
| 7 | Final Report Generation | Synthesis into REPORT_CHAT_BALANCE_AND_LOGS.md with code diffs | M3 | Deliverable |
| 8 | Verification & Gate Audit | Reviewer, Challenger, and Forensic Auditor verification | M4 | Quality Gate |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Log & Runtime Audit (R1) | bot.log, bot_supervisor.log, assistant_state.json analysis | none | PLANNED |
| M2 | Database & Sentiment Audit (R2) | stomat_bot.db, stomat_archive.db analysis (42k+ active, 117k+ archive, 351 PMs) | none | PLANNED |
| M3 | Rebalancing & Synthesis (R3) | Mathematical justification, code patches, REPORT_CHAT_BALANCE_AND_LOGS.md | M1, M2 | PLANNED |
| M4 | Gate & Forensic Verification | Reviewer, Challenger, and Auditor verification | M3 | PLANNED |

## Interface Contracts
- Explorer 1 Report: `.agents/teamwork_preview_explorer_logs_1/analysis_logs.md` & `handoff.md`
- Explorer 2 Report: `.agents/teamwork_preview_explorer_db_1/analysis_db.md` & `handoff.md`
- Explorer 3 Report: `.agents/teamwork_preview_explorer_code_1/analysis_code.md` & `handoff.md`
- Worker Deliverable: `REPORT_CHAT_BALANCE_AND_LOGS.md`
