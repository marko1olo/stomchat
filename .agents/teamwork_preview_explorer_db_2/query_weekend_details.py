import sqlite3

db_path = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"
conn = sqlite3.connect(db_path, uri=True)
cur = conn.cursor()

# Check bot_sent_messages
cur.execute("SELECT * FROM bot_sent_messages WHERE msg_id >= 177240 ORDER BY msg_id ASC")
bot_sent = cur.fetchall()
print(f"bot_sent_messages >= 177240 (count: {len(bot_sent)}):")
for b in bot_sent:
    print(b)

# Check if bot replies are in `messages` table
cur.execute("SELECT msg_id, sender_id, sender_name, sender_username, text FROM messages WHERE msg_id IN (SELECT msg_id FROM bot_sent_messages WHERE msg_id >= 177240)")
rows = cur.fetchall()
print(f"\nIn messages table matching bot_sent_messages (count: {len(rows)}):")
for r in rows:
    print(r[0], r[1], r[2], r[3], (r[4][:60] if r[4] else None))

# Check messages range 177245..177445
cur.execute("SELECT msg_id, reply_to_msg_id, sender_id, sender_name, sender_username, substr(text, 1, 40), date FROM messages WHERE msg_id >= 177245 ORDER BY msg_id ASC")
all_msgs = cur.fetchall()
print(f"\nTotal messages >= 177245: {len(all_msgs)}")
print("First 5:", all_msgs[:5])
print("Last 5:", all_msgs[-5:])

conn.close()
