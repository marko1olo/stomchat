import sys
import io
import re

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("=== CHECK SPECIFIC MESSAGE IDS IN LOGS ===")

def search_in_log(log_path, pattern):
    results = []
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if pattern in line:
                results.append(line.strip())
    return results

print("Checking msg 176844 in bot.log:")
for l in search_in_log('bot.log', '176844')[:5]:
    print(" ", l[:120])

print("\nChecking msg 176043 in bot.log.1:")
for l in search_in_log('bot.log.1', '176043')[:5]:
    print(" ", l[:120])

print("\nChecking msg 171104 in bot.log.3:")
for l in search_in_log('bot.log.3', '171104')[:5]:
    print(" ", l[:120])
