import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
from collections import defaultdict, Counter
from datetime import datetime

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

# Patterns
PATTERNS = {
    # Triggers
    "trigger_assistant": re.compile(r"Triggered assistant!\s+Reason:\s+(.*?)\.\s+Keywords:", re.IGNORECASE),
    "trigger_media": re.compile(r"Triggered media assistant!\s+Reason:\s+(.*?)\.\s+Keywords:", re.IGNORECASE),
    "trigger_mention_sent": re.compile(r"Bot mention reply sent to chat \S+, msg_id=(\d+)", re.IGNORECASE),
    "trigger_mention_shadow": re.compile(r"\[SHADOW\] Bot mention reply logged \(not sent\):", re.IGNORECASE),
    "trigger_referee": re.compile(r"Clinical Referee triggered for msg_id=(\d+)", re.IGNORECASE),
    "dialogue_continuation_approved": re.compile(r"Triage approved dialogue continuation \(bot_msg_count=(\d+)\)", re.IGNORECASE),
    "sequential_followup_approved": re.compile(r"Triage approved sequential follow-up from case author (\d+)", re.IGNORECASE),
    
    # Silence / Suppressions
    "suppress_passive_cooldown": re.compile(r"Passive text trigger suppressed: passive cooldown, (\d+) min left", re.IGNORECASE),
    "suppress_retry_backoff": re.compile(r"Passive text trigger suppressed: retry backoff after failed attempt, (\d+) min left", re.IGNORECASE),
    "suppress_media_cooldown": re.compile(r"Media Assistant: passive media cooldown active \(120 min\)", re.IGNORECASE),
    "suppress_dialogue_stale": re.compile(r"Dialogue reply is stale\.\s+(\d+) messages have passed since bot message (\d+)", re.IGNORECASE),
    "suppress_dialogue_triage_rejected": re.compile(r"Dialogue triage rejected continuation for chain with (\d+) bot replies", re.IGNORECASE),
    "suppress_passive_llm_triage_rejected": re.compile(r"LLM triage decided NOT to reply\. Cancelling trigger", re.IGNORECASE),
    "llama_triage_decision_false": re.compile(r"Llama Triage decision: should_reply=False.*?Reason:\s*(.*)", re.IGNORECASE),
    "llama_triage_confidence_low": re.compile(r"Llama triage confidence too low \((.*?)\)\. Overriding should_reply to False", re.IGNORECASE),
    "suppress_negative_feedback": re.compile(r"(?:Global n|N)egative feedback detected.*?: '(.*?)'\. Silencing bot", re.IGNORECASE),
    "suppress_bot_is_silenced": re.compile(r"Bot is silenced until (.*?)\. Skipping (.*)", re.IGNORECASE),
    "suppress_validator_rejected": re.compile(r"Response quality validator REJECTED draft:\s*(.*?)\.\s*Suppressing reply", re.IGNORECASE),
    "suppress_media_validator_rejected": re.compile(r"Media response quality validator REJECTED draft:\s*(.*?)\.\s*Suppressing reply", re.IGNORECASE),
    "suppress_validator_unavailable": re.compile(r"Response validator unavailable \((.*?)\)\.\s*Uninvited reply", re.IGNORECASE),
    
    # Errors
    "gemini_cascade_exhausted": re.compile(r"gemini cascade exhausted|All AI attempts exhausted", re.IGNORECASE),
    "triage_generation_failed": re.compile(r"Llama triage generation failed:\s*(.*)", re.IGNORECASE),
    "rate_limit_or_503": re.compile(r"503|504|ResourceExhausted|rate_limit|rate limit|quota", re.IGNORECASE),
}

stats = {name: Counter() for name in PATTERNS}
by_file = {os.path.basename(lf): {name: 0 for name in PATTERNS} for lf in log_files}

ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")

for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line_num, line in enumerate(f, 1):
            for pname, pat in PATTERNS.items():
                m = pat.search(line)
                if m:
                    by_file[fname][pname] += 1
                    stats[pname]["total"] += 1

print(f"{'Pattern':<40} | {'Total':<6} | " + " | ".join(f"{os.path.basename(lf):<10}" for lf in log_files))
print("-" * 95)
for pname in PATTERNS:
    row = [f"{pname:<40}", f"{stats[pname]['total']:<6}"]
    for lf in log_files:
        fn = os.path.basename(lf)
        row.append(f"{by_file[fn][pname]:<10}")
    print(" | ".join(row))
