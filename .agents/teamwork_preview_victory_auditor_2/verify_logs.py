import os
import re

log_files = ['bot.log', 'bot.log.1', 'bot.log.2', 'bot.log.3']

date_pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

total_lines = 0
for lf in log_files:
    if os.path.exists(lf):
        with open(lf, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            cnt = len(lines)
            total_lines += cnt
            first_date, last_date = None, None
            for l in lines:
                m = date_pattern.match(l)
                if m:
                    first_date = m.group(1)
                    break
            for l in reversed(lines):
                m = date_pattern.match(l)
                if m:
                    last_date = m.group(1)
                    break
            print(f"{lf:<15}: {cnt:>7} lines | {first_date} -> {last_date}")

print(f"Total lines across bot.log*: {total_lines}")

if os.path.exists('bot_supervisor.log'):
    with open('bot_supervisor.log', 'r', encoding='utf-8', errors='ignore') as f:
        su_lines = f.readlines()
        print(f"bot_supervisor.log: {len(su_lines)} lines")
