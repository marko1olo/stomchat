with open("REPORT_CHAT_BALANCE_AND_LOGS.md", encoding="utf-8") as f:
    text = f.read()

assert '1,472' in text, "1,472 not in text"
assert '5,093' in text, "5,093 not in text"
assert 'Section 2.6' in text or '### 2.6' in text, "Section 2.6 not in text"
assert 'last_passive_bot_msg_id' in text, "last_passive_bot_msg_id not in text"
assert 'DIALOGUE_MAX_STALE_SEQUENTIAL' in text, "DIALOGUE_MAX_STALE_SEQUENTIAL not in text"
assert 'ENABLE_PM_PROACTIVE_PINGS' in text, "ENABLE_PM_PROACTIVE_PINGS not in text"
assert 'facts_json' in text, "facts_json not in text"
assert 'pm_ping_scheduler_task' in text, "pm_ping_scheduler_task not in text"
print("Report assertions PASSED!")
