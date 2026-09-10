import sqlite3
import os

print("=== INDEPENDENT SQLITE DATABASE AUDIT ===")

# Check stomat_bot.db
db1_path = 'stomat_bot.db'
if os.path.exists(db1_path):
    conn1 = sqlite3.connect(db1_path)
    cur1 = conn1.cursor()
    cur1.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables1 = cur1.fetchall()
    print(f"\n[stomat_bot.db] Tables: {tables1}")
    
    for (t,) in tables1:
        cur1.execute(f"SELECT COUNT(*) FROM {t}")
        cnt = cur1.fetchone()[0]
        print(f"  Table '{t}': {cnt} rows")
        
    # Check messages date range
    if ('messages',) in tables1:
        cur1.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM messages")
        min_d, max_d, cnt = cur1.fetchone()
        print(f"  messages: min_date={min_d}, max_date={max_d}, total={cnt}")
        
    # Check bot_sent_messages
    if ('bot_sent_messages',) in tables1:
        cur1.execute("SELECT COUNT(*) FROM bot_sent_messages")
        cnt = cur1.fetchone()[0]
        print(f"  bot_sent_messages: {cnt}")
        
    # Check pm_messages
    if ('pm_messages',) in tables1:
        cur1.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM pm_messages")
        min_d, max_d, cnt = cur1.fetchone()
        print(f"  pm_messages: min_date={min_d}, max_date={max_d}, total={cnt}")

    # Check user_memories
    if ('user_memories',) in tables1:
        cur1.execute("SELECT COUNT(*) FROM user_memories")
        cnt = cur1.fetchone()[0]
        print(f"  user_memories: {cnt}")
        cur1.execute("SELECT COUNT(DISTINCT user_id) FROM user_memories")
        distinct_users = cur1.fetchone()[0]
        print(f"  user_memories distinct user_id: {distinct_users}")

    conn1.close()
else:
    print(f"[stomat_bot.db] NOT FOUND at {db1_path}")

# Check stomat_archive.db
db2_path = 'stomat_archive.db'
if os.path.exists(db2_path):
    conn2 = sqlite3.connect(db2_path)
    cur2 = conn2.cursor()
    cur2.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables2 = cur2.fetchall()
    print(f"\n[stomat_archive.db] Tables: {tables2}")
    
    for (t,) in tables2:
        cur2.execute(f"SELECT COUNT(*) FROM {t}")
        cnt = cur2.fetchone()[0]
        print(f"  Table '{t}': {cnt} rows")
        
    if ('messages',) in tables2:
        cur2.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM messages")
        min_d, max_d, cnt = cur2.fetchone()
        print(f"  archive messages: min_date={min_d}, max_date={max_d}, total={cnt}")

    conn2.close()
else:
    print(f"[stomat_archive.db] NOT FOUND at {db2_path}")
