# Execution Plan: StomChat Log, Database & Balance Audit

## Overview
Synthesize a comprehensive, mathematically rigorous audit report `REPORT_CHAT_BALANCE_AND_LOGS.md` addressing Requirements R1, R2, and R3.

## Phase 1: Survey & Detailed Exploration (Parallel)
- **Explorer 1 (`teamwork_preview_explorer_logs_2`)**:
  - Analyze `bot.log`, `bot.log.1`, `bot.log.2`, `bot_supervisor.log`, `assistant_state.json`.
  - Extract exact counts for triggers (Direct Reply, Mentions, Sequential Follow-ups, Passive Clinical, Media).
  - Extract quantitative distribution of silence causes (passive_cooldown, retry_backoff, dialogue_stale, dialogue_triage_rejected, negative_feedback_silenced, validator_rejected, LLM errors / cascade exhaustion).
  - Find real false-negative cases with log timestamps, line numbers, message IDs, and user questions.
  - Output: `analysis_logs.md` and `handoff.md`.
- **Explorer 2 (`teamwork_preview_explorer_db_2`)**:
  - Analyze `stomat_bot.db` (`messages`, `pm_messages`, `user_memories`, `bot_sent_messages`) and `stomat_archive.db`.
  - Query all 42k+ active group messages, 117k+ archive messages, 351 PM records without truncation.
  - Classify sentiment (positive, constructive, skeptical, negative/frustrated) with exact message IDs, quotes, and usernames.
  - Dental specialty distribution (endodontics, implantology, surgery, prosthetics, orthotropics/aligners).
  - Dialogue depth distribution (1 turn, 2-3 turns, 4+ turns).
  - Output: `analysis_db.md` and `handoff.md`.
- Note: Architecture & Code analysis is already comprehensively completed in `.agents/teamwork_preview_explorer_code_1/analysis_code.md`.

## Phase 2: Synthesis & Deliverable Generation
- **Worker (`teamwork_preview_worker_report_1`)**:
  - Read `analysis_logs.md`, `analysis_db.md`, and `analysis_code.md`.
  - Compile the complete, production-grade deliverable `c:\Users\danat\Desktop\stomchat\REPORT_CHAT_BALANCE_AND_LOGS.md`.
  - Ensure all tables, real quotes, message IDs, mathematical formulas, and concrete code diffs for `assistant.py` are included.

## Phase 3: Review & Gate Verification
- **Reviewer 1 (`teamwork_preview_reviewer_1`)**: Verify completeness, statistical rigor, and consistency against logs/databases.
- **Reviewer 2 (`teamwork_preview_reviewer_2`)**: Verify clinical depth, sentiment categorization, and mathematical feasibility.
- **Challenger (`teamwork_preview_challenger_1`)**: Check empirical claims, formula correctness, and sample quotes against SQLite.
- **Auditor (`teamwork_preview_auditor_1`)**: Verify forensic integrity (no fabricated quotes, no dummy metrics, authentic SQL analysis).

## Phase 4: Final Reporting
- Deliver final completion report to user.
