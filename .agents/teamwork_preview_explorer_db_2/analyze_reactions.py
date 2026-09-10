import sqlite3
import os
import sys
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# 1. Fetch all direct replies to bot messages
# Bot messages are identified by:
# a) msg_id in bot_sent_messages
# b) sender_id = 7971556097 or sender_username = 'docendobot'

c.execute("""
    SELECT 
        m.msg_id,
        m.reply_to_msg_id,
        m.sender_id,
        m.sender_name,
        m.sender_username,
        m.text,
        m.date,
        bot_m.text as bot_text,
        bot_m.date as bot_date
    FROM messages m
    LEFT JOIN messages bot_m ON m.reply_to_msg_id = bot_m.msg_id
    WHERE m.reply_to_msg_id IN (
        SELECT msg_id FROM bot_sent_messages
        UNION
        SELECT msg_id FROM messages WHERE sender_id = 7971556097 OR sender_username = 'docendobot'
    )
    ORDER BY m.date ASC
""")

direct_replies = c.fetchall()
print(f"Total direct replies to bot found: {len(direct_replies)}")

# Let's also look for messages that mention the bot or discuss the bot
keywords = [
    'бот', 'боту', 'ботом', 'боте', 'бота', 'боты',
    'docendobot', 'ии ', 'ии,', 'ии.', 'нейросеть', 'чатгпт', 'chatgpt', 'гпт', 'gpt'
]

# Build query for mentions
clause = " OR ".join([f"LOWER(text) LIKE '%{kw}%'" for kw in keywords])
c.execute(f"""
    SELECT msg_id, reply_to_msg_id, sender_id, sender_name, sender_username, text, date
    FROM messages
    WHERE ({clause})
      AND sender_id != 7971556097
    ORDER BY date ASC
""")

mentions = c.fetchall()
print(f"Total non-bot messages mentioning bot/AI keywords: {len(mentions)}")

# Save raw direct replies and mentions to json for further processing
replies_data = [dict(r) for r in direct_replies]
mentions_data = [dict(r) for r in mentions]

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\direct_replies.json", "w", encoding="utf-8") as f:
    json.dump(replies_data, f, ensure_ascii=False, indent=2)

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\mentions.json", "w", encoding="utf-8") as f:
    json.dump(mentions_data, f, ensure_ascii=False, indent=2)

print("Saved direct_replies.json and mentions.json successfully.")
conn.close()
