# -*- coding: utf-8 -*-
"""
Комплексный набор тестов UI/UX и архитектурных инвариантов для:
1. Интерактивных квизов в ЛС (quiz:generate, quiz:ans, quiz:done, /quiz).
2. Очистки и отсутствия утечки quiz_config в user_interactive_states.
3. Хранилища интерактивных викторин poll_storage (interactive_quizzes).
4. Защиты от повторных кликов и накликивания статистики (anti-double-click guard).
5. Оформления карточки кейса, обновления кнопок (✅/❌) и клинического EBM-разбора.
6. Оформления вечернего разбора и закрытия опросов консилиума в чате (/poll).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import poll_engine
import poll_storage


class TestQuizPollUX(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_stomat.db")
        import config
        self._orig_db_path = getattr(config, "DB_PATH", "stomat_bot.db")
        config.DB_PATH = self.db_path
        database.DB_PATH = self.db_path
        await poll_storage.init_poll_tables(self.db_path)
        # Инициализируем схемы базы данных StomChat
        def _init_raw():
            with database._connection() as db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_interactive_states (
                        user_id INTEGER PRIMARY KEY,
                        state_type TEXT,
                        current_step INTEGER,
                        case_id TEXT,
                        history TEXT
                    )
                    """
                )
        await database._run_db(_init_raw)

    async def asyncTearDown(self):
        try:
            import config
            config.DB_PATH = self._orig_db_path
            database.DB_PATH = self._orig_db_path
            database._DB_EXECUTOR.shutdown(wait=True)
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # =========================================================================
    # 1. ТЕСТЫ ОЧИСТКИ И ИЗОЛЯЦИИ user_interactive_states
    # =========================================================================

    async def test_cleanup_stale_quiz_configs_eliminates_leak(self):
        """Проверка очистки отрицательных user_id (quiz_config) из user_interactive_states."""
        now = time.time()
        # 1. Заброшенный квиз без ответов старше 2 часов -> должен удалиться
        stale_quiz_hist = json.dumps({
            "votes": [0, 0, 0, 0],
            "voters": {},
            "created_at": now - 7500,
            "last_updated": now - 7500,
            "question": "Вопрос?",
            "topic": "Эндодонтия"
        }, ensure_ascii=False)
        await database.set_user_interactive_state(
            user_id=-10001,
            state_type="quiz_config",
            current_step=1,
            case_id="Разбор",
            history=stale_quiz_hist
        )

        # 2. Завершенный квиз старше 10 минут -> должен удалиться
        completed_quiz_hist = json.dumps({
            "votes": [1, 0, 0, 0],
            "voters": {"999": 0},
            "created_at": now - 3600,
            "last_updated": now - 700,
            "is_completed": True,
            "question": "Вопрос 2?",
            "topic": "Ортопедия"
        }, ensure_ascii=False)
        await database.set_user_interactive_state(
            user_id=-10002,
            state_type="quiz_config",
            current_step=0,
            case_id="Разбор 2",
            history=completed_quiz_hist
        )

        # 3. Активный свежий квиз (создан 5 минут назад) -> должен остаться
        fresh_quiz_hist = json.dumps({
            "votes": [0, 0, 0, 0],
            "voters": {},
            "created_at": now - 300,
            "last_updated": now - 300,
            "question": "Свежий вопрос?",
            "topic": "Хирургия"
        }, ensure_ascii=False)
        await database.set_user_interactive_state(
            user_id=-10003,
            state_type="quiz_config",
            current_step=2,
            case_id="Свежий разбор",
            history=fresh_quiz_hist
        )

        # 4. Активный клинический симулятор врача (/case) -> должен остаться нетронутым!
        doctor_case_hist = json.dumps({
            "last_updated": now - 1800,
            "step": 2
        })
        await database.set_user_interactive_state(
            user_id=7770011,
            state_type="case",
            current_step=2,
            case_id="endo_perio",
            history=doctor_case_hist
        )

        # Запускаем очистку
        deleted = await database.cleanup_stale_interactive_states(max_age_seconds=3600, max_quiz_age_seconds=7200)
        self.assertEqual(deleted, 2, "Должно быть удалено ровно 2 устаревших квиза")

        # Проверяем оставшиеся строки
        q1 = await database.get_user_interactive_state(-10001)
        self.assertIsNone(q1, "Заброшенный квиз должен быть удален")

        q2 = await database.get_user_interactive_state(-10002)
        self.assertIsNone(q2, "Завершенный квиз должен быть удален")

        q3 = await database.get_user_interactive_state(-10003)
        self.assertIsNotNone(q3, "Свежий активный квиз должен сохраниться")

        doc = await database.get_user_interactive_state(7770011)
        self.assertIsNotNone(doc, "Сессия врача /case не должна затрагиваться очисткой квизов")
        self.assertEqual(doc["state_type"], "case")

    # =========================================================================
    # 2. ТЕСТЫ СПЕЦИАЛИЗИРОВАННОГО ХРАНИЛИЩА poll_storage (interactive_quizzes)
    # =========================================================================

    async def test_interactive_quiz_storage_lifecycle(self):
        """Проверка сохранения, извлечения и завершения интерактивных викторин в poll_storage."""
        quiz_id = -555123456
        options = [
            "Апикальная микрохирургия",
            "Ортоградная ревизия",
            "Гемисекция",
            "Удаление"
        ]
        ok = await poll_storage.save_interactive_quiz(
            quiz_id=quiz_id,
            chat_id=12345,
            user_id=777,
            quiz_type="pm",
            topic="Эндодонтия",
            question="Случай с очагом деструкции 3.6...",
            options=options,
            correct_option_id=1,
            explanation="Ортоградная ревизия — первоочередной метод.",
            history={"step": 1},
            db_path=self.db_path
        )
        self.assertTrue(ok)

        # Извлечение
        stored = await poll_storage.get_interactive_quiz(quiz_id, db_path=self.db_path)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["id"], quiz_id)
        self.assertEqual(stored["topic"], "Эндодонтия")
        self.assertEqual(len(stored["options"]), 4)
        self.assertEqual(stored["options"][1], "Ортоградная ревизия")
        self.assertEqual(stored["correct_option_id"], 1)
        self.assertFalse(stored["is_completed"])

        # Пометка о завершении
        mark_ok = await poll_storage.mark_interactive_quiz_completed(quiz_id, db_path=self.db_path)
        self.assertTrue(mark_ok)

        stored_after = await poll_storage.get_interactive_quiz(quiz_id, db_path=self.db_path)
        self.assertTrue(stored_after["is_completed"])

    async def test_anti_double_click_protection_and_stats_purity(self):
        """Защита от повторного клика и накликивания статистики."""
        quiz_id = -888777666
        user_id = 999888
        user_name = "Д-р Смирнов"

        await poll_storage.save_interactive_quiz(
            quiz_id=quiz_id,
            chat_id=user_id,
            user_id=user_id,
            quiz_type="pm",
            topic="Ортопедия",
            question="Препарирование под e.max CAD...",
            options=["Круговой уступ", "Сглаженный chamfer", "Прямой уступ", "Без уступа"],
            correct_option_id=1,
            explanation="Для дисиликата лития рекомендован сглаженный chamfer.",
            db_path=self.db_path
        )

        # 1. Первый голос: врач выбирает неверный ответ 0
        v1 = await poll_storage.record_vote(
            poll_id=quiz_id,
            user_id=user_id,
            user_name=user_name,
            selected_option=0,
            is_correct=False,
            allow_update=False,
            db_path=self.db_path
        )
        self.assertTrue(v1, "Первый ответ должен быть успешно зафиксирован")

        has_voted = await poll_storage.has_user_voted(quiz_id, user_id, db_path=self.db_path)
        self.assertTrue(has_voted, "has_user_voted должен возвращать True после первого голоса")

        # 2. Повторный клик: врач пытается переголосовать на правильный ответ 1
        v2 = await poll_storage.record_vote(
            poll_id=quiz_id,
            user_id=user_id,
            user_name=user_name,
            selected_option=1,
            is_correct=True,
            allow_update=False,
            db_path=self.db_path
        )
        self.assertFalse(v2, "Повторный клик должен быть отклонен (allow_update=False)")

        # 3. Проверка чистоты статистики: ответ остался 0, is_correct=False, накрутка не прошла
        user_stats = await poll_storage.get_user_quiz_stats(user_id, db_path=self.db_path)
        self.assertEqual(user_stats["total_answers"], 1, "Всего ответов должно быть строго 1")
        self.assertEqual(user_stats["correct_answers"], 0, "Правильных ответов не должно прибавиться при читерском клике")
        self.assertEqual(user_stats["accuracy_percent"], 0.0)

    # =========================================================================
    # 3. ТЕСТЫ UI/UX КАРТОЧКИ КВИЗА В ЛС И ОБНОВЛЕНИЯ КНОПОК (✅ / ❌)
    # =========================================================================

    async def test_pm_quiz_card_and_button_feedback_ui(self):
        """
        Проверка преобразования кнопок и карточки при ответе:
        - Кнопка правильного ответа подсвечивается галочкой ✅
        - Кнопка ошибочного выбора врача подсвечивается крестиком ❌
        - Callback_data кнопок вариантов блокируется роутом quiz:done:{quiz_id}
        - В тексте сообщения сохраняются формулировка вопроса, все варианты и EBM-обоснование.
        """
        import assistant

        quiz_id = -1234567890
        options = [
            "Апикальная микрохирургия (резекция)",
            "Повторное эндодонтическое лечение",
            "Гемисекция корня зуба",
            "Удаление с имплантацией"
        ]
        correct_idx = 1  # B
        clicked_idx = 0  # Врач выбрал A (ошибка)
        question = "Пациент с очагом деструкции 3.6..."
        explanation = "Ортоградная ревизия — первоочередной метод выбора при проходимых каналах."

        # Сохраняем квиз
        await poll_storage.save_interactive_quiz(
            quiz_id=quiz_id,
            chat_id=123,
            user_id=123,
            quiz_type="pm",
            topic="Эндодонтия",
            question=question,
            options=options,
            correct_option_id=correct_idx,
            explanation=explanation,
            db_path=self.db_path
        )
        await database.set_user_interactive_state(
            user_id=quiz_id,
            state_type="quiz_config",
            current_step=correct_idx,
            case_id=explanation,
            history=json.dumps({"question": question, "topic": "Эндодонтия", "options": options}, ensure_ascii=False)
        )

        # Мокаем Telethon Event
        event = MagicMock()
        event.chat_id = 123
        event.sender_id = 123
        event.data = f"quiz:ans:{correct_idx}:{clicked_idx}:{quiz_id}".encode("utf-8")
        event.answer = AsyncMock()

        sender_mock = MagicMock()
        sender_mock.first_name = "Д-р Иванов"
        event.get_sender = AsyncMock(return_value=sender_mock)

        captured_msg = None
        captured_buttons = None

        async def fake_edit_callback_message(client, ev, text, log_label, buttons=None, parse_mode='html'):
            nonlocal captured_msg, captured_buttons
            captured_msg = text
            captured_buttons = buttons

        original_edit = assistant.edit_callback_message
        assistant.edit_callback_message = fake_edit_callback_message

        try:
            # Вызываем обработчик колбэков
            await assistant.handle_callback_query(MagicMock(), event)
        finally:
            assistant.edit_callback_message = original_edit

        # 1. Проверяем карточку вопроса
        self.assertIsNotNone(captured_msg, "Сообщение должно быть отредактировано")
        self.assertIn("Клинический квиз [Эндодонтия]", captured_msg)
        self.assertIn(question, captured_msg, "Текст вопроса должен сохраняться в карточке")
        self.assertIn("❌ <b>НЕВЕРНО!</b>", captured_msg, "Должен быть вердикт об ошибке")
        self.assertIn("Ваш выбор: <b>A</b> | Правильный ответ: <b>B</b>", captured_msg)
        self.assertIn("💡 <b>Клиническое обоснование (EBM):</b>", captured_msg)
        self.assertIn(explanation, captured_msg)

        # 2. Проверяем кнопки
        self.assertIsNotNone(captured_buttons, "Кнопки должны обновиться")
        # Ряд 0: A (выбран ошибочно, крестик) и B (правильный, галочка)
        row0 = captured_buttons[0]
        btn_a_text = row0[0].text
        btn_b_text = row0[1].text
        self.assertTrue(btn_a_text.startswith("❌ A:"), f"Ошибочный ответ должен быть с крестиком: {btn_a_text}")
        self.assertTrue(btn_b_text.startswith("✅ B:"), f"Правильный ответ должен быть с галочкой: {btn_b_text}")

        # Проверяем блокировку кликов на кнопках вариантов: роут quiz:done:{quiz_id}
        self.assertEqual(row0[0].data.decode(), f"quiz:done:{quiz_id}")
        self.assertEqual(row0[1].data.decode(), f"quiz:done:{quiz_id}")

        # Ряд 1: C и D (нейтральные)
        row1 = captured_buttons[1]
        self.assertTrue(row1[0].text.startswith("C:"))
        self.assertTrue(row1[1].text.startswith("D:"))
        self.assertEqual(row1[0].data.decode(), f"quiz:done:{quiz_id}")
        self.assertEqual(row1[1].data.decode(), f"quiz:done:{quiz_id}")

        # Ряд 2: Кнопка следующего вопроса
        row2 = captured_buttons[2]
        self.assertEqual(row2[0].text, "🎲 Следующий вопрос")
        self.assertEqual(row2[0].data.decode(), "quiz:generate")

        # 3. Проверка роута quiz:done (при повторном клике на заблокированную кнопку)
        event_done = MagicMock()
        event_done.data = f"quiz:done:{quiz_id}".encode("utf-8")
        event_done.answer = AsyncMock()
        await assistant.handle_callback_query(MagicMock(), event_done)
        event_done.answer.assert_called_with("ℹ️ Вы уже ответили на этот вопрос!", alert=False)

    # =========================================================================
    # 4. ТЕСТЫ ОФОРМЛЕНИЯ ВЕЧЕРНЕГО РАЗБОРА И ЗАКРЫТИЯ ОПРОСОВ (/poll)
    # =========================================================================

    def test_evening_poll_debrief_formatting_full_context(self):
        """Проверка полного и понятного оформления клинического разбора при закрытии опроса."""
        debrief = poll_engine.generate_evening_poll_debrief(
            explanation_deep="При сепарации инструмента в апикальной трети первичной консервативной тактикой является обход (Bypass).",
            discussion_seed_question="Кто сталкивался с отломом NiTi в искривленном мезиальном канале — как выходите из ситуации?",
            vote_stats={
                "total_votes": 24,
                "poll_type": "quiz",
                "accuracy_percent": 62.5
            },
            question="Какова первоочередная тактика при сепарации NiTi-файла?",
            correct_option="Попытка обхода отломка (Bypass)",
            topic="Эндодонтия",
            poll_type="quiz"
        )

        self.assertIn("🏁 <b>Клинический разбор кейса [Эндодонтия]:</b>", debrief)
        self.assertIn("📋 <b>Кейс:</b> <i>Какова первоочередная тактика при сепарации NiTi-файла?</i>", debrief)
        self.assertIn("✅ <b>Эталонная тактика:</b> <i>Попытка обхода отломка (Bypass)</i>", debrief)
        self.assertIn("📊 <i>Итоги викторины: проголосовало 24 врачей, верный ответ выбрали 62.5%.</i>", debrief)
        self.assertIn("💡 <b>Клиническое обоснование (EBM):</b>", debrief)
        self.assertIn("При сепарации инструмента в апикальной трети", debrief)
        self.assertIn("💬 <b>Клинический вопрос к чату:</b>", debrief)
        self.assertIn("Кто сталкивался с отломом NiTi в искривленном мезиальном канале", debrief)


if __name__ == "__main__":
    unittest.main()
