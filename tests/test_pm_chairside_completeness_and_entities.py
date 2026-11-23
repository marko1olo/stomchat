"""
RED TEAM AUDIT & INQUISITOR TESTS:
CHAIRSIDE COMPLETENESS & CLINICAL ENTITY CONTINUITY IN PM

Verifies:
1. Doctor 5130294926: Rest vertical dimension (OVD) inquiry triggers CHAIRSIDE COMPLETENESS.
2. Doctor 5130294926: "Были ранее под коронками" retains teeth 43, 44, 45, crowns, ferrule, fiber post.
3. Doctor 5130294926: "Артикаина нет" retains novocaine/articaine/mepivacaine context.
4. Doctor 805936432: "Пишет , что шуршит как пакет" triggers subcutaneous emphysema complete protocol.
5. calculate_context_length_guidelines does not throttle PM output to 1-2 sentences.
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import assistant
import clinical_cognitive_core


class TestPMChairsideCompletenessAndEntities(unittest.IsolatedAsyncioTestCase):

    def test_calculate_context_length_guidelines_pm_completeness(self):
        """Проверяет, что в ЛС (is_group=False) при ультра-коротких репликах врача не включается удушающий лимит '1-2 предложения'."""
        short_history = [
            "валики во рту?",
            "Артикаина нет",
            "Были ранее под коронками",
            "Шуршит как пакет",
        ]
        guideline = assistant.calculate_context_length_guidelines(short_history, is_group=False)
        self.assertIn("CHAIRSIDE COMPLETENESS", guideline)
        self.assertNotIn("1-2 емких предложения", guideline)
        self.assertNotIn("2-3 предложения", guideline)

    def test_entity_extraction_incident_cases(self):
        """Проверяет извлечение клинических сущностей (зубы, анестетики, патологии, материалы) по реальным кейсам."""
        # Кейс 1: Зубы 43, 44, 45 под коронками
        history_case1 = [
            {"sender_name": "User", "text": "Зубы 43,44,45 депульпированы. Стоит на них ставить стекловолокно или нет?"},
            {"sender_name": "Assistant", "text": "Стекловолокно стоит ставить при недостаточной ретенции... 1.5-2 мм феррула, ParaBond + ParaCore"},
        ]
        entities1 = assistant.extract_pm_case_entities(history_case1, "Были ранее под коронками")
        self.assertIn("43", entities1)
        self.assertIn("44", entities1)
        self.assertIn("45", entities1)
        self.assertIn("Коронки", entities1)
        self.assertIn("СВШ", entities1)
        self.assertIn("Феррул", entities1)

        # Кейс 2: Аллергия и отсутствие артикаина
        history_case2 = [
            {"sender_name": "User", "text": "Если делали артикаин, можно делать мепивакаин. Аллергия была на новокаин"},
            {"sender_name": "Assistant", "text": "Мепивакаин можно рассматривать при аллергии на новокаин..."},
        ]
        entities2 = assistant.extract_pm_case_entities(history_case2, "Артикаина нет")
        self.assertIn("Артикаин", entities2)
        self.assertIn("Мепивакаин", entities2)
        self.assertIn("Новокаин", entities2)

        # Кейс 3: Эмфизема («шуршит как пакет»)
        history_case3 = [
            {"sender_name": "User", "text": "Использовала капромин, гемостаб, просушивала пустером, было вестибулярное препарирование. Отекла щека"},
            {"sender_name": "Assistant", "text": "Первой исключается подкожная эмфизема... Есть характерный хруст при пальпации?"},
        ]
        entities3 = assistant.extract_pm_case_entities(history_case3, "Пишет , что шуршит как пакет")
        self.assertIn("Подкожная эмфизема", entities3)
        self.assertIn("Крепитация", entities3)
        self.assertIn("Капромин", entities3)
        self.assertIn("Гемостаб", entities3)

    def test_classify_dialogue_turn_real_phrases(self):
        """Проверяет классификацию реальных коротких реплик врача как DIALOGUE_ANSWER в активном сеансе."""
        now = datetime.now()
        history = [
            {"sender_name": "Assistant", "text": "Оцените объем оставшегося дентина и феррул.", "date": now.isoformat()},
        ]
        # Реплики из инцидентов
        turn1, _ = assistant.classify_pm_dialogue_turn("Были ранее под коронками", history, now)
        self.assertEqual(turn1, "DIALOGUE_ANSWER")

        turn2, _ = assistant.classify_pm_dialogue_turn("Артикаина нет", history, now)
        self.assertEqual(turn2, "DIALOGUE_ANSWER")

        turn3, _ = assistant.classify_pm_dialogue_turn("Пишет , что шуршит как пакет", history, now)
        self.assertEqual(turn3, "DIALOGUE_ANSWER")

        turn4, _ = assistant.classify_pm_dialogue_turn("Они были ранее под коронками и сняты вчера", history, now)
        self.assertEqual(turn4, "DIALOGUE_ANSWER")

        turn5, _ = assistant.classify_pm_dialogue_turn("У нас в клинике сейчас нет артикаина", history, now)
        self.assertEqual(turn5, "DIALOGUE_ANSWER")

        # Вопросы коллеги должны быть FOLLOWUP
        turn6, _ = assistant.classify_pm_dialogue_turn("А валики во рту или рот пустой?", history, now)
        self.assertEqual(turn6, "DIALOGUE_FOLLOWUP")

        turn7, _ = assistant.classify_pm_dialogue_turn("Зуботехническим шпателем можно замерять?", history, now)
        self.assertEqual(turn7, "DIALOGUE_FOLLOWUP")

    async def test_chairside_completeness_in_prompt_assembly_case_5130294926(self):
        """Проверяет, что при обработке ответа 'Были ранее под коронками' промпт содержит преемственность зубов и CHAIRSIDE COMPLETENESS."""
        mock_client = AsyncMock()
        mock_client.get_me = AsyncMock(return_value=MagicMock(id=999999))

        mock_event = MagicMock()
        mock_event.chat_id = 5130294926
        mock_event.sender_id = 5130294926
        mock_event.message = MagicMock()
        mock_event.message.id = 1812
        mock_event.message.message = "Были ранее под коронками"
        mock_event.message.text = "Были ранее под коронками"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None

        now = datetime.now()
        fake_history = [
            {
                "sender_name": "User",
                "text": "Зубы 43,44,45 депульпированы. Стоит на них ставить стекловолокно или нет?",
                "date": (now - timedelta(minutes=5)).isoformat(),
            },
            {
                "sender_name": "Assistant",
                "text": "Стекловолокно стоит ставить при недостаточной ретенции... феррул 1.5–2 мм, ParaBond + ParaCore",
                "date": (now - timedelta(minutes=3)).isoformat(),
            },
            {
                "sender_name": "User",
                "text": "Были ранее под коронками",
                "date": now.isoformat(),
            }
        ]

        captured_prompts = []

        async def fake_gemini(prompt, status_ctx=None, timeout=None):
            captured_prompts.append(prompt)
            resp = MagicMock()
            resp.text = "<b>Оцените остаточную толщину стенок дентина на 43, 44, 45 после снятия коронок.</b>"
            return resp, None

        with patch("assistant.database.save_pm_message", AsyncMock(return_value=1813)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=fake_history)), \
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

        self.assertTrue(len(captured_prompts) >= 1)
        final_prompt = captured_prompts[-1]

        # Проверяем, что промпт содержит преемственность зубов и директиву полноты
        self.assertIn("Были ранее под коронками", final_prompt)
        self.assertIn("Зубы (FDI): 43, 44, 45", final_prompt)
        self.assertIn("ДИРЕКТИВА CHAIRSIDE COMPLETENESS", final_prompt)
        self.assertIn("CLINICAL ENTITY CONTINUITY", final_prompt)
        # Проверяем отсутствие искусственного ограничения до 3-5 предложений в инструкциях
        self.assertNotIn("3–5 плотных предложений", final_prompt)
        self.assertNotIn("3–6 плотных предложений", final_prompt)

    async def test_emphysema_incident_case_805936432(self):
        """Проверяет, что при реплике 'Пишет , что шуршит как пакет' промпт содержит эмфизему и протокол полноты."""
        mock_client = AsyncMock()
        mock_client.get_me = AsyncMock(return_value=MagicMock(id=999999))

        mock_event = MagicMock()
        mock_event.chat_id = 805936432
        mock_event.sender_id = 805936432
        mock_event.message = MagicMock()
        mock_event.message.id = 1806
        mock_event.message.message = "Пишет , что шуршит как пакет"
        mock_event.message.text = "Пишет , что шуршит как пакет"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None

        now = datetime.now()
        fake_history = [
            {
                "sender_name": "User",
                "text": "Использовала капромин, гемостаб, просушивала пустером, было вестибулярное препарирование. Отекла щека",
                "date": (now - timedelta(minutes=15)).isoformat(),
            },
            {
                "sender_name": "Assistant",
                "text": "Свяжитесь с пациентом сейчас. Первой исключается подкожная эмфизема... При пальпации есть снежный хруст?",
                "date": (now - timedelta(minutes=10)).isoformat(),
            },
            {
                "sender_name": "User",
                "text": "Пишет , что шуршит как пакет",
                "date": now.isoformat(),
            }
        ]

        captured_prompts = []

        async def fake_gemini(prompt, status_ctx=None, timeout=None):
            captured_prompts.append(prompt)
            resp = MagicMock()
            resp.text = "<b>Это подкожная эмфизема.</b> Верните пациента в клинику..."
            return resp, None

        with patch("assistant.database.save_pm_message", AsyncMock(return_value=1807)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=fake_history)), \
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

        self.assertTrue(len(captured_prompts) >= 1)
        final_prompt = captured_prompts[-1]

        self.assertIn("Пишет , что шуршит как пакет", final_prompt)
        self.assertIn("Подкожная эмфизема", final_prompt)
        self.assertIn("Крепитация", final_prompt)
        self.assertIn("ДИРЕКТИВА CHAIRSIDE COMPLETENESS", final_prompt)
        self.assertIn("НЕРАЗРЫВНОСТЬ КОНТЕКСТА", final_prompt)


if __name__ == "__main__":
    unittest.main()
