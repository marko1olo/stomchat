import sqlite3
import sys
import os

sys.stdout.reconfigure(encoding='utf-8')

DB_PATH = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"

conn = sqlite3.connect(DB_PATH, uri=True)
cursor = conn.cursor()

sample_ids = [177250, 177252, 177266, 177268, 177272, 177278, 177283, 177390, 177392]

print("=== VERIFYING TELEMETRY MESSAGE IDS IN DATABASE ===")
for mid in sample_ids:
    cursor.execute("SELECT msg_id, sender_id, sender_name, text, reply_to_msg_id, date FROM messages WHERE msg_id = ?", (mid,))
    row = cursor.fetchone()
    if row:
        sender_id = row[1]
        sender_name = row[2]
        dt = row[5]
        preview = (row[3] or "")[:60].replace("\n", " ")
        print(f"PASS: Msg {mid} | Date: {dt} | Sender: {sender_name} ({sender_id}) | Text: '{preview}'")
    else:
        print(f"FAIL: Msg {mid} not found in messages table!")

# Count weekend messages between 177243 and 177445
cursor.execute("SELECT count(*) FROM messages WHERE msg_id >= 177243 AND msg_id <= 177445")
count_range = cursor.fetchone()[0]
print(f"\nTotal messages in range 177243-177445: {count_range}")

conn.close()
