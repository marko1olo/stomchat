import json
import sys
import re
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding='utf-8')

log_path = "c:/Users/danat/Desktop/stomchat/bot.log"
with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
bot_msgs = [m for m in messages if m["sender_username"] == "docendobot"]

# Read all lines from bot.log for 2026-09-11..13
log_lines = []
with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if any(d in line for d in ["2026-09-11", "2026-09-12", "2026-09-13"]):
            log_lines.append(line)

print(f"Loaded {len(log_lines)} log lines.")

for b in bot_msgs:
    b_date_utc = datetime.strptime(b["date"], "%Y-%m-%d %H:%M:%S")
    b_date_local = b_date_utc + timedelta(hours=4)
    b_local_str = b_date_local.strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"\n=======================================================")
    print(f"Bot Msg {b['msg_id']} | UTC: {b['date']} | Local: {b_local_str} | ReplyTo: {b['reply_to_msg_id']}")
    print(f"Snippet: {b['text'][:60]}...")
    
    # find lines within 60 seconds of b_date_local
    matched = []
    for line in log_lines:
        m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if m:
            l_time = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            diff = (b_date_local - l_time).total_seconds()
            if -5 <= diff <= 90:
                matched.append(line)
    
    key_lines = [l.strip() for l in matched if any(k in l for k in ["Triggered assistant", "gemini-", "Cascade", "HTTP Request: POST", "Validation passed", "assistant - INFO - "])]
    for kl in key_lines[-6:]:
        print("  LOG:", kl[:120])
