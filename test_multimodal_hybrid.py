"""
Тесты гибридной мультимодальности (VisionDescription + Gemini image_urls + Groq text fallback).
Проверяет:
1. VisionDescription поведение как str и хранение image_urls
2. gemini_client: передача multimodal payload для Gemini
3. gemini_client: безопасный fallback в text-only для Groq и при ошибках vision
4. assistant: прокидывание image_urls в status_ctx и мультимодальные инструкции в промпты
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure repo root is in sys.path
repo_dir = os.path.abspath(os.path.dirname(__file__))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)

import vision
from vision import VisionDescription
import gemini_client
import assistant


class TestVisionDescription(unittest.TestCase):
    def test_str_subclass_behavior(self):
        urls = ["data:image/jpeg;base64,abc12345"]
        desc = VisionDescription("На снимке 36 зуб", image_urls=urls)
        
        self.assertIsInstance(desc, str)
        self.assertEqual(desc, "На снимке 36 зуб")
        self.assertEqual(desc.image_urls, urls)
        self.assertTrue(desc.startswith("На снимке"))
        self.assertIn("36 зуб", desc)
        self.assertEqual(len(desc), len("На снимке 36 зуб"))
        self.assertEqual(desc.strip(), "На снимке 36 зуб")
        self.assertEqual(desc.lower(), "на снимке 36 зуб")

    def test_empty_or_none_urls(self):
        desc = VisionDescription("Описание без картинок")
        self.assertIsInstance(desc, str)
        self.assertEqual(desc.image_urls, [])


class TestGeminiClientMultimodal(unittest.TestCase):
    def test_gemini_receives_multimodal_payload(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Разбор снимка: виден апикальный периодонтит"
        mock_client.chat.completions.create.return_value = mock_response

        status_ctx = {
            "kind": "assistant_media",
            "chat_id": -1001234567,
            "image_urls": ["data:image/jpeg;base64,XYZ123"],
            "thinking_level": "HIGH"
        }

        with patch.object(gemini_client, "get_provider_client", return_value=mock_client),              patch.object(gemini_client, "active_models", return_value=[("gemini-3.8-flash", "gemini")]),              patch.object(gemini_client, "available_keys", return_value=(["fake_key"], [], 0)):
            
            resp = gemini_client.generate_text("Разбери снимок зуба 46", status_context=status_ctx, timeout=30)
            
            self.assertIsNotNone(resp)
            self.assertIn("апикальный периодонтит", resp.text)
            
            # Verify call args
            create_args = mock_client.chat.completions.create.call_args[1]
            messages = create_args["messages"]
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["role"], "user")
            content = messages[0]["content"]
            self.assertIsInstance(content, list)
            self.assertEqual(content[0]["type"], "text")
            self.assertEqual(content[0]["text"], "Разбери снимок зуба 46")
            self.assertEqual(content[1]["type"], "image_url")
            self.assertEqual(content[1]["image_url"]["url"], "data:image/jpeg;base64,XYZ123")

    def test_groq_strips_image_urls_cleanly(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Текстовый разбор клинического случая"
        mock_client.chat.completions.create.return_value = mock_response

        status_ctx = {
            "kind": "assistant_media",
            "chat_id": -1001234567,
            "image_urls": ["data:image/jpeg;base64,XYZ123"],
            "thinking_level": "HIGH"
        }

        with patch.object(gemini_client, "get_provider_client", return_value=mock_client),              patch.object(gemini_client, "active_models", return_value=[("openai/gpt-oss-120b", "groq")]),              patch.object(gemini_client, "available_keys", return_value=(["fake_key"], [], 0)):
            
            resp = gemini_client.generate_text("Разбери снимок зуба 46", status_context=status_ctx, timeout=30)
            
            self.assertIsNotNone(resp)
            
            # Verify call args: Groq text model must receive pure string prompt in content!
            create_args = mock_client.chat.completions.create.call_args[1]
            messages = create_args["messages"]
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["role"], "user")
            self.assertEqual(messages[0]["content"], "Разбери снимок зуба 46")

    def test_vision_rejection_auto_retries_with_text(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Успешный ответ после text fallback"

        # First call raises 400 Bad Request about image_url
        # Second call succeeds
        mock_client.chat.completions.create.side_effect = [
            Exception("400 Bad Request: image_url is not supported by this endpoint"),
            mock_response
        ]

        status_ctx = {
            "kind": "assistant_media",
            "chat_id": -1001234567,
            "image_urls": ["data:image/jpeg;base64,XYZ123"],
            "thinking_level": "HIGH"
        }

        with patch.object(gemini_client, "get_provider_client", return_value=mock_client),              patch.object(gemini_client, "active_models", return_value=[("gemini-3.8-flash", "gemini")]),              patch.object(gemini_client, "available_keys", return_value=(["fake_key"], [], 0)):
            
            resp = gemini_client.generate_text("Разбери снимок", status_context=status_ctx, timeout=30)
            
            self.assertIsNotNone(resp)
            self.assertEqual(resp.text, "Успешный ответ после text fallback")
            self.assertEqual(mock_client.chat.completions.create.call_count, 2)
            
            # Second call should have text-only messages
            second_call_args = mock_client.chat.completions.create.call_args_list[1][1]
            self.assertEqual(second_call_args["messages"], [{"role": "user", "content": "Разбери снимок"}])


class TestAssistantMediaGrounding(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_media_passes_image_urls(self):
        desc = VisionDescription("На снимке КЛКТ эндодонтический доступ", image_urls=["data:image/jpeg;base64,TEST1234"])
        
        captured_ctx = []
        captured_prompt = []

        async def fake_generate(prompt, status_ctx, timeout=120):
            captured_prompt.append(prompt)
            captured_ctx.append(status_ctx)
            mock_resp = MagicMock()
            mock_resp.text = "Клинический вердикт: качественно запломбирован."
            return mock_resp, None

        bot_client = MagicMock()
        bot_client.send_message = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.id = 99999
        mock_msg.chat_id = -100999
        mock_msg.sender_id = 55555
        mock_msg.reply_to_msg_id = None
        mock_msg.client = bot_client

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_generate),              patch.object(assistant, "load_state", return_value={}),              patch.object(assistant, "save_state", return_value=None),              patch.object(assistant, "is_silenced", return_value=False),              patch.object(assistant.database, "get_last_n_messages", AsyncMock(return_value=[])),              patch.object(assistant, "fetch_dynamic_chat_context", AsyncMock(return_value=(["контекст"], [], []))),              patch.object(assistant, "is_explicitly_non_dental_media", return_value=False),              patch.object(assistant, "search_knowledge_corpus", AsyncMock(return_value=("", ""))),              patch.object(assistant, "check_response_quality", AsyncMock(return_value=(True, "ok"))),              patch.object(assistant, "clean_html_formatting", lambda x: x):

            await assistant.check_and_trigger_assistant_media(
                bot_client, mock_msg, 99999, "Коллеги, гляньте снимок зуба 46", desc
            )

            self.assertEqual(len(captured_ctx), 1)
            self.assertIn("image_urls", captured_ctx[0])
            self.assertEqual(captured_ctx[0]["image_urls"], ["data:image/jpeg;base64,TEST1234"])
            self.assertEqual(captured_ctx[0]["kind"], "assistant_media")
            
            # Check prompt contains multimodal notice
            self.assertEqual(len(captured_prompt), 1)
            self.assertIn("МУЛЬТИМОДАЛЬНОЕ ЗРЕНИЕ", captured_prompt[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
