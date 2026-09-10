import sqlite3
import re
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding='utf-8')

def main():
    conn = sqlite3.connect("stomat_bot.db")
    cur = conn.cursor()
    
    # 1. Inspect cut-off messages (5 < count_since <= 25)
    print("=== DIRECT REPLIES CUT OFF BY count_since > 5 ===")
    cur.execute("SELECT msg_id FROM bot_sent_messages")
    bot_msg_ids = {r[0] for r in cur.fetchall()}
    
    cur.execute("""
        SELECT m.msg_id, m.reply_to_msg_id, m.sender_name, m.text, m.date,
               b.text as bot_text, b.date as bot_date
        FROM messages m
        JOIN messages b ON m.reply_to_msg_id = b.msg_id
        WHERE m.reply_to_msg_id IN (SELECT msg_id FROM bot_sent_messages)
        ORDER BY m.msg_id ASC
    """)
    rows = cur.fetchall()
    print(f"Total direct replies to bot messages: {len(rows)}")

    cutoff_examples = []
    for r in rows:
        user_msg_id, bot_msg_id, sender, text, user_dt_str, bot_text, bot_dt_str = r
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < ? AND msg_id < 90000000",
            (bot_msg_id, user_msg_id)
        )
        cnt = cur.fetchone()[0]
        
        # calculate time delta
        time_sec = None
        try:
            udt = datetime.fromisoformat(user_dt_str)
            bdt = datetime.fromisoformat(bot_dt_str)
            time_sec = (udt - bdt).total_seconds()
        except Exception:
            pass

        if cnt > 5:
            cutoff_examples.append({
                "user_msg_id": user_msg_id,
                "bot_msg_id": bot_msg_id,
                "sender": sender,
                "text": text,
                "count_since": cnt,
                "time_sec": time_sec,
                "bot_text": bot_text
            })

    print(f"Count of direct replies with count_since > 5: {len(cutoff_examples)}")
    for ex in cutoff_examples:
        mins_str = f"{ex['time_sec']/60:.1f} min" if ex['time_sec'] is not None else "N/A"
        clean_user_txt = (ex['text'] or "").replace("\n", " ")
        clean_bot_txt = (ex['bot_text'] or "").replace("\n", " ")
        print(f"\n[msg #{ex['user_msg_id']} -> bot #{ex['bot_msg_id']}] by '{ex['sender']}':")
        print(f"  Passed msgs: {ex['count_since']} msgs | Time elapsed: {mins_str}")
        print(f"  User reply: \"{clean_user_txt[:120]}\"")
        print(f"  Bot message replied to: \"{clean_bot_txt[:100]}\"")

    # 2. Analyze user feedback / sentiment in DB
    print("\n=== USER SENTIMENT & REACTIONS TO BOT ===")
    cur.execute("""
        SELECT msg_id, sender_name, text, date 
        FROM messages 
        WHERE text LIKE '%бот%' OR text LIKE '%bot%' OR text LIKE '%stomchat%'
        ORDER BY date DESC 
        LIMIT 150
    """)
    bot_mention_rows = cur.fetchall()
    
    # Categorize mentions
    complaints = []
    praise = []
    confusion = []
    commands = []
    
    complaint_keywords = ["тупой", "заткнись", "умолк", "молчи", "надоел", "бред", "ерунда", "спам", "назойлив", "выключите", "уберите", "чушь", "глупый", "ошиб"]
    praise_keywords = ["молодец", "хорош", "спасибо", "круто", "огонь", "умный", "полезн", "красавчик", "супер"]
    confusion_keywords = ["почему молчит", "не отвечает", "не ответил", "где бот", "завис", "уснул", "спит", "молчит"]

    for r in bot_mention_rows:
        mid, sname, txt, dt = r
        if not txt:
            continue
        tl = txt.lower()
        if any(k in tl for k in complaint_keywords):
            complaints.append((mid, sname, txt, dt))
        elif any(k in tl for k in praise_keywords):
            praise.append((mid, sname, txt, dt))
        elif any(k in tl for k in confusion_keywords):
            confusion.append((mid, sname, txt, dt))

    print(f"\nComplaints / negative sentiment found: {len(complaints)}")
    for c in complaints[:10]:
        print(f"  [#{c[0]} {c[3]}] {c[1]}: \"{c[2].replace(chr(10), ' ')[:100]}\"")

    print(f"\nConfusion / Bot silence complaints: {len(confusion)}")
    for cf in confusion[:10]:
        print(f"  [#{cf[0]} {cf[3]}] {cf[1]}: \"{cf[2].replace(chr(10), ' ')[:100]}\"")

    print(f"\nPraise / positive sentiment found: {len(praise)}")
    for p in praise[:10]:
        print(f"  [#{p[0]} {p[3]}] {p[1]}: \"{p[2].replace(chr(10), ' ')[:100]}\"")

    conn.close()

if __name__ == "__main__":
    main()
