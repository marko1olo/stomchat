"""
test_pm_bundle.py — Тестирование сборщика пакета сообщений ЛС (handle_private_message_bundle)
и логики объединения медиа + текст + аудио.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import assistant
import media_tools


class TestPMBundle(unittest.IsolatedAsyncioTestCase):
    async def test_bundle_single_event_passes_directly(self):
        """Одиночное сообщение передается в handle_private_message без изменений."""
        mock_client = MagicMock()
        mock_event = MagicMock()
        mock_event.chat_id = 12345
        mock_event.message.message = "Обычный вопрос"

        with patch("assistant.handle_private_message", new_callable=AsyncMock) as mock_handle:
            await assistant.handle_private_message_bundle(mock_client, [mock_event])
            mock_handle.assert_awaited_once_with(mock_client, mock_event)

    async def test_bundle_photo_and_text(self):
        """Пакет из фото + текстового вопроса объединяется в один запрос с сохранением фото."""
        mock_client = MagicMock()

        # Событие 1: Фото без подписи
        ev_photo = MagicMock()
        ev_photo.chat_id = 99999
        ev_photo.message.photo = object()
        ev_photo.message.video = None
        ev_photo.message.voice = None
        ev_photo.message.audio = None
        ev_photo.message.video_note = None
        ev_photo.message.document = None
        ev_photo.message.message = ""

        # Событие 2: Текст с вопросом
        ev_text = MagicMock()
        ev_text.chat_id = 99999
        ev_text.message.photo = None
        ev_text.message.video = None
        ev_text.message.voice = None
        ev_text.message.audio = None
        ev_text.message.video_note = None
        ev_text.message.document = None
        ev_text.message.message = "Какой прогноз для этого зуба?"

        with patch("assistant.handle_private_message", new_callable=AsyncMock) as mock_handle:
            await assistant.handle_private_message_bundle(mock_client, [ev_photo, ev_text])
            
            mock_handle.assert_awaited_once()
            called_client, target_event = mock_handle.call_args[0]
            
            self.assertIs(called_client, mock_client)
            self.assertIs(target_event, ev_photo)
            self.assertIsNotNone(target_event.message.photo)
            self.assertIn("Какой прогноз для этого зуба?", target_event.message.message)

    async def test_bundle_photo_and_voice(self):
        """Пакет из фото + голосового сообщения: голос распознается и объединяется со снимком."""
        mock_client = MagicMock()

        ev_photo = MagicMock()
        ev_photo.chat_id = 88888
        ev_photo.message.id = 101
        ev_photo.message.photo = object()
        ev_photo.message.video = None
        ev_photo.message.voice = None
        ev_photo.message.audio = None
        ev_photo.message.video_note = None
        ev_photo.message.document = None
        ev_photo.message.message = ""

        ev_voice = MagicMock()
        ev_voice.chat_id = 88888
        ev_voice.message.id = 102
        ev_voice.message.photo = None
        ev_voice.message.video = None
        ev_voice.message.voice = object()
        ev_voice.message.audio = None
        ev_voice.message.video_note = None
        ev_voice.message.document = None
        ev_voice.message.message = ""
        ev_voice.message.download_media = AsyncMock(return_value="/tmp/fake_voice.ogg")

        with patch("assistant.handle_private_message", new_callable=AsyncMock) as mock_handle, \
             patch("gemini_client.transcribe_audio_gemini_multimodal", new_callable=AsyncMock) as mock_gemini, \
             patch("blocking_tools.correct_dental_transcription_async", new_callable=AsyncMock) as mock_correct, \
             patch("os.path.exists", return_value=True), \
             patch("os.remove", return_value=None):
            
            mock_gemini.return_value = ("подскажи как пройти медиальный канал", None)
            mock_correct.return_value = "подскажи как пройти мезиальный канал"

            await assistant.handle_private_message_bundle(mock_client, [ev_photo, ev_voice])

            mock_handle.assert_awaited_once()
            _, target_event = mock_handle.call_args[0]
            self.assertIs(target_event, ev_photo)
            self.assertIn("мезиальный канал", target_event.message.message)
            self.assertIn("[Голосовое сообщение врача]", target_event.message.message)

    async def test_bundle_multi_photo_album(self):
        """Пакет из нескольких фото (альбом) собирает все события в target_event._album_events."""
        mock_client = MagicMock()

        def make_photo_ev(msg_id, text=""):
            ev = MagicMock()
            ev.chat_id = 77777
            ev.message.id = msg_id
            ev.message.photo = object()
            ev.message.video = None
            ev.message.voice = None
            ev.message.audio = None
            ev.message.video_note = None
            ev.message.document = None
            ev.message.message = text
            return ev

        ev1 = make_photo_ev(201, "Прицельный снимок 3.6")
        ev2 = make_photo_ev(202, "")
        ev3 = make_photo_ev(203, "И фото препарирования под вкладку")

        with patch("assistant.handle_private_message", new_callable=AsyncMock) as mock_handle:
            await assistant.handle_private_message_bundle(mock_client, [ev1, ev2, ev3])

            mock_handle.assert_awaited_once()
            _, target_event = mock_handle.call_args[0]
            self.assertIs(target_event, ev1)
            self.assertTrue(hasattr(target_event, "_album_events"))
            self.assertEqual(len(target_event._album_events), 3)
            self.assertEqual([e.message.id for e in target_event._album_events], [201, 202, 203])
            self.assertIn("Прицельный снимок 3.6", target_event.message.message)
            self.assertIn("фото препарирования", target_event.message.message)


if __name__ == "__main__":
    unittest.main()
