import sqlite3
import os
import sys
import json
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT id, user_id, sender_name, text, date FROM pm_messages ORDER BY user_id, date")
pm_rows = c.fetchall()
print(f"Total PM messages: {len(pm_rows)}")

# Group by user_id
users = {}
for r in pm_rows:
    uid = r['user_id']
    if uid not in users:
        users[uid] = {
            "name": r['sender_name'],
            "messages": []
        }
    users[uid]["messages"].append({
        "id": r['id'],
        "text": r['text'],
        "date": str(r['date'])
    })

print(f"Total unique users who interacted in PM: {len(users)}")

# User message count distribution
counts = [len(u["messages"]) for u in users.values()]
print(f"PM dialogue message count per user: min={min(counts)}, max={max(counts)}, avg={sum(counts)/len(counts):.1f}")
print("Distribution of PM messages per user:")
dist = Counter(counts)
for cnt in sorted(dist.keys()):
    print(f"  {cnt} messages: {dist[cnt]} users")

# Analyze content & topics in PM
# Check commands vs clinical cases vs test messages vs chit-chat
categories = {
    "Clinical Case / Consultation": [],
    "Bot Commands (/start, /mode, /quiz, etc.)": [],
    "Testing / Greeting / Chitchat": [],
    "Complaints / Feedback": []
}

for uid, udata in users.items():
    user_text = " \n ".join([m['text'] for m in udata['messages'] if m['text']])
    first_msg = udata['messages'][0]['text'] if udata['messages'] else ""
    
    # Check category
    has_cmd = any(m['text'].startswith('/') for m in udata['messages'] if m['text'])
    is_pure_cmd = all(m['text'].startswith('/') for m in udata['messages'] if m['text'])
    
    has_clinical = any(len(m['text']) > 30 or any(k in m['text'].lower() for k in [
        'зуб', 'корон', 'канал', 'эндо', 'снимок', 'кт', 'уступ', 'цемент', 'преп',
        'пульпит', 'периодонтит', 'имплант', 'десн', 'боли', 'апекс', 'слепок', 'адгезив'
    ]) for m in udata['messages'] if m['text'])
    
    has_complaint = any(any(k in m['text'].lower() for k in ['бред', 'не работает', 'завис', 'глупый', 'почему']) for m in udata['messages'] if m['text'])
    
    if has_complaint:
        categories["Complaints / Feedback"].append((uid, udata['name'], udata['messages']))
    elif has_clinical:
        categories["Clinical Case / Consultation"].append((uid, udata['name'], udata['messages']))
    elif is_pure_cmd:
        categories["Bot Commands (/start, /mode, /quiz, etc.)"].append((uid, udata['name'], udata['messages']))
    else:
        categories["Testing / Greeting / Chitchat"].append((uid, udata['name'], udata['messages']))

print("\n--- PM USER CATEGORIES ---")
for cat, ulist in categories.items():
    total_msgs_in_cat = sum(len(u[2]) for u in ulist)
    print(f"  {cat}: {len(ulist)} users ({total_msgs_in_cat} messages)")

# Save detailed dump
with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\pm_analysis.json", "w", encoding="utf-8") as f:
    json.dump(users, f, ensure_ascii=False, indent=2)

print("\nSaved pm_analysis.json.")

# Print clinical consultations in PM
print("\n" + "="*70)
print("SAMPLE CLINICAL DIALOGUES IN PM:")
print("="*70)
for uid, name, msgs in categories["Clinical Case / Consultation"][:8]:
    print(f"\nUser: {name} (ID: {uid}) - {len(msgs)} messages:")
    for m in msgs[:6]:
        t = m['text'].replace('\n', ' ') if m['text'] else "EMPTY"
        print(f"  [{m['date']}] {t[:120]}")

conn.close()
