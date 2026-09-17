import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

log_path = "c:/Users/danat/Desktop/stomchat/bot.log"

if not os.path.exists(log_path):
    print("bot.log not found!")
    sys.exit(0)

# Search for lines from 2026-09-11 onwards
weekend_lines = []
with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if "2026-09-11" in line or "2026-09-12" in line or "2026-09-13" in line:
            weekend_lines.append(line)

print(f"Total lines in bot.log for 2026-09-11..13: {len(weekend_lines)}")

# Let's count key events:
# - Passive suppressions (cooldown)
# - Errors
# - 503 / cascade fallbacks
# - Sent replies
counts = {
    "sent_replies": 0,
    "passive_suppressions": 0,
    "cascade_503": 0,
    "errors": 0,
    "triage_no": 0,
    "dialogue_stale": 0
}

for line in weekend_lines:
    if "Sent reply" in line or "bot_sent" in line or "sendMessage" in line:
        counts["sent_replies"] += 1
    if "passive" in line.lower() and ("suppress" in line.lower() or "cooldown" in line.lower() or "skip" in line.lower()):
        counts["passive_suppressions"] += 1
    if "503" in line or "fallback" in line.lower() or "cascade" in line.lower():
        counts["cascade_503"] += 1
    if "ERROR" in line or "CRITICAL" in line:
        counts["errors"] += 1

print("Event counts from lines:", counts)

# Sample some suppression and cascade lines
print("\nSample lines with 'passive':")
passives = [l.strip() for l in weekend_lines if "passive" in l.lower()][:10]
for p in passives:
    print(" ", p[:120])

print("\nSample lines with '503' or 'fallback':")
fallbacks = [l.strip() for l in weekend_lines if "503" in l or "fallback" in l.lower()][:10]
for fb in fallbacks:
    print(" ", fb[:120])
