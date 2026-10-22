"""
RED TEAM INQUISITOR SUITE: Group Triage, Anti-Spam Loops, Cooldown Enforcer, Mention Safety.
100% Behavioral Proof of Invariants.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import config
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_redteam_")
config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")

TEST_CHAT_ID = -1001234567890
config.SOURCE_CHAT_ID = TEST_CHAT_ID

import database
import main
import assistant
assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
import tg_safety
import runtime_guard
from telethon.tl import types

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class MockSender:
    def __init__(self, sender_id, bot=False, username="doc"):
        self.id = sender_id
        self.bot = bot
        self.username = username
        self.first_name = "Коллега"
        self.last_name = "Врач"


class MockEvent:
    def __init__(self, msg_id, text, sender_id=555, reply_to_msg_id=None, reply_to_top_id=None, via_bot_id=None, sender=None):
        self.chat_id = TEST_CHAT_ID
        self.sender_id = sender_id
        self._sender = sender or MockSender(sender_id, bot=False)
        self.message = types.Message(
            id=msg_id,
            peer_id=types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
            from_id=types.PeerUser(sender_id),
            message=text,
            date=datetime.now(timezone.utc),
            via_bot_id=via_bot_id,
        )
        if reply_to_msg_id or reply_to_top_id:
            reply_header = types.MessageReplyHeader(
                reply_to_msg_id=reply_to_msg_id or 1000,
                reply_to_top_id=reply_to_top_id,
            )
            self.message.reply_to = reply_header
        self.client = main.bot_client

    async def get_sender(self):
        return self._sender

    async def reply(self, *a, **kw):
        pass


async def run_tests():
    await database.init_db()

    print("\n--- VECTOR 1: BOT-TO-BOT SPAM LOOP DEFENSE ---")
    # 1.1. Чужой бот из DEFAULT_KNOWN_BOT_IDS
    for bot_id in (7886475630, 8140417978, 7971556097, 7475073041, 8775535374):
        ev = MockEvent(1001, "@stomchat_bot дай совет", sender_id=bot_id)
        is_bot = tg_safety.is_bot_sender(bot_id, event=ev)
        check(f"is_bot_sender опознал бота ID={bot_id}", is_bot is True)

        res = await assistant.check_and_trigger_assistant(main.bot_client, ev, 1001, "@stomchat_bot совет", None)
        check(f"check_and_trigger_assistant отклонил бота ID={bot_id}", res is False)

        mention_res = await assistant.check_bot_mention_trigger(main.bot_client, ev, 1001, "бот, ответь мне")
        check(f"check_bot_mention_trigger отклонил бота ID={bot_id}", mention_res is False)

    # 1.2. Бот через via_bot_id (инлайн-бот)
    ev_via = MockEvent(1002, "какое-то сообщение", sender_id=123456, via_bot_id=999999)
    check("is_bot_sender отсекает сообщения via_bot_id", tg_safety.is_bot_sender(123456, event=ev_via) is True)

    # 1.3. Бот, цитирующий сообщение нашего бота (Reply to Bot)
    ev_reply_bot = MockEvent(1003, "Ответ на реплику бота", sender_id=8140417978, reply_to_msg_id=500)
    res_loop = await assistant.check_and_trigger_assistant(main.bot_client, ev_reply_bot, 1003, "Ответ на реплику", 500)
    check("Спам-петля пресечена: бот, цитирующий бота, не получает ответ", res_loop is False)

    print("\n--- VECTOR 2: COOLDOWNS & BURST DISCUSSIONS ---")
    # 2.1. check_bot_mention_trigger per-user debounce
    assistant.USER_COOLDOWNS.clear()
    ev_doc1 = MockEvent(2001, "бот, подскажи по протоколу", sender_id=98765)
    # Первый вызов через кулдаун
    cd_init = assistant.check_user_cooldown(TEST_CHAT_ID, 98765, "bot_mention_user", seconds=15)
    check("Первый вызов mention_user разрешён", cd_init == 0)
    cd_rapid = assistant.check_user_cooldown(TEST_CHAT_ID, 98765, "bot_mention_user", seconds=15)
    check("Второй вызов mention_user заблокирован кулдауном", cd_rapid > 0)

    # 2.2. Forum Topic Cooldown
    ev_topic = MockEvent(2002, "текст в топике", sender_id=98765, reply_to_top_id=444)
    top_id = tg_safety.extract_topic_id(ev_topic)
    check("extract_topic_id правильно извлёк ID топика форума", top_id == 444)

    cd_top1 = assistant.check_user_cooldown(TEST_CHAT_ID, top_id, "bot_mention_topic", seconds=10)
    check("Первое упоминание в топике разрешено", cd_top1 == 0)
    cd_top2 = assistant.check_user_cooldown(TEST_CHAT_ID, top_id, "bot_mention_topic", seconds=10)
    check("Второе упоминание в том же топике заблокировано", cd_top2 > 0)

    # 2.3. Sequential Follow-up Stalking Prevention
    # Записываем, что бот только что ответил врачу 555
    assistant.record_author_group_reply(555)
    check("Авторский кулдаун активен после ответа", assistant.is_author_group_cooldown_active(555) is True)

    # Врач пишет реплику без вопроса в чат ("пациент ушел домой, наблюдаем")
    state = assistant.load_state()
    state["last_case_author_id"] = 555
    state["last_case_time"] = datetime.now().isoformat()
    assistant.save_state(state)

    ev_doc_casual = MockEvent(2003, "пациент ушел домой, наблюдаем", sender_id=555)
    casual_reply = await assistant.check_and_trigger_assistant(
        main.bot_client, ev_doc_casual, 2003, "пациент ушел домой, наблюдаем", reply_to_msg_id=None
    )
    check("Бот НЕ встревает в пассивную реплику автора кейса при активном кулдауне", casual_reply is False)

    print("\n--- VECTOR 3: REGEX PURIFICATION & FALSE SILENCE EVASION ---")
    # 3.1. ReDoS иммунитет на экстремально длинных текстах
    adversarial_text = "бот " * 2000
    t0 = time.perf_counter()
    has_critique = bool(assistant._BOT_PERF_CRITIQUE_RE.search(adversarial_text))
    t_perf = time.perf_counter() - t0
    check(f"_BOT_PERF_CRITIQUE_RE выполнился мгновенно ({t_perf*1000:.2f}ms)", t_perf < 0.05)

    t0 = time.perf_counter()
    has_bot_word = bool(assistant._BOT_WORD_RE.search(adversarial_text))
    t_word = time.perf_counter() - t0
    check(f"_BOT_WORD_RE выполнился мгновенно ({t_word*1000:.2f}ms)", t_word < 0.05 and has_bot_word is True)

    # 3.2. Ложная тишина исключена
    ev_dummy = MockEvent(3001, "клинический пост", sender_id=777)
    check(
        "Фраза 'координатор отделения' не активирует тишину",
        await assistant.check_and_apply_silence(ev_dummy, "координатор отделения согласовал дату операции", None) is False
    )
    check(
        "Фраза 'пациент душный' не активирует тишину",
        await assistant.check_and_apply_silence(ev_dummy, "пациент очень душный, придирается к цвету пломбы", None) is False
    )
    check(
        "Фраза 'в кабинете душно' не активирует тишину",
        await assistant.check_and_apply_silence(ev_dummy, "в кабинете душно, кондиционер сломался", None) is False
    )
    check("Бот остался активным (не заглушен)", not assistant.is_silenced(assistant.load_state()))


try:
    asyncio.run(run_tests())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nTOTAL RED TEAM CHECKS: PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("FAILED CHECKS: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
