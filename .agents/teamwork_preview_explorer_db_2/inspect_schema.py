import sqlite3
import os

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"

for db_name in ["stomat_bot.db", "stomat_archive.db"]:
    db_path = os.path.join(WORKSPACE, db_name)
    print("=" * 60)
    print(f"DATABASE: {db_name} (Size: {os.path.getsize(db_path):,} bytes)")
    print("=" * 60)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cursor = conn.cursor()
    
    cursor.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name;")
    tables = cursor.fetchall()
    for name, tbl_type in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM \"{name}\"")
            count = cursor.fetchone()[0]
        except Exception as e:
            count = f"Error: {e}"
        print(f"[{tbl_type.upper()}] {name}: {count:,} rows" if isinstance(count, int) else f"[{tbl_type.upper()}] {name}: {count}")
        cursor.execute(f"PRAGMA table_info(\"{name}\")")
        cols = cursor.fetchall()
        for col in cols:
            cid, col_name, col_type, notnull, dflt_value, pk = col
            pk_str = " [PK]" if pk else ""
            print(f"    - {col_name} ({col_type}){pk_str}")
    conn.close()
