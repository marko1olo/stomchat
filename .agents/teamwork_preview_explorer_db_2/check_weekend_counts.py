import sqlite3
import sys
import json

sys.stdout.reconfigure(encoding='utf-8')

db_path = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"
conn = sqlite3.connect(db_path, uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Check date range of recent messages
cur.execute("""
    SELECT count(*), min(msg_id), max(msg_id), min(date), max(date)
    FROM messages
    WHERE date >= '2026-09-11'
""")
print("Messages date >= 2026-09-11:", dict(cur.fetchone()))

# Check messages where msg_id >= 177240
cur.execute("""
    SELECT count(*), min(msg_id), max(msg_id), min(date), max(date)
    FROM messages
    WHERE msg_id >= 177245
""")
print("Messages msg_id >= 177245:", dict(cur.fetchone()))

# Check bot_sent_messages in this range
cur.execute("""
    SELECT b.id, b.msg_id, b.chat_id, m.date, m.sender_id, m.sender_name, m.reply_to_msg_id, m.text
    FROM bot_sent_messages b
    LEFT JOIN messages m ON b.msg_id = m.msg_id
    WHERE b.id >= 820
    ORDER BY b.id ASC
""")
bot_rows = [dict(r) for r in cur.fetchall()]
print(f"\nBot sent messages id >= 820 (total {len(bot_rows)}):")
for r in bot_rows:
    print(f"b_id: {r['id']}, msg_id: {r['msg_id']}, chat_id: {r['chat_id']}, reply_to: {r['reply_to_msg_id']}, date: {r['date']}")

# Check PM messages in this timeframe
cur.execute("SELECT * FROM pm_messages WHERE date >= '2026-09-11' ORDER BY id ASC")
pm_rows = [dict(r) for r in cur.fetchall()]
print(f"\nPM messages >= 2026-09-11 (total {len(pm_rows)}):")
for r in pm_rows:
    print(dict(r))

conn.close()
