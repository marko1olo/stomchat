import sqlite3

db_path = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"

conn = sqlite3.connect(db_path, uri=True)
cur = conn.cursor()

cur.execute("SELECT MIN(msg_id), MAX(msg_id), COUNT(*), MIN(date), MAX(date) FROM messages WHERE msg_id >= 177200")
print("Messages msg_id >= 177200:", cur.fetchall())

cur.execute("SELECT MIN(msg_id), MAX(msg_id), COUNT(*), MIN(date), MAX(date) FROM messages WHERE date >= '2026-09-11'")
print("Messages date >= 2026-09-11:", cur.fetchall())

cur.execute("SELECT COUNT(*) FROM messages")
print("Total messages:", cur.fetchone()[0])

# Check bot_sent_messages
cur.execute("SELECT MIN(msg_id), MAX(msg_id), COUNT(*) FROM bot_sent_messages WHERE msg_id >= 177200")
print("bot_sent_messages msg_id >= 177200:", cur.fetchall())

conn.close()
