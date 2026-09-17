import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
msg_map = {m["msg_id"]: m for m in messages}

print("Msg 177434:", json.dumps(msg_map.get(177434), ensure_ascii=False, indent=2))
