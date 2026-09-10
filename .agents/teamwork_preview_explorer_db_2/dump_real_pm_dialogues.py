import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

test_ids = {777111, 777222, 9999991, 999}

c.execute("""
    SELECT p.id, p.user_id, p.sender_name, p.text, p.date,
           m.specialty, m.username, m.first_name
    FROM pm_messages p
    LEFT JOIN user_memories m ON p.user_id = m.user_id
    ORDER BY p.user_id, p.date
""")

rows = c.fetchall()

current_user = None
with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\real_pm_transcripts.txt", "w", encoding="utf-8") as f:
    for r in rows:
        uid = r['user_id']
        if uid in test_ids:
            continue
        if uid != current_user:
            current_user = uid
            f.write("\n" + "="*80 + "\n")
            f.write(f"DOCTOR: {r['sender_name']} (Telegram ID: {uid}, @{r['username']}, Name: {r['first_name']})\n")
            f.write(f"SPECIALTY IN MEMORY: {r['specialty']}\n")
            f.write("="*80 + "\n")
        f.write(f"[{r['date']}] ({r['sender_name']}): {r['text']}\n")

print("Wrote real_pm_transcripts.txt successfully.")
conn.close()
