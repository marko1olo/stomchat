import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
memories = data["memories"]

def print_range(s, e, title):
    print("\n" + "="*80)
    print(f"{title} (ID {s}..{e})")
    print("="*80)
    for m in messages:
        if s <= m["msg_id"] <= e:
            uid = str(m["sender_id"])
            mem = memories.get(uid, {})
            spec = mem.get("specialty", "N/A")
            print(f"[{m['msg_id']}] {m['date']} | {m['sender_name']} (@{m['sender_username']}) [ID: {m['sender_id']}, {spec}] -> reply_to: {m['reply_to_msg_id']}")
            if m.get('has_media'):
                print(f"   [MEDIA: {m.get('media_type')}] {m.get('media_description')[:140]}...")
            print(f"   Text: {m.get('text')}")

print_range(177250, 177256, "Thread 1: Implant transfer identification")
print_range(177266, 177290, "Thread 2: Leaf gauge & CR controversy")
print_range(177303, 177312, "Thread 3: Vertiprep & margin placement")
print_range(177344, 177355, "Thread 4: Emergence profile & soft-tissue stability")
print_range(177380, 177396, "Thread 5: Ceramic veneer margin step & disk polishing")
print_range(177397, 177412, "Thread 6: Bis-acryl temporary mock-up & vital tooth prep")
print_range(177425, 177430, "Thread 7: Multi-unit 11° implant cone compatibility")
