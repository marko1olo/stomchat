import sys
sys.stdout.reconfigure(encoding='utf-8')
import os

files_to_check = [
    r"c:\Users\danat\Desktop\stomchat\blocking_tools.py",
    r"c:\Users\danat\Desktop\stomchat\main.py",
    r"c:\Users\danat\Desktop\stomchat\tg_safety.py",
]

for fp in files_to_check:
    print(f"=== {os.path.basename(fp)} ===")
    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f, 1):
            low = line.lower()
            if any(w in low for w in ["503", "504", "exhaust", "rate limit", "cooldown", "cascade", "ban"]):
                print(f"{idx}: {line.strip()}")
