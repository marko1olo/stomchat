# -*- coding: utf-8 -*-
"""
tests/test_pm_bundling_and_dialogue_inquisitor.py — Red Team Inquisitor audit tests:
1. Интеллектуальный debounce-бандлинг ЛС:
   - Ликвидация race condition при раздельной отправке текста и фото (chat_id=779642660).
   - Детекция предвещающих медиа реплик («Оцени пломбировку», «Посмотри снимок»).
   - Удержание окна 3.5с тишины и запрет ложного busy-hint статуса во время debouncing.
2. Предотвращение амнезии диалога в ЛС (chat_id=325898674):
   - Классификация ответов врача («Достал») на прямые вопросы бота как DIALOGUE_ANSWER.
   - Формирование диалогового фокуса с сохранением клинического кейса (МБ2, фрагмент).
   - Запрет роботизированных отказов («уточнить, что означает, реплика не содержит данных»).
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
import main


class TestPMBundlingDebounceInquisitor(unittest.IsolatedAsyncioTestCase):
    """Тестирование сборщика пакета ЛС и защиты от race condition."""

    def test_media_anticipating_detection(self):
        """Проверка детекции реплик, предвещающих отправку снимка/медиа."""
        positive_cases = [
            "Оцени пломбировку",
            "Посмотри снимок",
            "Вот фото",
            "Скинул КТ",
            "Глянь 3.6",
            "Оцени реставрацию",
            "Посмотри прилегание коронки",
            "Оцени каналы",
            "Прикрепляю снимок зуба 4.6",
            "Взгляни на снимок",
            "Что скажешь по кт?",
        ]
        for text in positive_cases:
            self.assertTrue(
                assistant.is_pm_media_anticipating_text(text),
                f"Expected '{text}' to be recognized as media-anticipating"
            )

        negative_cases = [
            "Как разводить ЭДТА?",
            "Какая дозировка артикаина у детей?",
            "Что делать при боли в зубе?",
            "Достал",
            "Спасибо за помощь",
            "Приветствую, коллега",
        ]
        for text in negative_cases:
            self.assertFalse(
                assistant.is_pm_media_anticipating_text(text),
                f"Expected '{text}' to NOT be media-anticipating"
            )

    def test_has_pm_media_no_text(self):
        """Проверка распознавания медиа без сопроводительного текста."""
        ev_photo_no_text = MagicMock()
        ev_photo_no_text.message.photo = object()
        ev_photo_no_text.message.video = None
        ev_photo_no_text.message.voice = None
        ev_photo_no_text.message.audio = None
        ev_photo_no_text.message.video_note = None
        ev_photo_no_text.message.document = None
        ev_photo_no_text.message.message = ""
        self.assertTrue(assistant.has_pm_media_no_text(ev_photo_no_text))

        ev_photo_with_text = MagicMock()
        ev_photo_with_text.message.photo = object()
        ev_photo_with_text.message.video = None
        ev_photo_with_text.message.voice = None
        ev_photo_with_text.message.audio = None
        ev_photo_with_text.message.video_note = None
        ev_photo_with_text.message.document = None
        ev_photo_with_text.message.message = "Зуб 2.6"
        self.assertFalse(assistant.has_pm_media_no_text(ev_photo_with_text))

    async def test_debounce_bundles_text_and_photo(self):
        """Сценарий chat_id=779642660: Врач пишет 'Оцени пломбировку' и через 1.5с отправляет снимок."""
        chat_id = 779642660

        # Очищаем состояние очередей для теста
        main._PM_PENDING_QUEUES.pop(chat_id, None)
        main._PM_BURST_WORKERS.pop(chat_id, None)
        main._PM_LAST_ARRIVAL.pop(chat_id, None)
        main._PM_ACTIVE_GENERATION.discard(chat_id)

        # Событие 1: Текст 'Оцени пломбировку'
        ev_text = MagicMock()
        ev_text.chat_id = chat_id
        ev_text.is_private = True
        ev_text.message.message = "Оцени пломбировку"
        ev_text.message.photo = None
        ev_text.message.video = None
        ev_text.message.voice = None
        ev_text.message.audio = None
        ev_text.message.video_note = None
        ev_text.message.document = None

        # Событие 2: Снимок
        ev_photo = MagicMock()
        ev_photo.chat_id = chat_id
        ev_photo.is_private = True
        ev_photo.message.message = ""
        ev_photo.message.photo = object()
        ev_photo.message.video = None
        ev_photo.message.voice = None
        ev_photo.message.audio = None
        ev_photo.message.video_note = None
        ev_photo.message.document = None

        bundled_calls = []

        async def fake_bundle(bot_client, burst):
            bundled_calls.append(list(burst))

        async def fake_single(bot_client, event):
            bundled_calls.append([event])

        with patch("assistant.handle_private_message_bundle", side_effect=fake_bundle) as mock_bundle, \
             patch("assistant.handle_private_message", side_effect=fake_single) as mock_single:

            # 1. Приходит первое сообщение
            await main.handle_private_message(ev_text)

            # Проверяем: воркер запущен, в очереди 1 сообщение
            self.assertIn(chat_id, main._PM_PENDING_QUEUES)
            self.assertEqual(len(main._PM_PENDING_QUEUES[chat_id]), 1)
            self.assertIn(chat_id, main._PM_BURST_WORKERS)

            # Бот НЕ находится в режиме активной генерации LLM (он в окне дебаунса)
            self.assertNotIn(chat_id, main._PM_ACTIVE_GENERATION)

            # 2. Через 0.5 секунды имитируем приход снимка
            await asyncio.sleep(0.5)
            await main.handle_private_message(ev_photo)

            # В очереди теперь 2 сообщения!
            self.assertEqual(len(main._PM_PENDING_QUEUES[chat_id]), 2)

            # 3. Дожидаемся завершения воркера бандла
            worker = main._PM_BURST_WORKERS.get(chat_id)
            if worker:
                await worker

            # 4. Проверяем результат: оба сообщения ушли в handle_private_message_bundle ВМЕСТЕ!
            self.assertEqual(len(bundled_calls), 1, "Expected exactly 1 bundled call")
            burst_result = bundled_calls[0]
            self.assertEqual(len(burst_result), 2)
            self.assertIs(burst_result[0], ev_text)
            self.assertIs(burst_result[1], ev_photo)


class TestPMDialogueContextInquisitor(unittest.IsolatedAsyncioTestCase):
    """Тестирование сохранения диалогового контекста и ликвидации амнезии в ЛС."""

    def test_classify_pm_dialogue_turn(self):
        """Проверка классификации ответа врача на вопрос бота."""
        now = datetime.now()

        # История диалога по кейсу chat_id=325898674
        history = [
            {
                "sender_name": "User",
                "text": "Сломался инструмент в МБ2 зуба 2.6, обломок около 2.5 мм в апикальной трети",
                "date": (now - timedelta(minutes=20)).isoformat(),
            },
            {
                "sender_name": "Assistant",
                "text": "Что в итоге с фрагментом в МБ2 — удалось извлечь, байпасировать до рабочей длины или решили оставить?",
                "date": (now - timedelta(minutes=5)).isoformat(),
            }
        ]

        # 1. Ответ врача одним словом: 'Достал'
        turn, last_q = assistant.classify_pm_dialogue_turn("Достал", history, now)
        self.assertEqual(turn, "DIALOGUE_ANSWER")
        self.assertIn("Что в итоге с фрагментом в МБ2", last_q)

        # 2. Другие варианты ответов на вопрос
        for reply in ["Извлекли", "Сделал байпас", "Оставил", "Обошли до рабочей длины", "Не получилось", "Да"]:
            turn_r, _ = assistant.classify_pm_dialogue_turn(reply, history, now)
            self.assertEqual(turn_r, "DIALOGUE_ANSWER", f"Failed for '{reply}'")

        # 3. Уточняющий вопрос врача (follow-up)
        turn_q, _ = assistant.classify_pm_dialogue_turn("А на какой скорости ультразвуком работать?", history, now)
        self.assertEqual(turn_q, "DIALOGUE_FOLLOWUP")

        # 4. Новый клинический кейс (не должен смешиваться)
        turn_new, _ = assistant.classify_pm_dialogue_turn(
            "Пациент 30 лет, жалобы на острые боли в 4.6 при накусывании. Перкуссия резко болезненная.",
            history, now
        )
        self.assertEqual(turn_new, "STANDALONE_QUERY")

    async def test_pm_prompt_contains_dialogue_continuity_block(self):
        """Проверка сборки промпта при ответе 'Достал' на вопрос бота."""
        chat_id = 325898674
        mock_client = AsyncMock()

        # Настраиваем mock action
        class MockActionContext:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        mock_client.action = MagicMock(return_value=MockActionContext())

        mock_event = MagicMock()
        mock_event.chat_id = chat_id
        mock_event.sender_id = chat_id
        mock_event.message.message = "Достал"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None
        mock_event.message.id = 5555

        now = datetime.now()
        fake_db_history = [
            {
                "sender_name": "User",
                "text": "Сломался инструмент в МБ2 зуба 2.6, обломок около 2.5 мм в апикальной трети",
                "date": (now - timedelta(minutes=20)).isoformat(),
            },
            {
                "sender_name": "Assistant",
                "text": "Что в итоге с фрагментом в МБ2 — удалось извлечь, байпасировать до рабочей длины или решили оставить?",
                "date": (now - timedelta(minutes=5)).isoformat(),
            },
            {
                "sender_name": "User",
                "text": "Достал",
                "date": now.isoformat(),
            }
        ]

        captured_prompts = []

        async def fake_gemini(prompt, status_ctx=None, timeout=None):
            captured_prompts.append(prompt)
            resp = MagicMock()
            resp.text = "<b>Отличный результат, коллега!</b> Извлечение фрагмента из МБ2 сохраняет анатомию корня. Контроль: RG со штифтом."
            return resp, None

        with patch("assistant.database.save_pm_message", AsyncMock(return_value=101)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=fake_db_history)), \
             patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
             patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
             patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
             patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
             patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
             patch("assistant.generate_gemini_text_async", side_effect=fake_gemini), \
             patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))), \
             patch("assistant.send_message_chunks_async", AsyncMock()), \
             patch("assistant._async_pm_supplement_job", AsyncMock()):

            await assistant.handle_private_message(mock_client, mock_event)

            self.assertTrue(len(captured_prompts) > 0, "Expected generate_gemini_text_async to be called")
            final_prompt = captured_prompts[-1]

            # Проверяем, что промпт содержит явный блок преемственности диалога:
            self.assertIn("[АКТИВНЫЙ ДИАЛОГ: ПРЯМОЙ ОТВЕТ ВРАЧА НА ТВОЙ ВОПРОС / РЕПЛИКУ]", final_prompt)
            self.assertIn("Что в итоге с фрагментом в МБ2", final_prompt)
            self.assertIn("«Достал»", final_prompt)

            # Проверяем, что в промпте ЕСТЬ запрет требовать повторного ввода данных
            self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:", final_prompt)
            self.assertIn("Требовать от врача повторного ввода данных", final_prompt)

            # Проверяем, что в правилах истории закреплена непрерывность активного диалога
            self.assertIn("НЕПРЕРЫВНОСТЬ АКТИВНОГО ДИАЛОГА", final_prompt)


if __name__ == "__main__":
    unittest.main()
