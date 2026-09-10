import sys
import io
import sqlite3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("=== VERIFYING SECTION 2.4 EXACT CASES IN DB & LOGS ===")

conn = sqlite3.connect('stomat_bot.db')
cur = conn.cursor()

mids = [175560, 176314, 176308, 175954, 175946, 175314, 175308, 176849, 176854, 176858, 176867]

for mid in mids:
    cur.execute("SELECT msg_id, date, sender_id, sender_name, text FROM messages WHERE msg_id = ?", (mid,))
    row = cur.fetchone()
    if row:
        print(f"\n[Found msg_id={row[0]}] {row[1]} | {row[3]} (ID: {row[2]}):")
        print(f"  Text: {row[4][:120]}...")
    else:
        # Check bot_sent_messages
        cur.execute("SELECT msg_id, date, reply_to_msg_id, text FROM bot_sent_messages WHERE msg_id = ?", (mid,))
        brow = cur.fetchone()
        if brow:
            print(f"\n[Found in bot_sent_messages msg_id={brow[0]}] {brow[1]} | reply_to={brow[2]}:")
            print(f"  Text: {brow[3][:120]}...")
        else:
            print(f"\n[NOT FOUND anywhere] msg_id={mid}")

conn.close()

print("\n--- Checking Log Anchors ---")
def check_line(path, line_num):
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f, 1):
                if abs(i - line_num) <= 2:
                    print(f"  {path}:{i} | {line.strip()[:110]}")
    except Exception as e:
        print(f"  Error reading {path}: {e}")

print("Checking Case 1 (bot.log.1:25473):")
check_line('bot.log.1', 25473)

print("\nChecking Case 2 (bot.log.1:37558):")
check_line('bot.log.1', 37558)

print("\nChecking Case 3 (bot.log.1:29909):")
check_line('bot.log.1', 29909)

print("\nChecking Case 4 (bot.log.1:22691):")
check_line('bot.log.1', 22691)

print("\nChecking Case 5 (bot.log:8140):")
check_line('bot.log', 8140)

print("\nChecking Case 6 (bot.log.1:30350):")
check_line('bot.log.1', 30350)
