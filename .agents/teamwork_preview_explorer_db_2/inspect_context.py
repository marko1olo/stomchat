import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Let's inspect context around 171915:
print("Context around msg_id 171915 (@docendobot ну и):")
c.execute("SELECT msg_id, sender_id, sender_name, sender_username, text, date FROM messages WHERE msg_id BETWEEN 171910 AND 171925 ORDER BY msg_id")
for r in c.fetchall():
    print(f"  [{r['msg_id']}] {r['date']} {r['sender_name']} (@{r['sender_username']}): {r['text']}")

print("\nContext around msg_id 172926 (@docendobot , лечим?):")
c.execute("SELECT msg_id, sender_id, sender_name, sender_username, text, date FROM messages WHERE msg_id BETWEEN 172920 AND 172935 ORDER BY msg_id")
for r in c.fetchall():
    print(f"  [{r['msg_id']}] {r['date']} {r['sender_name']} (@{r['sender_username']}): {r['text']}")

# Now let's inspect ALL messages within 1-3 messages AFTER ANY bot message!
# This catches immediate reactions that didn't hit Reply or didn't use the word 'бот'!
print("\nScanning immediate subsequent messages (1-3 msgs after bot message)...")
c.execute("""
    SELECT 
        bot.msg_id as bot_msg_id,
        bot.text as bot_text,
        bot.date as bot_date,
        after_m.msg_id as after_msg_id,
        after_m.sender_id,
        after_m.sender_name,
        after_m.sender_username,
        after_m.text as after_text,
        after_m.date as after_date
    FROM messages bot
    JOIN messages after_m 
      ON after_m.msg_id > bot.msg_id 
     AND after_m.msg_id <= bot.msg_id + 3
    WHERE (bot.sender_id = 7971556097 OR bot.sender_username = 'docendobot')
      AND after_m.sender_id != 7971556097
    ORDER BY bot.msg_id, after_m.msg_id
""")

subsequent = c.fetchall()
print(f"Found {len(subsequent)} subsequent messages immediately after bot messages.")

conn.close()
