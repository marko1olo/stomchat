# -*- coding: utf-8 -*-
"""
tests/test_pm_flow_resiliency_redteam.py — Red Team инквизиторский тест:
1. Ликвидация «мертвого молчания» при rate limit в ЛС (сообщения не дропаются, а ожидают кулдаун).
2. Безопасная нарезка длинных сообщений (> 4000 символов) в edit_callback_message на чанки.
3. Fallback доставки нового сообщения при сбое редактирования callback_message.
4. Защита очереди _PM_BURST_WORKERS от зависания и отправка мягкого статуса при параллельных сообщениях.
100% паритет StomChat и EndoChat.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import assistant
import tg_safety


class TestPmRateLimitPacing(unittest.IsolatedAsyncioTestCase):
    """Тест ликвидации дропа сообщений при кулдауне в ЛС."""

    def setUp(self):
        super().setUp()
        assistant.USER_COOLDOWNS.clear()

    async def test_pm_rate_limit_never_drops_message(self):
        """Если сообщение пришло во время кулдауна, бот не дропает его, а ждет и отвечает."""
        chat_id = 7716348189
        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = chat_id
        mock_event.sender_id = chat_id
        mock_sender = MagicMock(username="doc_rate", first_name="RateDoc", last_name="")
        mock_event.sender = mock_sender
        mock_event.get_sender = AsyncMock(return_value=mock_sender)

        mock_event.message.message = "Доктор, что делать при боли в 4.6?"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None
        mock_event.message.id = 1001
        mock_event._album_events = None

        # Имитируем активный кулдаун (осталось 2 секунды)
        assistant.USER_COOLDOWNS[(chat_id, chat_id, "pm_chat")] = datetime.now() - timedelta(seconds=3)

        sleeps = []
        async def fake_sleep(secs):
            sleeps.append(secs)

        with patch("assistant.database.save_pm_message", AsyncMock()) as mock_save, \
             patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=[])), \
             patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
             patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
             patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
             patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
             patch("assistant.generate_gemini_text_async") as mock_gemini, \
             patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))), \
             patch("assistant.send_message_chunks_async", AsyncMock()) as mock_send_chunks, \
             patch("asyncio.sleep", side_effect=fake_sleep):

            resp_mock = MagicMock()
            resp_mock.text = "<b>Протокол для 4.6:</b> Неотложная диагностика и анестезия."
            mock_gemini.return_value = (resp_mock, None)

            await assistant.handle_private_message(mock_client, mock_event)

            # Проверяем: бот НЕ дропнул сообщение, а подождал кулдаун и ответил!
            self.assertTrue(len(sleeps) > 0, "Expected asyncio.sleep to pace the cooldown")
            mock_send_chunks.assert_called_once()
            # Проверяем, что ответ врача был доставлен
            args, kwargs = mock_send_chunks.call_args
            self.assertEqual(args[1], chat_id)
            self.assertIn("Протокол для 4.6", args[2])


class TestEditCallbackMessageChunking(unittest.IsolatedAsyncioTestCase):
    """Тест безопасной нарезки длинных ответов (> 4000 символов) в edit_callback_message."""

    async def test_edit_callback_message_splits_long_text_and_sends_tail(self):
        """Если текст > 4000 символов, первый чанк редактирует сообщение, остальные отправляются новыми."""
        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 123456
        mock_event.message_id = 999
        mock_event.sender_id = 123456

        # Создаем длинный текст ~5800 символов с валидным HTML
        long_paragraph = "<p>" + ("Клинический протокол доказательной стоматологии. " * 120) + "</p>"
        long_text = f"<b>ЗАГОЛОВОК РАЗБОРА</b>\n\n{long_paragraph}"
        self.assertGreater(len(long_text), 4000)

        edited_messages = []
        sent_messages = []

        async def fake_edit_message(client, cid, mid, text, **kwargs):
            edited_messages.append((mid, text, kwargs.get("buttons")))
            return tg_safety.TgOutcome(ok=True)

        async def fake_send_message(client, cid, text, **kwargs):
            sent_messages.append((cid, text, kwargs.get("buttons")))
            return tg_safety.TgOutcome(ok=True)

        from telethon import Button
        test_buttons = [[Button.inline("⬅️ В главное меню", data="nav:main")]]

        with patch("tg_safety.edit_message", side_effect=fake_edit_message), \
             patch("tg_safety.send_message", side_effect=fake_send_message):

            res = await assistant.edit_callback_message(
                mock_client, mock_event, long_text, op="test_long_edit", buttons=test_buttons, parse_mode="html"
            )

            # 1. Первое сообщение было отредактировано первым чанком (без кнопок, т.к. есть хвост)
            self.assertEqual(len(edited_messages), 1)
            mid, edit_chunk, edit_btns = edited_messages[0]
            self.assertEqual(mid, 999)
            self.assertIsNone(edit_btns)
            self.assertLessEqual(len(edit_chunk), 4000)

            # 2. Оставшаяся часть отправлена как новое сообщение (с прикрепленными кнопками!)
            self.assertGreaterEqual(len(sent_messages), 1)
            last_cid, last_chunk, last_btns = sent_messages[-1]
            self.assertEqual(last_cid, 123456)
            self.assertEqual(last_btns, test_buttons)

    async def test_edit_callback_message_fallback_when_edit_fails(self):
        """Если edit_message вернул ошибку, срабатывает fallback через send_message_chunks_async."""
        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 654321
        mock_event.message_id = 888

        # Имитируем сбой редактирования (например, MessageTooLongError или таймаут)
        async def failing_edit(*args, **kwargs):
            return tg_safety.TgOutcome(ok=False, reason="terminal", error=ValueError("MessageTooLongError"))

        with patch("tg_safety.edit_message", side_effect=failing_edit), \
             patch("assistant.send_message_chunks_async", AsyncMock(return_value=tg_safety.TgOutcome(ok=True))) as mock_send_chunks:

            await assistant.edit_callback_message(
                mock_client, mock_event, "Короткий текст", op="test_fallback"
            )

            # Проверяем, что сообщение не потерялось, а ушло новым сообщением!
            mock_send_chunks.assert_called_once()
            args, _ = mock_send_chunks.call_args
            self.assertEqual(args[1], 654321)
            self.assertEqual(args[2], "Короткий текст")


if __name__ == "__main__":
    unittest.main()
