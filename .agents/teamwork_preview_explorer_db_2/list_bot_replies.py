import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

db_path = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"
conn = sqlite3.connect(db_path, uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
    SELECT msg_id, reply_to_msg_id, date, text
    FROM messages
    WHERE date >= '2026-09-11' AND sender_username = 'docendobot'
    ORDER BY msg_id ASC
""")
rows = cur.fetchall()
print(f"Total bot messages: {len(rows)}")
for i, r in enumerate(rows, 1):
    print(f"{i}. msg_id: {r['msg_id']} | reply_to: {r['reply_to_msg_id']} | date: {r['date']} | text: {r['text'][:80]}...")

conn.close()
