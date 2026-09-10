import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
c = conn.cursor()

c.execute("SELECT DISTINCT user_id, sender_name, count(*) FROM pm_messages GROUP BY user_id, sender_name")
rows = c.fetchall()
print(f"Distinct users in pm_messages: {len(rows)}")
for r in rows:
    print(r)

c.execute("SELECT id, user_id, sender_name, text, date FROM pm_messages ORDER BY id LIMIT 10")
print("\nSample 10 pm_messages:")
for r in c.fetchall():
    print(r)

conn.close()
