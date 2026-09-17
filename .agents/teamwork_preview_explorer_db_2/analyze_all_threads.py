import json
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
memories = data["memories"]
profiles = data["profiles"]
msg_map = {m["msg_id"]: m for m in messages}

# List of target threads and their approximate bounds
threads_spec = [
    {"name": "Thread 1: Implant transfer identification", "start": 177250, "end": 177255},
    {"name": "Thread 2: Leaf gauge & centric relation (CR) controversy", "start": 177266, "end": 177295},
    {"name": "Thread 3: Vertiprep & margin placement", "start": 177303, "end": 177315},
    {"name": "Thread 4: Emergence profile & soft-tissue stability at 6 months", "start": 177345, "end": 177355},
    {"name": "Thread 5: Ceramic veneer margin step & disk polishing dispute", "start": 177380, "end": 177397},
    {"name": "Thread 6: Bis-acryl temporary mock-up & vital tooth prep", "start": 177398, "end": 177412},
    {"name": "Thread 7: Multi-unit 11° implant cone compatibility", "start": 177425, "end": 177430},
    {"name": "Thread 8: Invasive cervical resorption / pink tooth on 2.6", "start": 177430, "end": 177435},
    {"name": "Thread 9: E.max adhesive luting protocols", "start": 177436, "end": 177445},
]

print("="*80)
print("ANALYSIS OF 9 THREADS")
print("="*80)

for t in threads_spec:
    print(f"\n### {t['name']} (IDs {t['start']}..{t['end']})")
    in_range = [m for m in messages if t['start'] <= m['msg_id'] <= t['end']]
    for m in in_range:
        uid = str(m['sender_id'])
        mem = memories.get(uid, {})
        spec = mem.get('specialty', 'N/A')
        print(f"- [{m['msg_id']}] {m['date']} | {m['sender_name']} (@{m['sender_username']}, ID:{m['sender_id']}, Spec:{spec}) | reply_to: {m['reply_to_msg_id']}")
        if m.get('has_media'):
            print(f"  [MEDIA: {m.get('media_type')}] {m.get('media_description')[:120]}...")
        print(f"  Text: {m.get('text')}")
