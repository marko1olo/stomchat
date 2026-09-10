import re

with open(r"c:\Users\danat\Desktop\stomchat\assistant.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

log_patterns = [
    "trigger", "suppress", "cooldown", "stale", "triage", "silence", "silenced", 
    "validator", "rejected", "exhausted", "quota", "503", "rate limit", "backoff", 
    "negative_feedback", "feedback", "direct reply", "mention", "passive", "media"
]

results = []
for idx, line in enumerate(lines, 1):
    if "logger." in line:
        lower = line.lower()
        matched = [p for p in log_patterns if p in lower]
        if matched:
            results.append((idx, line.strip(), matched))

for idx, line, matched in results[40:]:
    print(f"{idx}: {line}")
