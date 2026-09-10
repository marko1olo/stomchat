import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
import json

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

re_ts = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?)")
re_diag_rej = re.compile(r"Dialogue triage rejected continuation for chain with (\d+) bot replies", re.IGNORECASE)

diag_rej_events = []

for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
        for idx, line in enumerate(lines):
            m = re_diag_rej.search(line)
            if m:
                # get context lines around this event
                context = [lines[j].strip() for j in range(max(0, idx - 15), min(len(lines), idx + 5))]
                diag_rej_events.append({
                    "file": fname,
                    "line": idx + 1,
                    "bot_replies": int(m.group(1)),
                    "log_context": context
                })

print(f"Total dialogue triage rejected events: {len(diag_rej_events)}")
for idx, ev in enumerate(diag_rej_events, 1):
    print(f"\n--- Dialogue Triage Rejected #{idx} ---")
    print(f"File: {ev['file']}:{ev['line']} | Bot replies in chain: {ev['bot_replies']}")
    for c in ev['log_context'][-10:]:
        print(f"   {c[:120]}")
