import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
memories = data["memories"]
msg_map = {m["msg_id"]: m for m in messages}

# Verify counts
total_msgs = len(messages)
bot_msgs = [m for m in messages if m["sender_username"] == "docendobot"]
human_msgs = [m for m in messages if m["sender_username"] != "docendobot"]

print(f"Total msgs: {total_msgs}")
print(f"Bot msgs: {len(bot_msgs)}")
print(f"Human msgs: {len(human_msgs)}")

# Bot breakdown
dialogue_bot = [m for m in bot_msgs if m["msg_id"] not in [177300, 177301, 177415, 177416]]
digest_bot = [m for m in bot_msgs if m["msg_id"] in [177300, 177301, 177415, 177416]]
print(f"Dialogue bot replies: {len(dialogue_bot)}")
print(f"Digest bot messages: {len(digest_bot)}")

# 9 threads message counts
threads = {
    "T1": [177250, 177251, 177252, 177253, 177254],
    "T2": list(range(177266, 177287)),
    "T3": list(range(177304, 177313)),
    "T4": [177345, 177346, 177347, 177348, 177349, 177350, 177351, 177352, 177353, 177394, 177395, 177396],
    "T5": list(range(177380, 177394)),
    "T6": list(range(177397, 177414)),
    "T7": [177427, 177428],
    "T8": [177430, 177431, 177432, 177433, 177434],
    "T9": list(range(177436, 177444))
}

for tname, ids in threads.items():
    found = [m for m in messages if m["msg_id"] in ids]
    bot_in_t = [m for m in found if m["sender_username"] == "docendobot"]
    print(f"Thread {tname}: {len(found)} msgs ({len(bot_in_t)} bot replies: {[b['msg_id'] for b in bot_in_t]})")
