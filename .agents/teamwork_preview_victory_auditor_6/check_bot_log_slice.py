# -*- coding: utf-8 -*-
import sys, re

sys.stdout.reconfigure(encoding='utf-8')
with open('bot.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

print(f'Total lines: {len(lines)}')
first_line = lines[0] if lines else ''
last_line = lines[-1] if lines else ''
print(f'First line: {first_line.strip()[:100]}')
print(f'Last line: {last_line.strip()[:100]}')

# Look for timestamps between Sept 11 and Sept 13
weekend_lines = []
for idx, l in enumerate(lines):
    if any(d in l for d in ['2026-09-11', '2026-09-12', '2026-09-13']):
        weekend_lines.append((idx, l))

print(f'Lines with 2026-09-11, 12, 13: {len(weekend_lines)}')
if weekend_lines:
    print(f'First weekend line ({weekend_lines[0][0]}): {weekend_lines[0][1].strip()[:100]}')
    print(f'Last weekend line ({weekend_lines[-1][0]}): {weekend_lines[-1][1].strip()[:100]}')
    w_start = weekend_lines[0][0]
    w_end = weekend_lines[-1][0]
    w_slice = lines[w_start:w_end+1]
    print(f'Slice from index {w_start} to {w_end} = {len(w_slice)} lines')
    
    # Check errors in this weekend slice
    w_errors = [l for l in w_slice if ' ERROR ' in l or ' CRITICAL ' in l]
    print(f'Errors in weekend slice: {len(w_errors)}')
    if w_errors:
        for e in w_errors[:5]:
            print(f'  Err: {e.strip()[:100]}')
            
    # Check passive in weekend slice
    w_pass = [l for l in w_slice if 'passive' in l.lower()]
    print(f'Passive in weekend slice: {len(w_pass)}')
    
    # Check 503 in weekend slice
    w_503 = [l for l in w_slice if '503' in l]
    print(f'503 in weekend slice: {len(w_503)}')
