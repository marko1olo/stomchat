# -*- coding: utf-8 -*-
"""
Fixes 353 corrupted source_ids in stomat_wiki.db where tokens contain 'MSG_' prefix.
E.g., 'MSG_2421,MSG_2424' -> '2421,2424'
This restores provenance for facts that savdel.py would otherwise ignore.
"""
import sqlite3
import re
import os
import sys

def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    db_path = os.path.join(os.path.dirname(__file__), "stomat_wiki.db")
    if not os.path.exists(db_path):
        print(f"Error: Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT id, source_ids FROM distilled_facts WHERE source_ids LIKE '%MSG_%';")
    rows = cursor.fetchall()

    print(f"Found {len(rows)} corrupted source_ids rows to fix.")

    fixed_count = 0
    for row_id, s_ids in rows:
        cleaned_ids = re.sub(r'MSG_', '', s_ids)
        cursor.execute("UPDATE distilled_facts SET source_ids = ? WHERE id = ?;", (cleaned_ids, row_id))
        fixed_count += 1

    conn.commit()

    # Verify fix
    cursor.execute("SELECT COUNT(*) FROM distilled_facts WHERE source_ids LIKE '%MSG_%';")
    remaining = cursor.fetchone()[0]
    print(f"OK: Successfully cleaned {fixed_count} rows. Remaining corrupted: {remaining}")

    conn.close()

if __name__ == "__main__":
    main()
