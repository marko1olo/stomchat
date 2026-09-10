# Verify arithmetic of Table 2.2

table_rows = [
    ("passive_triage_rejected", 25, 681, 516, 508, 1730),
    ("rate_limit_or_503", 181, 822, 329, 140, 1472),
    ("retry_backoff", 0, 25, 89, 626, 740),
    ("passive_cooldown", 31, 343, 0, 288, 662),
    ("llm_cascade_exhausted", 6, 116, 338, 0, 460),
    ("dialogue_stale", 7, 6, 0, 47, 60),
    ("media_validator_rejected", 0, 8, 27, 14, 49),
    ("validator_unavailable", 0, 1, 37, 0, 38),
    ("validator_rejected_text", 0, 5, 9, 5, 19),
    ("bot_is_silenced", 0, 19, 0, 0, 19),
    ("dialogue_triage_rejected", 7, 7, 1, 1, 16),
    ("mention_triage_rejected", 1, 1, 0, 1, 3),
    ("negative_feedback_silenced", 0, 1, 0, 1, 2)
]

total_b0 = sum(r[1] for r in table_rows)
total_b1 = sum(r[2] for r in table_rows)
total_b2 = sum(r[3] for r in table_rows)
total_b3 = sum(r[4] for r in table_rows)
total_dedup = sum(r[5] for r in table_rows)

print(f"Computed sums: bot.log={total_b0}, bot.log.1={total_b1}, bot.log.2={total_b2}, bot.log.3={total_b3}")
print(f"Total Dedup Sum: {total_dedup}")
print(f"Sum of file totals: {total_b0 + total_b1 + total_b2 + total_b3}")

for r in table_rows:
    row_sum = sum(r[1:5])
    if row_sum != r[5]:
        print(f"Row mismatch in {r[0]}: sum={row_sum}, listed={r[5]}")
    pct = (r[5] / total_dedup) * 100
    print(f"  {r[0]}: {r[5]} ({pct:.2f}%)")
