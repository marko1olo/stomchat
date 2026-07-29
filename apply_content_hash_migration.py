# -*- coding: utf-8 -*-
"""
Applies database migration to stomat_wiki.db:
1. Adds 'content_hash' column if not present.
2. Computes normalized SHA256 hash of 'content' for all existing rows.
3. Removes any exact duplicate content rows if present.
4. Creates a UNIQUE INDEX 'idx_content_hash' on distilled_facts(content_hash).
"""
import sqlite3
import hashlib
import re
import os
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

db_path = os.path.join(os.path.dirname(__file__), "stomat_wiki.db")
if not os.path.exists(db_path):
    print(f"Error: Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

def compute_hash(text: str) -> str:
    cleaned = re.sub(r'\s+', ' ', (text or '').lower().strip())
    return hashlib.sha256(cleaned.encode('utf-8')).hexdigest()

# 1. Add column content_hash if missing
cursor.execute("PRAGMA table_info(distilled_facts);")
cols = [row[1] for row in cursor.fetchall()]

if "content_hash" not in cols:
    print("Adding 'content_hash' column to distilled_facts...")
    cursor.execute("ALTER TABLE distilled_facts ADD COLUMN content_hash TEXT;")

# 2. Compute hashes for existing rows
cursor.execute("SELECT id, content FROM distilled_facts WHERE content_hash IS NULL OR content_hash = '';")
rows = cursor.fetchall()
print(f"Computing content_hash for {len(rows)} rows...")

for row_id, content in rows:
    chash = compute_hash(content)
    cursor.execute("UPDATE distilled_facts SET content_hash = ? WHERE id = ?;", (chash, row_id))

conn.commit()

# 3. Deduplicate exact duplicate content_hash rows if any exist
cursor.execute("SELECT content_hash, COUNT(*) FROM distilled_facts GROUP BY content_hash HAVING COUNT(*) > 1;")
dups = cursor.fetchall()

if dups:
    print(f"Found {len(dups)} duplicate content groups. Retaining lowest ID for each...")
    deleted_dups = 0
    for chash, count in dups:
        cursor.execute("SELECT id FROM distilled_facts WHERE content_hash = ? ORDER BY id ASC;", (chash,))
        ids = [r[0] for r in cursor.fetchall()]
        keep_id = ids[0]
        remove_ids = ids[1:]
        cursor.execute(f"DELETE FROM distilled_facts WHERE id IN ({','.join(map(str, remove_ids))});")
        deleted_dups += len(remove_ids)
    print(f"Removed {deleted_dups} duplicate rows.")

conn.commit()

# 4. Create UNIQUE index
print("Creating UNIQUE INDEX idx_content_hash...")
cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash ON distilled_facts(content_hash);")
conn.commit()

print("OK: Database migration completed successfully!")
conn.close()
