import sqlite3
import sys
import json

sys.stdout.reconfigure(encoding='utf-8')

db_path = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"
conn = sqlite3.connect(db_path, uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Get distinct senders in this range
cur.execute("""
    SELECT sender_id, sender_name, sender_username, count(*) as cnt
    FROM messages
    WHERE date >= '2026-09-11'
    GROUP BY sender_id, sender_name, sender_username
    ORDER BY cnt DESC
""")
senders = [dict(r) for r in cur.fetchall()]
print(f"Senders count: {len(senders)}")
for s in senders:
    print(f"ID: {s['sender_id']} | Name: {s['sender_name']} | User: @{s['sender_username']} | Count: {s['cnt']}")

# Total messages
cur.execute("SELECT count(*) FROM messages WHERE date >= '2026-09-11'")
print("\nTotal messages date >= 2026-09-11:", cur.fetchone()[0])

# Bot messages in messages table
cur.execute("SELECT count(*) FROM messages WHERE date >= '2026-09-11' AND sender_username = 'docendobot'")
print("Bot messages in messages table:", cur.fetchone()[0])

# Non-bot messages in messages table
cur.execute("SELECT count(*) FROM messages WHERE date >= '2026-09-11' AND (sender_username != 'docendobot' OR sender_username IS NULL)")
print("Non-bot messages in messages table:", cur.fetchone()[0])

# Check all bot_sent_messages in this period
cur.execute("""
    SELECT b.id, b.msg_id, b.chat_id
    FROM bot_sent_messages b
    WHERE b.id >= 822
    ORDER BY b.id ASC
""")
bot_msgs = [dict(r) for r in cur.fetchall()]
print(f"\nbot_sent_messages where id >= 822: {len(bot_msgs)}")
for b in bot_msgs:
    print(b)

conn.close()
