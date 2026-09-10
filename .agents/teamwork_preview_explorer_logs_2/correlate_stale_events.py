import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
import sqlite3
import json

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

db_path = r"c:\Users\danat\Desktop\stomchat\stomat_bot.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

re_ts = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?)")
re_sil_stale = re.compile(r"Dialogue reply is stale\.\s*(\d+)\s*messages have passed since bot message (\d+)", re.IGNORECASE)

stale_events = []

for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        curr_ts = ""
        for line_num, line in enumerate(f, 1):
            m_ts = re_ts.match(line)
            if m_ts:
                curr_ts = m_ts.group(1)
            m = re_sil_stale.search(line)
            if m:
                msgs_passed = int(m.group(1))
                bot_msg_id = int(m.group(2))
                stale_events.append({
                    "file": fname,
                    "line": line_num,
                    "ts": curr_ts,
                    "msgs_passed": msgs_passed,
                    "bot_msg_id": bot_msg_id
                })

print(f"Total stale events found: {len(stale_events)}")

# Find doctor replies that replied to bot_msg_id around that time
enriched_stale = []
for ev in stale_events:
    b_id = ev["bot_msg_id"]
    # Look for messages in DB replying to b_id
    cur.execute("""
        SELECT msg_id, sender_id, sender_name, date, text 
        FROM messages 
        WHERE reply_to_msg_id = ? 
        ORDER BY msg_id ASC
    """, (b_id,))
    replies = cur.fetchall()
    
    # Also fetch bot message text
    cur.execute("SELECT text FROM messages WHERE msg_id = ?", (b_id,))
    bot_row = cur.fetchone()
    bot_text = bot_row[0] if bot_row else "Not in DB"

    ev["bot_text"] = bot_text[:100]
    ev["replies"] = [{
        "msg_id": r[0],
        "sender_id": r[1],
        "sender_name": r[2],
        "date": r[3],
        "text": r[4]
    } for r in replies]
    enriched_stale.append(ev)

# Print the top 10 most recent stale events
print("\n=== SAMPLE STALE SUPPRESSIONS WITH USER QUESTIONS ===")
for idx, ev in enumerate(enriched_stale[:15], 1):
    print(f"\n--- Stale Event #{idx} ---")
    print(f"File: {ev['file']}:{ev['line']} | Timestamp: {ev['ts']}")
    print(f"Bot Msg ID: {ev['bot_msg_id']} | msgs_passed: {ev['msgs_passed']}")
    print(f"Bot Msg: {ev['bot_text']}")
    if ev["replies"]:
        for rep in ev["replies"]:
            print(f"  -> User reply msg_id {rep['msg_id']} by {rep['sender_name']} ({rep['date']}):")
            print(f"     \"{rep['text']}\"")
    else:
        print("  -> No direct reply in DB with reply_to_msg_id == bot_msg_id (could be sequential follow-up or quote)")

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_logs_2\stale_events_enriched.json", "w", encoding="utf-8") as f:
    json.dump(enriched_stale, f, ensure_ascii=False, indent=2)
print("\nWrote stale_events_enriched.json")
