import sqlite3
import os
import sys
import json
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute("SELECT COUNT(*) FROM user_memories")
total_mem = c.fetchone()[0]
print(f"Total records in user_memories: {total_mem}")

c.execute("""
    SELECT 
        user_id, username, first_name, specialty, 
        clinical_summary, group_summary, facts_json,
        message_count, pm_message_count, group_message_count,
        last_pm_analyzed_id, last_group_analyzed_id, last_updated
    FROM user_memories
    ORDER BY message_count DESC
""")
memories = c.fetchall()

# Statistics:
# 1. How many have non-null/non-empty specialty?
with_specialty = [m for m in memories if m['specialty'] and m['specialty'].strip()]
# 2. How many have clinical_summary?
with_clin_summary = [m for m in memories if m['clinical_summary'] and m['clinical_summary'].strip()]
# 3. How many have group_summary?
with_grp_summary = [m for m in memories if m['group_summary'] and m['group_summary'].strip()]
# 4. How many have facts_json?
with_facts = [m for m in memories if m['facts_json'] and m['facts_json'].strip() and m['facts_json'] != '{}']
# 5. Message count stats
msg_counts = [m['message_count'] or 0 for m in memories]
pm_msg_counts = [m['pm_message_count'] or 0 for m in memories]
grp_msg_counts = [m['group_message_count'] or 0 for m in memories]

print(f"\n--- COMPLETENESS METRICS (out of {total_mem} profiles) ---")
print(f"  Profiles with Specialty: {len(with_specialty)} ({len(with_specialty)/total_mem*100:.1f}%)")
print(f"  Profiles with Clinical Summary: {len(with_clin_summary)} ({len(with_clin_summary)/total_mem*100:.1f}%)")
print(f"  Profiles with Group Summary: {len(with_grp_summary)} ({len(with_grp_summary)/total_mem*100:.1f}%)")
print(f"  Profiles with Non-empty Facts JSON: {len(with_facts)} ({len(with_facts)/total_mem*100:.1f}%)")

# Specialties breakdown
spec_list = []
for m in with_specialty:
    # Split composite specialties e.g. "Стоматолог-ортопед / Хирург"
    parts = [p.strip() for p in m['specialty'].replace('/', ',').split(',')]
    spec_list.extend(parts)

spec_counter = Counter(spec_list)
print("\n--- SPECIALTIES BREAKDOWN (raw & parsed) ---")
for spec, count in spec_counter.most_common(20):
    print(f"  {spec}: {count}")

# Message count distribution in memories
print("\n--- ACTIVITY IN MEMORIES ---")
print(f"  Total messages tracked: {sum(msg_counts):,}")
print(f"  Max messages for single doctor: {max(msg_counts)}")
print(f"  Doctors with >= 10 messages: {len([c for c in msg_counts if c >= 10])}")
print(f"  Doctors with >= 50 messages: {len([c for c in msg_counts if c >= 50])}")
print(f"  Doctors with >= 100 messages: {len([c for c in msg_counts if c >= 100])}")
print(f"  Doctors with PM messages > 0: {len([c for c in pm_msg_counts if c > 0])}")

# Top 10 doctors by profile richness & activity
print("\n--- TOP 10 CLINICAL PROFILES BY MESSAGE COUNT ---")
for m in memories[:10]:
    print(f"User ID: {m['user_id']} | @{m['username'] or 'no_user'} ({m['first_name']})")
    print(f"  Specialty: {m['specialty']}")
    print(f"  Messages: total={m['message_count']}, group={m['group_message_count']}, pm={m['pm_message_count']}")
    print(f"  Clinical Summary: {(m['clinical_summary'] or '')[:150]}...")
    print(f"  Group Summary: {(m['group_summary'] or '')[:150]}...")
    print("-" * 60)

# Save memory audit data
summary_data = {
    "total_records": total_mem,
    "with_specialty": len(with_specialty),
    "with_clinical_summary": len(with_clin_summary),
    "with_group_summary": len(with_grp_summary),
    "with_facts": len(with_facts),
    "specialties_distribution": dict(spec_counter.most_common(30)),
    "top_profiles": [dict(m) for m in memories[:15]]
}

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\memory_audit.json", "w", encoding="utf-8") as f:
    json.dump(summary_data, f, ensure_ascii=False, indent=2)

print("Saved memory_audit.json successfully.")
conn.close()
