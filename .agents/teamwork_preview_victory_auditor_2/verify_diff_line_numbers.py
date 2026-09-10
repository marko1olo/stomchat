import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("=== VERIFYING CODE DIFF LINE NUMBERS AND TARGETS ===")

with open('config.py', 'r', encoding='utf-8') as f:
    config_content = f.read()
    print(f"config.py: {len(config_content.splitlines())} lines")
    for term in ['PASSIVE_COOLDOWN_MINUTES', 'PASSIVE_RETRY_MINUTES', 'ENABLE_PM_PROACTIVE_PINGS', 'TRIAGE_CONFIDENCE_THRESHOLD']:
        found = term in config_content
        print(f"  {term} present in config.py: {found}")

with open('assistant.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

print(f"\nassistant.py: {len(lines)} lines")
check_lines = [
    (1364, "record_passive_success"),
    (2204, "check_llm_triage prompt"),
    (2588, "dialogue stale / count_since"),
    (2691, "sequential follow-up count_since"),
    (2709, "passive_gate_block_reason"),
    (2789, "passive_cooldown_active"),
    (2995, "quality validator / emoji"),
    (3074, "record_passive_success in shadow mode")
]

for lnum, desc in check_lines:
    start = max(0, lnum - 3)
    end = min(len(lines), lnum + 3)
    print(f"\nTarget around line {lnum} ({desc}):")
    for i in range(start, end):
        print(f"  {i+1}: {lines[i].strip()[:95]}")

with open('main.py', 'r', encoding='utf-8') as f:
    main_lines = f.readlines()
print(f"\nmain.py: {len(main_lines)} lines")
print("Target around line 838 in main.py (pm_ping_scheduler_task):")
for i in range(835, min(len(main_lines), 845)):
    print(f"  {i+1}: {main_lines[i].strip()[:95]}")
