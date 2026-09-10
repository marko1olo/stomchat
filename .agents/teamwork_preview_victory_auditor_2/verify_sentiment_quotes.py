import sqlite3
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("=== VERIFYING SECTION 3.3 SENTIMENT QUOTES IN STOMAT_BOT.DB ===")

conn = sqlite3.connect('stomat_bot.db')
cur = conn.cursor()

mids = [168674, 172926, 172291, 174089, 172194, 172311, 176197, 168847, 168965, 171844, 171912, 172057]

for mid in mids:
    cur.execute("SELECT msg_id, date, sender_id, sender_name, text FROM messages WHERE msg_id = ?", (mid,))
    row = cur.fetchone()
    if row:
        print(f"\n[msg_id={row[0]}] {row[1]} | {row[3]} (ID: {row[2]}):")
        print(f"  {row[4][:140]}...")
    else:
        print(f"\n[msg_id={mid}] NOT FOUND in messages")

print("\n=== VERIFYING SECTION 3.4 PM USERS AND VOLUME IN PM_MESSAGES ===")
cur.execute("""
    SELECT sender_id, sender_name, COUNT(*) as cnt 
    FROM pm_messages 
    GROUP BY sender_id 
    ORDER BY cnt DESC
""")
pm_users = cur.fetchall()
for u in pm_users[:12]:
    print(f"  User {u[0]} ({u[1]}): {u[2]} messages")

conn.close()
