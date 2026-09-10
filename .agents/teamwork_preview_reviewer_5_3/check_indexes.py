import sqlite3

conn = sqlite3.connect("stomat_bot.db")
cur = conn.cursor()
cur.execute("PRAGMA index_list('messages')")
indexes = cur.fetchall()
print("Indexes on messages:")
for idx in indexes:
    print(idx)
    cur.execute(f"PRAGMA index_info('{idx[1]}')")
    print("  Columns:", cur.fetchall())
conn.close()
