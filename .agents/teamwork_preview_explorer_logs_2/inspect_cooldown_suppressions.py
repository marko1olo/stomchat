import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
import sqlite3
from collections import Counter

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

re_ts = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?)")
re_pass_cooldown = re.compile(r"Passive text trigger suppressed: passive cooldown,\s*(\d+)\s*min left", re.IGNORECASE)
re_retry_backoff = re.compile(r"Passive text trigger suppressed: retry backoff after failed attempt,\s*(\d+)\s*min left", re.IGNORECASE)
re_msg_in = re.compile(r"MSG_(\d+)\s+от\s+(.*?)\s+(?:\(@(.*?)\))?$", re.IGNORECASE)
re_preview = re.compile(r"message_text_preview msg_id=(\d+)\s+text=(.*)", re.IGNORECASE)

cd_mins = []
backoff_mins = []

# Collect events with preceding message text if available
suppressed_msgs = []

for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
        for idx, line in enumerate(lines):
            m_cd = re_pass_cooldown.search(line)
            m_bo = re_retry_backoff.search(line)
            if m_cd or m_bo:
                kind = "cooldown" if m_cd else "retry_backoff"
                mins = int(m_cd.group(1)) if m_cd else int(m_bo.group(1))
                if kind == "cooldown":
                    cd_mins.append(mins)
                else:
                    backoff_mins.append(mins)
                
                # Check previous lines for msg_id and preview
                msg_id = None
                text_prev = None
                sender = None
                for j in range(max(0, idx - 10), idx):
                    m_prev = re_preview.search(lines[j])
                    if m_prev:
                        msg_id = int(m_prev.group(1))
                        text_prev = m_prev.group(2).strip()
                    m_min = re_msg_in.search(lines[j])
                    if m_min:
                        sender = m_min.group(2).strip()

                if msg_id and text_prev:
                    suppressed_msgs.append({
                        "file": fname,
                        "line": idx + 1,
                        "kind": kind,
                        "mins_left": mins,
                        "msg_id": msg_id,
                        "sender": sender,
                        "text": text_prev
                    })

print(f"Total cooldown events: {len(cd_mins)}, backoff events: {len(backoff_mins)}")
print(f"Suppressed messages identified from log context: {len(suppressed_msgs)}")

# Distribution of mins left
print("\nCooldown minutes left distribution (sample percentiles):")
cd_mins.sort()
if cd_mins:
    print(f"  Min: {cd_mins[0]}, p25: {cd_mins[len(cd_mins)//4]}, p50: {cd_mins[len(cd_mins)//2]}, p75: {cd_mins[len(cd_mins)*3//4]}, Max: {cd_mins[-1]}")

backoff_mins.sort()
if backoff_mins:
    print("\nRetry backoff minutes left distribution:")
    print(f"  Min: {backoff_mins[0]}, p25: {backoff_mins[len(backoff_mins)//4]}, p50: {backoff_mins[len(backoff_mins)//2]}, p75: {backoff_mins[len(backoff_mins)*3//4]}, Max: {backoff_mins[-1]}")

# Filter suppressed messages that had strong clinical content
clinical_keywords = ["зуб", "кана", "корон", "имплант", "циркони", "слепок", "уступ", "пломб", "анестези", "десн", "кт", "рентген", "резекц", "синус", "протокол"]
clinical_suppressed = [m for m in suppressed_msgs if any(k in m["text"].lower() for k in clinical_keywords) and len(m["text"]) > 25]

print(f"\nClinical messages suppressed by cooldown/backoff: {len(clinical_suppressed)}")
print("\n=== TOP CLINICAL MESSAGES SUPPRESSED BY COOLDOWN / RETRY BACKOFF ===")
for idx, m in enumerate(clinical_suppressed[:25], 1):
    print(f"#{idx:02d} [{m['kind']}, {m['mins_left']}m left] Msg {m['msg_id']} ({m['sender']}):")
    print(f"    \"{m['text']}\" (in {m['file']}:{m['line']})")
