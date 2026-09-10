import sys
sys.stdout.reconfigure(encoding='utf-8')
import re
from collections import Counter
from datetime import datetime

supervisor_path = r"c:\Users\danat\Desktop\stomchat\bot_supervisor.log"

re_start = re.compile(r"^(\w+ \d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\.\d{2}) - Starting stomat bot")
re_stop = re.compile(r"^(\w+ \d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\.\d{2}) - Bot stopped with code (-?\d+)")

starts = 0
stops = 0
exit_codes = Counter()

with open(supervisor_path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        m1 = re_start.match(line)
        if m1:
            starts += 1
        m2 = re_stop.match(line)
        if m2:
            stops += 1
            exit_codes[m2.group(2)] += 1

print(f"Supervisor Total Starts: {starts}")
print(f"Supervisor Total Stops/Crashes: {stops}")
print("Exit Codes Distribution:")
for code, cnt in exit_codes.most_common():
    print(f"  Code {code}: {cnt} times")
