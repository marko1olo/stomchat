import json
import re
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\mentions.json", "r", encoding="utf-8") as f:
    mentions = json.load(f)

print(f"Total mentions loaded: {len(mentions)}")

# Let's filter mentions specifically about this bot or AI bot in the chat
# Many messages might just have "бот" as part of words like "работа", "заботит", "суббота" etc.!
# Regex word boundary \bбот[а-я]*\b or @docendobot

bot_pattern = re.compile(r'(@docendobot|\bботу?\b|\bбота\b|\bботом\b|\bботе\b|\bботы\b|\bнейросет|\bии\b|\bчатгпт\b|\bchatgpt\b|\bgpt\b)', re.IGNORECASE)

relevant_mentions = []
for m in mentions:
    text = m.get("text") or ""
    if bot_pattern.search(text):
        # Exclude false positives like "работа", "забота", "суббота" (which regex with \b should already handle)
        relevant_mentions.append(m)

print(f"Filtered true bot/AI mentions: {len(relevant_mentions)}")

# Let's categorize these into:
# 1. Negative / Frustrated (complaints about silence, annoyance, bad advice, hallucinations)
# 2. Skeptical / Mockery (making fun of bot, jokes about AI, testing bot with silly prompts)
# 3. Positive / Praise (appreciation, "умный бот", "полезно", "круто")
# 4. Constructive / Interaction (prompting bot, asking clinical questions, discussing bot features)
# 5. General meta-discussion about AI in dentistry

categories = {
    "Negative/Frustrated": [],
    "Skeptical/Mockery": [],
    "Positive/Praise": [],
    "Constructive/Prompting": [],
    "Meta/General_AI": []
}

for m in relevant_mentions:
    t = (m.get("text") or "").lower()
    msg_id = m.get("msg_id")
    user = m.get("sender_username") or "no_user"
    name = m.get("sender_name") or "unknown"
    date = m.get("date")
    raw = m.get("text") or ""

    # Check Negative / Frustrated
    if any(re.search(p, t) for p in [
        r'\bтуп(ой|ит|ят)\b', r'\bбред\b', r'\bхерн', r'\bчушь\b', r'\bзамолчи\b', 
        r'\bзаткнись\b', r'\bне лезь\b', r'\bбесит\b', r'\bнадоел\b', r'\bвыруб(ите|и)\b',
        r'\bпочему молч', r'\bудали(те)? бота\b', r'\bотключ(ите|и) бота\b', r'\bзаеб',
        r'\bошиб(ся|ка)\b', r'\bврет\b', r'\bврань', r'\bкривой\b', r'\bглуп(о|ый)\b'
    ]):
        categories["Negative/Frustrated"].append((msg_id, user, name, date, raw))
    
    # Check Skeptical / Mockery
    elif any(re.search(p, t) for p in [
        r'😂', r'🤡', r'🤣', r'\bржака\b', r'\bугар\b', r'\bприкол\b', r'\bсмешн', 
        r'\bгаллюцин', r'\bвыдумал\b', r'\bсочинил\b', r'\bтролл', r'\bрофл', 
        r'\bсказочник\b', r'\bпьяный\b', r'\bкурит\b', r'\bчудит\b', r'\bдичь\b'
    ]):
        categories["Skeptical/Mockery"].append((msg_id, user, name, date, raw))
        
    # Check Positive / Praise
    elif any(re.search(p, t) for p in [
        r'\bспасибо\b', r'\bмолодец\b', r'\bкрасавчик\b', r'\bтоп\b', r'\bкруто\b',
        r'\bогонь\b', r'\bумный бот\b', r'\bхорош\b', r'\bотлично\b', r'\bсупер\b',
        r'\bлайк\b', r'\bполезн(о|ый)\b', r'\bгений\b', r'\bбраво\b'
    ]):
        categories["Positive/Praise"].append((msg_id, user, name, date, raw))
        
    # Check Constructive / Direct Prompting
    elif any(re.search(p, t) for p in [
        r'@docendobot', r'\bбот,?\s*(скажи|ответь|подскажи|как|что|почему|какой|посоветуй|напиши)\b',
        r'\b/ask\b', r'\b/quiz\b', r'\b/summary\b'
    ]):
        categories["Constructive/Prompting"].append((msg_id, user, name, date, raw))
        
    else:
        categories["Meta/General_AI"].append((msg_id, user, name, date, raw))

print("\nDISTRIBUTION OF BOT/AI MENTIONS:")
for cat, items in categories.items():
    print(f"  {cat}: {len(items)} ({len(items)/len(relevant_mentions)*100:.1f}%)")

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\mentions_classified.json", "w", encoding="utf-8") as f:
    json.dump({cat: [{"msg_id": i[0], "user": i[1], "name": i[2], "date": i[3], "text": i[4]} for i in items] for cat, items in categories.items()}, f, ensure_ascii=False, indent=2)

print("\n--- SAMPLE NEGATIVE / FRUSTRATED (first 10) ---")
for i in categories["Negative/Frustrated"][:10]:
    print(f"[{i[0]}] {i[3]} @{i[1]} ({i[2]}): {i[4].replace(chr(10), ' ')[:140]}")

print("\n--- SAMPLE SKEPTICAL / MOCKERY (first 10) ---")
for i in categories["Skeptical/Mockery"][:10]:
    print(f"[{i[0]}] {i[3]} @{i[1]} ({i[2]}): {i[4].replace(chr(10), ' ')[:140]}")

print("\n--- SAMPLE POSITIVE / PRAISE (first 10) ---")
for i in categories["Positive/Praise"][:10]:
    print(f"[{i[0]}] {i[3]} @{i[1]} ({i[2]}): {i[4].replace(chr(10), ' ')[:140]}")

print("\n--- SAMPLE CONSTRUCTIVE / PROMPTING (first 10) ---")
for i in categories["Constructive/Prompting"][:10]:
    print(f"[{i[0]}] {i[3]} @{i[1]} ({i[2]}): {i[4].replace(chr(10), ' ')[:140]}")
