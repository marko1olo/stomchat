# -*- coding: utf-8 -*-
import sqlite3, json, sys

sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect('stomat_bot.db')
cur = conn.cursor()

print('=== 1. DB MESSAGE TOTALS ===')
cur.execute('SELECT COUNT(*) FROM messages WHERE msg_id >= 177243 AND msg_id <= 177445')
total_200 = cur.fetchone()[0]
print(f'Total messages in 177243-177445: {total_200}')

cur.execute('SELECT msg_id FROM bot_sent_messages WHERE msg_id >= 177243 AND msg_id <= 177445')
bot_sent_ids = set(r[0] for r in cur.fetchall())
print(f'bot_sent_messages in range: {len(bot_sent_ids)}')

digest_ids = {177300, 177301, 177415, 177416}
all_bot_ids = bot_sent_ids.union(digest_ids)
print(f'All bot message IDs (clinical replies + digests): {len(all_bot_ids)} -> {sorted(all_bot_ids)}')

id_list = ','.join(str(x) for x in all_bot_ids)
cur.execute(f'SELECT COUNT(*) FROM messages WHERE msg_id >= 177243 AND msg_id <= 177445 AND msg_id NOT IN ({id_list})')
user_msgs_count = cur.fetchone()[0]
print(f'Clinician messages count: {user_msgs_count}')

cur.execute(f'SELECT DISTINCT sender_id, sender_username, sender_name FROM messages WHERE msg_id >= 177243 AND msg_id <= 177445 AND msg_id NOT IN ({id_list})')
senders = cur.fetchall()
print(f'Distinct clinician senders count: {len(senders)}')

cur.execute(f'SELECT DISTINCT m.sender_id, (SELECT COUNT(*) FROM user_memories u WHERE u.user_id = m.sender_id) as mem_count FROM messages m WHERE m.msg_id >= 177243 AND msg_id <= 177445 AND m.msg_id NOT IN ({id_list})')
clinicians = cur.fetchall()
with_profile = [c for c in clinicians if c[1] > 0]
print(f'Clinicians with profile in user_memories: {len(with_profile)} / {len(clinicians)} ({len(with_profile)/len(clinicians)*100:.2f}%)')

cur.execute('SELECT msg_id, date, reply_to_msg_id, text FROM messages WHERE msg_id IN (177390, 177392)')
for r in cur.fetchall():
    print(f'Dual reply row: msg_id={r[0]}, date={r[1]}, reply_to={r[2]}, text={r[3][:60]}...')

cur.execute("SELECT msg_id, sender_id, sender_name, date, text FROM messages WHERE text LIKE '%Выйдешь работать за меня%'")
print('Denis quote:', cur.fetchall())

threads = [
    (1, 'Implant transfer', 177250, 177252),
    (2, 'Leaf gauge / CR', 177266, 177283),
    (3, 'Vertiprep', 177304, 177308),
    (4, 'Emergence profile', 177345, 177348),
    (5, 'Ceramic veneer / disk polishing', 177381, 177392),
    (6, 'Bis-acryl temporary mock-up', 177398, 177409),
    (7, 'Multi-unit 11 deg cone', 177427, 177428),
    (8, 'Invasive cervical resorption 2.6', 177431, 177432),
    (9, 'E.max adhesive luting', 177436, 177437)
]
print('\n=== 9 CLINICAL THREADS CHECK ===')
for tid, name, s_id, e_id in threads:
    cur.execute(f'SELECT msg_id FROM messages WHERE msg_id >= ? AND msg_id <= ? AND msg_id IN ({id_list})', (s_id, e_id))
    bot_in_thread = cur.fetchall()
    cur.execute('SELECT COUNT(*) FROM messages WHERE msg_id >= ? AND msg_id <= ?', (s_id, e_id))
    total_in_thread = cur.fetchone()[0]
    print(f'Thread {tid} ({name}) {s_id}->{e_id}: total msgs={total_in_thread}, bot replies={len(bot_in_thread)} (IDs: {[b[0] for b in bot_in_thread]})')

conn.close()

print('\n=== 2. BOT.LOG ANALYSIS ===')
with open('bot.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()
print(f'Total lines in bot.log: {len(lines)}')
err_crit = [l for l in lines if ' ERROR ' in l or ' CRITICAL ' in l]
print(f'ERROR / CRITICAL count: {len(err_crit)}')

hard_floor = [l for l in lines if 'passive_cooldown_hard_floor' in l or 'hard floor' in l.lower()]
dynamic = [l for l in lines if 'passive_cooldown_dynamic' in l]
retry_backoff = [l for l in lines if 'retry_backoff' in l]
print(f'Hard floor: {len(hard_floor)}, Dynamic: {len(dynamic)}, Retry backoff: {len(retry_backoff)}')
print(f'Total passive suppressions: {len(hard_floor) + len(dynamic) + len(retry_backoff)}')

f503 = [l for l in lines if '503' in l and any(k in l.lower() for k in ['gemini', 'upstream', 'model', 'unavailable', 'resource'])]
print(f'Gemini 503 log occurrences: {len(f503)}')

m_38 = [l for l in lines if '503' in l and 'gemini-3.8-flash' in l]
m_37 = [l for l in lines if '503' in l and 'gemini-3.7-flash' in l]
m_36 = [l for l in lines if '503' in l and 'gemini-3.6-flash' in l]
print(f'503 by model: 3.8-flash: {len(m_38)}, 3.7-flash: {len(m_37)}, 3.6-flash: {len(m_36)}')