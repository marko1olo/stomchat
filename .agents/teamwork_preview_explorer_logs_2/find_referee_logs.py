import sys
sys.stdout.reconfigure(encoding='utf-8')
import re

with open(r"c:\Users\danat\Desktop\stomchat\assistant.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for idx in range(7660, 7850):
    line = lines[idx]
    if "logger." in line or "send_message" in line or "send_" in line:
        print(f"{idx+1}: {line.strip()}")
