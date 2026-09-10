import sqlite3
import os

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
archive_db_path = os.path.join(WORKSPACE, "stomat_archive.db")

conn_bot = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
c_bot = conn_bot.cursor()

c_bot.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM messages")
min_d, max_d, cnt = c_bot.fetchone()
print(f"messages date range: {min_d} to {max_d} (Total: {cnt})")

c_bot.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM pm_messages")
min_d_pm, max_d_pm, cnt_pm = c_bot.fetchone()
print(f"pm_messages date range: {min_d_pm} to {max_d_pm} (Total: {cnt_pm})")

c_bot.execute("SELECT MIN(id), MAX(id), COUNT(*) FROM bot_sent_messages")
min_b, max_b, cnt_b = c_bot.fetchone()
print(f"bot_sent_messages id range: {min_b} to {max_b} (Total: {cnt_b})")

c_bot.execute("""
    SELECT b.msg_id, m.sender_id, m.sender_name, m.sender_username, m.text, m.date 
    FROM bot_sent_messages b 
    LEFT JOIN messages m ON b.msg_id = m.msg_id 
    LIMIT 5
""")
print("bot_sent_messages sample joined with messages:")
for r in c_bot.fetchall():
    print(r)

# Check if bot messages exist in `messages` directly (e.g. by sender_name or if bot messages are NOT in messages)
c_bot.execute("""
    SELECT count(*) FROM bot_sent_messages b
    INNER JOIN messages m ON b.msg_id = m.msg_id
""")
print(f"bot_sent_messages present in messages table: {c_bot.fetchone()[0]} / {cnt_b}")

# Check who sender_id is for bot messages or distinct bot senders
c_bot.execute("""
    SELECT DISTINCT m.sender_id, m.sender_name, m.sender_username 
    FROM bot_sent_messages b
    JOIN messages m ON b.msg_id = m.msg_id
""")
print("Distinct senders for bot_sent_messages in messages:")
for r in c_bot.fetchall():
    print(r)

conn_archive = sqlite3.connect(f"file:{archive_db_path}?mode=ro", uri=True)
c_arch = conn_archive.cursor()
c_arch.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM archive_messages")
min_da, max_da, cnt_a = c_arch.fetchone()
print(f"archive_messages date range: {min_da} to {max_da} (Total: {cnt_a})")

conn_bot.close()
conn_archive.close()
