import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

db_path = "stomat_bot.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Check total user_memories
cur.execute("SELECT COUNT(*) FROM user_memories")
total_mem = cur.fetchone()[0]
print(f"Total user_memories: {total_mem}")

# Check specialty
cur.execute("SELECT COUNT(*) FROM user_memories WHERE specialty IS NOT NULL AND trim(specialty) != ''")
specialty_count = cur.fetchone()[0]
print(f"Specialty populated: {specialty_count} ({specialty_count/total_mem*100:.1f}%)")

# Check group_summary
cur.execute("SELECT COUNT(*) FROM user_memories WHERE group_summary IS NOT NULL AND trim(group_summary) != ''")
group_summary_count = cur.fetchone()[0]
print(f"Group summary populated: {group_summary_count} ({group_summary_count/total_mem*100:.1f}%)")

# Check facts_json
cur.execute("SELECT COUNT(*) FROM user_memories WHERE facts_json IS NOT NULL")
facts_non_null = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM user_memories WHERE facts_json = '[]'")
facts_empty_brackets = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM user_memories WHERE facts_json IS NOT NULL AND facts_json != '[]'")
facts_non_empty = cur.fetchone()[0]
print(f"facts_json non-null: {facts_non_null}")
print(f"facts_json literal '[]': {facts_empty_brackets}")
print(f"facts_json populated with real facts: {facts_non_empty}")

# Check clinical_summary
cur.execute("SELECT COUNT(*) FROM user_memories WHERE clinical_summary IS NOT NULL AND trim(clinical_summary) != ''")
clin_summary_count = cur.fetchone()[0]
print(f"Clinical summary (PM) populated: {clin_summary_count} ({clin_summary_count/total_mem*100:.1f}%)")

conn.close()
