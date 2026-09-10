import sys
sys.stdout.reconfigure(encoding='utf-8')

with open(r"c:\Users\danat\Desktop\stomchat\assistant.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

def print_section(start, end):
    for i in range(start - 1, min(end, len(lines))):
        line = lines[i]
        if any(k in line for k in ["logger.", "return ", "if ", "elif ", "else:", "trigger_reason", "is_dialogue", "is_followup"]):
            print(f"{i+1}: {line.rstrip()}")

print("=== check_and_trigger_assistant (2467-3020) ===")
print_section(2467, 2780)
