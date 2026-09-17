import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open("c:/Users/danat/Desktop/stomchat/.agents/teamwork_preview_explorer_db_2/weekend_dump.json", "r", encoding="utf-8") as f:
    data = json.load(f)

messages = data["messages"]
memories = data["memories"]
msg_map = {m["msg_id"]: m for m in messages}

def dump_thread(title, start_id, end_id):
    print("\n" + "="*90)
    print(f"THREAD: {title} (ID {start_id}..{end_id})")
    print("="*90)
    sub = [m for m in messages if start_id <= m["msg_id"] <= end_id]
    for m in sub:
        uid = str(m["sender_id"])
        mem = memories.get(uid, {})
        spec = mem.get("specialty", "N/A")
        fn = mem.get("first_name", "")
        un = mem.get("username", "")
        print(f"\n[Msg ID: {m['msg_id']}] | Date: {m['date']} | Sender: {m['sender_name']} (@{m['sender_username']})")
        print(f"  User ID: {m['sender_id']} | Profile Specialty: {spec} | Memory Name: {fn} (@{un})")
        print(f"  Reply-to Msg ID: {m['reply_to_msg_id']}")
        if m.get("has_media"):
            print(f"  Media: {m.get('media_type')} | Description: {m.get('media_description')}")
        print(f"  Text: {m.get('text')}")

dump_thread("Thread 1: Implant transfer identification", 177250, 177255)
dump_thread("Thread 2: Leaf gauge & centric relation (CR) controversy", 177265, 177286)
dump_thread("Thread 3: Vertiprep & margin placement", 177302, 177312)
dump_thread("Thread 4: Emergence profile & soft-tissue stability at 6 months", 177344, 177352)
dump_thread("Thread 5: Ceramic veneer margin step & disk polishing dispute", 177378, 177395)
dump_thread("Thread 6: Bis-acryl temporary mock-up & vital tooth prep", 177397, 177412)
dump_thread("Thread 7: Multi-unit 11° implant cone compatibility", 177424, 177430)
dump_thread("Thread 8: Invasive cervical resorption / pink tooth on 2.6", 177430, 177435)
dump_thread("Thread 9: E.max adhesive luting protocols", 177436, 177445)
