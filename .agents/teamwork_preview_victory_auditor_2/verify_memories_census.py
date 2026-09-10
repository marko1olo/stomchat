import sqlite3
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("=== INDEPENDENT USER_MEMORIES CENSUS ===")

conn = sqlite3.connect('stomat_bot.db')
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM user_memories")
total_memories = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM user_memories WHERE facts_json = '[]'")
facts_empty = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM user_memories WHERE group_summary IS NOT NULL AND length(trim(group_summary)) > 0")
gs_count = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM user_memories WHERE specialty IS NOT NULL AND length(trim(specialty)) > 0")
spec_count = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM user_memories WHERE clinical_summary IS NOT NULL AND length(trim(clinical_summary)) > 0")
cs_count = cur.fetchone()[0]

print(f"Total user_memories: {total_memories}")
print(f"facts_json == '[]': {facts_empty} ({facts_empty / total_memories * 100:.1f}%)")
print(f"group_summary present: {gs_count} ({gs_count / total_memories * 100:.1f}%)")
print(f"specialty present: {spec_count} ({spec_count / total_memories * 100:.1f}%)")
print(f"clinical_summary present: {cs_count} ({cs_count / total_memories * 100:.1f}%)")

cur.execute("SELECT specialty, COUNT(*) FROM user_memories WHERE specialty IS NOT NULL GROUP BY specialty ORDER BY COUNT(*) DESC LIMIT 20")
print("\nTop 20 raw specialties in user_memories:")
for s, c in cur.fetchall():
    print(f"  {s}: {c}")

conn.close()
