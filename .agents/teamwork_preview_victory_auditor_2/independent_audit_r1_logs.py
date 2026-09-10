import re
import os

log_files = ['bot.log', 'bot.log.1', 'bot.log.2', 'bot.log.3']

print("=== INDEPENDENT LOG AUDIT: TRIGGERS & SUPPRESSIONS ===")

for lf in log_files:
    if not os.path.exists(lf):
        print(f"Error: {lf} missing!")
        continue
        
    with open(lf, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
        
    # Table 2.1 Triggers
    # 1. Direct Reply: "Processing direct reply" / "Dialogue continuation trigger" / "reply to bot message"
    dr = sum(1 for l in lines if "Triage approved dialogue continuation" in l or "Processing direct reply to bot message" in l or "Handling reply to bot" in l or "dialogue continuation trigger" in l.lower())
    
    # 2. Mentions: "Bot mention triage decision: 'YES'" or "Processing bot mention"
    men = sum(1 for l in lines if "Bot mention triage decision: 'YES'" in l or "Processing bot mention" in l or "Triggered assistant! Reason: mention" in l)
    
    # 3. Sequential follow-ups:
    seq = sum(1 for l in lines if "Triage approved sequential follow-up" in l)
    
    # 4. Passive clinical:
    pas = sum(1 for l in lines if "Triggered assistant! Reason: passive" in l or "Triggered assistant! Reason: question" in l or "Triggered assistant! Reason: clinical" in l or "Triggered assistant! Reason: group_discussion" in l)
    
    # 5. Media triggers:
    med = sum(1 for l in lines if "Triggered media assistant!" in l)
    
    # 6. Clinical referee:
    ref = sum(1 for l in lines if "Clinical Referee triggered for msg_id=" in l)
    
    print(f"\n[{lf}] (Total lines: {len(lines)})")
    print(f"  Triggers raw count: direct_reply={dr}, mentions={men}, seq_followup={seq}, passive={pas}, media={med}, referee={ref}")
    print(f"  Total triggers: {dr + men + seq + pas + med + ref}")
