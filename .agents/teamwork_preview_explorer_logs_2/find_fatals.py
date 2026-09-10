import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

fatal_patterns = [
    re.compile(r"CRITICAL|FATAL|Traceback|sys\.exit|Stopping bot|Shutting down|KeyboardInterrupt|restart", re.IGNORECASE)
]

for lf in log_files:
    fname = os.path.basename(lf)
    print(f"=== {fname} ===")
    crit_count = 0
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f, 1):
            if "CRITICAL" in line or "FATAL" in line or "SystemExit" in line or "exit code" in line:
                print(f"  {idx}: {line.strip()[:100]}")
                crit_count += 1
                if crit_count > 10:
                    print("  ... and more")
                    break
