import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
import json
from collections import defaultdict, Counter

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

# Trackers
triggers = {
    "direct_reply": [],
    "mention": [],
    "sequential_followup": [],
    "passive_clinical": [],
    "media": [],
    "referee": [],
}

silences = {
    "passive_cooldown": [],
    "retry_backoff": [],
    "dialogue_stale": [],
    "dialogue_triage_rejected": [],
    "passive_triage_rejected": [],
    "negative_feedback_silenced": [],
    "validator_rejected": [],
    "media_validator_rejected": [],
    "validator_unavailable": [],
    "llm_cascade_exhausted": [],
    "triage_generation_failed": [],
    "rate_limit_503": [],
    "bot_is_silenced_penalty": [],
}

# Regexes
re_ts = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?)")

# Trigger regexes
re_trig_assist = re.compile(r"Triggered assistant!\s+Reason:\s+(.*?)\.\s+Keywords:", re.IGNORECASE)
re_trig_media = re.compile(r"Triggered media assistant!\s+Reason:\s+(.*?)\.\s+Keywords:", re.IGNORECASE)
re_trig_mention_sent = re.compile(r"Bot mention reply sent to chat \S+,\s+msg_id=(\d+)", re.IGNORECASE)
re_trig_mention_shadow = re.compile(r"\[SHADOW\] Bot mention reply logged \(not sent\):\s*(.*)", re.IGNORECASE)
re_trig_referee = re.compile(r"Clinical Referee triggered for msg_id=(\d+)", re.IGNORECASE)
re_diag_approved = re.compile(r"Triage approved dialogue continuation \(bot_msg_count=(\d+)\)", re.IGNORECASE)
re_seq_approved = re.compile(r"Triage approved sequential follow-up from case author (\d+)", re.IGNORECASE)

# Silence regexes
re_sil_cooldown = re.compile(r"Passive text trigger suppressed: passive cooldown,\s*(\d+)\s*min left", re.IGNORECASE)
re_sil_retry = re.compile(r"Passive text trigger suppressed: retry backoff after failed attempt,\s*(\d+)\s*min left", re.IGNORECASE)
re_sil_stale = re.compile(r"Dialogue reply is stale\.\s*(\d+)\s*messages have passed since bot message (\d+)", re.IGNORECASE)
re_sil_diag_triage = re.compile(r"Dialogue triage rejected continuation for chain with (\d+) bot replies", re.IGNORECASE)
re_sil_pass_triage = re.compile(r"LLM triage decided NOT to reply\. Cancelling trigger", re.IGNORECASE)
re_llama_triage_false = re.compile(r"Llama Triage decision:\s*should_reply=False.*?Reason:\s*(.*)", re.IGNORECASE)
re_sil_neg_feed = re.compile(r"(?:Global n|N)egative feedback detected.*?: '(.*?)'\. Silencing bot", re.IGNORECASE)
re_sil_pen = re.compile(r"Bot is silenced until (.*?)\. Skipping (.*)", re.IGNORECASE)
re_sil_val_rej = re.compile(r"Response quality validator REJECTED draft:\s*(.*?)\.\s*Suppressing reply", re.IGNORECASE)
re_sil_media_val_rej = re.compile(r"Media response quality validator REJECTED draft:\s*(.*?)\.\s*Suppressing reply", re.IGNORECASE)
re_sil_val_unavail = re.compile(r"Response validator unavailable \((.*?)\)\.\s*Uninvited reply", re.IGNORECASE)

re_llm_exhaust = re.compile(r"gemini cascade exhausted|All AI attempts exhausted", re.IGNORECASE)
re_triage_fail = re.compile(r"Llama triage generation failed:\s*(.*)", re.IGNORECASE)
re_503 = re.compile(r"(?:503|504|ResourceExhausted|rate limit exceeded|quota exceeded)", re.IGNORECASE)

