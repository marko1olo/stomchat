import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

tests = [
    "tests/test_reaction_engine.py",
    "tests/test_chairside_tools.py",
    "tests/test_clinical_rescue.py",
    "tests/test_album_antidup.py",
    "tests/test_reachable.py",
    "tests/test_routing_behaviour.py",
    "tests/test_group_command_triggers.py",
    "tests/test_validator_policy.py",
    "tests/test_taxonomy_single.py",
    "tests/test_validator_coverage.py",
    "tests/test_fix_db.py",
    "tests/test_storage_subsystem.py",
    "tests/test_anesthesia_calc.py",
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
]

all_ok = True
bot_label = "STOMCHAT" if "stom" in sys.executable.lower() or "stom" in sys.path[0].lower() else "ENDOCHAT"
print(f"==================== RUNNING ALL SUITES ({len(tests)} SUITES) ====================", flush=True)
for test in tests:
    res = subprocess.run([sys.executable, test], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode == 0:
        lines = [l for l in res.stdout.strip().split("\n") if l.strip()]
        last_line = lines[-1] if lines else "OK"
        print(f"  [PASS] {test:42} -> {last_line}", flush=True)
    else:
        all_ok = False
        print(f"  [FAIL] {test:42} -> Return code {res.returncode}", flush=True)
        if res.stdout:
            print("STDOUT:", res.stdout[-1000:], flush=True)
        if res.stderr:
            print("STDERR:", res.stderr[-1000:], flush=True)

if not all_ok:
    print(f"\nSOME {bot_label} TESTS FAILED!", flush=True)
    sys.exit(1)
print(f"\nALL {bot_label} TEST SUITES ({len(tests)}/{len(tests)}) ARE 100% GREEN!", flush=True)
