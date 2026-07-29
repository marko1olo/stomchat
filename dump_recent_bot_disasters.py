# -*- coding: utf-8 -*-
import sqlite3
import datetime

import os

def main():
    db_path = r"C:\Users\danat\Desktop\stomchat\stomat_bot.db"
    if not os.path.exists(db_path):
        return
    db = sqlite3.connect(db_path)
    cur = db.cursor()

    print("=== ПОСЛЕДНИЕ 20 СООБЩЕНИЙ В ОСНОВНОЙ ГРУППЕ ===")
    cur.execute("SELECT msg_id, sender_id, sender_name, text, date FROM messages ORDER BY id DESC LIMIT 20")
    for r in cur.fetchall():
        print(f"[{r[4]}] msg_id={r[0]} sender={r[2]} ({r[1]}): {str(r[3])[:100].replace('\n', ' ')}")

    print("\n=== ПОСЛЕДНИЕ СООБЩЕНИЯ БОТА (ИЛИ ОТВЕТЫ С ТРИГГЕРОМ) ===")
    cur.execute("SELECT msg_id, chat_id FROM bot_sent_messages ORDER BY id DESC LIMIT 10")
    bot_sent = cur.fetchall()
    print("Bot sent IDs:", bot_sent)

    print("\n=== ПРОВЕРКА ПОСЛЕДНИХ ЛОГОВ БОТА ===")
    log_path = r"C:\Users\danat\Desktop\stomchat\bot.log"
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            print("Last 40 lines of bot.log:")
            for l in lines[-40:]:
                print(l.strip())

if __name__ == "__main__":
    main()
