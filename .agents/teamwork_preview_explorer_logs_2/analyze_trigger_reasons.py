import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
from collections import Counter

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

pat_assist = re.compile(r"Triggered assistant!\s+Reason:\s+(.*?)\.\s+Keywords:", re.IGNORECASE)
pat_media = re.compile(r"Triggered media assistant!\s+Reason:\s+(.*?)\.\s+Keywords:", re.IGNORECASE)

assist_reasons = Counter()
media_reasons = Counter()

for lf in log_files:
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = pat_assist.search(line)
            if m:
                # normalize reason
                r = m.group(1).strip()
                assist_reasons[r] += 1
            m2 = pat_media.search(line)
            if m2:
                r2 = m2.group(1).strip()
                media_reasons[r2] += 1

print("=== ASSISTANT TRIGGER REASONS ===")
for r, c in assist_reasons.most_common():
    print(f"{c:4d} : {r}")

print("\n=== MEDIA TRIGGER REASONS ===")
for r, c in media_reasons.most_common():
    print(f"{c:4d} : {r}")
