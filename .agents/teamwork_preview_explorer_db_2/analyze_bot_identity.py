import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")

conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
c = conn.cursor()

# Check bot_sent_messages
c.execute("SELECT COUNT(*), MIN(msg_id), MAX(msg_id) FROM bot_sent_messages")
print("bot_sent_messages:", c.fetchone())

# Check replies where reply_to_msg_id is in bot_sent_messages
c.execute("""
    SELECT COUNT(*) 
    FROM messages 
    WHERE reply_to_msg_id IN (SELECT msg_id FROM bot_sent_messages)
""")
print("Replies in messages pointing to bot_sent_messages:", c.fetchone()[0])

# Let's inspect what messages exist where reply_to_msg_id IS NOT NULL
c.execute("SELECT COUNT(*) FROM messages WHERE reply_to_msg_id IS NOT NULL")
print("Total messages with reply_to_msg_id in messages:", c.fetchone()[0])

# Let's check sender_id / sender_username in messages for rows whose msg_id is in bot_sent_messages
c.execute("""
    SELECT DISTINCT sender_id, sender_name, sender_username
    FROM messages
    WHERE msg_id IN (SELECT msg_id FROM bot_sent_messages)
""")
print("Distinct senders of bot_sent_messages that exist in messages table:")
for row in c.fetchall():
    print(row)

# What about messages where text mentions bot or has bot keywords?
# What is the bot's username? Let's check main.py or config.py or assistant.py
conn.close()