for lf in log_files:
    fname = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        curr_ts = ""
        for line_num, line in enumerate(f, 1):
            m_ts = re_ts.match(line)
            if m_ts:
                curr_ts = m_ts.group(1)

            # Check triggers
            m = re_trig_assist.search(line)
            if m:
                reason = m.group(1)
                entry = {"file": fname, "line": line_num, "ts": curr_ts, "reason": reason}
                if "dialogue" in reason.lower():
                    triggers["direct_reply"].append(entry)
                elif "sequential" in reason.lower():
                    triggers["sequential_followup"].append(entry)
                else:
                    triggers["passive_clinical"].append(entry)

            m = re_trig_media.search(line)
            if m:
                triggers["media"].append({"file": fname, "line": line_num, "ts": curr_ts, "reason": m.group(1)})

            m = re_trig_mention_sent.search(line)
            if m:
                triggers["mention"].append({"file": fname, "line": line_num, "ts": curr_ts, "msg_id": m.group(1), "status": "sent"})

            m = re_trig_mention_shadow.search(line)
            if m:
                triggers["mention"].append({"file": fname, "line": line_num, "ts": curr_ts, "text": m.group(1)[:50], "status": "shadow"})

            m = re_trig_referee.search(line)
            if m:
                triggers["referee"].append({"file": fname, "line": line_num, "ts": curr_ts, "msg_id": m.group(1)})

            # Check silences
            m = re_sil_cooldown.search(line)
            if m:
                silences["passive_cooldown"].append({"file": fname, "line": line_num, "ts": curr_ts, "min_left": int(m.group(1))})

            m = re_sil_retry.search(line)
            if m:
                silences["retry_backoff"].append({"file": fname, "line": line_num, "ts": curr_ts, "min_left": int(m.group(1))})

            m = re_sil_stale.search(line)
            if m:
                silences["dialogue_stale"].append({"file": fname, "line": line_num, "ts": curr_ts, "msgs_passed": int(m.group(1)), "bot_msg_id": int(m.group(2))})

            m = re_sil_diag_triage.search(line)
            if m:
                silences["dialogue_triage_rejected"].append({"file": fname, "line": line_num, "ts": curr_ts, "bot_msg_count": int(m.group(1))})

            m = re_sil_pass_triage.search(line)
            if m:
                silences["passive_triage_rejected"].append({"file": fname, "line": line_num, "ts": curr_ts})

            m = re_sil_neg_feed.search(line)
            if m:
                silences["negative_feedback_silenced"].append({"file": fname, "line": line_num, "ts": curr_ts, "text": m.group(1)})

            m = re_sil_pen.search(line)
            if m:
                silences["bot_is_silenced_penalty"].append({"file": fname, "line": line_num, "ts": curr_ts, "until": m.group(1), "action": m.group(2)})

            m = re_sil_val_rej.search(line)
            if m:
                silences["validator_rejected"].append({"file": fname, "line": line_num, "ts": curr_ts, "reason": m.group(1)})

            m = re_sil_media_val_rej.search(line)
            if m:
                silences["media_validator_rejected"].append({"file": fname, "line": line_num, "ts": curr_ts, "reason": m.group(1)})

            m = re_sil_val_unavail.search(line)
            if m:
                silences["validator_unavailable"].append({"file": fname, "line": line_num, "ts": curr_ts, "detail": m.group(1)})

            m = re_llm_exhaust.search(line)
            if m:
                silences["llm_cascade_exhausted"].append({"file": fname, "line": line_num, "ts": curr_ts})

            m = re_triage_fail.search(line)
            if m:
                silences["triage_generation_failed"].append({"file": fname, "line": line_num, "ts": curr_ts, "error": m.group(1)})

            m = re_503.search(line)
            if m:
                silences["rate_limit_503"].append({"file": fname, "line": line_num, "ts": curr_ts})

# Summary output
print("=================== TRIGGER EVENTS SUMMARY ===================")
total_triggers = 0
for k, v in triggers.items():
    cnt = len(v)
    total_triggers += cnt
    print(f"  {k:<25}: {cnt:>5}")
print(f"  {'TOTAL TRIGGERS':<25}: {total_triggers:>5}")

print("\n=================== SILENCE / SUPPRESSION EVENTS SUMMARY ===================")
total_silences = 0
for k, v in silences.items():
    cnt = len(v)
    total_silences += cnt
    print(f"  {k:<30}: {cnt:>5}")
print(f"  {'TOTAL SILENCE EVENTS':<30}: {total_silences:>5}")

# Save detailed counts to json for analysis
summary_data = {
    "trigger_counts": {k: len(v) for k, v in triggers.items()},
    "silence_counts": {k: len(v) for k, v in silences.items()},
    "dialogue_stale_samples": silences["dialogue_stale"][:15],
    "negative_feedback_samples": silences["negative_feedback_silenced"],
    "validator_rejected_samples": silences["validator_rejected"][:15],
    "media_validator_rejected_samples": silences["media_validator_rejected"][:15],
    "validator_unavailable_samples": silences["validator_unavailable"][:15],
    "dialogue_triage_rejected_samples": silences["dialogue_triage_rejected"],
}

with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_logs_2\summary_stats.json", "w", encoding="utf-8") as f:
    json.dump(summary_data, f, ensure_ascii=False, indent=2)
print("\nWrote summary_stats.json")
