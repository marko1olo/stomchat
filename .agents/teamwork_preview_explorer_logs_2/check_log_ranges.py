import os
import glob
import re

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
    r"c:\Users\danat\Desktop\stomchat\bot_supervisor.log",
]

ts_regex = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")

for lp in log_files:
    if not os.path.exists(lp):
        print(f"File not found: {lp}")
        continue
    line_count = 0
    first_ts = None
    last_ts = None
    with open(lp, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line_count += 1
            m = ts_regex.match(line)
            if m:
                if first_ts is None:
                    first_ts = m.group(1)
                last_ts = m.group(1)
    print(f"{os.path.basename(lp)}: {line_count:,} lines, first: {first_ts}, last: {last_ts}")
