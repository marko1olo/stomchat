import sys
sys.stdout.reconfigure(encoding='utf-8')
import re

with open(r"c:\Users\danat\Desktop\stomchat\assistant.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

for idx, line in enumerate(lines, 1):
    if line.startswith("async def ") or line.startswith("def "):
        if any(w in line for w in ["handle", "process", "check_", "on_message", "trigger"]):
            print(f"{idx}: {line.strip()}")
