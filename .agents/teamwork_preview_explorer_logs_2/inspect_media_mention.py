import sys
sys.stdout.reconfigure(encoding='utf-8')

with open(r"c:\Users\danat\Desktop\stomchat\assistant.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

def print_section(start, end):
    for i in range(start - 1, min(end, len(lines))):
        line = lines[i]
        if any(k in line for k in ["logger.", "return ", "if ", "trigger_reason", "cooldown"]):
            print(f"{i+1}: {line.rstrip()}")

print("=== check_and_trigger_assistant_media (3052-3345) ===")
print_section(3052, 3345)

print("\n=== check_bot_mention_trigger (5393-5570) ===")
print_section(5393, 5570)
