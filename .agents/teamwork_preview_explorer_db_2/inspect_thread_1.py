import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
memories = data["memories"]

for m in messages:
    if 177250 <= m["msg_id"] <= 177255:
        uid = str(m["sender_id"])
        mem = memories.get(uid, {})
        spec = mem.get("specialty", "N/A")
        print(f"[{m['msg_id']}] {m['date']} | {m['sender_name']} (@{m['sender_username']}) [ID: {m['sender_id']}, {spec}] -> reply_to: {m['reply_to_msg_id']}")
        if m.get('has_media'):
            print(f"   [MEDIA: {m.get('media_type')}] {m.get('media_description')[:140]}...")
        print(f"   Text: {m.get('text')}\n")
