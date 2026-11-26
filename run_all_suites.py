"""
run_all_suites.py — запускает ВСЕ скрипт-тесты (PASS/FAIL + sys.exit) через subprocess.

Две категории тестов проекта:
  1. PYTEST-тесты (tests/test_*.py без sys.exit на модульном уровне)
     → запускаются обычным: python -m pytest tests/
  2. СКРИПТ-тесты (tests/test_*.py с sys.exit(0/1) на модульном уровне)
     → запускаются ЭТИМ файлом через subprocess.

Добавить новый скрипт-тест: просто внести в список tests ниже.
Порядок — по приоритету: критические контракты первыми.
"""
import subprocess
import sys
import os

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

_cwd = os.path.basename(os.getcwd()).lower()
bot_label = "STOMCHAT" if "stom" in _cwd else ("ENDOCHAT" if "endo" in _cwd else "BOT")

# --- КРИТИЧЕСКИЕ СТРАЖИ (запускаются первыми) ---
SENTINEL = [
    "tests/test_config_contract.py",
    "tests/test_import_safety.py",
    "tests/test_isolation.py",
    "tests/test_json_scan.py",
    "tests/test_dead_tools.py",
    "tests/test_reachable.py",
]

# --- ОСНОВНЫЕ ИНКВИЗИТОРЫ И REDTEAM ---
INQUISITORS = [
    "tests/test_reaction_engine.py",
    "tests/test_chairside_tools.py",
    "tests/test_clinical_rescue.py",
    "tests/test_album_antidup.py",
    "tests/test_routing_behaviour.py",
    "tests/test_group_command_triggers.py",
    "tests/test_validator_policy.py",
    "tests/test_taxonomy_single.py",
    "tests/test_validator_coverage.py",
    "tests/test_fix_db.py",
    "tests/test_storage_subsystem.py",
    "tests/test_protocols_ui.py",
    "tests/test_intent_routing.py",
    "tests/test_memory_redteam_inquisitor.py",
    "tests/test_clinician_memory_hygiene.py",
    "tests/test_voice_media_inquisitor.py",
    "tests/test_pm_clinical_flow_inquisitor.py",
    "tests/test_llm_load_balance_inquisitor.py",
    "tests/test_redteam_group_safety.py",
    "tests/test_interactive_case_quiz_inquisitor.py",
    "tests/test_concurrency_ram_inquisitor.py",
    "tests/test_pm_flow_resiliency_redteam.py",
    "tests/test_ping_policy.py",
    "tests/test_group_ping_job.py",
    "tests/test_pm_persistence_inquisitor.py",
    "tests/test_memory_retrieval_redteam.py",
    "tests/test_memory_injection_redteam.py",
    "tests/test_memory_performance_redteam.py",
    "tests/test_passive_probability_and_rescue_redteam.py",
    "tests/test_inflight_followup_triage_redteam.py",
    "tests/test_poll_antidup_inquisitor.py",
    "tests/test_zero_mirroring_redteam.py",
    "tests/test_emergency_group_fastpath.py",
    "tests/test_audit_20h_deep_repair.py",
    "tests/test_llm_cascade_budget_audit.py",
    "tests/test_audit_fixes_20h.py",
    "tests/test_inquisitor_fixes_72h.py",
]

