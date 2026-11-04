import os
import sys
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import sqlite3
import re

def test_sieve(limit=5000):
    con = sqlite3.connect('file:' + os.path.abspath(r'c:\Users\danat\Desktop\stomchat\stomat_bot.db') + '?mode=ro', uri=True)
    cur = con.cursor()

    # Query human messages sorted by date or length
    cur.execute("""
        SELECT msg_id, sender_name, text, date 
        FROM messages 
        WHERE length(text) >= 200
        ORDER BY msg_id DESC
        LIMIT ?
    """, (limit,))

    raw_rows = cur.fetchall()
    rows = [r for r in raw_rows if not any(b in (r[1] or "").lower() for b in ["бот", "bot", "docendo", "endochat", "assistant"])]
    print(f"Loaded {len(rows)} real human messages with len >= 200 (out of {len(raw_rows)})")

    proc_keywords = [
        "протокол", "препарир", "ирригац", "обтурац", "гипохлорит", "naocl", "эдта", "edta",
        "травлен", "силанизац", "адгезив", "бондинг", "коффердам", "рабердам",
        "гуттаперч", "силер", "церасил", "ceraseal", "мта", "mta", "ступеньк", "байпас",
        "реставрац", "композит", "цемент", "дезобтурац", "распломбиров",
        "оттиск", "сканирован", "bopt", "визир", "ультразвук", "эндочак",
        "штифт", "культ", "винир", "коронк", "апекс", "пломбир"
    ]

    imperative_or_step_markers = [
        r'\b1[\.\)]\s+', r'\b2[\.\)]\s+', r'\b3[\.\)]\s+',
        r'\bэтап\s*[1-9]', r'\bшаг\s*[1-9]',
        r'\bсначала\b', r'\bзатем\b', r'\bдалее\b', r'\bпосле этого\b',
        r'\bпромыва', r'\bвносим', r'\bсушим', r'\bраздува', r'\bсветим',
        r'\bэкспозиц', r'\bсекунд\b', r'\bминут\b', r'\bпесочим\b',
        r'\bпротравк', r'\bполимер'
    ]

    question_markers = [
        "подскажите", "кто подскажет", "как думаете", "что посоветуете",
        "кто как делает", "в чем может быть причина", "что делать",
        "правильно ли", "нормально ли", "кто сталкивался"
    ]

    passed = []
    rejected_reasons = {"short": 0, "no_proc": 0, "no_steps": 0, "is_question": 0}

    for mid, name, text, dt in rows:
        t_low = text.lower().strip()

        # 1. Check if predominantly a question
        # If text ends with '?' and has few lines or starts with question marker
        is_q = False
        if any(qm in t_low[:60] for qm in question_markers):
            is_q = True
        elif t_low.endswith("?") and "?" not in t_low[:-1] and len(t_low.splitlines()) <= 3:
            is_q = True
        elif t_low.count("?") >= 2 and not any(re.search(pat, t_low) for pat in [r'\b1[\.\)]\s+', r'\bэтап', r'\bшаг']):
            is_q = True

        if is_q:
            rejected_reasons["is_question"] += 1
            continue

        # 2. Procedural keywords
        has_proc = any(kw in t_low for kw in proc_keywords)
        if not has_proc:
            rejected_reasons["no_proc"] += 1
            continue

        # 3. Steps or procedural actions
        has_step = any(re.search(pat, t_low) for pat in imperative_or_step_markers)
        if not has_step:
            rejected_reasons["no_steps"] += 1
            continue

        passed.append((mid, name, text, dt))

    print(f"Passed sieve filter: {len(passed)} candidates")
    print(f"Rejections breakdown: {rejected_reasons}")

    print("\n--- TOP 10 CANDIDATE PROTOCOLS FOUND ---")
    for mid, name, text, dt in passed[:10]:
        print("\n==========================================")
        print(f"MSG #{mid} | Автор: {name} | Дата: {dt} | Символов: {len(text)}")
        print(text[:400] + ("..." if len(text) > 400 else ""))

    con.close()
    return passed

if __name__ == '__main__':
    test_sieve(5000)
