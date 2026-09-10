import sqlite3
import os
import sys
import re
import json
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
arch_db_path = os.path.join(WORKSPACE, "stomat_archive.db")

# Specialty lexicons compiled regexes
lexicons = {
    "Endodontics": [
        r'\bэндо', r'\bканал', r'\bпульпит', r'\bпериодонтит', r'\bапекс', r'\bапекслокатор',
        r'\bгуттаперч', r'\bгипохлорит', r'\bmta\b|\bмта\b', r'\bфайл', r'\bпротейпер',
        r'\bреципрок', r'\bраспломбиров', r'\bсилер\b', r'\bэндомотор', r'\bобтурац'
    ],
    "Implantology": [
        r'\bимплант', r'\bабатмент', r'\bостеоинтеграц', r'\bсинус[- ]?лифт', r'\bмультиюнит',
        r'\bвинтов(ая|ой|ую)\b', r'\bтибаз|\bти-баз', r'\bторк\b', r'\bшестигранник',
        r'\bосстем\b|\bosstem\b', r'\bдентиум\b|\bdentium\b', r'\bштрауман\b|\bstraumann\b',
        r'\bнобель\b|\bnobel\b', r'\ball[- ]?on[- ]?[46]\b'
    ],
    "Surgery": [
        r'\bхирург', r'\bудалени', r'\bэкстракци', r'\bлунк', r'\bальвеолит', r'\bшовный\b|\bшвы\b',
        r'\bкостная пластика', r'\bаугментац', r'\bграфтинг', r'\bbio[- ]?oss\b|\bбиосс\b',
        r'\bдистопи', r'\bретинирован', r'\bвосьмерк', r'\bзуб(а|ы)? мудрости', r'\bпериостит', r'\bабсцесс'
    ],
    "Prosthetics": [
        r'\bортопед', r'\bкоронк', r'\bмостовидн|\bмост(а|ы)?\b', r'\bвинир', r'\bуступ',
        r'\bвертипреп\b|\bvertiprep\b', r'\bферрул', r'\bслепок', r'\bоттиск', r'\bсканер|\bсканирован',
        r'\bcad[ /]?cam\b', r'\bциркон', r'\be\.?max\b|\bемакс\b', r'\bбюгель', r'\bбисакрил',
        r'\bокклюзи', r'\bартикулятор', r'\bлицевая дуга', r'\bприкус', r'\bцентральн(ое|ая) соотношени|\bцс\b|\bцо\b',
        r'\bкопирк', r'\bшимсток'
    ],
    "Orthotropics/Aligners": [
        r'\bортодонт', r'\bэлайнер', r'\bбрекет', r'\bдуг(а|и)\b', r'\bдистализац',
        r'\bсплинт', r'\bвнчс\b', r'\bтрг\b', r'\bкапп(а|ы)\b', r'\bсуставн', r'\bортотропи'
    ],
    "General/Therapy": [
        r'\bтерапевт', r'\bкариес', r'\bкомпозит', r'\bпломб', r'\bадгезив|\bадгезия',
        r'\bбонд(инг)?\b', r'\bкоффердам', r'\bкламп', r'\bматриц', r'\bклин(ья)?\b',
        r'\bполировк', r'\bконтактн(ый|ого) пункт', r'\bклиновидн', r'\bреставрац'
    ],
    "Pediatric": [
        r'\bдетск', r'\bребен(ок|ка|ку)', r'\bмолочн(ый|ые|ого|ых) зуб', r'\bпульпотоми',
        r'\bсменный прикус', r'\bсеребрени'
    ],
    "Admin/Equipment": [
        r'\bоборудован', r'\bустановк', r'\bнаконечник', r'\bмикроскоп', r'\bбинокуляр',
        r'\bавтоклав', r'\bстерилизац', r'\bсанпин', r'\bрентген', r'\bклкт\b|\bcbct\b|\bкт\b',
        r'\bвизиограф', r'\bассистент', r'\bклиник(а|и|е)', r'\bпроцент(ы)? от кассы',
        r'\bзарплат', r'\bпрайс', r'\bналог', r'\bюрист'
    ]
}

compiled_lex = {cat: [re.compile(p, re.IGNORECASE) for p in pats] for cat, pats in lexicons.items()}

def classify_message(text):
    if not text:
        return "Non-Clinical / Chit-chat"
    scores = {}
    for cat, pats in compiled_lex.items():
        score = sum(1 for p in pats if p.search(text))
        if score > 0:
            scores[cat] = score
    if not scores:
        return "Non-Clinical / Chit-chat"
    # Return highest scoring category
    return max(scores.items(), key=lambda x: x[1])[0]

print("Processing Active Group Messages (stomat_bot.db: messages)...")
conn_bot = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
c_bot = conn_bot.cursor()

