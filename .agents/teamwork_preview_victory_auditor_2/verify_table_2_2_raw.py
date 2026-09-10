import re
import os

log_files = {
    'bot.log': 'bot.log',
    'bot.log.1': 'bot.log.1',
    'bot.log.2': 'bot.log.2',
    'bot.log.3': 'bot.log.3'
}

results = {}

for name, path in log_files.items():
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
        
    res = {}
    
    # 1. passive_triage_rejected
    # "LLM triage decided NOT to reply. Cancelling trigger."
    # "Llama triage decision: should_reply=False" or "Llama triage confidence too low"
    # In assistant.py: "LLM triage decided NOT to reply"
    res['passive_triage_rejected'] = sum(1 for l in lines if "LLM triage decided NOT to reply" in l)
    
    # 2. rate_limit_or_503
    # true 503 or 429 or rate limit errors
    true_503 = 0
    for l in lines:
        has_actual_error = bool(re.search(r'\b503\b|rate[ _]?limit|resource_?exhausted|\b429\b|service unavailable', l, re.IGNORECASE))
        if has_actual_error:
            true_503 += 1
    res['rate_limit_or_503'] = true_503
    
    # 3. retry_backoff:
    # "Passive text trigger suppressed: retry backoff" or "Passive media trigger suppressed: retry backoff"
    res['retry_backoff'] = sum(1 for l in lines if "trigger suppressed: retry backoff" in l.lower() or "passive text trigger suppressed: retry backoff" in l.lower() or "passive media trigger suppressed: retry backoff" in l.lower())
    
    # 4. passive_cooldown:
    # "Passive text trigger suppressed: passive cooldown" or "passive media cooldown active (120 min)"
    res['passive_cooldown'] = sum(1 for l in lines if "trigger suppressed: passive cooldown" in l.lower() or "passive media cooldown active" in l.lower())
    
    # 5. llm_cascade_exhausted:
    # "gemini cascade exhausted"
    # Note: some are co-logged with validator_unavailable
    res['llm_cascade_exhausted'] = sum(1 for l in lines if "gemini cascade exhausted" in l.lower() and "validator unavailable" not in l.lower())
    
    # 6. dialogue_stale:
    # "Dialogue reply suppressed: dialogue stale" or "count_since"
    res['dialogue_stale'] = sum(1 for l in lines if "dialogue stale" in l.lower() or "count_since >" in l.lower() or "dialogue stale (count_since" in l.lower())
    
    # 7. media_validator_rejected:
    # "Media response quality validator REJECTED draft:"
    res['media_validator_rejected'] = sum(1 for l in lines if "Media response quality validator REJECTED draft:" in l)
    
    # 8. validator_unavailable:
    # "Response validator unavailable"
    res['validator_unavailable'] = sum(1 for l in lines if "Response validator unavailable" in l)
    
    # 9. validator_rejected (Text):
    # "Response quality validator REJECTED draft:" (excluding Media)
    res['validator_rejected_text'] = sum(1 for l in lines if "Response quality validator REJECTED draft:" in l and "Media " not in l)
    
    # 10. bot_is_silenced:
    # "Passive text trigger suppressed: bot is silenced" or "trigger suppressed: bot is silenced"
    res['bot_is_silenced'] = sum(1 for l in lines if "trigger suppressed: bot is silenced" in l.lower())
    
    # 11. dialogue_triage_rejected:
    # "Dialogue triage rejected continuation"
    res['dialogue_triage_rejected'] = sum(1 for l in lines if "Dialogue triage rejected continuation" in l)
    
    # 12. mention_triage_rejected:
    # "Bot mention triage decision: 'NO'"
    res['mention_triage_rejected'] = sum(1 for l in lines if "Bot mention triage decision: 'NO'" in l or "Bot mention triage decision: \"NO\"" in l)
    
    # 13. negative_feedback_silenced:
    # "Triggering negative feedback silencing" or "negative feedback detected"
    res['negative_feedback_silenced'] = sum(1 for l in lines if "negative feedback silencing" in l.lower() or "negative feedback" in l.lower() and "silenc" in l.lower())
    
    results[name] = res

print(f"{'Reason':<30} | {'bot.log':<8} | {'bot.log.1':<10} | {'bot.log.2':<10} | {'bot.log.3':<10} | {'TOTAL':<8}")
print("-" * 90)
all_keys = list(next(iter(results.values())).keys())
total_by_file = {k: 0 for k in log_files.keys()}
overall_total = 0

for k in all_keys:
    b0 = results['bot.log'][k]
    b1 = results['bot.log.1'][k]
    b2 = results['bot.log.2'][k]
    b3 = results['bot.log.3'][k]
    row_tot = b0 + b1 + b2 + b3
    total_by_file['bot.log'] += b0
    total_by_file['bot.log.1'] += b1
    total_by_file['bot.log.2'] += b2
    total_by_file['bot.log.3'] += b3
    overall_total += row_tot
    print(f"{k:<30} | {b0:<8} | {b1:<10} | {b2:<10} | {b3:<10} | {row_tot:<8}")

print("-" * 90)
print(f"{'TOTAL':<30} | {total_by_file['bot.log']:<8} | {total_by_file['bot.log.1']:<10} | {total_by_file['bot.log.2']:<10} | {total_by_file['bot.log.3']:<10} | {overall_total:<8}")