# --- РАСШИРЕННЫЕ СКРИПТ-ТЕСТЫ (ранее не были в suite runner) ---
EXTENDED = [
    "tests/test_bot_modernization_4features.py",
    "tests/test_budget_nesting.py",
    "tests/test_case_simulator.py",
    "tests/test_chat_statistics.py",
    "tests/test_clip_single.py",
    "tests/test_commands_surface.py",
    "tests/test_debt_registry.py",
    "tests/test_dialogue_reply_limit.py",
    "tests/test_digest_formatting.py",
    "tests/test_digest_window.py",
    "tests/test_distill_pipeline.py",
    "tests/test_distill_scripts_safety.py",
    "tests/test_edit_delete.py",
    "tests/test_exhaustion_retry_backoff.py",
    "tests/test_fix_pm.py",
    "tests/test_fix_pm2.py",
    "tests/test_fix_scheduler.py",
    "tests/test_fix_subprocess.py",
    "tests/test_fix_vision.py",
    "tests/test_fix_weekly.py",
    "tests/test_flood_discipline.py",
    "tests/test_gemini_knowledge_json.py",
    "tests/test_gemini_pacing.py",
    "tests/test_group_quiz.py",
    "tests/test_group_summary.py",
    "tests/test_key_health.py",
    "tests/test_llm_failover.py",
    "tests/test_media_diagnostics.py",
    "tests/test_media_loop_guard.py",
    "tests/test_media_recovery.py",
    "tests/test_media_selection.py",
    "tests/test_media_stderr.py",
    "tests/test_message_splitting.py",
    "tests/test_observability.py",
    "tests/test_passive_gate.py",
    "tests/test_pm_experience.py",
    "tests/test_prompt_context.py",
    "tests/test_rag_quality.py",
    "tests/test_report_targets.py",
    "tests/test_round3.py",
    "tests/test_savdel_taxonomy.py",
    "tests/test_scheduler_state.py",
    "tests/test_schema_migration.py",
    "tests/test_search_case.py",
    "tests/test_search_command.py",
    "tests/test_silence_all_paths.py",
    "tests/test_silence_policy.py",
    "tests/test_silent_failures.py",
    "tests/test_snapshot_document.py",
    "tests/test_startup_boot.py",
    "tests/test_startup_watchdog.py",
    "tests/test_state_atomicity.py",
    "tests/test_style_setting.py",
    "tests/test_summary_delivery.py",
    "tests/test_table_rendering_pipeline.py",
    "tests/test_tg_delivery.py",
    "tests/test_thread_dedupe.py",
    "tests/test_trigger_discipline.py",
    "tests/test_verify_challenger_3.py",
    "tests/test_video_import.py",
    "tests/test_vision_budget.py",
    "tests/test_vision_pipeline.py",
    "tests/test_vision_safety.py",
    "tests/test_voice_offline.py",
    "tests/test_voice_pipeline.py",
    "tests/test_web_lookup.py",
    "tests/test_wiki_pagination.py",
    "tests/test_wipe_tracking.py",
    "tests/test_zero_slash_and_menu.py",
    "tests/test_archive_dump.py",
    "tests/test_digest_branding_redteam.py",
    "tests/test_fix_group_quiz_buttons.py",
    "tests/test_summary_dynamic_redteam.py",
    "tests/test_user_memory.py",
    "tests/test_vision_cache.py",
]

def main():
    tests = SENTINEL + INQUISITORS + EXTENDED

    all_ok = True
    print(f"==================== RUNNING ALL SUITES ({len(tests)} SUITES) ====================", flush=True)
    print(f"  Sentinel guards: {len(SENTINEL)}  |  Inquisitors: {len(INQUISITORS)}  |  Extended: {len(EXTENDED)}", flush=True)
    print(flush=True)

    passed_count = 0
    failed_list = []

    for test in tests:
        # Запускаем через pytest -q, а не python test.py:
        # pytest обеспечивает asyncio_mode=auto и правильный sys.path.
        # Скрипт-тесты без def test_* pytest не найдёт (exit 5 или exit 3 INTERNALERROR на SystemExit) →
        # для них fallback: прямой запуск python test.py.
        try:
            res = subprocess.run(
                [sys.executable, "-m", "pytest", test, "-q", "--tb=short", "--no-header"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
            )
            # pytest exit 5 = "no tests ran", exit 3 with caught unexpected SystemExit = скрипт-тест
            is_script_test = (res.returncode == 5) or (
                res.returncode == 3
                and ("caught unexpected SystemExit" in (res.stderr or "") or "no tests ran" in (res.stdout or ""))
            )
            if is_script_test:
                res = subprocess.run(
                    [sys.executable, test],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
                )
        except subprocess.TimeoutExpired:
            all_ok = False
            failed_list.append(test)
            print(f"  [FAIL] {test:60} -> TIMED OUT (>120s)", flush=True)
            continue

        if res.returncode == 0:
            lines = [l for l in res.stdout.strip().split("\n") if l.strip()]
            last_line = lines[-1] if lines else "OK"
            passed_count += 1
            print(f"  [PASS] {test:60} -> {last_line}", flush=True)
        else:
            all_ok = False
            failed_list.append(test)
            print(f"  [FAIL] {test:60} -> rc={res.returncode}", flush=True)
            if res.stdout:
                print("STDOUT:", res.stdout[-1200:], flush=True)
            if res.stderr:
                print("STDERR:", res.stderr[-400:], flush=True)

    print(flush=True)
    if not all_ok:
        print(f"SOME {bot_label} TESTS FAILED! ({len(failed_list)}/{len(tests)})", flush=True)
        print("Failed:", ", ".join(failed_list), flush=True)
        sys.exit(1)
    print(f"ALL {bot_label} TEST SUITES ({passed_count}/{len(tests)}) ARE 100% GREEN!", flush=True)


if __name__ == "__main__":
    main()
