# -*- coding: utf-8 -*-
"""
test_reaction_engine.py — Тесты для батчевого нейросетевого движка реакций.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import reaction_engine


class TestBatchReactionEngine(unittest.TestCase):

    def test_parse_batch_reactions_json_valid(self):
        """Парсинг валидного JSON-массива вердиктов."""
        raw = '[{"msg_id": 101, "reaction": "🔥", "reason": "Крутой кейс"}, {"msg_id": 105, "reaction": "🤝", "reason": "Полезный совет"}]'
        results = reaction_engine.parse_batch_reactions_json(raw)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["msg_id"], 101)
        self.assertEqual(results[0]["reaction"], "🔥")
        self.assertEqual(results[1]["msg_id"], 105)
        self.assertEqual(results[1]["reaction"], "🤝")

    def test_parse_batch_reactions_json_fenced(self):
        """Парсинг JSON-массива в markdown блоке."""
        raw = '```json\n[{"msg_id": 202, "reaction": "😂", "reason": "Юмор"}]\n```'
        results = reaction_engine.parse_batch_reactions_json(raw)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["msg_id"], 202)
        self.assertEqual(results[0]["reaction"], "😂")

    def test_parse_batch_reactions_json_empty_or_null(self):
        """Пустой массив или null реакции."""
        self.assertEqual(reaction_engine.parse_batch_reactions_json("[]"), [])
        self.assertEqual(reaction_engine.parse_batch_reactions_json('[{"msg_id": 303, "reaction": null, "reason": "не надо"}]'), [])
        self.assertEqual(reaction_engine.parse_batch_reactions_json("мусор"), [])

    def test_build_batch_prompt(self):
        """Проверка структуры батчевого промпта."""
        messages = [
            reaction_engine.BufferedMessage(
                msg_id=1, chat_id=-100, sender_id=10, sender_name="Врач1",
                text="Кейс 16", has_media=True, timestamp=100.0, event=None
            ),
            reaction_engine.BufferedMessage(
                msg_id=2, chat_id=-100, sender_id=20, sender_name="Врач2",
                text="Отлично", has_media=False, timestamp=101.0, event=None
            )
        ]
        prompt = reaction_engine.build_batch_prompt(messages, "🔥 👍")
        self.assertIn("[#1] Врач1 (МЕДИА/СНИМОК): «Кейс 16»", prompt)
        self.assertIn("[#2] Врач2: «Отлично»", prompt)
        self.assertIn("НЕ БОЛЕЕ 2", prompt)
        self.assertIn("НИКОГДА не ставить легкомысленные или радостные реакции", prompt)

    @patch("gemini_client.generate_text")
    def test_evaluate_batch_sync(self, mock_gen):
        """Проверка работы evaluate_batch_sync и лимита MAX_REACTIONS_PER_BATCH."""
        mock_resp = MagicMock()
        mock_resp.text = '[{"msg_id": 1, "reaction": "🔥", "reason": "ок"}, {"msg_id": 2, "reaction": "👍", "reason": "ок2"}, {"msg_id": 3, "reaction": "👏", "reason": "ок3"}]'
        mock_gen.return_value = mock_resp

        messages = [
            reaction_engine.BufferedMessage(msg_id=i, chat_id=-100, sender_id=i, sender_name=f"Doc{i}", text="txt", has_media=False, timestamp=100.0, event=None)
            for i in range(1, 4)
        ]

        results = reaction_engine.evaluate_batch_sync(messages)
        # Должно быть ограничено MAX_REACTIONS_PER_BATCH = 2
        self.assertLessEqual(len(results), reaction_engine.MAX_REACTIONS_PER_BATCH)
        self.assertEqual(results[0]["reaction"], "🔥")

    def test_process_incoming_event_skips_commands(self):
        """Проверка отсечения команд со слешем."""
        event = MagicMock()
        event.is_group = True
        event.text = "/chairside"
        event.media = None
        event.photo = None
        event.document = None
        event.get_sender = AsyncMock(return_value=MagicMock(bot=False, id=123, first_name="Doc"))

        res = asyncio.run(reaction_engine.process_incoming_event(event))
        self.assertFalse(res)

    def test_process_incoming_event_unpacked_kwargs_stomchat(self):
        """Проверка вызова с распакованными kwargs, как в stomchat main.py."""
        event = MagicMock()
        event.is_group = True
        event.chat_id = -1001820467444

        res = asyncio.run(reaction_engine.process_incoming_event(
            event=event,
            msg_id=501,
            chat_id=event.chat_id,
            sender_id=12345,
            sender_name="Доктор Иванов",
            text="Сложный клинический случай перелечивания 46",
            has_media=False,
            extra_param_resilience="safe"
        ))
        self.assertTrue(res)

    def test_process_incoming_event_unpacked_kwargs_endochat(self):
        """Проверка вызова с распакованными kwargs, как в endochat main.py."""
        event = MagicMock()
        event.is_group = True
        event.chat_id = -1001617420995

        res = asyncio.run(reaction_engine.process_incoming_event(
            event=event,
            msg_id=502,
            text="Фото обтурации МТА",
            sender_name="Доктор Петров",
            has_media=True,
            sender_id=67890
        ))
        self.assertTrue(res)

    def test_evaluate_batch_prevents_duplicate_reactions_to_same_author(self):
        """Проверка: в одном батче одному врачу ставится строго не более 1 реакции."""
        m1 = reaction_engine.BufferedMessage(
            msg_id=701, chat_id=-1001, sender_id=999, sender_name="Д-р Автор",
            text="Качественная обтурация трех каналов 46 биокерамикой", has_media=True,
            timestamp=100.0, event=MagicMock()
        )
        m2 = reaction_engine.BufferedMessage(
            msg_id=702, chat_id=-1001, sender_id=999, sender_name="Д-р Автор",
            text="И прицельный снимок после фиксации накладки", has_media=True,
            timestamp=105.0, event=MagicMock()
        )
        mock_json = '[{"msg_id": 701, "reaction": "🔥", "reason": "Обтурация"}, {"msg_id": 702, "reaction": "👏", "reason": "Фиксация"}]'
        with patch("gemini_client.generate_text") as mock_gen:
            mock_gen.return_value = MagicMock(text=mock_json)
            results = reaction_engine.evaluate_batch_sync([m1, m2])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["msg_id"], 701)


if __name__ == "__main__":
    unittest.main()
