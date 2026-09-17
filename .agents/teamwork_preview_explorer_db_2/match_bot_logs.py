import json
import sys
import re
from datetime import datetime

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

# For each bot msg, let's find the window around m['date']
for b in bot_msgs:
    b_date_str = b["date"] # e.g. "2026-09-11 12:05:50"
    b_time = datetime.strptime(b_date_str, "%Y-%m-%d %H:%M:%S")
    snippet = b["text"][:30].strip()
    
    print(f"\n=======================================================")
    print(f"Bot Msg {b['msg_id']} | Date: {b_date_str} | ReplyTo: {b['reply_to_msg_id']}")
    print(f"Snippet: {b['text'][:60]}...")
    
    # find lines within 60 seconds before b_time
    relevant_lines = []
    for line in log_lines:
        # line timestamp: 2026-09-11 12:05:50,123
        m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if m:
            l_time = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            diff = (b_time - l_time).total_seconds()
            if -5 <= diff <= 60:
                relevant_lines.append(line)
    
    # Search for model, budget, duration, latency
    models_used = []
    latencies = []
    for l in relevant_lines:
        if "gemini_client" in l and "Budget" in l:
            models_used.append(l.strip())
        if "HTTP Request: POST" in l:
            models_used.append(l.strip())
        if "Validation" in l or "Assistant response generated" in l or "Triggered assistant" in l:
            models_used.append(l.strip())
    
    for mu in models_used[-5:]:
        print("  LOG:", mu[:120])
