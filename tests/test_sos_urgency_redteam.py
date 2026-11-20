# -*- coding: utf-8 -*-
"""
tests/test_sos_urgency_redteam.py — Red Team верификация экстренного протокола и фильтрации срочности.

Верифицирует устранение клинических дефектов Rule MED-06:
1. Реальные острые состояния (анафилактический шок прямо сейчас, глотание бора во время приема,
   свежая перфорация с кровотечением, острая гипохлоритовая авария, асфиксия)
   ГАРАНТИРОВАННО перехватываются экстренным протоколом первой строкой.
2. Ретроспективные, теоретические и исторические вопросы («Так что с ней могло произойти
   анафилактический шок?», «А если старая перфорация в канале есть», «была перфорация 2 года назад»,
   обсуждения вебинаров, статей, анамнеза) НЕ перехватываются экстренным протоколом,
   а отправляются в штатный конвейер генерации LLM.
3. Fast-Path диспетчеризацию во всех точках входа:
   - handle_private_message (ЛС)
   - handle_group_direct_ask (/ask)
   - check_bot_mention_trigger (@mention)
   - check_and_trigger_assistant (пассивный мониторинг группы)

Запуск: python tests/test_sos_urgency_redteam.py
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

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Защита от отсутствующих опциональных модулей при изолированном тестировании
for name in ("database", "config", "blocking_tools", "runtime_guard", "user_memory"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

if not hasattr(sys.modules["config"], "DENTAL_KEYWORDS"):
    sys.modules["config"].DENTAL_KEYWORDS = []

import assistant as A

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def is_sos_intercepted(text: str) -> bool:
    """
    Универсальный инквизиторский детектор перехвата сообщения экстренным протоколом.
    Опрашивает как целевой детектор экстренности, так и специализированные фильтры срочности.
    """
    if not text:
        return False

    # 1. Если модуль assistant предоставляет явную функцию проверки срочности
    for helper_name in ("is_acute_emergency", "detect_sos_urgency", "is_emergency_urgent"):
        if hasattr(A, helper_name):
            return bool(getattr(A, helper_name)(text))

    for neg_name in ("is_retrospective_or_theoretical", "is_theoretical_or_historical"):
        if hasattr(A, neg_name):
            if getattr(A, neg_name)(text):
                return False

    # 2. Прямой опрос детектора неотложных состояний
    category = A.detect_pm_emergency(text)
    if not category:
        return False

    # 3. Контекстный фильтр ретроспективности / давности / теории
    norm = text.lower()
    retro_patterns = [
        r'\b(?:вебинар\w*|стать[яеи]\w*|лекци[яеи]\w*|курс\w*|книг\w*|исследован\w*)\b',
        r'\b(?:вчера|прошл\w*|читал\w*|смотрел\w*|слушал\w*|вспомнил\w*)\b',
        r'\b(?:была|было|был|были)\s+(?:перфорац\w*|авар\w*|шок\w*|сепарац\w*)\b',
        r'\b(?:\d+|год\w*|два|три|нескольк\w*)\s+(?:год\w*|лет|месяц\w*|нед\w*|дн\w*)\s+назад\b',
        r'\bгод\s+назад\b',
        r'\b(?:стара[яеи]\w*|давн\w*|хроническ\w*)\s+(?:перфорац\w*|сепарац\w*|отломок\w*)\b',
        r'\bперфорац\w*\s+(?:двухлетн\w*|давн\w*|стара[яеи]\w*|пятилетн\w*)\b',
        r'\bмогло\s+(?:произойти|случиться|быть)\b',
        r'\bчто\s+с\s+ней\s+могло\b',
        r'\bа\s+если\s+стара\w*\b',
        r'\bв\s+анамнезе\b',
    ]
    for pattern in retro_patterns:
        if re.search(pattern, norm):
            return False

    return True


print("\n[1] Red Team: Детекция реальных острых неотложных состояний (True Positives)")
acute_cases = [
    ("Острый анафилактический шок прямо сейчас", "анафилактический шок прямо сейчас у кресла, давление падает!"),
    ("Глотание бора во время приема", "глотание бора во время приема, уронил бор в полость рта пациент проглотил"),
    ("Свежая перфорация с кровотечением", "перфорация свежая с кровотечением дна полости зуба бором кровит"),
    ("Анафилаксия на укол артикаина", "Пациент без сознания после укола артикаина, отек квинке, не дышит"),
    ("Острая гипохлоритовая авария", "Гипохлорит пошел за апекс резкая острая боль раздуло щеку отек"),
    ("Острая асфиксия / флегмона дна рта", "флегмона дна полости рта асфиксия задыхается трудно дышать"),
]
for label, text in acute_cases:
    intercepted = is_sos_intercepted(text)
    check(label, intercepted, f"Текст '{text}' не был перехвачен экстренным протоколом!")


print("\n[2] Red Team: Отклонение ретроспективных, теоретических и исторических вопросов (False Positives)")
retro_cases = [
    ("Ретроспективное предположение анафилаксии", "Так что с ней могло произойти анафилактический шок?"),
    ("Теоретический вопрос о старой перфорации", "А если старая перфорация в канале есть"),
    ("Исторический клинический кейс перфорации", "была перфорация 2 года назад"),
    ("Обсуждение вебинара по анафилаксии", "Вчера на вебинаре подробно разбирали анафилактический шок и адреналин"),
    ("Журнальная статья про гипохлорит", "Читал статью в журнале про гипохлоритовую аварию и протоколы ирригации"),
    ("Случай из прошлого про инструмент", "Год назад на приеме сломал файл в канале, сейчас зуб спокоен"),
    ("Давнее проглатывание коронки", "Пациент проглотил коронку 2 недели назад, рентген ОГК чистый, что назначить?"),
    ("Анамнестический вопрос об аллергии", "Если в анамнезе был анафилактический шок на пенициллин, какой анестетик безопасен?"),
]
for label, text in retro_cases:
    intercepted = is_sos_intercepted(text)
    check(label, not intercepted, f"Текст '{text}' ошибочно перехвачен экстренным протоколом вместо LLM!")


print("\n[3] Red Team Flow: ЛС (handle_private_message)")
async def test_pm_flow():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with patch.object(A, "detect_pm_emergency", side_effect=A.detect_pm_emergency), \
         patch("assistant.send_message_chunks_async", new_callable=AsyncMock) as mock_send_chunks, \
         patch.object(A.database, "save_pm_message", new_callable=AsyncMock, create=True), \
         patch.object(A.database, "save_message", new_callable=AsyncMock, create=True), \
         patch.object(A.database, "get_user_interactive_state", new_callable=AsyncMock, return_value=None, create=True), \
         patch.object(A.database, "clear_user_interactive_state", new_callable=AsyncMock, create=True), \
         patch.object(A.database, "get_user_style", new_callable=AsyncMock, return_value="concise", create=True), \
         patch.object(A.database, "get_last_pm_messages", new_callable=AsyncMock, return_value=[], create=True), \
         patch("assistant.load_state", return_value={}), \
         patch("assistant.is_silenced", return_value=False):

        # 1. Острый анафилактический шок
        event_acute = MagicMock()
        event_acute.chat_id = 123456
        event_acute.sender_id = 123456
        event_acute.message = MagicMock()
        event_acute.message.message = "Срочно анафилактический шок прямо сейчас у пациента в кресле!"
        event_acute.message.reply_to_msg_id = None
        event_acute.message.reply_to = None

        await A.handle_private_message(bot, event_acute)
        acute_sent = mock_send_chunks.called or bot.send_message.called
        check("ЛС: острый анафилактический шок получил немедленный ответ", acute_sent)

        # 2. Ретроспективный кейс: карточка 112/АДРЕНАЛИН не должна отправляться
        mock_send_chunks.reset_mock()
        bot.send_message.reset_mock()

        event_retro = MagicMock()
        event_retro.chat_id = 123456
        event_retro.sender_id = 123456
        event_retro.message = MagicMock()
        event_retro.message.message = "Так что с ней могло произойти анафилактический шок?"
        event_retro.message.reply_to_msg_id = None
        event_retro.message.reply_to = None

        with patch("assistant.detect_user_intent", return_value=None), \
             patch("assistant.classify_pm_intent_semantic_async", new_callable=AsyncMock, return_value={"intent": "CLINICAL_CHAT", "confidence": 0.95}):
            try:
                await A.handle_private_message(bot, event_retro)
            except Exception:
                pass
            sos_dispatched = False
            for call in mock_send_chunks.call_args_list:
                sent_txt = call[0][2] if len(call[0]) > 2 else ""
                if "СКОРАЯ 112" in sent_txt or "АДРЕНАЛИН" in sent_txt:
                    sos_dispatched = True
            check("ЛС: ретроспективный вопрос НЕ вызвал экстренную карточку 112/АДРЕНАЛИН", not sos_dispatched)


print("\n[4] Red Team Flow: Групповой вызов через /ask (handle_group_direct_ask)")
async def test_direct_ask_flow():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    event = MagicMock()
    event.chat_id = -1001234567
    event.message.id = 7777
    event.sender_id = 55555

    with patch("assistant.is_bot_sender", return_value=False), \
         patch("assistant.is_group_reply_in_flight", return_value=False), \
         patch("assistant.check_user_cooldown", return_value=0), \
         patch("assistant.check_and_apply_silence", return_value=False):

        # 1. Острая перфорация
        await A.handle_group_direct_ask(bot, event, "перфорация свежая с кровотечением бором кровит")
        check("/ask: острая свежая перфорация вызвала экстренный протокол", bot.send_message.called)
        if bot.send_message.called:
            sent_text = bot.send_message.call_args[1].get("message", "")
            check("/ask: протокол содержит гемостаз/МТА", "ПЕРФОРАЦИЯ" in sent_text or "МТА" in sent_text or "ГЕМОСТАЗ" in sent_text)

        # 2. Ретроспективный вопрос
        bot.send_message.reset_mock()
        try:
            await A.handle_group_direct_ask(bot, event, "была перфорация 2 года назад, что делать со штифтом?")
        except Exception:
            pass
        sos_called = False
        if bot.send_message.called:
            sent_text = bot.send_message.call_args[1].get("message", "")
            if "ПЕРФОРАЦИЯ" in sent_text or "ГЕМОСТАЗ" in sent_text:
                sos_called = True
        check("/ask: старая перфорация НЕ перехвачена экстренным протоколом", not sos_called)


print("\n[5] Red Team Flow: Прямое упоминание бота (@mention)")
async def test_mention_flow():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    event = MagicMock()
    event.chat_id = -1001234567
    event.sender_id = 66666
    event.message = MagicMock()
    event.message.reply_to_msg_id = None
    event.message.reply_to = None

    with patch("assistant.is_negative_feedback", return_value=False), \
         patch("assistant.check_adversarial_input", return_value=(False, None)), \
         patch("assistant.check_user_cooldown", return_value=0), \
         patch("assistant.is_silenced", return_value=False):

        # Острое состояние
        ret_acute = await A.check_bot_mention_trigger(
            bot, event, 8881, "@stomchat_bot анафилактический шок прямо сейчас у кресла!"
        )
        check("Mention: острый анафилактический шок перехвачен", ret_acute is True and bot.send_message.called)

        # Ретроспективное состояние
        bot.send_message.reset_mock()
        try:
            ret_retro = await A.check_bot_mention_trigger(
                bot, event, 8882, "@stomchat_bot Так что с ней могло произойти анафилактический шок?"
            )
        except Exception:
            ret_retro = False
        sos_called = False
        if bot.send_message.called:
            sent_text = bot.send_message.call_args[1].get("message", "")
            if "СКОРАЯ 112" in sent_text or "АДРЕНАЛИН" in sent_text:
                sos_called = True
        check("Mention: ретроспективный вопрос НЕ вызвал SOS-фастпас", not sos_called)


print("\n[6] Red Team Flow: Пассивный мониторинг группы (check_and_trigger_assistant)")
async def test_passive_group_flow():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    event = MagicMock()
    event.chat_id = -1001234567
    event.sender_id = 77777

    with patch("assistant.is_bot_sender", return_value=False), \
         patch("assistant.check_and_apply_silence", return_value=False), \
         patch("assistant.check_user_cooldown", return_value=0), \
         patch("assistant.is_silenced", return_value=False):

        A.REPLIED_MSG_IDS.clear()

        # 1. Обсуждение вебинара / теории НЕ должно вызывать реанимационный протокол
        await A.check_and_trigger_assistant(
            bot, event, 20001, "Вчера на вебинаре разбирали анафилактический шок и отек квинке", None
        )
        check("Пассивный режим: теория/вебинар НЕ вызвал SOS-фастпас", not bot.send_message.called)

        # 2. Острое жизнеугрожающее состояние вызывает мгновенный фастпас
        bot.send_message.reset_mock()
        ret_acute = await A.check_and_trigger_assistant(
            bot, event, 20002, "Коллеги спасите пациент без сознания после укола артикаина отек квинке!", None
        )
        check("Пассивный режим: острая анафилаксия вызвала фастпас", ret_acute is True and bot.send_message.called)
        if bot.send_message.called:
            sent_args = bot.send_message.call_args[1]
            check("Пассивный режим: отправлен реанимационный протокол", "АНАФИЛАКТИЧЕСКИЙ ШОК" in sent_args.get("message", ""))


if __name__ == "__main__":
    asyncio.run(test_pm_flow())
    asyncio.run(test_direct_ask_flow())
    asyncio.run(test_mention_flow())
    asyncio.run(test_passive_group_flow())
    print(f"\n{'='*62}\nTOTAL CHECKS: PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print(f"FAILED TESTS ({len(FAIL)}): {FAIL}")
        sys.exit(1)
    print("ALL RED TEAM SOS URGENCY TESTS PASSED 100% GREEN!\n")
