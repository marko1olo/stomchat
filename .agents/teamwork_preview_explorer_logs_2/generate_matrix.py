import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
import re
import json

log_files = [
    r"c:\Users\danat\Desktop\stomchat\bot.log",
    r"c:\Users\danat\Desktop\stomchat\bot.log.1",
    r"c:\Users\danat\Desktop\stomchat\bot.log.2",
    r"c:\Users\danat\Desktop\stomchat\bot.log.3",
]

# Patterns
re_assist = re.compile(r"Triggered assistant!\s+Reason:\s+(.*?)\.\s+Keywords:", re.IGNORECASE)
re_media = re.compile(r"Triggered media assistant!\s+Reason:\s+(.*?)\.\s+Keywords:", re.IGNORECASE)
re_mention_sent = re.compile(r"Bot mention reply sent to chat \S+,\s+msg_id=(\d+)", re.IGNORECASE)
re_mention_triage = re.compile(r"Bot mention triage decision:\s*'YES", re.IGNORECASE)
re_seq = re.compile(r"Triage approved sequential follow-up", re.IGNORECASE)
re_diag_app = re.compile(r"Triage approved dialogue continuation", re.IGNORECASE)
re_referee = re.compile(r"Clinical Referee triggered for msg_id=", re.IGNORECASE)

re_cd = re.compile(r"Passive text trigger suppressed: passive cooldown,\s*(\d+)\s*min left", re.IGNORECASE)
re_bo = re.compile(r"Passive text trigger suppressed: retry backoff after failed attempt,\s*(\d+)\s*min left", re.IGNORECASE)
re_stale = re.compile(r"Dialogue reply is stale\.\s*(\d+)\s*messages have passed", re.IGNORECASE)
re_diag_triage_no = re.compile(r"Dialogue triage rejected continuation for chain", re.IGNORECASE)
re_pass_triage_no = re.compile(r"LLM triage decided NOT to reply\. Cancelling trigger", re.IGNORECASE)
re_neg_feed = re.compile(r"(?:Global n|N)egative feedback detected.*?: '(.*?)'\. Silencing bot", re.IGNORECASE)
re_sil_pen = re.compile(r"Bot is silenced until (.*?)\. Skipping", re.IGNORECASE)
re_val_rej = re.compile(r"Response quality validator REJECTED draft:\s*(.*?)\.\s*Suppressing reply", re.IGNORECASE)
re_media_val_rej = re.compile(r"Media response quality validator REJECTED draft:\s*(.*?)\.\s*Suppressing reply", re.IGNORECASE)
re_val_unavail = re.compile(r"Response validator unavailable \((.*?)\)\.\s*Uninvited reply", re.IGNORECASE)
re_cascade_ex = re.compile(r"gemini cascade exhausted|All AI attempts exhausted", re.IGNORECASE)
re_503 = re.compile(r"503|504|ResourceExhausted|rate limit exceeded|quota exceeded", re.IGNORECASE)

metrics = [
    # Triggers
    "Direct Reply (Dialogue Continuation)",
    "Mentions (@bot / name)",
    "Sequential Follow-ups",
    "Passive Clinical Triggers",
    "Media Triggers (Images/Docs)",
    "Clinical Referee Triggers",
    "Total Triggers",
    
    # Silences
    "passive_cooldown (120m)",
    "retry_backoff (10m)",
    "dialogue_stale (count_since > 5)",
    "dialogue_triage_rejected (triage NO)",
    "passive_triage_rejected (Llama NO)",
    "negative_feedback_silenced (4h penalty)",
    "bot_is_silenced (active penalty skips)",
    "validator_rejected (Text Drafts)",
    "media_validator_rejected (Media Drafts)",
    "validator_unavailable (Cascade failure)",
    "llm_cascade_exhausted (Fatal API drop)",
    "rate_limit_or_503 (Provider throttling)",
    "Total Silence / Suppression Events"
]

data = {m: {os.path.basename(lf): 0 for lf in log_files} for m in metrics}
for m in metrics:
    data[m]["TOTAL"] = 0

