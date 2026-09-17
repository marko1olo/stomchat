import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

memories = data["memories"]
profiles = data["profiles"]
messages = data["messages"]

# Find all senders in weekend messages
senders_stats = {}
for m in messages:
    sid = m["sender_id"]
    if sid == 7971556097:
        continue
    if sid not in senders_stats:
        senders_stats[sid] = {
            "name": m["sender_name"],
            "username": m["sender_username"],
            "count": 0,
            "first_msg_date": m["date"],
            "last_msg_date": m["date"],
        }
    senders_stats[sid]["count"] += 1
    senders_stats[sid]["last_msg_date"] = m["date"]

print(f"Total participating doctors: {len(senders_stats)}")
print("="*100)
print(f"{'User ID':<12} | {'Name':<22} | {'Username':<22} | {'Specialty in Memory':<35} | {'Msgs'}")
print("="*100)

for sid, sinfo in sorted(senders_stats.items(), key=lambda x: -x[1]["count"]):
    mem = memories.get(str(sid), {})
    spec = mem.get("specialty", "N/A (No memory)")
    un = sinfo["username"] or "None"
    print(f"{sid:<12} | {sinfo['name'][:22]:<22} | @{un[:20]:<21} | {spec[:35]:<35} | {sinfo['count']}")

# Also let's inspect the clinical_summary and group_summary of key doctors:
key_doctors = [
    747411762,   # Артём Захарян
    65668126,    # Gregory Mark
    448838231,   # Алексей Фомичев (Fiksich)
    861340008,   # Denis (boje782)
    2103708375,  # Frans
    -1001641799065, # Сергей Елисеев (vertiprep)
    5969900203,  # А (eska1234)
    1748528850,  # ilya t
    371830303,   # Михаил Михайлов
    6544359473,  # Даниил Шаронов
    831786934,   # Kate_Zhukova_preventiv doc
    5668987918,  # Дарья
    290516391,   # Чес Чернояров
    3337715      # Дониёр Абдуалимов
]

print("\n" + "="*100)
print("DEEP DIVE: KEY DOCTOR PROFILES IN USER_MEMORIES")
print("="*100)

for kid in key_doctors:
    mem = memories.get(str(kid))
    if not mem:
        print(f"\nUser {kid}: No profile in user_memories!")
        continue
    print(f"\nDoctor: {mem.get('first_name')} (@{mem.get('username')}) [ID: {kid}]")
    print(f"Specialty: {mem.get('specialty')}")
    print(f"Message Count (Total DB): {mem.get('message_count')} (PM: {mem.get('pm_message_count')}, Group: {mem.get('group_message_count')})")
    print(f"Last updated: {mem.get('last_updated')}")
    print(f"Clinical Summary: {mem.get('clinical_summary')}")
    print(f"Group Summary: {mem.get('group_summary')}")
    if mem.get('facts_json'):
        print(f"Facts JSON: {mem.get('facts_json')[:250]}...")
