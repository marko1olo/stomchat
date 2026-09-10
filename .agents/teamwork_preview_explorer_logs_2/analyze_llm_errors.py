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

re_banned = re.compile(r"Model ([\w.-]+) is temporarily banned due to 503/504 for another (\d+)s", re.IGNORECASE)
re_exhaust = re.compile(r"gemini cascade exhausted: (.*)", re.IGNORECASE)
re_error = re.compile(r"(?:Gemini|Groq|OpenAI) error.*?(\d{3}|ResourceExhausted|TimeoutError|rate limit|quota)", re.IGNORECASE)
re_http_err = re.compile(r"HTTP Request: POST \S+ \"HTTP/1\.1 (\d{3})", re.IGNORECASE)

banned_models = Counter()
http_codes = Counter()
exhaust_reasons = Counter()

for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f, 1):
            m = re_banned.search(line)
            if m:
                banned_models[m.group(1)] += 1
            m_code = re_http_err.search(line)
            if m_code:
                http_codes[m_code.group(1)] += 1
            m_ex = re_exhaust.search(line)
            if m_ex:
                exhaust_reasons[m_ex.group(1)[:60]] += 1

print("=== TEMPORARILY BANNED MODELS (503/504) ===")
for mod, cnt in banned_models.most_common():
    print(f"  {mod:<25}: {cnt} times skipped")

print("\n=== HTTP RESPONSE CODES FROM LLM APIS ===")
for code, cnt in http_codes.most_common():
    print(f"  HTTP {code}: {cnt} times")

print("\n=== CASCADE EXHAUSTION REASONS ===")
for r, cnt in exhaust_reasons.most_common():
    print(f"  {r}: {cnt} times")
