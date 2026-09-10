import sqlite3

conn = sqlite3.connect('stomat_archive.db')
cur = conn.cursor()
cur.execute("PRAGMA table_info(archive_messages)")
cols = cur.fetchall()
print("archive_messages columns:", cols)
cur.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM archive_messages")
print("archive date range:", cur.fetchone())
conn.close()
