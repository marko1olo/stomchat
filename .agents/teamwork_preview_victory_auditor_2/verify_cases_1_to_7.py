import sys
import io
import sqlite3

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("=== VERIFYING SECTION 2.4 CASES 1-6 IN STOMAT_BOT.DB ===")

conn = sqlite3.connect('stomat_bot.db')
cur = conn.cursor()

cases = [
    (1, [176882, 176884], "Implant micro-mobility / Osstem vs Dentium"),
    (2, [176767], "SST Graft protocol"),
    (3, [176751, 176752], "Root fracture / Anchor biomechanics"),
    (4, [176662], "Provicol temporary cement"),
    (5, [176435], "DME subgingival composite"),
    (6, [172965], "Biomimetic veneer prep")
]

for case_num, msg_ids, desc in cases:
    print(f"\n--- Case {case_num}: {desc} ---")
    for mid in msg_ids:
        cur.execute("SELECT msg_id, date, sender_id, sender_name, text FROM messages WHERE msg_id = ?", (mid,))
        row = cur.fetchone()
        if row:
            print(f"  [Found msg_id={row[0]}] {row[1]} | {row[3]} (ID: {row[2]}):")
            print(f"    Text: {row[4]}")
        else:
            print(f"  [NOT FOUND in messages] msg_id={mid}")

conn.close()
