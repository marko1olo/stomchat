import sys
sys.stdout.reconfigure(encoding='utf-8')
import sqlite3

conn = sqlite3.connect(r"c:\Users\danat\Desktop\stomchat\stomat_bot.db")
cur = conn.cursor()

sample_ids = [176849, 176854, 176858, 176867, 175309, 175546, 175560, 176314, 175954, 175314, 171910, 176870, 176872]

print("=== DETAILED MESSAGE DETAILS FROM SQLITE ===")
for mid in sample_ids:
    cur.execute("SELECT msg_id, sender_name, date, reply_to_msg_id, text FROM messages WHERE msg_id = ?", (mid,))
    row = cur.fetchone()
    if row:
        print(f"\nMessage #{row[0]} | Date: {row[2]} | Author: {row[1]} | ReplyTo: {row[3]}")
        print(f"Content: {row[4]}")
    else:
        print(f"\nMessage #{mid} not found in DB")
