import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

db_path = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"
conn = sqlite3.connect(db_path, uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT msg_id, date, sender_name, sender_username, text FROM messages WHERE date >= '2026-09-11' ORDER BY msg_id ASC")
rows = cur.fetchall()
print(f"Total rows >= 2026-09-11: {len(rows)}")
print("First 10 messages:")
for r in rows[:10]:
    print(r['msg_id'], r['date'], r['sender_name'], (r['text'][:40] if r['text'] else ''))

print("\nLast 10 messages:")
for r in rows[-10:]:
    print(r['msg_id'], r['date'], r['sender_name'], (r['text'][:40] if r['text'] else ''))

# If we look at msg_id >= 177250:
cur.execute("SELECT count(*) FROM messages WHERE msg_id >= 177250")
print("Total messages msg_id >= 177250:", cur.fetchone()[0])

# If we look at msg_id from 177248 to 177441:
# Let's see what is msg_id 177250
cur.execute("SELECT * FROM messages WHERE msg_id = 177250")
print("Msg 177250:", dict(cur.fetchone()))

# Check the difference between 200 and 194: 200 - 6 = 194, or 194 user messages?
# 194 + 20? Or 194 group messages in total including/excluding something?
conn.close()
