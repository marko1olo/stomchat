"""
Red Team Inquisitor Test Suite: Железобетонная персистентность истории и пайплайна ЛС.
Верифицирует:
1. Единую точку сохранения сообщений врача (ровно 1 раз, никаких дублей при кулдауне).
2. Гарантированное сохранение при has_media_intent (даже при падении скачивания или поврежденном файле).
3. Атомарное обогащение записи врача описанием снимка без дублирования.
4. Устойчивость _async_pm_supplement_job (не пишет в БД при ошибках Telegram, отменяется при context drift).
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Убеждаемся в utf-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import database
import assistant
import tg_safety


class TestPmPersistenceInquisitor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Очищаем тестовые сообщения перед тестом
        def clear_db():
            with database._connection() as db:
                db.execute("DELETE FROM pm_messages WHERE user_id = 999888777")
        await database._run_db(clear_db)

    async def asyncTearDown(self):
        def clear_db():
            with database._connection() as db:
                db.execute("DELETE FROM pm_messages WHERE user_id = 999888777")
        await database._run_db(clear_db)

    async def test_save_pm_message_returns_lastrowid_and_update_works(self):
        """Проверяем, что save_pm_message возвращает id, а update_pm_message_text корректно обновляет запись."""
        user_id = 999888777
        msg_id = await database.save_pm_message(user_id, "User", "[Прикреплён клинический снимок]")
        self.assertIsNotNone(msg_id)
        self.assertGreater(msg_id, 0)

        # Проверяем первичное сохранение
        history = await database.get_last_pm_messages(user_id, limit=5)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["text"], "[Прикреплён клинический снимок]")

        # Обновляем текст с описанием снимка
        enriched = "[Прикреплён клинический снимок: Зуб 4.6 PAI 4]\nВопрос врача: Что делать?"
        await database.update_pm_message_text(msg_id, enriched)

        # Проверяем, что запись обновилась на месте, а не добавилась вторая
        history_after = await database.get_last_pm_messages(user_id, limit=5)
        self.assertEqual(len(history_after), 1)
        self.assertEqual(history_after[0]["text"], enriched)

    async def test_no_duplicate_on_cooldown(self):
        """Проверяем, что при срабатывании кулдауна сообщение сохраняется ровно 1 раз."""
        user_id = 999888777
        chat_id = user_id

        # Создаем мок event
        mock_event = MagicMock()
        mock_event.chat_id = chat_id
        mock_event.sender_id = chat_id
        mock_event.message = MagicMock()
        mock_event.message.id = 1001
        mock_event.message.message = "Клинический вопрос про 3.6"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None

        mock_bot = MagicMock()

        # Имитируем активный кулдаун (cooldown_secs = 3)
        with patch("assistant.check_user_cooldown", side_effect=lambda cid, uid, key, seconds=5: 3 if key == "pm_chat" else 0), \
             patch("assistant.asyncio.sleep", new_callable=AsyncMock), \
             patch("assistant.generate_gemini_text_async", new_callable=AsyncMock, return_value=("Ответ консилиума", None)), \
             patch("assistant.tg_safety.send_message", new_callable=AsyncMock, return_value=tg_safety.TgOutcome(ok=True)), \
             patch("assistant.tg_safety.edit_message", new_callable=AsyncMock, return_value=tg_safety.TgOutcome(ok=True)):

            await assistant.handle_private_message(mock_bot, mock_event)

        # Проверяем записи в базе: должно быть ровно 1 сообщение от User и 1 от Assistant
        history = await database.get_last_pm_messages(user_id, limit=10)
        user_msgs = [m for m in history if m["sender_name"] == "User"]
        self.assertEqual(len(user_msgs), 1, f"Ожидалось ровно 1 сообщение User, получено {len(user_msgs)}: {user_msgs}")
        self.assertEqual(user_msgs[0]["text"], "Клинический вопрос про 3.6")

    async def test_media_saved_even_if_download_fails(self):
        """Проверяем, что входящее сообщение со снимком сохраняется даже при сбое скачивания файла."""
        user_id = 999888777
        chat_id = user_id

        mock_event = MagicMock()
        mock_event.chat_id = chat_id
        mock_event.sender_id = chat_id
        mock_event.message = MagicMock()
        mock_event.message.id = 1002
        mock_event.message.message = "Снимок прицельный 2.1"
        mock_event.message.photo = MagicMock()
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None

        # Имитируем падение скачивания медиа
        mock_event.message.download_media = AsyncMock(side_effect=asyncio.TimeoutError("Download timeout"))
        mock_bot = MagicMock()

        with patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant.generate_gemini_text_async", new_callable=AsyncMock, return_value=("Ответ консилиума по тексту", None)), \
             patch("assistant.tg_safety.send_message", new_callable=AsyncMock, return_value=tg_safety.TgOutcome(ok=True)), \
             patch("assistant.tg_safety.edit_message", new_callable=AsyncMock, return_value=tg_safety.TgOutcome(ok=True)):

            await assistant.handle_private_message(mock_bot, mock_event)

        # Проверяем, что вопрос врача НЕ потерян
        history = await database.get_last_pm_messages(user_id, limit=10)
        user_msgs = [m for m in history if m["sender_name"] == "User"]
        self.assertEqual(len(user_msgs), 1, f"Сообщение врача потеряно при сбое скачивания медиа: {history}")
        self.assertIn("Снимок прицельный 2.1", user_msgs[0]["text"])

    async def test_supplement_job_no_save_on_telegram_failure(self):
        """Проверяем, что фоновое дополнение не сохраняется в базу, если отправка в Telegram завершилась ошибкой."""
        user_id = 999888777
        chat_id = user_id
        req_id = 12345.0
        assistant._ACTIVE_PM_REQUESTS[chat_id] = req_id

        mock_bot = MagicMock()

        with patch("assistant.asyncio.sleep", new_callable=AsyncMock), \
             patch("assistant.generate_pm_supplement_async", new_callable=AsyncMock, return_value=("<ul><li>Важный клинический нюанс по дозировке</li></ul>", None)), \
             patch("assistant.check_response_quality", new_callable=AsyncMock, return_value=(True, "ok")), \
             patch("assistant.tg_safety.send_message", new_callable=AsyncMock, return_value=tg_safety.TgOutcome(ok=False, reason="flood_wait")):

            await assistant._async_pm_supplement_job(
                bot_client=mock_bot,
                chat_id=chat_id,
                user_question="Вопрос",
                initial_answer="Ответ",
                req_id=req_id
            )

        # В базе НЕ должно быть сообщения от Assistant, так как отправка упала
        history = await database.get_last_pm_messages(user_id, limit=10)
        assistant_msgs = [m for m in history if m["sender_name"] == "Assistant"]
        self.assertEqual(len(assistant_msgs), 0, f"Мусорное дополнение сохранилось в базу при сбое Telegram: {assistant_msgs}")

    async def test_supplement_job_aborted_on_context_drift(self):
        """Проверяем, что фоновое дополнение не отправляется, если пользователь задал новый вопрос во время проверки."""
        user_id = 999888777
        chat_id = user_id
        old_req_id = 100.0
        assistant._ACTIVE_PM_REQUESTS[chat_id] = old_req_id

        mock_bot = MagicMock()
        mock_send = AsyncMock(return_value=tg_safety.TgOutcome(ok=True))

        async def fake_check_quality(*args, **kwargs):
            # Во время работы рецензента приходит новый запрос от врача!
            assistant._ACTIVE_PM_REQUESTS[chat_id] = 200.0
            return True, "ok"

        with patch("assistant.asyncio.sleep", new_callable=AsyncMock), \
             patch("assistant.generate_pm_supplement_async", new_callable=AsyncMock, return_value=("<ul><li>Устаревший нюанс</li></ul>", None)), \
             patch("assistant.check_response_quality", side_effect=fake_check_quality), \
             patch("assistant.tg_safety.send_message", mock_send):

            await assistant._async_pm_supplement_job(
                bot_client=mock_bot,
                chat_id=chat_id,
                user_question="Старый вопрос",
                initial_answer="Старый ответ",
                req_id=old_req_id
            )

        # Отправка не должна была состояться!
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
