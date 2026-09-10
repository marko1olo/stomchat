import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

re_neg = re.compile(r"(?:Global n|N)egative feedback detected.*?: '(.*?)'\. Silencing bot", re.IGNORECASE)
re_pen = re.compile(r"Bot is silenced until (.*?)\. Skipping (.*)", re.IGNORECASE)

print("=== NEGATIVE FEEDBACK DETECTED ===")
for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f, 1):
            m = re_neg.search(line)
            if m:
                print(f"{fname}:{idx} -> Text: '{m.group(1)}'")

print("\n=== BOT IS SILENCED PENALTY EVENTS ===")
for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f, 1):
            m = re_pen.search(line)
            if m:
                print(f"{fname}:{idx} -> Until: {m.group(1)} | Action: {m.group(2)}")
