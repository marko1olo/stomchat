import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Get all unique user_ids from pm_messages and check their presence in `messages` or `user_memories`
c.execute("""
    SELECT DISTINCT p.user_id,
           (SELECT username FROM user_memories WHERE user_id = p.user_id) as mem_username,
           (SELECT first_name FROM user_memories WHERE user_id = p.user_id) as mem_first_name,
           (SELECT specialty FROM user_memories WHERE user_id = p.user_id) as mem_specialty,
           (SELECT sender_name FROM messages WHERE sender_id = p.user_id LIMIT 1) as group_name,
           (SELECT sender_username FROM messages WHERE sender_id = p.user_id LIMIT 1) as group_username,
           COUNT(*) as pm_count,
           MIN(p.date) as first_date,
           MAX(p.date) as last_date
    FROM pm_messages p
    GROUP BY p.user_id
    ORDER BY pm_count DESC
""")

rows = c.fetchall()
print(f"Total distinct user_ids in pm_messages: {len(rows)}")
for r in rows:
    print(f"User ID: {r['user_id']} | PM Count: {r['pm_count']} | First: {r['first_date']} | Last: {r['last_date']}")
    print(f"   Memory: @{r['mem_username']} ({r['mem_first_name']}) - Specialty: {r['mem_specialty']}")
    print(f"   Group: @{r['group_username']} ({r['group_name']})")
    print("-" * 60)

conn.close()
