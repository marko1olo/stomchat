import re
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

log_path = "c:/Users/danat/Desktop/stomchat/bot.log"

weekend_lines = []
with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if "2026-09-11" in line or "2026-09-12" in line or "2026-09-13" in line:
            weekend_lines.append(line)

# Let's search for trigger lines, suppression lines, fallback lines
suppression_types = {}
for line in weekend_lines:
    if "suppressed" in line.lower():
        # extract reason
        m = re.search(r'suppressed:\s*([^,\n]+)', line)
        reason = m.group(1).strip() if m else "other"
        suppression_types[reason] = suppression_types.get(reason, 0) + 1

print("Suppression Types:")
for k, v in sorted(suppression_types.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")

# Fallback / 503 counts
fallbacks = 0
for line in weekend_lines:
    if "503" in line and "Gemini server overloaded" in line:
        fallbacks += 1

print(f"\nGemini 503 overloaded fallbacks: {fallbacks}")

# Total errors (ERROR or CRITICAL level in log)
error_lines = [l for l in weekend_lines if " - ERROR - " in l or " - CRITICAL - " in l]
print(f"Total error lines in weekend log: {len(error_lines)}")

# Let's find how each of the 20 bot replies was generated:
# Look for "Sent reply" or "Assistant response generated" or similar near the message timestamps
reply_logs = []
for line in weekend_lines:
    if any(term in line for term in ["Sent reply", "Triggered assistant", "Calling Gemini", "Candidate response", "Validation passed"]):
        reply_logs.append(line)

print(f"\nKey assistant execution log lines: {len(reply_logs)}")
for l in reply_logs[:20]:
    print(" ", l.strip()[:140])
