"""
tests/test_emergency_group_fastpath.py — Верификация экстренного SOS фаст-паса в группах и валидатора
Запуск: python tests/test_emergency_group_fastpath.py
"""
import asyncio
import os
import re
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

for name in ("vision", "database", "config", "blocking_tools", "runtime_guard"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["config"].DENTAL_KEYWORDS = []
sys.modules["blocking_tools"].generate_gemini_text_async = None

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import assistant as A

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


print("\n[1] Верификация format_emergency_response с флагом is_group")
text_pm, btns_pm = A.format_emergency_response("anaphylaxis", is_group=False)
check("PM anaphylaxis text contains 112 & Adrenaline", "112" in text_pm and "АДРЕНАЛИН" in text_pm)
check("PM anaphylaxis has inline navigation buttons", any("nav:" in getattr(b, "data", b"").decode("utf-8", "ignore") for row in btns_pm for b in row))

text_grp, btns_grp = A.format_emergency_response("anaphylaxis", is_group=True)
check("Group anaphylaxis text matches clinical protocol", "112" in text_grp and "АДРЕНАЛИН" in text_grp)
check("Group anaphylaxis has NO nav:main inline callback buttons", not any("nav:main" in getattr(b, "data", b"").decode("utf-8", "ignore") for row in btns_grp for b in row))
if btns_grp:
    check("Group anaphylaxis has clean deep-link URL button", hasattr(btns_grp[0][0], "url") and "t.me" in btns_grp[0][0].url)

print("\n[2] Верификация детекции острых состояний (detect_pm_emergency)")
check("Детекция анафилаксии по артикаину", A.detect_pm_emergency("Пациент без сознания после укола артикаина, отек квинке") == "anaphylaxis")
check("Детекция гипохлоритовой аварии", A.detect_pm_emergency("Гипохлорит пошел за апекс, раздуло щеку") == "hypochlorite")
check("Детекция перфорации корня", A.detect_pm_emergency("Перфорация дна полости бором кровит") == "perforation")
check("Детекция сепарации инструмента", A.detect_pm_emergency("Сломал файл в канале за верхушку") == "file_fracture")
check("Детекция стеноза/флегмоны", A.detect_pm_emergency("Флегмона дна полости рта, асфиксия тяжело дышать") == "phlegmon_airway")
check("Обычный клинический вопрос не дает ложного срабатывания", A.detect_pm_emergency("Какими клампами изолировать нижние резцы при пришеечном кариесе?") is None)

print("\n[3] Верификация фаст-паса в handle_group_direct_ask (/ask)")
async def test_direct_ask():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    event = MagicMock()
    event.chat_id = -1001234567
    event.message.id = 8888
    event.sender_id = 111111

    with patch("assistant.is_bot_sender", return_value=False), \
         patch("assistant.is_group_reply_in_flight", return_value=False), \
         patch("assistant.check_user_cooldown", return_value=0), \
         patch("assistant.check_and_apply_silence", return_value=False):
        await A.handle_group_direct_ask(bot, event, "срочно анафилаксия на ультракаин, без сознания!")
        check("Direct ask отправил экстренное сообщение", bot.send_message.called)
        sent_args = bot.send_message.call_args[1]
        check("Direct ask отправил адреналиновый протокол", "АДРЕНАЛИН" in sent_args["message"])
        check("Direct ask указал правильный reply_to", sent_args["reply_to"] == 8888)

print("\n[4] Верификация фаст-паса в check_bot_mention_trigger (@docendobot)")
async def test_bot_mention():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    event = MagicMock()
    event.chat_id = -1001234567
    event.sender_id = 222222
    event.message = MagicMock()
    event.message.reply_to_msg_id = None
    event.message.reply_to = None

    with patch("assistant.is_negative_feedback", return_value=False), \
         patch("assistant.check_adversarial_input", return_value=(False, None)), \
         patch("assistant.check_user_cooldown", return_value=0), \
         patch("assistant.is_silenced", return_value=False):
        ret = await A.check_bot_mention_trigger(
            bot, event, 9999, "@docendobot гипохлорит за апекс раздуло щеку отек"
        )
        check("Bot mention вернул True на SOS фаст-пасе", ret is True)
        check("Bot mention отправил сообщение", bot.send_message.called)
        sent_args = bot.send_message.call_args[1]
        check("Bot mention отправил протокол гипохлоритовой аварии", "ГИПОХЛОРИТОВАЯ АВАРИЯ" in sent_args["message"])

print("\n[5] Верификация защиты от ложных срабатываний в пассивном режиме (check_and_trigger_assistant)")
async def test_passive_emergency():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    event = MagicMock()
    event.chat_id = -1001234567
    event.sender_id = 333333

    # Историческое/теоретическое обсуждение вебинара НЕ должно вызывать реанимационный протокол
    with patch("assistant.is_bot_sender", return_value=False), \
         patch("assistant.check_and_apply_silence", return_value=False), \
         patch("assistant.check_user_cooldown", return_value=0), \
         patch("assistant.is_silenced", return_value=False):
        A.REPLIED_MSG_IDS.clear()
        ret_webinar = await A.check_and_trigger_assistant(
            bot, event, 10001, "Вчера на вебинаре разбирали анафилактический шок и отек квинке", None
        )
        check("Теоретический вебинар НЕ вызвал экстренный фаст-пас", not bot.send_message.called)

        # Острое реальное состояние вызывает экстренный фаст-пас
        ret_acute = await A.check_and_trigger_assistant(
            bot, event, 10002, "Коллеги спасите пациент без сознания после укола артикаина отек квинке!", None
        )
        check("Острая анафилаксия в группе вызвала мгновенный фаст-пас", ret_acute is True)
        check("Сообщение отправлено", bot.send_message.called)
        sent_args = bot.send_message.call_args[1]
        check("Отправлен реанимационный протокол", "АНАФИЛАКТИЧЕСКИЙ ШОК" in sent_args["message"])

if __name__ == "__main__":
    asyncio.run(test_direct_ask())
    asyncio.run(test_bot_mention())
    asyncio.run(test_passive_emergency())
    print(f"\nTOTAL CHECKS: PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print(f"FAILED TESTS: {FAIL}")
        sys.exit(1)
    print("EMERGENCY GROUP FAST-PATH & VALIDATOR HARDENING VERIFICATION: 100% GREEN!\n")
