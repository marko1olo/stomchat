import json
import sys
import re

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
memories = data["memories"]
bot_replies_to = set(m["reply_to_msg_id"] for m in messages if m["sender_username"] == "docendobot" and m["reply_to_msg_id"])

print("Messages replied to by bot:", sorted(list(bot_replies_to)))

# Find clinical queries / questions from users that received no bot reply:
# Criteria: message contains '?', or 'подскажите', or 'коллеги', or has media, and sender is not bot
unanswered = []
for m in messages:
    if m["sender_username"] == "docendobot":
        continue
    text = m.get("text", "") or ""
    has_q = "?" in text or any(w in text.lower() for w in ["подскажите", "посоветуйте", "кто использовал", "как думаете", "можно ли", "по диагнозу"])
    has_media = m.get("has_media")
    
    # Did bot reply to this message directly or indirectly?
    if m["msg_id"] not in bot_replies_to:
        if has_q or has_media:
            unanswered.append(m)

print(f"\nPotential clinical questions/media without direct bot reply: {len(unanswered)}")
for u in unanswered:
    uid = str(u["sender_id"])
    mem = memories.get(uid, {})
    spec = mem.get("specialty", "N/A")
    print(f"- [Msg {u['msg_id']}] {u['date']} | {u['sender_name']} (@{u['sender_username']}, {spec}) | media: {u.get('media_type')}")
    print(f"    Text: {u.get('text')[:100]}")
