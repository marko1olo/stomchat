import sys
sys.stdout.reconfigure(encoding='utf-8')
import re

with open(r"c:\Users\danat\Desktop\stomchat\assistant.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for idx, line in enumerate(lines, 1):
    if any(w in line.lower() for w in ["sent direct", "sent assistant reply", "sent bot mention reply", "reply sent to chat"]):
        print(f"{idx}: {line.strip()}")
