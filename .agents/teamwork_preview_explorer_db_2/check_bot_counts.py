import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
c = conn.cursor()

c.execute("SELECT COUNT(*) FROM messages WHERE sender_id = 7971556097 OR sender_username = 'docendobot'")
print("Total messages in messages table by bot:", c.fetchone()[0])

c.execute("""
    SELECT COUNT(*) 
    FROM messages 
    WHERE reply_to_msg_id IN (
        SELECT msg_id FROM messages WHERE sender_id = 7971556097 OR sender_username = 'docendobot'
    )
""")
print("Replies in messages pointing to bot's messages in messages table:", c.fetchone()[0])

# Also check how many replies point to either bot_sent_messages OR sender_id = 7971556097
c.execute("""
    SELECT COUNT(*)
    FROM messages
    WHERE reply_to_msg_id IN (
        SELECT msg_id FROM bot_sent_messages
        UNION
        SELECT msg_id FROM messages WHERE sender_id = 7971556097 OR sender_username = 'docendobot'
    )
""")
print("Total direct replies to bot (union of bot_sent_messages and bot sender):", c.fetchone()[0])

# Also check mentions of docendobot or 'бот' or 'bot' in messages
c.execute("""
    SELECT COUNT(*) FROM messages 
    WHERE text LIKE '%docendobot%' 
       OR text LIKE '%@docendobot%' 
       OR text LIKE '%бот%' 
       OR text LIKE '%Бот%'
""")
print("Messages mentioning bot keywords:", c.fetchone()[0])

conn.close()
