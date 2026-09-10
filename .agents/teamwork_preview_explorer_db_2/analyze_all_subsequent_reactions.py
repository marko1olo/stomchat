import sqlite3
import os
import sys
import json
import re

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# Get all messages where bot sent something (either in bot_sent_messages or messages)
c.execute("""
    SELECT DISTINCT msg_id FROM (
        SELECT msg_id FROM bot_sent_messages
        UNION
        SELECT msg_id FROM messages WHERE sender_id = 7971556097 OR sender_username = 'docendobot'
    )
""")
bot_msg_ids = [r[0] for r in c.fetchall()]
print(f"Total distinct bot message IDs: {len(bot_msg_ids)}")

c.execute("""
    SELECT 
        m.msg_id,
        m.reply_to_msg_id,
        m.sender_id,
        m.sender_name,
        m.sender_username,
        m.text,
        m.date
    FROM messages m
    WHERE m.sender_id != 7971556097
    ORDER BY m.msg_id ASC
""")
all_user_msgs = c.fetchall()
print(f"Total user messages in db: {len(all_user_msgs)}")

msgs_by_id = {m['msg_id']: m for m in all_user_msgs}
replies_to_bot = [m for m in all_user_msgs if m['reply_to_msg_id'] in bot_msg_ids]
print(f"Direct replies to bot msg_ids: {len(replies_to_bot)}")

followups = []
for b_id in bot_msg_ids:
    for offset in range(1, 4):
        target = b_id + offset
        if target in msgs_by_id:
            followups.append((b_id, msgs_by_id[target]))

print(f"Immediate post-bot messages (offset 1-3): {len(followups)}")

def classify_sentiment(text):
    if not text or not text.strip():
        return "Neutral/Other", []
    t = text.lower()
    
    triggers = []
    
    neg_pats = [
        (r'\bтуп(ой|ая|ое|ит|ят)\b', 'тупой/тупит'),
        (r'\bбред\b', 'бред'),
        (r'\bхерн(я|ю|е)\b', 'херня'),
        (r'\bчушь\b', 'чушь'),
        (r'\bзамолчи\b', 'замолчи'),
        (r'\bзаткнись\b', 'заткнись'),
        (r'\bне лезь\b', 'не лезь'),
        (r'\bбесит\b', 'бесит'),
        (r'\bнадоел\b', 'надоел'),
        (r'\bигнорит\b', 'игнорит'),
        (r'\bпочему молч', 'почему молчит'),
        (r'\bвыруб(ите|и)\b', 'выруби'),
        (r'\bудали(те)?\b', 'удали'),
        (r'\bотключи(те)?\b', 'отключи'),
        (r'\bошиб(ся|ка)\b', 'ошибка'),
        (r'\bнеправильно\b', 'неправильно'),
        (r'\bответ неверный\b', 'ответ неверный'),
        (r'\bкривой\b', 'кривой'),
        (r'\bглупость\b', 'глупость'),
        (r'\bложь\b|\bврет\b|\bврань', 'вранье/врет'),
        (r'\bпозорище\b', 'позорище')
    ]
    
    skep_pats = [
        (r'😂|🤣|🤡', 'смех/клоун'),
        (r'\bахах|\bхаха', 'ахах'),
        (r'\bты уверен\b', 'ты уверен?'),
        (r'\bточно\b\?', 'точно?'),
        (r'\bсерьезно\b', 'серьезно?'),
        (r'\bсомневаюсь\b', 'сомневаюсь'),
        (r'\bстранн(о|ый)\b', 'странно'),
        (r'\bгаллюцин', 'галлюцинация'),
        (r'\bвыдумал\b|\bсочинил\b', 'выдумал/сочинил'),
        (r'\bну такое\b', 'ну такое'),
        (r'\bне верю\b', 'не верю'),
        (r'\bшах и мат\b', 'шах и мат'),
        (r'\bрофл|\bтролл', 'рофл/троллинг'),
        (r'\bчто взять с\b', 'что взять с первоклассника')
    ]
    
    pos_pats = [
        (r'\bспасибо\b', 'спасибо'),
        (r'\bблагодарю\b', 'благодарю'),
        (r'\bотлично\b', 'отлично'),
        (r'\bсупер\b', 'супер'),
        (r'\bкрасавчик\b', 'красавчик'),
        (r'\bмолодец\b', 'молодец'),
        (r'\bтоп\b', 'топ'),
        (r'\bсогласен\b', 'согласен'),
        (r'\bчетко\b', 'четко'),
        (r'\bпонял\b|\bпонятно\b', 'понял'),
        (r'\bлайк\b', 'лайк'),
        (r'\bумный бот\b', 'умный бот'),
        (r'\bхорош(ий|о)\b', 'хорошо'),
        (r'👍|🤝|🔥', 'смайлы одобрения')
    ]
    
    for pat, label in neg_pats:
        if re.search(pat, t):
            triggers.append(label)
    if triggers:
        return "Negative/Frustrated", triggers
        
    for pat, label in skep_pats:
        if re.search(pat, t):
            triggers.append(label)
    if triggers:
        return "Skeptical", triggers
        
    for pat, label in pos_pats:
        if re.search(pat, t):
            triggers.append(label)
    if triggers:
        return "Positive", triggers
        
    if '?' in t or any(w in t for w in ['почему', 'как', 'какой', 'сколько', 'если', 'уточни', 'подскажи', 'а что']):
        return "Constructive", ['клинический вопрос/уточнение']
        
    return "Neutral/Other", []

classified_reactions = {
    "direct_replies": {"Positive": [], "Constructive": [], "Skeptical": [], "Negative/Frustrated": [], "Neutral/Other": []},
    "post_bot_followups": {"Positive": [], "Constructive": [], "Skeptical": [], "Negative/Frustrated": [], "Neutral/Other": []}
}

for r in replies_to_bot:
    sent, trigs = classify_sentiment(r['text'])
    classified_reactions["direct_replies"][sent].append({
        "msg_id": r['msg_id'],
        "reply_to_msg_id": r['reply_to_msg_id'],
        "sender_id": r['sender_id'],
        "sender_name": r['sender_name'],
        "sender_username": r['sender_username'],
        "text": r['text'],
        "date": str(r['date']),
        "triggers": trigs
    })

seen_followups = set()
for b_id, f in followups:
    if f['msg_id'] in seen_followups or f['reply_to_msg_id'] in bot_msg_ids:
        continue
    seen_followups.add(f['msg_id'])
    sent, trigs = classify_sentiment(f['text'])
    classified_reactions["post_bot_followups"][sent].append({
        "msg_id": f['msg_id'],
        "bot_msg_id": b_id,
        "sender_id": f['sender_id'],
        "sender_name": f['sender_name'],
        "sender_username": f['sender_username'],
        "text": f['text'],
        "date": str(f['date']),
        "triggers": trigs
    })

print("\n--- DIRECT REPLIES SENTIMENT BREAKDOWN ---")
total_dr = len(replies_to_bot)
for sent, items in classified_reactions["direct_replies"].items():
    print(f"  {sent}: {len(items)} ({len(items)/total_dr*100:.1f}%)")

print("\n--- POST-BOT FOLLOWUPS SENTIMENT BREAKDOWN ---")
total_fb = len(seen_followups)
for sent, items in classified_reactions["post_bot_followups"].items():
    print(f"  {sent}: {len(items)} ({len(items)/total_fb*100:.1f}%)")

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\classified_reactions.json", "w", encoding="utf-8") as f:
    json.dump(classified_reactions, f, ensure_ascii=False, indent=2)

print("Saved classified_reactions.json successfully.")
conn.close()
