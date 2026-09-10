import json
import re
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\direct_replies.json", "r", encoding="utf-8") as f:
    replies = json.load(f)

print(f"Loaded {len(replies)} direct replies.")

def classify_text(text):
    if not text or not text.strip():
        return "Neutral/Other", "empty/media"
    t = text.lower()
    
    # Negative / Frustrated
    neg_patterns = [
        r'\bтуп(ой|ая|ое|ит|ят)\b', r'\bбред\b', r'\bерунд(а|у)\b', r'\bхерн(я|ю)\b',
        r'\bчушь\b', r'\bзамолчи\b', r'\bотстань\b', r'\bне лезь\b', r'\bхватит\b',
        r'\bзаткнись\b', r'\bпочему молч(ишь|ит)\b', r'\bглупость\b',
        r'\bложь\b', r'\bвранье\b', r'\bвыруб(ите|и)\b', r'\bудали(те)?\b',
        r'\bбесит\b', r'\bнадоел\b', r'\bпозорище\b', r'\bошиб(ся|ка)\b',
        r'\bнеправильно\b', r'\bгалимать(я|ю)\b', r'\bс ума сошел\b', r'\bгнать\b',
        r'\bотключи\b', r'\bужас\b', r'\bкошмар\b', r'\bдно\b'
    ]
    
    # Skeptical
    skep_patterns = [
        r'\bты уверен\b', r'\bточно\b\?', r'\bсерьезно\b', r'\bсомневаюсь\b',
        r'\bстранн(о|ый|ая)\b', r'\bгаллюцин', r'\bнейросеть выдумала\b',
        r'\bспорн(о|ый)\b', r'\bну такое\b', r'\bне верю\b', r'\bфейк\b',
        r'\bты сам придумал\b', r'\bкто так делает\b', r'\bоткуда инфа\b',
        r'\bпруф\b', r'\bисточник\b'
    ]
    
    # Positive
    pos_patterns = [
        r'\bспасибо\b', r'\bблагодарю\b', r'\bотлично\b', r'\bсупер\b',
        r'\bкрасавчик\b', r'\bмолодец\b', r'\bтоп\b', r'\bсогласен\b',
        r'\bверно\b', r'\bчетко\b', r'\bкласс\b', r'\bпонял\b', r'\bлайк\b',
        r'\bумный бот\b', r'\bхороший ответ\b', r'\bгодно\b', r'\bплюсую\b',
        r'\bогонь\b', r'\b👍\b', r'\b🤝\b', r'\bспасиб\b'
    ]
    
    # Constructive (Clinical follow-ups, nuance questions, detailed clarification)
    const_patterns = [
        r'\bа если\b', r'\bпочему\b', r'\bкак именно\b', r'\bкакой\b',
        r'\bсколько\b', r'\bкаким образом\b', r'\bа что насчет\b',
        r'\bуточни\b', r'\bподробнее\b', r'\bподскажи\b', r'\bчто думаешь\b',
        r'\bкакие протоколы\b', r'\bа для\b', r'\bно ведь\b', r'\bа в чем\b',
        r'\bподскажите\b', r'\bскажи\b', r'\bа при\b'
    ]
    
    for pat in neg_patterns:
        if re.search(pat, t):
            return "Negative/Frustrated", pat
    for pat in skep_patterns:
        if re.search(pat, t):
            return "Skeptical", pat
    for pat in pos_patterns:
        if re.search(pat, t):
            return "Positive", pat
    for pat in const_patterns:
        if re.search(pat, t):
            return "Constructive", pat
            
    if '?' in t or len(t) > 35:
        return "Constructive", "clinical_query"
        
    return "Neutral/Other", "none"

cat_counts = {}
samples = {
    "Positive": [],
    "Constructive": [],
    "Skeptical": [],
    "Negative/Frustrated": [],
    "Neutral/Other": []
}

for r in replies:
    cat, trigger = classify_text(r["text"])
    cat_counts[cat] = cat_counts.get(cat, 0) + 1
    samples[cat].append((r["msg_id"], r["sender_name"], r["sender_username"], r["text"], trigger))

print("Classification Summary of 236 Direct Replies:")
for cat in ["Positive", "Constructive", "Skeptical", "Negative/Frustrated", "Neutral/Other"]:
    count = cat_counts.get(cat, 0)
    pct = count / len(replies) * 100
    print(f"  {cat}: {count} ({pct:.1f}%)")

print("\n" + "="*50)
print("SAMPLES BY CATEGORY:")
for cat in ["Negative/Frustrated", "Skeptical", "Positive", "Constructive", "Neutral/Other"]:
    print(f"\n--- {cat} ({len(samples[cat])} items) ---")
    for msg_id, name, username, text, trigger in samples[cat][:15]:
        clean_text = text.replace('\n', ' ')[:120] if text else "EMPTY"
        print(f"[{msg_id}] @{username or 'no_user'} ({name}): \"{clean_text}\" (Matched: {trigger})")
