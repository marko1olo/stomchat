import sqlite3
import sys
import json

sys.stdout.reconfigure(encoding='utf-8')

db_path = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"
conn = sqlite3.connect(db_path, uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Let's inspect the last 50 bot_sent_messages
cur.execute("""
    SELECT b.id, b.msg_id, b.chat_id, m.date, m.sender_id, m.sender_name, m.sender_username, m.reply_to_msg_id, substr(m.text, 1, 100) as snippet
    FROM bot_sent_messages b
    LEFT JOIN messages m ON b.msg_id = m.msg_id
    ORDER BY b.id DESC
    LIMIT 35
""")
rows = [dict(r) for r in cur.fetchall()]
print(f"Total rows fetched from bot_sent_messages: {len(rows)}")
for r in reversed(rows):
    print(f"b_id: {r['id']} | msg_id: {r['msg_id']} | reply_to: {r['reply_to_msg_id']} | date: {r['date']} | sender: {r['sender_name']} (@{r['sender_username']}) | text: {r['snippet']}")

conn.close()
