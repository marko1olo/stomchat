import json
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\unanswered_clinical_followups.json", "r", encoding="utf-8") as f:
    items = json.load(f)

recent = [i for i in items if i["last_msg_id"] > 175000]
print(f"Recent unanswered followups (> 175000, August-September 2026): {len(recent)}")
for r in recent:
    print(f"\n[Msg ID: {r['last_msg_id']}] {r['date']} | {r['user']}")
    print(f"  Doctor: \"{r['user_text']}\"")
    print(f"  Intervening msgs: {r['intervening_count_at_reply']}")
    print(f"  Bot Prev [{r['parent_bot_id']}]: \"{r['parent_bot_text']}\"")
