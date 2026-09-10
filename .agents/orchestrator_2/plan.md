# Execution Plan: StomChat Chat Balance & Logs Audit

## Objective
Deliver `c:\Users\danat\Desktop\stomchat\REPORT_CHAT_BALANCE_AND_LOGS.md` fulfilling all requirements (R1, R2, R3) and acceptance criteria.

## Phase 1: Survey & Parallel Exploration
- Step 1.1: Dispatch Explorer 1 to audit runtime logs (`bot.log`, `bot_supervisor.log`, `assistant_state.json`) for trigger breakdowns, silence causes, errors, and false negatives.
- Step 1.2: Dispatch Explorer 2 to audit SQLite databases (`stomat_bot.db` with 42k+ messages, 351 PMs, memories, sent messages; and `stomat_archive.db` with 117k+ messages) for clinical engagement, doctor feedback, sentiment, and dialogue depth.
- Step 1.3: Dispatch Explorer 3 to analyze codebase triggers, thresholds, triage prompt sensitivities, and quality validator logic (`assistant.py`, `config.py`, `main.py`, etc.).

## Phase 2: Synthesis & Deep Dives (Milestones M1 & M2)
- Step 2.1: Synthesize findings into quantitative distributions (trigger vs silence frequencies, suppression cause tables).
- Step 2.2: Extract exact message IDs and real quotes for false-negative silences and multi-turn clinical discussions.
- Step 2.3: Cross-validate clinical specialty distribution and feedback categories (positive, constructive, skeptical, negative/frustrated).

## Phase 3: Mathematical Modeling & Rebalancing Proposals (Milestone M3)
- Step 3.1: Formulate mathematical model for dynamic passive cooldown (message velocity vs fixed 120m).
- Step 3.2: Re-evaluate dialogue freshness constraints (`count_since <= 5`, 10 min window).
- Step 3.3: Triage sensitivity tuning and quality validator threshold adjustments.
- Step 3.4: Dispatch Worker to draft `REPORT_CHAT_BALANCE_AND_LOGS.md` with complete analysis, tables, quotes, and concrete code/config patches.

## Phase 4: Verification & Audit Gate (Milestone M4)
- Step 4.1: Dispatch Reviewer to rigorously verify completeness against all ACs.
- Step 4.2: Dispatch Challenger to verify calculations, SQL counts, and claims.
- Step 4.3: Dispatch Forensic Auditor to verify integrity and authentic analysis.
- Step 4.4: Gate check & final handoff.
