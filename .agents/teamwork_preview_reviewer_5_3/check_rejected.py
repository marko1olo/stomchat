import sys
import collections

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

patterns = collections.defaultdict(int)
by_file = collections.defaultdict(lambda: collections.defaultdict(int))

for lf in ["bot.log", "bot.log.1", "bot.log.2", "bot.log.3"]:
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            if "REJECTED" in line:
                if "Media response quality validator REJECTED" in line:
                    patterns["media_validator_rejected"] += 1
                    by_file[lf]["media_validator_rejected"] += 1
                elif "Response quality validator REJECTED" in line:
                    patterns["text_validator_rejected"] += 1
                    by_file[lf]["text_validator_rejected"] += 1
                elif "triage" in line.lower():
                    patterns["triage_rejected"] += 1
                    by_file[lf]["triage_rejected"] += 1
                else:
                    patterns["other_rejected"] += 1
                    by_file[lf]["other_rejected"] += 1

print("Summary of REJECTED patterns:")
for k, v in patterns.items():
    print(f"  {k}: {v}")

print("\nBy file:")
for lf in ["bot.log", "bot.log.1", "bot.log.2", "bot.log.3"]:
    print(f"  {lf}: {dict(by_file[lf])}")
