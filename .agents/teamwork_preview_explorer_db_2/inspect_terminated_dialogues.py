import sqlite3
import os
import sys
import json

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\dialogue_depth_analysis.json", "r", encoding="utf-8") as f:
    depth_data = json.load(f)

threads = depth_data["threads"]

# Filter threads where last message was from a user (is_last_bot == False)
user_ended_threads = [t for t in threads if not t["is_last_bot"]]
print(f"Total threads ending with user message: {len(user_ended_threads)}")

unanswered_questions = []

for t in user_ended_threads:
    last_id = t["last_msg"]
    c.execute("SELECT msg_id, reply_to_msg_id, sender_id, sender_name, sender_username, text, date FROM messages WHERE msg_id = ?", (last_id,))
    last_msg = c.fetchone()
    
    # Check previous bot message in thread
    c.execute("""
        SELECT msg_id, text, date 
        FROM messages 
        WHERE msg_id = ?
    """, (last_msg['reply_to_msg_id'],))
    parent_bot_msg = c.fetchone()
    
    # Check how many messages intervened immediately after parent_bot_msg before last_msg
    if parent_bot_msg:
        c.execute("SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < ?", (parent_bot_msg['msg_id'], last_id))
        intervening_count = c.fetchone()[0]
    else:
        intervening_count = 0
        
    text = (last_msg['text'] or "").strip()
    # Check if text looks like a question or continued clinical discussion
    is_q = '?' in text or any(w in text.lower() for w in ['как', 'почему', 'что', 'если', 'какой', 'сколько', 'а если', 'но ведь', 'непонял', 'не понял'])
    
    unanswered_questions.append({
        "last_msg_id": last_id,
        "date": str(last_msg['date']),
        "user": f"@{last_msg['sender_username'] or 'no_user'} ({last_msg['sender_name']})",
        "user_text": text,
        "intervening_count_at_reply": intervening_count,
        "is_question": is_q,
        "parent_bot_id": parent_bot_msg['msg_id'] if parent_bot_msg else None,
        "parent_bot_text": parent_bot_msg['text'][:100] if parent_bot_msg and parent_bot_msg['text'] else None
    })

print(f"Total user followups: {len(unanswered_questions)}")
clinical_unanswered = [q for q in unanswered_questions if q["is_question"]]
print(f"Clear unanswered clinical questions/doubts: {len(clinical_unanswered)}")

print("\n" + "="*80)
print("SAMPLE UNANSWERED CLINICAL FOLLOW-UPS (BOT WENT SILENT):")
print("="*80)
for q in clinical_unanswered[:15]:
    print(f"\n[Msg ID: {q['last_msg_id']}] {q['date']} | {q['user']}")
    print(f"  Doctor Asked: \"{q['user_text']}\"")
    print(f"  Intervening messages when replied: {q['intervening_count_at_reply']}")
    if q['parent_bot_id']:
        print(f"  Bot Previous [{q['parent_bot_id']}]: \"{q['parent_bot_text']}...\"")

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\unanswered_clinical_followups.json", "w", encoding="utf-8") as f:
    json.dump(clinical_unanswered, f, ensure_ascii=False, indent=2)

print("\nSaved unanswered_clinical_followups.json successfully.")
conn.close()
