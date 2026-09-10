import sqlite3

def check_db():
    conn = sqlite3.connect("stomat_bot.db")
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages")
    print("stomat_bot.db messages:", c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM bot_sent_messages")
    print("stomat_bot.db bot_sent_messages:", c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM pm_messages")
    print("stomat_bot.db pm_messages:", c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM user_memories")
    print("stomat_bot.db user_memories:", c.fetchone()[0])
    conn.close()

    conn2 = sqlite3.connect("stomat_archive.db")
    c2 = conn2.cursor()
    c2.execute("SELECT COUNT(*) FROM archive_messages")
    print("stomat_archive.db archive_messages:", c2.fetchone()[0])
    conn2.close()

if __name__ == "__main__":
    check_db()