for lf in log_files:
    fn = os.path.basename(lf)
    if not os.path.exists(lf):
        continue
    with open(lf, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            # Triggers
            m_a = re_assist.search(line)
            if m_a:
                reas = m_a.group(1).lower()
                if "dialogue" in reas:
                    data["Direct Reply (Dialogue Continuation)"][fn] += 1
                elif "sequential" in reas:
                    data["Sequential Follow-ups"][fn] += 1
                else:
                    data["Passive Clinical Triggers"][fn] += 1
            
            if re_media.search(line):
                data["Media Triggers (Images/Docs)"][fn] += 1
            if re_mention_sent.search(line):
                data["Mentions (@bot / name)"][fn] += 1
            if re_referee.search(line):
                data["Clinical Referee Triggers"][fn] += 1

            # Silences
            if re_cd.search(line):
                data["passive_cooldown (120m)"][fn] += 1
            if re_bo.search(line):
                data["retry_backoff (10m)"][fn] += 1
            if re_stale.search(line):
                data["dialogue_stale (count_since > 5)"][fn] += 1
            if re_diag_triage_no.search(line):
                data["dialogue_triage_rejected (triage NO)"][fn] += 1
            if re_pass_triage_no.search(line):
                data["passive_triage_rejected (Llama NO)"][fn] += 1
            if re_neg_feed.search(line):
                data["negative_feedback_silenced (4h penalty)"][fn] += 1
            if re_sil_pen.search(line):
                data["bot_is_silenced (active penalty skips)"][fn] += 1
            if re_val_rej.search(line):
                data["validator_rejected (Text Drafts)"][fn] += 1
            if re_media_val_rej.search(line):
                data["media_validator_rejected (Media Drafts)"][fn] += 1
            if re_val_unavail.search(line):
                data["validator_unavailable (Cascade failure)"][fn] += 1
            if re_cascade_ex.search(line):
                data["llm_cascade_exhausted (Fatal API drop)"][fn] += 1
            if re_503.search(line):
                data["rate_limit_or_503 (Provider throttling)"][fn] += 1

# Compute Totals and Row Totals
for fn in [os.path.basename(lf) for lf in log_files]:
    data["Total Triggers"][fn] = (
        data["Direct Reply (Dialogue Continuation)"][fn] +
        data["Mentions (@bot / name)"][fn] +
        data["Sequential Follow-ups"][fn] +
        data["Passive Clinical Triggers"][fn] +
        data["Media Triggers (Images/Docs)"][fn] +
        data["Clinical Referee Triggers"][fn]
    )
    data["Total Silence / Suppression Events"][fn] = (
        data["passive_cooldown (120m)"][fn] +
        data["retry_backoff (10m)"][fn] +
        data["dialogue_stale (count_since > 5)"][fn] +
        data["dialogue_triage_rejected (triage NO)"][fn] +
        data["passive_triage_rejected (Llama NO)"][fn] +
        data["negative_feedback_silenced (4h penalty)"][fn] +
        data["bot_is_silenced (active penalty skips)"][fn] +
        data["validator_rejected (Text Drafts)"][fn] +
        data["media_validator_rejected (Media Drafts)"][fn] +
        data["validator_unavailable (Cascade failure)"][fn] +
        data["llm_cascade_exhausted (Fatal API drop)"][fn] +
        data["rate_limit_or_503 (Provider throttling)"][fn]
    )

for m in metrics:
    data[m]["TOTAL"] = sum(data[m][os.path.basename(lf)] for lf in log_files)

# Print Markdown Table
col_w = [45, 12, 12, 12, 12, 12]
header = ["Metric / Event Category", "bot.log", "bot.log.1", "bot.log.2", "bot.log.3", "TOTAL"]
sep = ["-" * w for w in col_w]

print("| " + " | ".join(f"{h:<{col_w[i]}}" for i, h in enumerate(header)) + " |")
print("| " + " | ".join(sep) + " |")

for m in metrics:
    if m in ["Total Triggers", "Total Silence / Suppression Events"]:
        print("| " + " | ".join(sep) + " |")
    vals = [
        f"{m:<{col_w[0]}}",
        f"{data[m]['bot.log']:>{col_w[1]}}",
        f"{data[m]['bot.log.1']:>{col_w[2]}}",
        f"{data[m]['bot.log.2']:>{col_w[3]}}",
        f"{data[m]['bot.log.3']:>{col_w[4]}}",
        f"{data[m]['TOTAL']:>{col_w[5]}}"
    ]
    print("| " + " | ".join(vals) + " |")
    if m == "Total Triggers":
        print("| " + " | ".join(sep) + " |")

# Also dump to JSON
with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_logs_2\matrix_stats.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