# Get bot message IDs
c_bot.execute("SELECT DISTINCT msg_id FROM bot_sent_messages UNION SELECT msg_id FROM messages WHERE sender_id = 7971556097")
bot_msg_ids = set(r[0] for r in c_bot.fetchall())

# Fetch all active messages
c_bot.execute("SELECT msg_id, reply_to_msg_id, sender_id, text FROM messages")
active_msgs = c_bot.fetchall()
print(f"Total active messages loaded: {len(active_msgs):,}")

# Build reply counts map: target_msg_id -> list of reply_msg_ids
replies_map = defaultdict(list)
for msg_id, reply_to, sender_id, text in active_msgs:
    if reply_to:
        replies_map[reply_to].append((msg_id, sender_id))

active_stats = defaultdict(lambda: {
    "count": 0,
    "has_reply_count": 0,
    "total_replies": 0,
    "bot_replied_count": 0
})

for msg_id, reply_to, sender_id, text in active_msgs:
    if sender_id == 7971556097: # Skip bot's own messages
        continue
    cat = classify_message(text)
    active_stats[cat]["count"] += 1
    
    # Check if this message was replied to
    if msg_id in replies_map:
        active_stats[cat]["has_reply_count"] += 1
        active_stats[cat]["total_replies"] += len(replies_map[msg_id])
        # Check if bot replied to it
        if any(r[1] == 7971556097 or r[0] in bot_msg_ids for r in replies_map[msg_id]):
            active_stats[cat]["bot_replied_count"] += 1

conn_bot.close()

print("\nProcessing Archive Messages (stomat_archive.db: archive_messages)...")
conn_arch = sqlite3.connect(f"file:{arch_db_path}?mode=ro", uri=True)
c_arch = conn_arch.cursor()

c_arch.execute("SELECT msg_id, reply_to_msg_id, sender_id, text FROM archive_messages")
arch_msgs = c_arch.fetchall()
print(f"Total archive messages loaded: {len(arch_msgs):,}")

arch_replies_map = defaultdict(list)
for msg_id, reply_to, sender_id, text in arch_msgs:
    if reply_to:
        arch_replies_map[reply_to].append(msg_id)

archive_stats = defaultdict(lambda: {
    "count": 0,
    "has_reply_count": 0,
    "total_replies": 0
})

for msg_id, reply_to, sender_id, text in arch_msgs:
    if sender_id == 7971556097:
        continue
    cat = classify_message(text)
    archive_stats[cat]["count"] += 1
    if msg_id in arch_replies_map:
        archive_stats[cat]["has_reply_count"] += 1
        archive_stats[cat]["total_replies"] += len(arch_replies_map[msg_id])

conn_arch.close()

# Save results
out_data = {
    "active": {k: dict(v) for k, v in active_stats.items()},
    "archive": {k: dict(v) for k, v in archive_stats.items()}
}

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\specialty_engagement.json", "w", encoding="utf-8") as f:
    json.dump(out_data, f, ensure_ascii=False, indent=2)

print("\n" + "="*80)
print("ACTIVE MESSAGES SPECIALTY BREAKDOWN & ENGAGEMENT (42,333 msgs)")
print("="*80)
total_active_user_msgs = sum(s["count"] for s in active_stats.values())
print(f"{'Category':<24} | {'Msgs':<8} | {'Share %':<8} | {'Replied %':<10} | {'Avg Replies':<12} | {'Bot Replied':<12}")
print("-" * 85)
for cat, s in sorted(active_stats.items(), key=lambda x: x[1]["count"], reverse=True):
    cnt = s["count"]
    pct = cnt / total_active_user_msgs * 100
    rep_pct = s["has_reply_count"] / cnt * 100 if cnt > 0 else 0
    avg_rep = s["total_replies"] / cnt if cnt > 0 else 0
    bot_rep = s["bot_replied_count"]
    print(f"{cat:<24} | {cnt:<8,} | {pct:>6.1f}%  | {rep_pct:>8.1f}%  | {avg_rep:>10.2f}  | {bot_rep:>10}")

print("\n" + "="*80)
print("ARCHIVE MESSAGES SPECIALTY BREAKDOWN & ENGAGEMENT (117,847 msgs)")
print("="*80)
total_arch_user_msgs = sum(s["count"] for s in archive_stats.values())
print(f"{'Category':<24} | {'Msgs':<8} | {'Share %':<8} | {'Replied %':<10} | {'Avg Replies':<12}")
print("-" * 75)
for cat, s in sorted(archive_stats.items(), key=lambda x: x[1]["count"], reverse=True):
    cnt = s["count"]
    pct = cnt / total_arch_user_msgs * 100
    rep_pct = s["has_reply_count"] / cnt * 100 if cnt > 0 else 0
    avg_rep = s["total_replies"] / cnt if cnt > 0 else 0
    print(f"{cat:<24} | {cnt:<8,} | {pct:>6.1f}%  | {rep_pct:>8.1f}%  | {avg_rep:>10.2f}")
