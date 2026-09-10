import os
import time

def check_file(fp):
    if os.path.exists(fp):
        mtime = os.path.getmtime(fp)
        tstr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
        size = os.path.getsize(fp)
        print(f"{fp:<45} | {tstr} | {size:>10} bytes")
    else:
        print(f"{fp:<45} | NOT FOUND")

files = [
    'REPORT_CHAT_BALANCE_AND_LOGS.md',
    'bot.log',
    'bot.log.1',
    'bot.log.2',
    'bot.log.3',
    'bot_supervisor.log',
    'stomat_bot.db',
    'stomat_archive.db',
    'assistant_state.json',
    'assistant.py',
    'config.py',
    'database.py',
    'main.py'
]

print("=== Core Files Timestamp and Size Audit ===")
for f in files:
    check_file(f)
