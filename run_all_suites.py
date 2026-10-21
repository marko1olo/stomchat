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
    "tests/test_reachable.py",
    "tests/test_routing_behaviour.py",
    "tests/test_group_command_triggers.py",
    "tests/test_validator_policy.py",
    "tests/test_taxonomy_single.py",
    "tests/test_validator_coverage.py",
]

all_ok = True
print("==================== RUNNING ALL SUITES FOR STOMCHAT ====================")
for test in tests:
    res = subprocess.run([sys.executable, test], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode == 0:
        lines = [l for l in res.stdout.strip().split("\n") if l.strip()]
        last_line = lines[-1] if lines else "OK"
        print(f"  [PASS] {test:38} -> {last_line}")
    else:
        all_ok = False
        print(f"  [FAIL] {test:38} -> Return code {res.returncode}")
        if res.stdout:
            print("STDOUT:", res.stdout[-1000:])
        if res.stderr:
            print("STDERR:", res.stderr[-1000:])

if not all_ok:
    print("\nSOME TESTS FAILED!")
    sys.exit(1)
print("\nALL STOMCHAT TEST SUITES ARE 100% GREEN!")
