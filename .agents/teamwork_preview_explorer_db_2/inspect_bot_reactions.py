import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
memories = data["memories"]
msg_map = {m["msg_id"]: m for m in messages}

bot_msg_ids = set(m["msg_id"] for m in messages if m["sender_username"] == "docendobot")

print(f"Bot message IDs: {sorted(list(bot_msg_ids))}")

# Find messages replying directly to bot messages
direct_replies = [m for m in messages if m.get("reply_to_msg_id") in bot_msg_ids]
print(f"\nDirect replies to bot (count: {len(direct_replies)}):")
for m in direct_replies:
    parent_bot = msg_map.get(m["reply_to_msg_id"], {})
    uid = str(m["sender_id"])
    mem = memories.get(uid, {})
    spec = mem.get("specialty", "N/A")
    print(f"- [Msg {m['msg_id']}] {m['date']} | {m['sender_name']} (@{m['sender_username']}, {spec}) -> reply to bot msg {m['reply_to_msg_id']}:")
    print(f"    Bot text snippet: {parent_bot.get('text', '')[:60]}...")
    print(f"    Doctor text: {m.get('text')}")

# Find messages sent within 5 minutes after a bot reply (subsequent reactions in chat)
print("\n" + "="*80)
print("Subsequent messages within 10 minutes after each bot message:")
print("="*80)

from datetime import datetime

for b_id in sorted(list(bot_msg_ids)):
    b_msg = msg_map[b_id]
    b_time = datetime.strptime(b_msg["date"], "%Y-%m-%d %H:%M:%S")
    print(f"\nBOT MSG {b_id} at {b_msg['date']}: {b_msg.get('text', '')[:70]}...")
    followups = []
    for m in messages:
        if m["msg_id"] > b_id:
            m_time = datetime.strptime(m["date"], "%Y-%m-%d %H:%M:%S")
            diff = (m_time - b_time).total_seconds()
            if 0 < diff <= 600: # within 10 minutes
                followups.append((diff, m))
    for diff, m in followups:
        uid = str(m["sender_id"])
        mem = memories.get(uid, {})
        spec = mem.get("specialty", "N/A")
        print(f"  + {int(diff)}s | [Msg {m['msg_id']}] {m['sender_name']} (@{m['sender_username']}, {spec}, reply_to: {m.get('reply_to_msg_id')}): {m.get('text')}")
