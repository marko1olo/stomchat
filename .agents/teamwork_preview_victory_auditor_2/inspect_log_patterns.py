import re

log_files = {
    'bot.log': 'bot.log',
    'bot.log.1': 'bot.log.1',
    'bot.log.2': 'bot.log.2',
    'bot.log.3': 'bot.log.3'
}

# In Table 2.1:
# - Direct Reply: "Processing direct reply..." or "Dialogue continuation trigger" or similar
# - Mentions: "Bot mention trigger" or similar
# - Sequential Follow-ups: "Sequential follow-up trigger"
# - Passive Clinical: "Passive clinical trigger" or similar
# - Media Triggers: "Media trigger" or "Vision trigger"
# - Clinical Referee: "Clinical referee trigger"

# Let's inspect trigger lines in logs
for name, path in log_files.items():
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    print(f"=== {name} ===")
    
    direct_reply = len(re.findall(r'Processing direct reply to bot message|Dialogue continuation trigger|Handling reply to bot', content, re.IGNORECASE))
    print(f"  Direct reply candidates: {direct_reply}")
    
    mentions = len(re.findall(r'Processing bot mention|Mention trigger|Triggered by mention', content, re.IGNORECASE))
    print(f"  Mention candidates: {mentions}")
    
    seq_followup = len(re.findall(r'Sequential follow-up|sequential_followup', content, re.IGNORECASE))
    print(f"  Sequential follow-up candidates: {seq_followup}")
    
    passive = len(re.findall(r'Passive clinical trigger|Passive trigger fired|Triggered passive response', content, re.IGNORECASE))
    print(f"  Passive clinical candidates: {passive}")
    
    media = len(re.findall(r'Media trigger|Vision trigger|Processing media|image received', content, re.IGNORECASE))
    print(f"  Media candidates: {media}")
    
    referee = len(re.findall(r'Clinical referee trigger|Referee trigger|clinical_referee', content, re.IGNORECASE))
    print(f"  Referee candidates: {referee}")
