import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
from collections import Counter
import json

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

re_val_rej = re.compile(r"Response quality validator REJECTED draft:\s*(.*?)\.\s*Suppressing reply", re.IGNORECASE)
re_media_val_rej = re.compile(r"Media response quality validator REJECTED draft:\s*(.*?)\.\s*Suppressing reply", re.IGNORECASE)
re_val_unavail = re.compile(r"Response validator unavailable \((.*?)\)\.\s*Uninvited reply", re.IGNORECASE)

val_rejections = []

for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line_num, line in enumerate(f, 1):
            m = re_val_rej.search(line)
            if m:
                val_rejections.append({"file": fname, "line": line_num, "kind": "text", "reason": m.group(1)})
            m2 = re_media_val_rej.search(line)
            if m2:
                val_rejections.append({"file": fname, "line": line_num, "kind": "media", "reason": m2.group(1)})
            m3 = re_val_unavail.search(line)
            if m3:
                val_rejections.append({"file": fname, "line": line_num, "kind": "unavailable", "reason": m3.group(1)})

print(f"Total validator rejections: {len(val_rejections)}")

# Categorize reasons
categories = Counter()
for r in val_rejections:
    reas = r["reason"].lower()
    if "emoji" in reas or "эмодз" in reas or "смайл" in reas:
        categories["emoji_or_smileys"] += 1
    elif "псевдонауч" in reas or "жаргон" in reas or "поверхностн" in reas:
        categories["pseudoscience_or_jargon"] += 1
    elif "выдуман" in reas or "цифр" in reas or "дозировк" in reas or "протокол" in reas:
        categories["hallucinated_protocol_or_figures"] += 1
    elif "однострочн" in reas or "вброс" in reas or "коротк" in reas or "длин" in reas:
        categories["one_liner_or_length"] += 1
    elif "токсич" in reas or "высокомер" in reas or "сарказм" in reas or "тон" in reas:
        categories["arrogant_or_toxic_tone"] += 1
    elif "не относится" in reas or "оффтоп" in reas or "уводит" in reas:
        categories["off_topic"] += 1
    elif "exhausted" in reas or "cascade" in reas or "ни одна модель" in reas:
        categories["validator_cascade_exhausted"] += 1
    elif "timeout" in reas:
        categories["validator_timeout"] += 1
    else:
        categories["other_clinical_reason"] += 1

print("\n=== VALIDATOR REJECTION CATEGORIES ===")
for cat, cnt in categories.most_common():
    print(f"{cnt:4d} ({cnt/len(val_rejections)*100:5.1f}%) : {cat}")

print("\n=== SAMPLE DETAILED REASONS (first 20) ===")
for idx, r in enumerate(val_rejections[:20], 1):
    print(f"#{idx:02d} [{r['kind']}] {r['file']}:{r['line']}")
    print(f"    {r['reason']}")

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_logs_2\validator_rejections.json", "w", encoding="utf-8") as f:
    json.dump(val_rejections, f, ensure_ascii=False, indent=2)
print("\nWrote validator_rejections.json")
