# -*- coding: utf-8 -*-
"""
Юнит-тесты для модуля question_lifeline.py («Unanswered Question Lifeline»).
Проверяет эвристику кандидатов, таймеры, отмену при ответах врачей и лимиты.
"""

import asyncio
import datetime
import unittest

from question_lifeline import (
    is_clinical_question_candidate,
    QuestionLifelineManager
)


class TestQuestionLifeline(unittest.IsolatedAsyncioTestCase):

    def test_candidate_detection(self):
        # 1. Четкий клинический вопрос с вопросительным знаком
        q1 = "Коллеги, подскажите: при распломбировке МВ2 канала уперся в ступеньку на 14 мм. Чем лучше обойти?"
        self.assertTrue(is_clinical_question_candidate(q1))

        # 2. Клинический вопрос с фото/снимком без вопросительного знака
        q2 = "Поделитесь опытом по снимку зуба 4.6"
        self.assertTrue(is_clinical_question_candidate(q2, has_media=True))

        # 3. Бытовой флуд / приветствие
        self.assertFalse(is_clinical_question_candidate("Всем доброе утро и хорошей смены!"))

        # 4. Команда боту
        self.assertFalse(is_clinical_question_candidate("/calc 3.6 4.6"))

        # 5. Прямое обращение к боту (это задача ассистента, а не лайфлайна)
        self.assertFalse(is_clinical_question_candidate("@docendobot подскажи дозировку артикаина"))
        self.assertFalse(is_clinical_question_candidate("Бот, рассчитай анестезию для ребенка 20 кг"))

        # 6. Сообщение от другого бота
        self.assertFalse(is_clinical_question_candidate(q1, sender_is_bot=True))

    async def test_lifeline_cancellation_on_reply(self):
        """Проверяет, что таймер отменяется, если коллега ответил на вопрос."""
        triggered = []

        async def fake_send(chat_id, reply_to_msg_id, text):
            triggered.append((chat_id, reply_to_msg_id, text))

        mgr = QuestionLifelineManager(
            delay_seconds=2,  # 2 секунды для быстрого теста
            send_message_callback=fake_send
        )

        # Доктор 101 задает вопрос в чате 777
        pq = mgr.track_question(
            msg_id=5001,
            chat_id=777,
            sender_id=101,
            sender_name="Дмитрий",
            text="Коллеги, чем закрываете перфорации в области фуркации моляров?"
        )
        self.assertIsNotNone(pq)
        self.assertIn(5001, mgr.pending_questions)

        # Через 0.5 сек Доктор 102 отвечает в чат на этот вопрос
        await asyncio.sleep(0.5)
        mgr.on_human_message(chat_id=777, sender_id=102, reply_to_msg_id=5001)

        # Ждем завершения времени таймера (2 сек)
        await asyncio.sleep(2.0)

        # Лайфлайн НЕ должен был сработать!
        self.assertEqual(len(triggered), 0)
        self.assertNotIn(5001, mgr.pending_questions)

    async def test_lifeline_trigger_when_no_reply(self):
        """Проверяет, что лайфлайн срабатывает, если никто не ответил за отведенное время."""
        triggered = []

        async def fake_send(chat_id, reply_to_msg_id, text):
            triggered.append((chat_id, reply_to_msg_id, text))

        async def fake_llm(prompt, *args, **kwargs):
            return type("Resp", (), {"text": "Коллега, в подобной развилке ключевое — микроскоп и тонкие файлы.\n\n💬 <i>Коллеги, кто сталкивался?</i>"})(), None

        mgr = QuestionLifelineManager(
            delay_seconds=1,  # 1 секунда для теста
            send_message_callback=fake_send,
            llm_caller=fake_llm
        )

        pq = mgr.track_question(
            msg_id=5002,
            chat_id=888,
            sender_id=201,
            sender_name="Алексей",
            text="Коллеги, как поступить при отломе кончика файла #15 в апексе 2.1?"
        )
        self.assertIsNotNone(pq)

        # Никто не отвечает, ждем 1.5 сек
        await asyncio.sleep(1.5)

        # Лайфлайн должен был сработать
        self.assertEqual(len(triggered), 1)
        chat_id, reply_id, text = triggered[0]
        self.assertEqual(chat_id, 888)
        self.assertEqual(reply_id, 5002)
        self.assertIn("Коллеги", text)
        self.assertNotIn(5002, mgr.pending_questions)

    async def test_lifeline_rate_limiting(self):
        """Проверяет суточный лимит и кулдаун."""
        mgr = QuestionLifelineManager(
            delay_seconds=1,
            max_per_day=1,
            cooldown_seconds=3600
        )

        # Первый разрешен
        self.assertFalse(mgr.is_rate_limited(chat_id=999))

        # Имитируем отправку
        mgr.sent_lifelines.setdefault(999, []).append(datetime.datetime.now())

        # Второй сразу заблокирован по кулдауну и лимиту
        self.assertTrue(mgr.is_rate_limited(chat_id=999))

    async def test_unrelated_message_does_not_cancel(self):
        """Проверяет, что посторонний флуд другого доктора НЕ отменяет вопрос Доктора 1."""
        triggered = []

        async def fake_send(chat_id, reply_to_msg_id, text):
            triggered.append((chat_id, reply_to_msg_id, text))

        async def fake_llm(prompt, *args, **kwargs):
            return type("Resp", (), {"text": "Коллега, в подобной развилке ключевое — микроскоп."})(), None

        mgr = QuestionLifelineManager(
            delay_seconds=1,
            send_message_callback=fake_send,
            llm_caller=fake_llm
        )

        pq = mgr.track_question(
            msg_id=7001,
            chat_id=555,
            sender_id=10,
            sender_name="Иван",
            text="Коллеги, уступ на дистальной стенке 2.6, файл уперся в ступеньку на 16 мм, как обойти?"
        )
        self.assertIn(7001, mgr.pending_questions)

        # Другой участник пишет просто 'Доброе утро' без реплая и без упоминания Ивана
        mgr.on_human_message(chat_id=555, sender_id=20, reply_to_msg_id=None, text="Всем доброе утро!", sender_name="Ольга")

        # Вопрос Ивана НЕ должен отмениться!
        self.assertIn(7001, mgr.pending_questions)

        await asyncio.sleep(1.2)
        # Лайфлайн успешно сработал в помощь Ивану
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0][1], 7001)

    async def test_mention_cancels_question(self):
        """Проверяет, что упоминание автора вопроса отменяет таймер."""
        mgr = QuestionLifelineManager(delay_seconds=1)
        mgr.track_question(
            msg_id=7002,
            chat_id=555,
            sender_id=10,
            sender_name="Иван",
            text="Коллеги, как обойти ступеньку на 16 мм?"
        )
        self.assertIn(7002, mgr.pending_questions)

        # Другой участник обращается к Ивану в тексте
        mgr.on_human_message(chat_id=555, sender_id=20, reply_to_msg_id=None, text="Иван, попробуй C-Pilot 08 изогнутый", sender_name="Ольга")

        # Должен отмениться
        self.assertNotIn(7002, mgr.pending_questions)


if __name__ == "__main__":
    unittest.main()
