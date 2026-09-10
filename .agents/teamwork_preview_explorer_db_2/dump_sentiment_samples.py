import json
import sqlite3
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\classified_reactions.json", "r", encoding="utf-8") as f:
    data = json.load(f)

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

def get_msg(msg_id):
    if not msg_id:
        return None
    c.execute("SELECT msg_id, sender_name, sender_username, text, date FROM messages WHERE msg_id = ?", (msg_id,))
    return c.fetchone()

print("="*70)
print("NEGATIVE / FRUSTRATED REACTIONS (Direct & Followups)")
print("="*70)
all_neg = data["direct_replies"]["Negative/Frustrated"] + data["post_bot_followups"]["Negative/Frustrated"]
for item in all_neg:
    b_msg = get_msg(item.get("reply_to_msg_id") or item.get("bot_msg_id"))
    print(f"\nMessage ID: {item['msg_id']} | Date: {item['date']} | User: @{item['sender_username']} ({item['sender_name']})")
    print(f"Triggers: {item['triggers']}")
    print(f"Doctor text: \"{item['text']}\"")
    if b_msg:
        print(f"  --> In response to Bot [{b_msg['msg_id']}]: \"{b_msg['text'][:140]}...\"")

print("\n" + "="*70)
print("SKEPTICAL / MOCKERY REACTIONS (Direct & Followups)")
print("="*70)
all_skep = data["direct_replies"]["Skeptical"] + data["post_bot_followups"]["Skeptical"]
for item in all_skep[:15]:
    b_msg = get_msg(item.get("reply_to_msg_id") or item.get("bot_msg_id"))
    print(f"\nMessage ID: {item['msg_id']} | Date: {item['date']} | User: @{item['sender_username']} ({item['sender_name']})")
    print(f"Triggers: {item['triggers']}")
    print(f"Doctor text: \"{item['text']}\"")
    if b_msg:
        print(f"  --> In response to Bot [{b_msg['msg_id']}]: \"{b_msg['text'][:140]}...\"")

print("\n" + "="*70)
print("POSITIVE REACTIONS (Direct & Followups)")
print("="*70)
all_pos = data["direct_replies"]["Positive"] + data["post_bot_followups"]["Positive"]
for item in all_pos[:10]:
    b_msg = get_msg(item.get("reply_to_msg_id") or item.get("bot_msg_id"))
    print(f"\nMessage ID: {item['msg_id']} | Date: {item['date']} | User: @{item['sender_username']} ({item['sender_name']})")
    print(f"Doctor text: \"{item['text']}\"")
    if b_msg:
        print(f"  --> In response to Bot [{b_msg['msg_id']}]: \"{b_msg['text'][:140]}...\"")

conn.close()
