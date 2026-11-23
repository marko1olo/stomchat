"""
TEST SUITE: PM CHAIRSIDE MENTOR TONE, DIALOGUE CONTINUITY & VALIDATOR RELAXATION

Verifies:
1. Regex matches all real chairside doctor in-treatment phrases:
   «обошел», «достал отломок», «спилил коронку», «не садится трансфер», «а если композит?»,
   «не садится», «застрял», «заклинило», «кровит», «какой торк», «чем промыть».
2. 100% case entity continuity (tooth number, implant platform, canal, procedural findings)
   across multiple turns without memory loss or asking the doctor to repeat.
3. Prompt assembly correctly flags is_short_progress_update, sets max_retries=1,
   and injects the Chairside Mentor directives (strictly 2–4 sentences, collegial support,
   clear next step, exactly 1 control question, zero boilerplate headings).
4. Validator paranoia overrule: 2–4 sentence chairside drafts rejected by LLM validator for
   brevity, lack of alternative plan, missing home regime or catalog numbers are safely
   overruled (ok=True), while toxic/dangerous practices remain strictly blocked (ok=False).
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


def _create_mock_client():
    mock_client = AsyncMock()
    mock_client.get_me = AsyncMock(return_value=MagicMock(id=999999))
    action_cm = MagicMock()
    action_cm.__aenter__ = AsyncMock(return_value=None)
    action_cm.__aexit__ = AsyncMock(return_value=None)
    mock_client.action = MagicMock(return_value=action_cm)
    return mock_client


class TestPMChairsideMentorToneAndContinuity(unittest.IsolatedAsyncioTestCase):

    def test_clinical_progress_regex_matches_all_chairside_phrases(self):
        """Проверяет распознавание оперативных реплик врача у кресла через CLINICAL_PROGRESS_RE."""
        phrases = [
            "обошел",
            "Обошел уступ",
            "достал отломок",
            "Извлек отломок инструмента",
            "спилил коронку",
            "Снял мост",
            "не садится трансфер",
            "Трансфер не садится",
            "а если композит?",
            "а если цирконий?",
            "а если emax?",
            "не садится",
            "застрял файл",
            "заклинило",
            "не идет глубже",
            "кровит из устья",
            "закровил десневой край",
            "какой торк выставить?",
            "чем промыть после извлечения?",
            "что дальше делать?",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                match = assistant.CLINICAL_PROGRESS_RE.search(phrase)
                self.assertIsNotNone(
                    match,
                    f"Фраза врача у кресла '{phrase}' должна распознаваться как clinical_progress!"
                )

    def test_case_entity_extraction_retains_teeth_and_implant_platforms(self):
        """Проверяет сохранение 100% контекста кейса: номер зуба, имплантационная система, корневой канал."""
        # Кейс 1: Эндодонтия 2.6 (MB2, отломок)
        history_endo = [
            {"sender_name": "User", "text": "Зуб 26, в МБ2 сломался протейпер F1 в средней трети."},
            {"sender_name": "Assistant", "text": "Оцените визуализацию в микроскоп. Используйте ультразвуковую насадку без подачи воды..."},
        ]
        entities_endo = assistant.extract_pm_case_entities(history_endo, "обошел")
        self.assertIn("26", entities_endo)
        self.assertIn("МБ2", entities_endo)
        self.assertIn("Отломок", entities_endo)

        # Кейс 2: Имплантация 4.6 (платформа Osstem / Neodent / Straumann, трансфер)
        history_impl = [
            {"sender_name": "User", "text": "Устанавливаю формирователь на имплантат Osstem TS III в позиции 46 зуба, платформа Regular."},
            {"sender_name": "Assistant", "text": "Проверьте торк и уровень костной ткани над шейкой..."},
        ]
        entities_impl = assistant.extract_pm_case_entities(history_impl, "не садится трансфер")
        self.assertIn("46", entities_impl)
        self.assertIn("Имплантат", entities_impl)
        self.assertIn("Трансфер", entities_impl)
        self.assertTrue(
            "Osstem" in entities_impl or "Имплантат" in entities_impl,
            "Имплантационная платформа или имплантат должны быть зафиксированы в сущностях кейса!"
        )

        # Кейс 3: Ортопедия 3.6 (спилил коронку, феррул, композит vs цирконий)
        history_ortho = [
            {"sender_name": "User", "text": "Пациент с коронкой на 36 зубе, подвижность 1 степени, скол керамики."},
            {"sender_name": "Assistant", "text": "Распилите коронку по вестибулярной поверхности..."},
        ]
        entities_ortho = assistant.extract_pm_case_entities(history_ortho, "спилил коронку")
        self.assertIn("36", entities_ortho)
        self.assertIn("Коронки", entities_ortho)

    async def test_pm_prompt_assembly_enforces_chairside_mentor_directives(self):
        """Проверяет, что при коротких репликах врача у кресла промпт содержит четкие директивы Chairside Mentor."""
        mock_client = _create_mock_client()
        chat_id = 123456789

        test_cases = [
            ("обошел", "26"),
            ("достал отломок", "26"),
            ("не садится трансфер", "46"),
            ("спилил коронку", "36"),
            ("а если композит?", "36"),
        ]

        for phrase, expected_tooth in test_cases:
            with self.subTest(phrase=phrase):
                mock_event = MagicMock()
                mock_event.chat_id = chat_id
                mock_event.sender_id = chat_id
                mock_event.message = MagicMock()
                mock_event.message.id = 555
                mock_event.message.message = phrase
                mock_event.message.text = phrase
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
                        "text": f"Работаю с зубом {expected_tooth}, сложная анатомия, глубокий уступ.",
                        "date": (now - timedelta(minutes=5)).isoformat(),
                    },
                    {
                        "sender_name": "Assistant",
                        "text": f"При работе с зубом {expected_tooth} обеспечьте прямолинейный доступ и обильную ирригацию.",
                        "date": (now - timedelta(minutes=2)).isoformat(),
                    },
                ]

                captured_prompts = []

                async def mock_generate_text(prompt, ctx, timeout=300.0):
                    captured_prompts.append(prompt)
                    mock_resp = MagicMock()
                    mock_resp.text = "Отлично, что справились с этапом! Промойте 3% NaOCl и просушите канал. Контрольный снимок уже сделали?"
                    return mock_resp, None

                async def mock_validator(context_msgs, draft, invited=True, reference=""):
                    return True, "chairside_approved"

                with patch("assistant.database.save_pm_message", AsyncMock(return_value=1813)), \
                     patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=fake_history)), \
                     patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
                     patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
                     patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
                     patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
                     patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
                     patch("assistant.generate_gemini_text_async", side_effect=mock_generate_text), \
                     patch("assistant.check_response_quality", side_effect=mock_validator), \
                     patch("assistant.send_message_chunks_async", AsyncMock()), \
                     patch("assistant._async_pm_supplement_job", AsyncMock()):

                    await assistant.handle_private_message(mock_client, mock_event)

                self.assertTrue(len(captured_prompts) > 0, f"Для фразы '{phrase}' должен был собраться промпт!")
                prompt_text = captured_prompts[-1]

                # 1. Проверяем сохранение зуба в активных сущностях
                self.assertIn(expected_tooth, prompt_text, f"Зуб {expected_tooth} обязан присутствовать в промпте!")

                # 2. Проверяем директиву CHAIRSIDE MENTOR
                self.assertIn(
                    "CHAIRSIDE MENTOR",
                    prompt_text,
                    "Промпт обязан содержать директиву CHAIRSIDE MENTOR!"
                )

                # 3. Проверяем требование 2-4 емких предложений
                self.assertIn(
                    "2–4",
                    prompt_text,
                    "Промпт обязан требовать лаконичный ответ в 2–4 предложения!"
                )

                # 4. Проверяем категорический запрет шаблонных заголовков GPT-6.1 Sol
                self.assertIn(
                    "1. Клинико-рентгенологический",
                    prompt_text,
                    "Промпт обязан содержать запрет шаблонного заголовка '1. Клинико-рентгенологический статус'!"
                )

    async def test_validator_paranoia_overrule_on_chairside_concise_drafts(self):
        """Проверяет устранение паранойи валидатора: лаконичные chairside-ответы не отклоняются ложно."""
        chairside_draft = (
            "Отлично, что обошли ступеньку! "
            "Промойте 3% NaOCl без давления на рабочей длине и просушите бумажными пинами. "
            "Контрольный снимок с мастер-файлом уже сделали?"
        )
        context = [
            "User: В зубе 26 уступ в небном канале на 16 мм.",
            "Assistant: Попробуйте предварительно изогнутый К-файл 08 или 10.",
            "Врач (сейчас): обошел",
        ]

        # 1. Валидатор отклоняет из-за краткости / отсутствия альтернативного плана / отсутствия каталожных артикулов
        paranoid_reasons = [
            "слишком краткий ответ, нет альтернативного плана",
            "черновик короткий, отсутствует трехшаговый стандарт",
            "не описан домашний режим пациента и сроки контроля",
            "нет каталожных артикулов и мало информации",
        ]

        for reason in paranoid_reasons:
            with self.subTest(reason=reason):
                fake_llm_json = f'{{"ok": false, "reason": "{reason}"}}'
                mock_resp = MagicMock()
                mock_resp.text = fake_llm_json

                with patch.object(assistant, "generate_gemini_text_async", AsyncMock(return_value=(mock_resp, None))):
                    ok, res_reason = await assistant.check_response_quality(
                        context, chairside_draft, invited=True, reference=""
                    )
                    self.assertTrue(
                        ok,
                        f"Лаконичный chairside-ответ не должен блокироваться из-за паранойи: '{reason}'!"
                    )
                    self.assertIn("chairside_concise_approved", res_reason)

    async def test_validator_does_not_overrule_toxic_practices(self):
        """Проверяет, что оверрул паранойи НЕ пропускает смертельно опасные или токсичные практики."""
        toxic_draft = (
            "Положите мышьяковистую пасту на устье и закройте временной пломбой на 48 часов. "
            "Боль купируется быстро."
        )
        context = ["User: Врач (сейчас): обошел"]
        fake_llm_json = '{"ok": false, "reason": "слишком краткий ответ"}'
        mock_resp = MagicMock()
        mock_resp.text = fake_llm_json

        with patch.object(assistant, "generate_gemini_text_async", AsyncMock(return_value=(mock_resp, None))):
            ok, res_reason = await assistant.check_response_quality(
                context, toxic_draft, invited=True, reference=""
            )
            self.assertFalse(ok, "Токсичные практики (мышьяк) обязаны блокироваться валидатором даже при краткости!")


if __name__ == "__main__":
    unittest.main()
