import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
arch_db_path = os.path.join(WORKSPACE, "stomat_archive.db")
conn = sqlite3.connect(f"file:{arch_db_path}?mode=ro", uri=True)
c = conn.cursor()

c.execute("SELECT category_l1, count(*) FROM archive_messages GROUP BY category_l1 ORDER BY count(*) DESC")
print("Archive category_l1 distribution:")
for r in c.fetchall():
    print(f"  {r[0]}: {r[1]:,}")

conn.close()
