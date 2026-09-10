import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re

with open(r"c:\Users\danat\Desktop\stomchat\bot.log", "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

for idx, line in enumerate(lines):
    if "Dialogue triage rejected continuation" in line:
        print(f"--- Line {idx+1} ---")
        for j in range(max(0, idx - 8), min(len(lines), idx + 4)):
            print(f"  {lines[j].strip()}")
