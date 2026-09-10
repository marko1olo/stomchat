import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
from collections import Counter

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

re_triage_false = re.compile(r"Llama Triage decision:\s*should_reply=False\s*\(confidence=([0-9.]+)\)\.\s*Reason:\s*(.*)", re.IGNORECASE)

reasons = []
confidences = []

for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f, 1):
            m = re_triage_false.search(line)
            if m:
                conf = float(m.group(1))
                reason = m.group(2).strip()
                reasons.append({"file": fname, "line": idx, "conf": conf, "reason": reason})
                confidences.append(conf)

print(f"Total Llama triage False decisions: {len(reasons)}")

# Categorize reasons
cat_reasons = Counter()
for r in reasons:
    low = r["reason"].lower()
    if "живой" in low or "переписыва" in low or "уже обсужда" in low or "коллег" in low or "ответил" in low or "участник" in low:
        cat_reasons["already_discussed_by_humans"] += 1
    elif "флуд" in low or "юмор" in low or "шутк" in low or "быт" in low or "неклинич" in low or "не относит" in low:
        cat_reasons["chitchat_flood_humor_nonclinical"] += 1
    elif "коротк" in low or "реплик" in low or "одностроч" in low or "мысли вслух" in low:
        cat_reasons["short_remark_thought_out_loud"] += 1
    elif "нет вопроса" in low or "вопрос не задан" in low or "не просит" in low or "не требует" in low:
        cat_reasons["no_question_asked"] += 1
    else:
        cat_reasons["other"] += 1

print("\n=== TRIAGE REJECTION CATEGORIES ===")
for cat, cnt in cat_reasons.most_common():
    print(f"{cnt:4d} ({cnt/len(reasons)*100:5.1f}%) : {cat}")

print("\n=== CONFIDENCE DISTRIBUTION ===")
conf_buckets = Counter()
for c in confidences:
    b = round(c, 2)
    conf_buckets[b] += 1
for b, cnt in sorted(conf_buckets.items(), reverse=True):
    print(f"  Confidence {b:.2f}: {cnt} times")

print("\n=== SAMPLES OF 'already_discussed_by_humans' WHERE CLINICAL QUESTION EXISTED ===")
sample_count = 0
for r in reasons:
    low = r["reason"].lower()
    if any(w in low for w in ["клинич", "вопрос", "кейс", "лечени", "зуб", "протокол"]):
        print(f"[{r['file']}:{r['line']} | conf={r['conf']}] {r['reason']}")
        sample_count += 1
        if sample_count >= 15:
            break
