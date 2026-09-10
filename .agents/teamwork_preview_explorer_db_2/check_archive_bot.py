import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
arch_db_path = os.path.join(WORKSPACE, "stomat_archive.db")
conn = sqlite3.connect(f"file:{arch_db_path}?mode=ro", uri=True)
c = conn.cursor()

c.execute("SELECT COUNT(*) FROM archive_messages WHERE sender_id = 7971556097 OR sender_username = 'docendobot'")
print("Bot messages in archive_messages:", c.fetchone()[0])

c.execute("""
    SELECT COUNT(*) FROM archive_messages 
    WHERE text LIKE '%docendobot%' 
       OR text LIKE '%@docendobot%' 
       OR text LIKE '%бот%' 
       OR text LIKE '%Бот%'
""")
print("Archive messages mentioning bot keywords:", c.fetchone()[0])

c.execute("SELECT MIN(date), MAX(date) FROM archive_messages")
print("Archive date range:", c.fetchone())

conn.close()
