# -*- coding: utf-8 -*-
import sys, re
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')
with open('bot.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# Extract lines between 2026-09-11 00:00:59 and 2026-09-13 14:24:17
window_lines = []
pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')

t_start = datetime(2026, 9, 11, 0, 0, 59)
t_end = datetime(2026, 9, 13, 14, 24, 17)

current_in_window = False
for l in lines:
    m = pattern.match(l)
    if m:
        try:
            t = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
            if t_start <= t <= t_end:
                current_in_window = True
            else:
                current_in_window = False
        except:
            pass
    if current_in_window:
        window_lines.append(l)

print(f'Total lines in production window ({t_start} to {t_end}): {len(window_lines)}')
err = [l for l in window_lines if ' ERROR ' in l or ' CRITICAL ' in l]
print(f'ERROR / CRITICAL count: {len(err)}')

# Check passive text trigger suppressed
pass_supp = [l for l in window_lines if 'Passive text trigger suppressed:' in l]
print(f'Passive text trigger suppressed count: {len(pass_supp)}')

hard = [l for l in pass_supp if 'passive_cooldown_hard_floor' in l or '45' in l]
dynamic = [l for l in pass_supp if 'passive_cooldown_dynamic' in l or 'circadian' in l.lower()]
retry = [l for l in pass_supp if 'retry_backoff' in l]
print(f'  Hard floor: {len(hard)}, Dynamic: {len(dynamic)}, Retry: {len(retry)}')

# Check 503 bans: 'Banning model' and '503'
bans = [l for l in window_lines if '503' in l and 'Banning model' in l]
print(f'503 Banning model count: {len(bans)}')
b38 = [l for l in bans if 'gemini-3.8-flash' in l]
b37 = [l for l in bans if 'gemini-3.7-flash' in l]
b36 = [l for l in bans if 'gemini-3.6-flash' in l]
print(f'  3.8-flash: {len(b38)}, 3.7-flash: {len(b37)}, 3.6-flash: {len(b36)}')

# Dual reply timestamps in log
dual_logs = [l for l in window_lines if '177390' in l or '177392' in l]
print(f'Dual reply log mentions: {len(dual_logs)}')
for l in dual_logs[:6]:
    print('  ', l.strip()[:110])
