import sqlite3
import sys
import json

sys.stdout.reconfigure(encoding='utf-8')

db_path = "file:c:/Users/danat/Desktop/stomchat/stomat_bot.db?mode=ro"
conn = sqlite3.connect(db_path, uri=True)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 1. Fetch all messages in the range
cur.execute("""
    SELECT msg_id, reply_to_msg_id, sender_id, sender_name, sender_username, 
           text, date, has_media, media_type, media_description, media_remote_url
    FROM messages
    WHERE date >= '2026-09-11'
    ORDER BY msg_id ASC
""")
messages = [dict(r) for r in cur.fetchall()]
print(f"Total messages fetched: {len(messages)}")

# 2. Get all distinct senders (doctors)
sender_ids = sorted(list(set(m['sender_id'] for m in messages if m['sender_id'] != 7971556097)))
print(f"Distinct doctors participating: {len(sender_ids)}")

# 3. Query user_memories for these doctors
placeholders = ','.join('?' for _ in sender_ids)
cur.execute(f"""
    SELECT user_id, username, first_name, specialty, clinical_summary, group_summary, facts_json,
           message_count, pm_message_count, group_message_count, last_updated
    FROM user_memories
    WHERE user_id IN ({placeholders})
""", sender_ids)
memories = {r['user_id']: dict(r) for r in cur.fetchall()}
print(f"Doctors with user_memories profile: {len(memories)} of {len(sender_ids)}")

# 4. Also check user_profiles if any
cur.execute(f"""
    SELECT * FROM user_profiles WHERE user_id IN ({placeholders})
""", sender_ids)
profiles = {r['user_id']: dict(r) for r in cur.fetchall()}
print(f"Doctors with user_profiles: {len(profiles)}")

# Save everything to a structured JSON file in our agent directory for deep inspection
data_dump = {
    "total_messages": len(messages),
    "messages": messages,
    "doctors_count": len(sender_ids),
    "memories": memories,
    "profiles": profiles
}

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "w", encoding="utf-8") as f:
    json.dump(data_dump, f, ensure_ascii=False, indent=2)

print("Saved weekend_dump.json successfully.")

conn.close()
