import sys
sys.stdout.reconfigure(encoding='utf-8')
import re

with open(r"c:\Users\danat\Desktop\stomchat\bot.log", "r", encoding="utf-8", errors="ignore") as f:
    for idx, line in enumerate(f, 1):
        if "2026-09-07 12:" in line or "2026-09-07 13:" in line:
            print(f"{idx}: {line.strip()}")
            if idx > 100:
                break
