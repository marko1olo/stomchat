import sys
import re

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

raw_503_lines = []
for lf in ["bot.log", "bot.log.1", "bot.log.2", "bot.log.3"]:
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "503" in line:
                raw_503_lines.append((lf, line.strip()))

print(f"Total raw lines containing '503': {len(raw_503_lines)}")

# Let's see what contains '503' that is NOT an HTTP 503 error
fp_lines = []
tp_lines = []

for lf, line in raw_503_lines:
    # Check if '503' appears only as timestamp millisecond (e.g. ,503 - ) or part of user_id / msg_id
    # True 503 error usually has: "503", "status: 503", "code 503", "503 Service", "503 UNAVAILABLE", "The service is temporarily unavailable", "status_code: 503", etc.
    # If 503 only appears in timestamp like "2026-08-30 14:36:37,503" and nowhere else on line:
    line_without_timestamp = re.sub(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}', '', line)
    if "503" not in line_without_timestamp:
        fp_lines.append((lf, line))
    else:
        # Check if 503 is part of a longer number (e.g. user id or msg id)
        # e.g. 1025034309 or 175503
        nums_with_503 = re.findall(r'\d*503\d*', line_without_timestamp)
        is_real_503 = False
        for n in nums_with_503:
            if n == '503':
                is_real_503 = True
                break
        if is_real_503:
            tp_lines.append((lf, line))
        else:
            fp_lines.append((lf, line))

print(f"False positives (timestamp ms ,503 or user/msg ID): {len(fp_lines)}")
print(f"True HTTP 503 / provider rate-limit errors: {len(tp_lines)}")
print(f"Total raw (TP + FP): {len(tp_lines) + len(fp_lines)}")

# Check by file
by_file_tp = collections = {}
for lf in ["bot.log", "bot.log.1", "bot.log.2", "bot.log.3"]:
    file_tp = sum(1 for f, l in tp_lines if f == lf)
    file_fp = sum(1 for f, l in fp_lines if f == lf)
    print(f"  {lf}: TP={file_tp}, FP={file_fp}, Total={file_tp + file_fp}")
