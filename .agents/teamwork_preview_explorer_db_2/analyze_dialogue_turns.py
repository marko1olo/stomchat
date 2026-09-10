import sqlite3
import os
import sys
import json
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

WORKSPACE = r"c:\Users\danat\Desktop\stomchat"
bot_db_path = os.path.join(WORKSPACE, "stomat_bot.db")
conn = sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
c = conn.cursor()

# 1. Identify all bot messages and user messages
c.execute("""
    SELECT DISTINCT msg_id FROM (
        SELECT msg_id FROM bot_sent_messages
        UNION
        SELECT msg_id FROM messages WHERE sender_id = 7971556097 OR sender_username = 'docendobot'
    )
""")
bot_msg_ids = set(r[0] for r in c.fetchall())

c.execute("SELECT msg_id, reply_to_msg_id, sender_id, sender_name, sender_username, text, date FROM messages ORDER BY msg_id ASC")
all_msgs = c.fetchall()

# Map msg_id -> row
msg_map = {m['msg_id']: m for m in all_msgs}
# Map reply_to -> list of direct replies
children_map = defaultdict(list)
for m in all_msgs:
    if m['reply_to_msg_id']:
        children_map[m['reply_to_msg_id']].append(m['msg_id'])

print(f"Total messages: {len(all_msgs)}, Bot messages: {len(bot_msg_ids)}")

# Let's find all root bot conversations
# A bot dialogue thread is a tree of replies containing at least one bot message and at least one user message
# Let's trace trees starting from user questions that bot replied to, OR bot proactive messages that user replied to.

# Let's trace every bot message:
# For each bot_msg: what was it replying to? (parent)
# And what replied to it? (children)
# We can form connected components of reply trees that involve the bot!

visited = set()
threads = []

# Collect all reply edges (undirected for component discovery)
adj = defaultdict(set)
for m in all_msgs:
    if m['reply_to_msg_id'] and m['reply_to_msg_id'] in msg_map:
        adj[m['msg_id']].add(m['reply_to_msg_id'])
        adj[m['reply_to_msg_id']].add(m['msg_id'])

# For each bot message, find its connected component in the reply graph
thread_components = []
seen_nodes = set()

for b_id in sorted(bot_msg_ids):
    if b_id in seen_nodes or b_id not in msg_map:
        continue
    # BFS
    comp = []
    queue = [b_id]
    seen_nodes.add(b_id)
    while queue:
        curr = queue.pop(0)
        comp.append(curr)
        for neighbor in adj[curr]:
            if neighbor not in seen_nodes:
                seen_nodes.add(neighbor)
                queue.append(neighbor)
    thread_components.append(sorted(comp))

print(f"Total reply threads involving bot: {len(thread_components)}")

# Now let's analyze each thread:
# - How many turns? (number of alternating User/Bot or total messages in thread)
# - Distribution of turns:
#   1 turn: Bot replied once to User (or Bot posted, no user reply)
#   2 turns: User -> Bot -> User (User reacted once)
#   3 turns: User -> Bot -> User -> Bot
#   4-6 turns: Extended dialogue
#   7+ turns: Deep conversation

thread_stats = []

for comp in thread_components:
    msgs = [msg_map[mid] for mid in comp]
    # Filter msgs in order of date/msg_id
    msgs.sort(key=lambda x: x['msg_id'])
    
    bot_count = sum(1 for m in msgs if m['sender_id'] == 7971556097 or m['msg_id'] in bot_msg_ids)
    user_count = len(msgs) - bot_count
    
    # Calculate depth of reply chain (longest directed path in tree)
    # Roots are messages whose reply_to_msg_id is not in comp
    roots = [m['msg_id'] for m in msgs if not m['reply_to_msg_id'] or m['reply_to_msg_id'] not in comp]
    
    def get_max_depth(mid):
        children = [c_id for c_id in children_map[mid] if c_id in comp]
        if not children:
            return 1
        return 1 + max(get_max_depth(c_id) for c_id in children)
        
    max_depth = max(get_max_depth(r) for r in roots) if roots else 1
    
    # Last message in thread
    last_msg = msgs[-1]
    is_last_bot = (last_msg['sender_id'] == 7971556097 or last_msg['msg_id'] in bot_msg_ids)
    
    # Why did it drop off?
    # If last was user: why did bot not reply?
    drop_reason = "Bot concluded / User satisfied" if is_last_bot else "Bot stopped replying"
    if not is_last_bot:
        last_user_text = (last_msg['text'] or "").strip().lower()
        if any(w in last_user_text for w in ['спасибо', 'понял', 'принял', 'ок', 'ясно', 'договорились', 'благодарю']):
            drop_reason = "User concluded (gratitude/acknowledgement)"
        elif any(w in last_user_text for w in ['😂', '🤣', 'уберите', 'отключите', 'кикнут', 'бред']):
            drop_reason = "User ended with mockery/skepticism"
        else:
            # Check how many messages in group passed after last_msg
            c.execute("SELECT COUNT(*) FROM messages WHERE msg_id > ?", (last_msg['msg_id'],))
            subseq_chat_count = c.fetchone()[0]
            drop_reason = f"Unanswered clinical/followup question (chat velocity {subseq_chat_count} msgs later)"
            
    thread_stats.append({
        "thread_id": comp[0],
        "size": len(msgs),
        "max_depth": max_depth,
        "bot_count": bot_count,
        "user_count": user_count,
        "is_last_bot": is_last_bot,
        "drop_reason": drop_reason,
        "first_msg": msgs[0]['msg_id'],
        "last_msg": last_msg['msg_id'],
        "sample_snippet": (msgs[0]['text'] or '')[:80]
    })

# Turn depth distribution
depth_buckets = {
    "1 turn (Single Bot answer / No followup)": 0,
    "2-3 turns (Brief followup or acknowledgment)": 0,
    "4-6 turns (Active multi-turn clinical exchange)": 0,
    "7+ turns (Deep debate / extended problem solving)": 0
}

drop_reasons_counter = defaultdict(int)

for t in thread_stats:
    d = t["max_depth"]
    if d <= 1:
        depth_buckets["1 turn (Single Bot answer / No followup)"] += 1
    elif 2 <= d <= 3:
        depth_buckets["2-3 turns (Brief followup or acknowledgment)"] += 1
    elif 4 <= d <= 6:
        depth_buckets["4-6 turns (Active multi-turn clinical exchange)"] += 1
    else:
        depth_buckets["7+ turns (Deep debate / extended problem solving)"] += 1
        
    drop_reasons_counter[t["drop_reason"]] += 1

print("\n" + "="*70)
print("MULTI-TURN CONVERSATION DEPTH DISTRIBUTION (Group Chat)")
print("="*70)
total_threads = len(thread_stats)
for bucket, count in depth_buckets.items():
    print(f"  {bucket:<50}: {count} ({count/total_threads*100:.1f}%)")

print("\n" + "="*70)
print("DROP-OFF REASONS AT CONVERSATION TERMINATION")
print("="*70)
for r, cnt in sorted(drop_reasons_counter.items(), key=lambda x: x[1], reverse=True)[:10]:
    print(f"  {r}: {cnt} ({cnt/total_threads*100:.1f}%)")

# Save detailed thread stats
with open(r"c:\Users\danat\Desktop\stomchat\.agents\teamwork_preview_explorer_db_2\dialogue_depth_analysis.json", "w", encoding="utf-8") as f:
    json.dump({"threads": thread_stats, "depth_distribution": depth_buckets}, f, ensure_ascii=False, indent=2)

print("\nSaved dialogue_depth_analysis.json successfully.")
conn.close()
