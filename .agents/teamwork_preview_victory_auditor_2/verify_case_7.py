import os
import re

log_files = ['bot.log', 'bot.log.1', 'bot.log.2', 'bot.log.3']

print("=== VERIFY CASE 7: VALIDATOR DROPPED / TIMEOUTS ===")

all_rejections = []
for lf in log_files:
    if os.path.exists(lf):
        with open(lf, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f, 1):
                if "REJECTED draft:" in line or "validator_unavailable:" in line or "Response validator unavailable" in line:
                    all_rejections.append((lf, i, line.strip()))

print(f"Total rejection / unavailable lines: {len(all_rejections)}")

cascade_unavailable = [r for r in all_rejections if "cascade exhausted" in r[2].lower() or "validator_unavailable" in r[2].lower() or "validator unavailable" in r[2].lower()]
print(f"Cascade / unavailable lines: {len(cascade_unavailable)}")

# Sample lines
for r in cascade_unavailable[:10]:
    print(f"  {r[0]}:{r[1]} | {r[2][:110]}")
