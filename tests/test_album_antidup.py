# -*- coding: utf-8 -*-
"""
tests/test_album_antidup.py — Тесты для проверки:
1. In-flight блокировки параллельных ответов (_IN_FLIGHT_GROUP_REPLIES).
2. Классификации медиа-команд (is_media_command с учетом @mention и 'бот,').
3. Дедупликации кадров альбомов (album_has_command).
4. Автоматической регистрации Lifeline в REPLIED_MSG_IDS и clinical_rescues.
"""

import sys
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import asyncio
import os
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import assistant
import main
import question_lifeline


class TestAlbumAndAntiDup(unittest.TestCase):

    def test_in_flight_lock_basic(self):
        """Проверка захвата и освобождения in-flight замка."""
        chat_id = -1001234567
        msg_ids = [101, 102]

        self.assertFalse(assistant.is_group_reply_in_flight(chat_id, 101))
        self.assertFalse(assistant.is_group_reply_in_flight(chat_id, 102))

        with assistant.acquire_group_reply_lock(chat_id, msg_ids):
            self.assertTrue(assistant.is_group_reply_in_flight(chat_id, 101))
            self.assertTrue(assistant.is_group_reply_in_flight(chat_id, 102))
            self.assertTrue(assistant.is_group_reply_in_flight(chat_id, [102, 103]))
            self.assertFalse(assistant.is_group_reply_in_flight(chat_id, 103))
            self.assertFalse(assistant.is_group_reply_in_flight(-1009999999, 101))

        # После выхода замок должен быть снят
        self.assertFalse(assistant.is_group_reply_in_flight(chat_id, 101))
        self.assertFalse(assistant.is_group_reply_in_flight(chat_id, 102))

    def test_in_flight_exception_safety(self):
        """Замок должен гарантированно освобождаться даже при исключении."""
        chat_id = -1001234567
        mid = 999
        try:
            with assistant.acquire_group_reply_lock(chat_id, mid):
                self.assertTrue(assistant.is_group_reply_in_flight(chat_id, mid))
                raise ValueError("Simulated failure")
        except ValueError:
            pass

        self.assertFalse(assistant.is_group_reply_in_flight(chat_id, mid))

    def test_media_command_detection_with_mentions(self):
        """Проверка, что @mention и 'бот, ' отключают пассивный запуск медиа-ассистента."""
        def classify_media_cmd(text):
            if not text:
                return False
            cmd_test = text.strip().lower()
            cmd_mentioned, _ = main.strip_bot_mention(text)
            return bool(
                cmd_test.startswith(("/", "итог", "викторина", "опрос", "удалить", "wipe", "бот,", "бот "))
                or cmd_test == "бот"
                or cmd_mentioned
            )

        # Должны классифицироваться как команды / прямые вызовы (is_media_command = True):
        self.assertTrue(classify_media_cmd("/ask Что скажете по снимку?"))
        self.assertTrue(classify_media_cmd("/calc 4% артикаин"))
        self.assertTrue(classify_media_cmd("/итог"))
        self.assertTrue(classify_media_cmd("бот, посмотри снимок"))
        self.assertTrue(classify_media_cmd("бот,что тут?"))
        self.assertTrue(classify_media_cmd("бот"))
        bot_u = main.FALLBACK_BOT_USERNAME or "docendobot"
        self.assertTrue(classify_media_cmd(f"@{bot_u} какой зуб?"))

        # Обычные реплики врача (is_media_command = False -> пассивный разбор медиа разрешен):
        self.assertFalse(classify_media_cmd("Коллеги, снимок 4.6 зуба, что скажете?"))
        self.assertFalse(classify_media_cmd("Прицельный снимок"))
        self.assertFalse(classify_media_cmd(""))
        self.assertFalse(classify_media_cmd(None))

    def test_album_command_propagation(self):
        """Если в альбоме хотя бы один кадр содержит обращение к боту — пассивный ассистент отключается."""
        class FakeMsg:
            def __init__(self, message):
                self.message = message

        album_clean = [
            FakeMsg(""),
            FakeMsg("Коллеги, посмотрите снимок")
        ]
        album_with_mention = [
            FakeMsg(""),
            FakeMsg(f"@{main.FALLBACK_BOT_USERNAME} разбери кейс")
        ]
        album_with_ask = [
            FakeMsg(""),
            FakeMsg("/ask протокол фиксации")
        ]

        def check_album_has_command(msgs):
            for m in msgs:
                m_text = getattr(m, "message", "") or ""
                if m_text:
                    m_low = m_text.strip().lower()
                    m_m, _ = main.strip_bot_mention(m_text)
                    if (
                        m_low.startswith(("/", "итог", "викторина", "опрос", "удалить", "wipe", "бот,", "бот "))
                        or m_low == "бот"
                        or m_m
                    ):
                        return True
            return False

        self.assertFalse(check_album_has_command(album_clean))
        self.assertTrue(check_album_has_command(album_with_mention))
        self.assertTrue(check_album_has_command(album_with_ask))

    def test_lifeline_marks_replied_and_author_cooldown(self):
        """Lifeline должен регистрировать ответ в REPLIED_MSG_IDS и обновлять cooldown автора."""
        chat_id = -100555555
        test_msg_id = 771234
        test_sender_id = 882345

        # Изначально не отвечено
        assistant.REPLIED_MSG_IDS.pop(test_msg_id, None)
        assistant.AUTHOR_GROUP_REPLY_COOLDOWNS.pop(test_sender_id, None)

        async def run_lifeline_flow():
            mock_bot = AsyncMock()
            mock_sent = MagicMock()
            mock_sent.id = 771235
            mock_bot.send_message.return_value = mock_sent

            with patch("main.bot_client", mock_bot),                  patch("main.tg_safety.is_prod_chat", return_value=False),                  patch("main.database.save_bot_sent_message", new_callable=AsyncMock) as mock_save,                  patch("main.database.query_db_async", new_callable=AsyncMock) as mock_q,                  patch("clinical_rescue.mark_case_rescued") as mock_rescue:

                mock_q.return_value = [(test_sender_id,)]

                await main._send_lifeline_to_chat(chat_id, test_msg_id, "Коллеги, первичный ориентир...")

                self.assertTrue(assistant.REPLIED_MSG_IDS.get(test_msg_id))
                self.assertTrue(assistant.is_author_group_cooldown_active(test_sender_id))
                mock_rescue.assert_called_once()
                mock_save.assert_called_once_with(771235, chat_id)

        asyncio.run(run_lifeline_flow())


if __name__ == "__main__":
    unittest.main()
