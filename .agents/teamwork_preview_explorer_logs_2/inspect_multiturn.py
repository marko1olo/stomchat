import sys
sys.stdout.reconfigure(encoding='utf-8')
import os

with open(r"c:\Users\danat\Desktop\stomchat\bot.log", "r", encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()

for idx, line in enumerate(lines):
    if any(k in line for k in ["176882", "176883", "176884", "176885", "176888", "176889", "176891", "176893"]):
        print(f"{idx+1}: {line.strip()}")
