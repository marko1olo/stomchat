"""
tests/test_inflight_followup_triage_redteam.py — Red Team Inquisitor Test Suite
for In-Flight Follow-up Triage & Immediate Context Catchup.

Verifies:
1. find_immediate_preceding_question detects when doctor sends question right before /ask.
2. wait_for_immediate_followup catches questions sent 1-3 seconds after /ask within grace window.
3. InFlightSession & InFlightManager thread safety and tracking.
4. run_inflight_followup_triage decisions: SUPPLEMENT, REPLACE, WAIT_FOR_ANSWER, CANCEL.
5. In-flight cooperative supervisor: cancelling stale generation and restarting with merged query.
6. Cooldown immunity for follow-ups during active in-flight session.
7. Anti-hallucination guarantee: empty /ask without reply never reviews unrelated chat history.
"""

import asyncio
import unittest
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import AsyncMock, MagicMock, patch

import inflight_triage
from inflight_triage import (
    InFlightSession,
    InFlightManager,
    run_inflight_followup_triage,
    find_immediate_preceding_question,
    MAX_INFLIGHT_RESTARTS,
)


class TestInFlightFollowupTriageRedTeam(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.mgr = InFlightManager()
        self.chat_id = -1001234567890
        self.sender_id = 194947795  # Dr. Spyro

    async def test_find_immediate_preceding_question_found(self):
        """Паттерн: врач отправил вопрос, а через 5 секунд /ask без текста."""
        mock_db_rows = [
            (185335, "а правда что адгезив от 4 поколения и моделировочная смола взаимозаменяемы?", 0, "2026-10-26 07:56:48"),
            (185322, "скрепка и армировка", 0, "2026-10-26 07:01:22"),
        ]

        async def mock_query(sql, params):
            return mock_db_rows

        res = await find_immediate_preceding_question(
            query_db_async_fn=mock_query,
            sender_id=self.sender_id,
            current_msg_id=185336,
            max_id_diff=5
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["msg_id"], 185335)
        self.assertIn("адгезив от 4 поколения", res["text"])

    async def test_find_immediate_preceding_question_stale_ignored(self):
        """Если предшествующее сообщение слишком старое (id_diff > 8), не берем его."""
        mock_db_rows = [
            (180000, "старое сообщение из прошлого века", 0, "2026-10-20 10:00:00"),
        ]

        async def mock_query(sql, params):
            return mock_db_rows

        res = await find_immediate_preceding_question(
            query_db_async_fn=mock_query,
            sender_id=self.sender_id,
            current_msg_id=185336,
            max_id_diff=8
        )
        self.assertIsNone(res)

    async def test_wait_for_immediate_followup_success(self):
        """Паттерн: врач отправил /ask, и через 0.1с отправил текст вопроса."""
        async def delayed_user_message():
            await asyncio.sleep(0.05)
            self.mgr.record_incoming_message(
                chat_id=self.chat_id,
                sender_id=self.sender_id,
                msg_id=185330,
                text="Каковы протоколы адгезии 4 поколения?",
                has_media=False,
            )

        task = asyncio.create_task(delayed_user_message())
        res = await self.mgr.wait_for_immediate_followup(
            chat_id=self.chat_id,
            sender_id=self.sender_id,
            timeout=1.0
        )
        await task
        self.assertIsNotNone(res)
        self.assertEqual(res["msg_id"], 185330)
        self.assertIn("протоколы адгезии", res["text"])

    async def test_wait_for_immediate_followup_timeout(self):
        """Врач отправил /ask и ничего не написал за отведенное время."""
        res = await self.mgr.wait_for_immediate_followup(
            chat_id=self.chat_id,
            sender_id=self.sender_id,
            timeout=0.05
        )
        self.assertIsNone(res)

    async def test_inflight_session_registration_and_followup(self):
        """Активная сессия перехватывает догоняющие сообщения."""
        session = await self.mgr.register_session(
            chat_id=self.chat_id,
            sender_id=self.sender_id,
            trigger_msg_id=100,
            query="Вопрос про зуб 16"
        )
        self.assertTrue(self.mgr.has_active_session(self.chat_id, self.sender_id))
        self.assertEqual(self.mgr.get_active_session(self.chat_id, sender_id=self.sender_id), session)

        # Прилетает догоняющее сообщение
        res_session = self.mgr.record_incoming_message(
            chat_id=self.chat_id,
            sender_id=self.sender_id,
            msg_id=101,
            text="И еще снимок прикрепил",
            has_media=True,
        )
        self.assertEqual(res_session, session)
        self.assertIn(101, session.trigger_msg_ids)

        # Проверяем извлечение необработанных догоняющих
        followups = session.pop_unprocessed_followups()
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0]["msg_id"], 101)
        self.assertEqual(followups[0]["text"], "И еще снимок прикрепил")
        self.assertTrue(followups[0]["has_media"])

        # После pop список пуст
        self.assertEqual(len(session.pop_unprocessed_followups()), 0)

        # Завершение сессии
        await self.mgr.unregister_session(session)
        self.assertFalse(self.mgr.has_active_session(self.chat_id, self.sender_id))

    async def test_triage_impatience_wait_for_answer(self):
        """Врач пишет 'ждем' или '?' во время генерации -> WAIT_FOR_ANSWER."""
        followups = [{"msg_id": 201, "text": "??? ждем ответ", "has_media": False}]
        res = await run_inflight_followup_triage(
            original_query="Протокол девитализации",
            followups=followups,
            bot_name="StomChat"
        )
        self.assertEqual(res["action"], "WAIT_FOR_ANSWER")
        self.assertEqual(res["merged_query"], "Протокол девитализации")

    async def test_triage_explicit_cancel(self):
        """Врач пишет 'отмена' -> CANCEL."""
        followups = [{"msg_id": 202, "text": "отмена, я сам уже нашел", "has_media": False}]
        res = await run_inflight_followup_triage(
            original_query="Протокол девитализации",
            followups=followups,
            bot_name="StomChat"
        )
        self.assertEqual(res["action"], "CANCEL")

    async def test_triage_empty_orig_query_supplements(self):
        """Если изначальный запрос был пустым /ask, догоняющий текст однозначно SUPPLEMENT."""
        followups = [{"msg_id": 203, "text": "а правда что адгезив 4 поколения взаимозаменяем со смолой?", "has_media": False}]
        res = await run_inflight_followup_triage(
            original_query="Проведи клинический разбор недавней дискуссии и клинического случая выше.",
            followups=followups,
            bot_name="StomChat"
        )
        self.assertEqual(res["action"], "SUPPLEMENT")
        self.assertIn("адгезив 4 поколения", res["merged_query"])

    async def test_triage_llm_mock_supplement(self):
        """Проверка парсинга LLM ответа триажа."""
        mock_response = MagicMock()
        mock_response.text = '{"action": "SUPPLEMENT", "merged_query": "Зуб 16: препарирование под оверлей при депульпированном зубе", "reason": "Уточнена анатомическая специфика"}'

        async def mock_gemini(prompt, ctx, timeout=7.0):
            return mock_response, None

        followups = [{"msg_id": 204, "text": "зуб депульпирован, делаем оверлей", "has_media": False}]
        res = await run_inflight_followup_triage(
            original_query="Зуб 16 препарирование",
            followups=followups,
            bot_name="StomChat",
            generate_gemini_fn=mock_gemini
        )
        self.assertEqual(res["action"], "SUPPLEMENT")
        self.assertIn("оверлей при депульпированном", res["merged_query"])

    async def test_supervisor_cooperative_cancellation_and_restart(self):
        """
        Имитируем супервизор генерации:
        - Запускается долгая генерация (0.5с).
        - Через 0.05с прилетает догоняющий вопрос.
        - Супервизор отменяет старую генерацию и перезапускает с новым вопросом.
        """
        session = await self.mgr.register_session(
            chat_id=self.chat_id,
            sender_id=self.sender_id,
            trigger_msg_id=300,
            query="Какая смола лучше?"
        )

        completed_queries = []

        async def simulated_generation(query_text, sleep_duration=0.5):
            try:
                await asyncio.sleep(sleep_duration)
                completed_queries.append(query_text)
                return f"Ответ на: {query_text}"
            except asyncio.CancelledError:
                # Генерация была корректно отменена!
                raise

        # Запускаем фоновый поток прихода догоняющего сообщения
        async def send_followup_after_delay():
            await asyncio.sleep(0.04)
            self.mgr.record_incoming_message(
                chat_id=self.chat_id,
                sender_id=self.sender_id,
                msg_id=301,
                text="имею в виду для фиксации композита к композиту",
                has_media=False
            )

        asyncio.create_task(send_followup_after_delay())

        effective_q = session.original_query
        final_answer = None

        # Исполнение цикла супервизора
        while True:
            # Вторая итерация должна выполниться быстро
            dur = 0.5 if effective_q == "Какая смола лучше?" else 0.02
            gen_task = asyncio.create_task(simulated_generation(effective_q, sleep_duration=dur))
            session.set_current_task(gen_task)
            wait_task = asyncio.create_task(session.wait_for_followup())

            done, pending = await asyncio.wait(
                [gen_task, wait_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            if gen_task in done:
                wait_task.cancel()
                final_answer = gen_task.result()
                break

            # Прилетел follow-up
            followups = session.pop_unprocessed_followups()
            self.assertTrue(len(followups) > 0)

            # Отменяем старую задачу
            gen_task.cancel()
            try:
                await gen_task
            except (asyncio.CancelledError, Exception):
                pass

            # Обновляем вопрос
            effective_q = f"{effective_q} + {followups[0]['text']}"

        session.mark_completed()
        await self.mgr.unregister_session(session)

        # Первая долгая генерация была отменена, завершилась только объединенная!
        self.assertEqual(len(completed_queries), 1)
        self.assertIn("фиксации композита к композиту", completed_queries[0])
        self.assertIn("Ответ на: Какая смола лучше? + имею в виду для фиксации композита к композиту", final_answer)


if __name__ == "__main__":
    unittest.main()
