import glob
import re
import os

log_files = ["bot.log", "bot.log.1", "bot.log.2", "bot.log.3"]
log_files = [f for f in log_files if os.path.exists(f)]
print("Found log files:", log_files)

# 1. Text vs Media validator rejections
text_rejections = 0
media_rejections = 0
raw_validator_rejections = 0

for lf in log_files:
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "Response quality validator REJECTED draft:" in line:
                raw_validator_rejections += 1
                if "Media response quality validator REJECTED draft:" in line:
                    media_rejections += 1
                else:
                    text_rejections += 1

print(f"Validator rejections: Raw={raw_validator_rejections}, Media={media_rejections}, Text={text_rejections}")

# 2. HTTP 503 and Rate limits
raw_503 = 0
fp_503 = 0
true_503 = 0

for lf in log_files:
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "503" in line or "rate_limit" in line.lower() or "resourceexhausted" in line.lower() or "429" in line:
                # check if it is purely a timestamp or user id false positive
                # timestamp: e.g. ,503 or user id e.g. 1025034309 without actual 503 error
                has_actual_error = bool(re.search(r'\b503\b|rate[ _]?limit|resource_?exhausted|\b429\b|service unavailable', line, re.IGNORECASE))
                # Check for timestamp false positive: e.g. ,503 in timestamp without 503 error
                if ",503" in line and not has_actual_error:
                    fp_503 += 1
                elif "503" in line and not has_actual_error:
                    fp_503 += 1
                if has_actual_error:
                    true_503 += 1
                if "503" in line:
                    raw_503 += 1

print(f"503 analysis: raw '503' lines={raw_503}, true_error lines={true_503}, fp_lines={fp_503}")

# 3. Mention triage
mention_triage = 0
for lf in log_files:
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "Bot mention triage decision: 'NO'" in line or ("mention triage" in line.lower() and "'NO'" in line):
                mention_triage += 1
                print(f"Found mention triage NO in {lf}: {line.strip()[:120]}")

print(f"Mention triage rejections: {mention_triage}")

# 4. Check all 155 rejection lines and 94 unique incidents
rejection_lines = []
for lf in log_files:
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "REJECTED draft:" in line or "validator_unavailable:" in line:
                rejection_lines.append(line.strip())

print(f"Total rejection / unavailable lines: {len(rejection_lines)}")
