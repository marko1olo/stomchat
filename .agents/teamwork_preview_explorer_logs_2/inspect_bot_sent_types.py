import sys
sys.stdout.reconfigure(encoding='utf-8')
import sqlite3

conn = sqlite3.connect(r"c:\Users\danat\Desktop\stomchat\stomat_bot.db")
cur = conn.cursor()

cur.execute("""
SELECT 
    CASE WHEN chat_id < 0 THEN 'Group Chat' ELSE 'PM (Private)' END as chat_type,
    COUNT(*) as count
FROM bot_sent_messages
GROUP BY chat_type
""")
for row in cur.fetchall():
    print(f"{row[0]}: {row[1]}")

cur.execute("""
SELECT 
    m.msg_id, m.date, m.reply_to_msg_id, m.text 
FROM messages m 
JOIN bot_sent_messages b ON m.msg_id = b.msg_id 
ORDER BY m.msg_id DESC LIMIT 10
""")
print("\nLast 10 group messages sent by bot in messages table:")
for r in cur.fetchall():
    print(f"ID: {r[0]} | Date: {r[1]} | ReplyTo: {r[2]} | Text: {r[3][:60]}...")
