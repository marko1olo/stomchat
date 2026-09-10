#!/usr/bin/env python3
"""
Stress-test dialogue freshness window expansion against:
1. Fast clinical chatter (high velocity).
2. Spam burst (flood attack / sticker storm).
3. Stale reply after topical drift (long time, few messages vs many messages, short time).
4. Concurrent conversations / interleaved replies.
5. Inconsistency between Diff 3 proposal and actual assistant.py logic.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass
class ChatMessage:
    msg_id: int
    sender_id: int
    text: str
    reply_to_msg_id: int | None
    date: datetime

def simulate_dialogue_staleness_check(
    messages: list[ChatMessage],
    reply_msg: ChatMessage,
    nearest_bot_msg: ChatMessage,
    config_direct_stale: int = 25,
    config_sequential_stale: int = 12,
    config_max_minutes: float = 90.0,
    is_direct_parent_bot: bool = True
):
    """
    Simulates the logic proposed in Diff 3 vs current assistant.py.
    """
    ref_id = nearest_bot_msg.msg_id
    
    # Messages between ref_id and reply_msg.msg_id
    msgs_since = [m for m in messages if ref_id < m.msg_id < reply_msg.msg_id]
    count_since = len(msgs_since)
    
    # Time elapsed
    elapsed_minutes = (reply_msg.date - nearest_bot_msg.date).total_seconds() / 60.0
    
    # 1. Baseline logic (assistant.py currently)
    max_allowed_msgs_current = 25 if is_direct_parent_bot else 5
    max_allowed_min_current = 45.0 if is_direct_parent_bot else 15.0
    
    current_allowed = (count_since <= max_allowed_msgs_current) and (elapsed_minutes <= max_allowed_min_current)
    
    # 2. Diff 3 logic as written in REPORT_CHAT_BALANCE_AND_LOGS.md:
    is_direct_quote_reply = bool(reply_msg.reply_to_msg_id)
    diff3_max_msgs = config_direct_stale if is_direct_quote_reply else config_sequential_stale
    diff3_allowed_msgs = count_since <= diff3_max_msgs
    diff3_allowed_time = elapsed_minutes <= config_max_minutes
    diff3_allowed = diff3_allowed_msgs and diff3_allowed_time
    
    return {
        "count_since": count_since,
        "elapsed_min": elapsed_minutes,
        "current_allowed": current_allowed,
        "diff3_allowed": diff3_allowed,
    }

def run_dialogue_stress_scenarios():
    print("=== TEST DIALOGUE FRESHNESS UNDER ADVERSARIAL SCENARIOS ===")
    
    t0 = datetime(2026, 9, 8, 12, 0, 0)
    bot_msg = ChatMessage(msg_id=100, sender_id=999, text="Рекомендую эндодонтический доступ с сохранением перицервикального дентина.", reply_to_msg_id=99, date=t0)
    
    # Scenario A: Fast clinical chatter (26 messages in 4 minutes)
    # A doctor carefully reads bot's advice and answers at t0 + 4 min, but 26 short messages passed
    msgs_a = [bot_msg]
    for i in range(1, 27):
        msgs_a.append(ChatMessage(msg_id=100 + i, sender_id=200 + (i % 5), text=f"Коллеги, а что по клммеру {i}?", reply_to_msg_id=None, date=t0 + timedelta(seconds=i*9)))
    doctor_reply_a = ChatMessage(msg_id=127, sender_id=101, text="Понял, а какую ультразвуковую насадку лучше взять для перешейка?", reply_to_msg_id=100, date=t0 + timedelta(minutes=4))
    
    res_a = simulate_dialogue_staleness_check(msgs_a, doctor_reply_a, bot_msg)
    print(f"Scenario A (26 msgs in 4 min, direct quote):")
    print(f"  count_since={res_a['count_since']}, elapsed={res_a['elapsed_min']:.1f}m")
    print(f"  Current allowed: {res_a['current_allowed']} | Diff3 allowed: {res_a['diff3_allowed']}")
    print(f"  Vulnerability: At count_since=26, EVEN Diff3 (limit 25) drops the doctor! Window still too tight for busy bursts!")

    # Scenario B: Spam burst (50 sticker / junk messages in 30 seconds)
    msgs_b = [bot_msg]
    for i in range(1, 51):
        msgs_b.append(ChatMessage(msg_id=100 + i, sender_id=666, text="🔥", reply_to_msg_id=None, date=t0 + timedelta(seconds=i*0.5)))
    doctor_reply_b = ChatMessage(msg_id=151, sender_id=101, text="Спасибо! А файл какой конусности?", reply_to_msg_id=100, date=t0 + timedelta(minutes=1))
    res_b = simulate_dialogue_staleness_check(msgs_b, doctor_reply_b, bot_msg)
    print(f"\nScenario B (50 spam stickers in 30s):")
    print(f"  count_since={res_b['count_since']}, elapsed={res_b['elapsed_min']:.1f}m")
    print(f"  Current allowed: {res_b['current_allowed']} | Diff3 allowed: {res_b['diff3_allowed']}")
    print(f"  Vulnerability: A spammer flood of 50 stickers blinds the bot to legitimate direct replies because count_since counts ALL messages blindly!")

    # Scenario C: Topic drift over 85 minutes with only 10 messages (Quiet night, topic changed to beer)
    msgs_c = [bot_msg]
    for i in range(1, 11):
        msgs_c.append(ChatMessage(msg_id=100 + i, sender_id=300 + i, text=f"Кто где пиво пьет в пятницу?", reply_to_msg_id=None, date=t0 + timedelta(minutes=i*8)))
    # Someone replies to bot from 85 minutes ago
    doctor_reply_c = ChatMessage(msg_id=111, sender_id=101, text="Ага, согласен", reply_to_msg_id=100, date=t0 + timedelta(minutes=85))
    res_c = simulate_dialogue_staleness_check(msgs_c, doctor_reply_c, bot_msg)
    print(f"\nScenario C (85 min elapsed, 10 messages, topic moved to off-topic):")
    print(f"  count_since={res_c['count_since']}, elapsed={res_c['elapsed_min']:.1f}m")
    print(f"  Current allowed: {res_c['current_allowed']} | Diff3 allowed: {res_c['diff3_allowed']}")
    print(f"  Vulnerability: Under Diff 3 (90 min window), the bot wakes up 85 minutes later to say 'Ага, согласен' into an off-topic chat room!")

    # Scenario D: Doctor replies to another doctor in thread where bot spoke
    doctor_reply_d = ChatMessage(msg_id=115, sender_id=101, text="Коллега, вы не правы", reply_to_msg_id=105, date=t0 + timedelta(minutes=5))
    res_d = simulate_dialogue_staleness_check(msgs_c, doctor_reply_d, bot_msg, is_direct_parent_bot=False)
    print(f"\nScenario D (Reply to another human in thread where bot participated):")
    print(f"  is_direct_parent_bot=False, reply_to_msg_id=105 (human)")
    print(f"  Current allowed: {res_d['current_allowed']} (limit 5) | Diff3 allowed: {res_d['diff3_allowed']} (limit 25!)")
    print(f"  Vulnerability in Diff 3: bool(reply_to_msg_id) is True, so Diff 3 treats human-to-human reply as direct bot reply (limit 25), hijacking human dialogue!")

if __name__ == "__main__":
    run_dialogue_stress_scenarios()
