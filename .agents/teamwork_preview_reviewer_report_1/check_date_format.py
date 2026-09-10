import sqlite3

conn = sqlite3.connect("stomat_bot.db")
c = conn.cursor()
c.execute("SELECT msg_id, date, sender_name FROM messages ORDER BY msg_id DESC LIMIT 5")
rows = c.fetchall()
for r in rows:
    print(r)
conn.close()
