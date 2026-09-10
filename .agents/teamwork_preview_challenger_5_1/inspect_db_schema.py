#!/usr/bin/env python3
"""
Inspect SQLite schema, indexes, and EXPLAIN QUERY PLAN for proposed velocity queries.
"""
import sqlite3
import os

DB_PATH = r"c:\Users\danat\Desktop\stomchat\stomat_bot.db"

def inspect_schema():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    print("=== MESSAGES TABLE SCHEMA ===")
    for row in cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"):
        print(row[0])

    print("\n=== INDEXES ON MESSAGES ===")
    for row in cur.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='messages'"):
        print(f"Index: {row[0]} -> {row[1]}")

    print("\n=== EXPLAIN QUERY PLAN FOR VELOCITY QUERY ===")
    # Query from Diff 2:
    # SELECT COUNT(*) FROM messages WHERE date >= ? AND msg_id < 90000000
    cur.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages WHERE date >= '2026-09-08 00:00:00' AND msg_id < 90000000")
    for row in cur.fetchall():
        print(row)

    print("\n=== EXPLAIN QUERY PLAN FOR MSG_ID COUNT QUERY ===")
    # Query from Diff 2 & 3:
    # SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < 90000000
    cur.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages WHERE msg_id > 170000 AND msg_id < 90000000")
    for row in cur.fetchall():
        print(row)

    con.close()

if __name__ == "__main__":
    inspect_schema()
