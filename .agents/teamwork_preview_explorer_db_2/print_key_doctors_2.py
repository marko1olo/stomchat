import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

memories = data["memories"]

batch = [
    747411762,   # Артём Захарян
    65668126,    # Gregory Mark
    448838231,   # Алексей Фомичев (Fiksich)
    861340008,   # Denis (boje782)
    2103708375,  # Frans
    -1001641799065, # Сергей Елисеев (vertiprep)
    5969900203,  # А (eska1234)
]

for kid in batch:
    mem = memories.get(str(kid))
    if not mem:
        print(f"\nUser {kid}: No profile in user_memories!")
        continue
    print(f"\nDoctor: {mem.get('first_name')} (@{mem.get('username')}) [ID: {kid}]")
    print(f"Specialty: {mem.get('specialty')}")
    print(f"Message Count (Total DB): {mem.get('message_count')} (PM: {mem.get('pm_message_count')}, Group: {mem.get('group_message_count')})")
    print(f"Group Summary: {mem.get('group_summary')}")
