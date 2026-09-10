import sys
sys.stdout.reconfigure(encoding='utf-8')
import sqlite3

db_path = r"c:\Users\danat\Desktop\stomchat\stomat_bot.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Check tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print(f"Tables in {db_path}: {tables}")

for t in ["bot_sent_messages", "messages", "pm_messages", "user_memories"]:
    if t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        cnt = cur.fetchone()[0]
        print(f"Table {t}: {cnt:,} rows")

if "bot_sent_messages" in tables:
    cur.execute("PRAGMA table_info(bot_sent_messages)")
    cols = cur.fetchall()
    print("Columns in bot_sent_messages:", [(c[1], c[2]) for c in cols])
    
    cur.execute("SELECT * FROM bot_sent_messages ORDER BY rowid DESC LIMIT 10")
    rows = cur.fetchall()
    print("\nRecent 10 rows in bot_sent_messages:")
    for r in rows:
        print(r)
