import sys
import re

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

all_logs = ["bot.log", "bot.log.1", "bot.log.2", "bot.log.3", "bot_supervisor.log"]

for name in ["rate_limit", "503", "429", "ResourceExhausted"]:
    total = 0
    for lf in all_logs:
        cnt = 0
        with open(lf, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if name.lower() in line.lower():
                    cnt += 1
        total += cnt
        print(f"{lf} - {name}: {cnt}")
    print(f"TOTAL {name}: {total}\n")
