import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

memories = data["memories"]

for kid in [747411762, 65668126]:
    mem = memories.get(str(kid))
    print(f"\nDoctor: {mem.get('first_name')} (@{mem.get('username')}) [ID: {kid}]")
    print(f"Specialty: {mem.get('specialty')}")
    print(f"Group Summary: {mem.get('group_summary')}")
