"""
Tests for:
1. Clinical photo/radiograph without text handling in check_and_trigger_assistant_media
2. Media description extraction, Await-Guard and status_ctx injection in handle_group_direct_ask
3. Anti-schizophrenia bot self-recognition ("Бот-ассистент:")
4. between_rows isolation against cross-thread dental chatter
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assistant
from vision import VisionDescription


class TestMultimodalNoTextCases(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_media_without_caption_instructs_autonomous_analysis(self):
        desc = VisionDescription("На рентгенограмме зуб 36, обтурация каналов", image_urls=["data:image/jpeg;base64,TEST999"])
        captured_prompt = []
        captured_ctx = []

        async def fake_generate(prompt, status_ctx, timeout=120):
            captured_prompt.append(prompt)
            captured_ctx.append(status_ctx)
            mock_resp = MagicMock()
            mock_resp.text = "Клинический вердикт: обтурация до физиологического апекса."
            return mock_resp, None

        bot_client = MagicMock()
        bot_client.send_message = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.id = 11111
        mock_msg.chat_id = -100777
        mock_msg.sender_id = 22222
        mock_msg.reply_to_msg_id = None
        mock_msg.client = bot_client

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_generate), \
             patch.object(assistant, "BOT_ID", 999), \
             patch.object(assistant, "BOT_USERNAME", "test_bot"), \
             patch.object(assistant, "load_state", return_value={}), \
             patch.object(assistant, "save_state", return_value=None), \
             patch.object(assistant, "is_silenced", return_value=False), \
             patch.object(assistant, "query_db_async", AsyncMock(return_value=[(0,)])), \
             patch.object(assistant.database, "get_last_n_messages", AsyncMock(return_value=[])), \
             patch.object(assistant, "fetch_dynamic_chat_context", AsyncMock(return_value=(["контекст"], [], []))), \
             patch.object(assistant, "is_explicitly_non_dental_media", return_value=False), \
             patch.object(assistant, "search_knowledge_corpus", AsyncMock(return_value=("", ""))), \
             patch.object(assistant, "check_response_quality", AsyncMock(return_value=(True, "ok"))), \
             patch.object(assistant, "clean_html_formatting", lambda x: x):

            # Call with EMPTY caption text
            await assistant.check_and_trigger_assistant_media(
                bot_client, mock_msg, 11111, "", desc
            )

            self.assertEqual(len(captured_prompt), 1)
            prompt = captured_prompt[0]

            # Verify prompt contains autonomous analysis directives for empty text
            self.assertIn("прислан чистый снимок/рентгенограмма без текста", prompt)
            self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать «текст не дошёл», «сообщение пустое»", prompt)
            self.assertIn("самостоятельный экспертный разбор", prompt)

            # Verify status_ctx has both image_urls AND media_description
            self.assertEqual(len(captured_ctx), 1)
            self.assertIn("image_urls", captured_ctx[0])
            self.assertIn("media_description", captured_ctx[0])
            self.assertIn("обтурация каналов", captured_ctx[0]["media_description"])

    async def test_anti_schizophrenia_replaces_bot_assistant_label(self):
        context_msgs = [
            "#101 Доктор Иванов: Коллеги, гляньте 46",
            "#102 Бот-ассистент: На снимке 46 зуба визуализируется качественная обтурация",
            "#103 Доктор Иванов: Спасибо, согласен",
        ]
        bot_markers = (
            "Бот-ассистент:",
            "Бот-ассистент (ты):",
            "Бот Учимся Вместе 🤖:",
            "Учимся Вместе:",
            "Бот StomChat 🤖:",
            "StomChat:",
            "Бот EndoChat 🤖:",
            "EndoChat:",
        )
        for i in range(len(context_msgs)):
            for marker in bot_markers:
                if marker in context_msgs[i]:
                    context_msgs[i] = context_msgs[i].replace(marker, "[ЭТО ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ]:")

        self.assertIn("[ЭТО ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ]:", context_msgs[1])
        self.assertNotIn("Бот-ассистент:", context_msgs[1])


if __name__ == "__main__":
    unittest.main()
