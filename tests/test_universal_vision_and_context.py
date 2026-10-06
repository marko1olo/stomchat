"""
РЕД-ТИМ ТЕСТ: Универсальное клиническое зрение, тредовая память и когнитивное масштабирование.

Проверяет:
1. Масштабирование длины ответа (calculate_context_length_guidelines):
   - Отсутствие 15-словного удушения для клинических случаев
   - Адекватная лаконичность для короткого чата без удушения
2. Тредовая непрерывность (fetch_dynamic_chat_context):
   - Инъекция media_description родительского сообщения в цепочку контекста
   - Игнорирование фиктивных/ошибочных маркеров медиа
3. Универсальность клинического промпта в assistant.py:
   - Принцип масштабирования клинического мышления (Macro vs Micro)
   - Отсутствие частных заплаточных слов (уступы, Попова-Годона как догма)
   - Строгая HTML-разметка
4. Универсальность промпта в vision.py:
   - 5-уровневая иерархия наблюдения
   - Железный запрет выдумывать зубы/кариес на немедицинских картинках
   - Наличие старших моделей в пуле
"""

import asyncio
import re
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import assistant
import vision


class TestUniversalLengthGuidelines(unittest.TestCase):
    """Тестирование масштабирования объема ответа."""

    def test_clinical_case_has_generous_structured_guideline(self):
        """Для клинического случая дается развернутый структурированный регламент."""
        res = assistant.calculate_context_length_guidelines([], is_clinical_case=True)
        self.assertIn("разбор клинического случая", res.lower())
        self.assertIn("150-250", res)
        # Убедимся, что нет удушения до 15 слов
        self.assertNotIn("15 слов", res)

    def test_short_chat_does_not_choke_to_15_words(self):
        """Даже при ультракоротких сообщениях чата бот не удушается до 15 слов."""
        short_history = ["ок", "тотал", "+1", "да"]
        res = assistant.calculate_context_length_guidelines(short_history, is_clinical_case=False)
        self.assertNotIn("до 15 слов", res)
        self.assertIn("25-30 слов", res)

    def test_moderate_and_long_chat_pacing(self):
        """Для длинных обсуждений регламент дает простор."""
        long_msgs = ["слово " * 60] * 5
        res = assistant.calculate_context_length_guidelines(long_msgs, is_clinical_case=False)
        self.assertIn("100-120", res)


class TestDynamicChatContextMediaMemory(unittest.IsolatedAsyncioTestCase):
    """Тестирование непрерывности контекста и передачи описания снимков в тред."""

    async def test_parent_media_description_injected_into_reply_chain(self):
        """Если у родительского сообщения есть снимок, его описание попадает в контекст."""
        # Эмулируем строку БД: (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, media_description)
        parent_row = [(
            178703,
            None,
            111,
            "Сергей",
            "Кто накидает план за 5 минут?",
            datetime(2026, 9, 28, 23, 15, 0),
            "На серии фото: перекрёстный прикус, отсутствие 1.6-1.7, нёбное разрушение 1.1-2.2, фронтальная шина на НЧ."
        )]
        child_row = [(
            178716,
            178703,
            111,
            "Сергей",
            "Кроме шуток. Помогай давай",
            datetime(2026, 9, 28, 23, 17, 0),
            None
        )]

        async def fake_query(sql, params):
            mid = params[0]
            if mid == 178716:
                return child_row
            elif mid == 178703:
                return parent_row
            return []

        with patch("assistant.query_db_async", side_effect=fake_query):
            context_lines, bot_cnt, nearest_bot = await assistant.fetch_dynamic_chat_context(
                msg_id=178716,
                reply_to_msg_id=178703,
                base_limit=12,
                max_limit=20
            )

        full_context = "\n".join(context_lines)
        # Описание снимка родительского поста #178703 обязано присутствовать в контексте
        self.assertIn("Прикреплённый снимок/клиническая картина к посту #178703", full_context)
        self.assertIn("перекрёстный прикус", full_context)
        self.assertIn("отсутствие 1.6-1.7", full_context)

    async def test_sentinel_media_not_injected(self):
        """Фиктивные маркеры (-, MEDIA_UNAVAILABLE) не должны засорять контекст."""
        row = [(
            100,
            None,
            222,
            "Иван",
            "Просто текст",
            datetime(2026, 9, 28, 23, 0, 0),
            "MEDIA_UNAVAILABLE"
        )]

        async def fake_query(sql, params):
            return row if params[0] == 100 else []

        with patch("assistant.query_db_async", side_effect=fake_query):
            context_lines, _, _ = await assistant.fetch_dynamic_chat_context(
                msg_id=100,
                reply_to_msg_id=None
            )

        full_context = "\n".join(context_lines)
        self.assertNotIn("Прикреплённый снимок", full_context)
        self.assertNotIn("MEDIA_UNAVAILABLE", full_context)


class TestPromptUniversalPrinciples(unittest.TestCase):
    """Проверка промптов на отсутствие частных заплаток и наличие универсальных законов."""

    def test_assistant_prompt_universal_scale_matching(self):
        """Промпт ассистента содержит принцип масштабирования клинического мышления (Macro vs Micro)."""
        with open("assistant.py", "r", encoding="utf-8") as f:
            code = f.read()

        # Проверяем универсальный закон масштабирования
        self.assertIn("ПРИНЦИП МАСШТАБИРОВАНИЯ КЛИНИЧЕСКОГО МЫШЛЕНИЯ", code)
        self.assertIn("Узкофокусный вопрос", code)
        self.assertIn("Комплексный случай", code)
        self.assertIn("РАЗМЕТКА — СТРОГО HTML", code)

        # Проверяем, что в самом блоке промпта медиа-ассистента нет вчерашних частных затычек
        prompt_block = code[code.find("ПРИНЦИП МАСШТАБИРОВАНИЯ КЛИНИЧЕСКОГО МЫШЛЕНИЯ"):code.find("ПРИНЦИП МАСШТАБИРОВАНИЯ КЛИНИЧЕСКОГО МЫШЛЕНИЯ") + 1000]
        self.assertNotIn("Попова-Годона", prompt_block)
        self.assertNotIn("кариозном уступе", prompt_block)

    def test_vision_prompt_universal_principles(self):
        """Промпт vision.py содержит универсальные правила достоверности и детекции немедицинского контента."""
        with open("vision.py", "r", encoding="utf-8") as f:
            code = f.read()

        # Немедицинские изображения
        self.assertIn("НЕМЕДИЦИНСКИЕ ИЗОБРАЖЕНИЯ", code)
        self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать зубы", code)

        # Иерархия Macro + Micro
        self.assertIn("Macro + Micro", code)
        self.assertIn("Архитектура и прикус", code)
        self.assertIn("ПРАВИЛА ДОСТОВЕРНОСТИ", code)

        # Старшие модели присутствуют в пуле
        self.assertIn("gemini-3.8-flash", code)
        self.assertIn("gemini-3.7-flash", code)


if __name__ == "__main__":
    unittest.main()
