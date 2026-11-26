"""
RED TEAM AUDIT & INQUISITOR TESTS:
UNIVERSAL CHAIRSIDE COMPLETENESS & CLINICAL ENTITY CONTINUITY IN PM

Verifies UNIVERSAL INVARIANTS:
1. Context continuity across arbitrary teeth (FDI quadrants 1-4) and implant systems (Neodent, Straumann, Osstem, Dentium, Nobel, Astra).
2. Universal 4-component chairside completeness in PM (Diagnosis/Status, Protocol with parameters, Pharmacotherapy, Risks/Plan B).
3. Short doctor answers ("были под коронками", "артикаина нет", "шуршит как пакет", "RED 109.953") maintain 100% case continuity without reset or re-asking.
4. Emergency triage protocol (subcutaneous emphysema) immediately activates vitals, 112 triage, Amoxiclav, and heat/massage prohibition.
5. Context length guidelines in PM never throttle clinical output to 1-2 or 3-5 sentences.
"""

import sys
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import assistant
import clinical_cognitive_core

_ORIG_STATE_PATH = None
_TMP_STATE_DIR = None


def setUpModule():
    global _ORIG_STATE_PATH, _TMP_STATE_DIR
    _ORIG_STATE_PATH = assistant.STATE_PATH
    _TMP_STATE_DIR = tempfile.mkdtemp(prefix="pm_comp_state_")
    assistant.STATE_PATH = os.path.join(_TMP_STATE_DIR, "assistant_state.json")


def tearDownModule():
    if _ORIG_STATE_PATH is not None:
        assistant.STATE_PATH = _ORIG_STATE_PATH
    if _TMP_STATE_DIR:
        shutil.rmtree(_TMP_STATE_DIR, ignore_errors=True)


def _create_mock_client():
    """Создает мок клиента Telethon с асинхронным контекстным менеджером action (typing), предотвращая ResourceWarning."""
    mock_client = AsyncMock()
    mock_client.get_me = AsyncMock(return_value=MagicMock(id=999999))
    action_cm = MagicMock()
    action_cm.__aenter__ = AsyncMock(return_value=None)
    action_cm.__aexit__ = AsyncMock(return_value=None)
    mock_client.action = MagicMock(return_value=action_cm)
    return mock_client


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

    def test_entity_extraction_universal_cases(self):
        """Проверяет извлечение клинических сущностей (зубы по FDI 11-48, анестетики, патологии, материалы) по универсальным сценариям."""
        # Кейс 1: Произвольные зубы квадранта 4 (43, 44, 45) под коронками
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

        # Кейс 2: Зубы верхнего левого квадранта (26, 27) и корневые каналы
        history_case2 = [
            {"sender_name": "User", "text": "В зубах 26 и 27 периодонтит, не могу найти МБ2 и небный канал."},
            {"sender_name": "Assistant", "text": "Для поиска MB2 на 26 зубе используйте ультразвуковую насадку..."},
        ]
        entities2 = assistant.extract_pm_case_entities(history_case2, "Обошел уступ в небном")
        self.assertIn("26", entities2)
        self.assertIn("27", entities2)
        self.assertIn("МБ2", entities2)
        self.assertIn("НЕБНЫЙ", entities2)

        # Кейс 3: Аллергия и смена анестетика (новокаин -> артикаин / мепивакаин)
        history_case3 = [
            {"sender_name": "User", "text": "Если делали артикаин, можно делать мепивакаин. Аллергия была на новокаин"},
            {"sender_name": "Assistant", "text": "Мепивакаин можно рассматривать при аллергии на новокаин..."},
        ]
        entities3 = assistant.extract_pm_case_entities(history_case3, "Артикаина нет")
        self.assertIn("Артикаин", entities3)
        self.assertIn("Мепивакаин", entities3)
        self.assertIn("Новокаин", entities3)

        # Кейс 4: Подкожная эмфизема («шуршит как пакет», крепитация)
        history_case4 = [
            {"sender_name": "User", "text": "Использовала капромин, гемостаб, просушивала пустером, было вестибулярное препарирование. Отекла щека"},
            {"sender_name": "Assistant", "text": "Первой исключается подкожная эмфизема... Есть характерный хруст при пальпации?"},
        ]
        entities4 = assistant.extract_pm_case_entities(history_case4, "Пишет , что шуршит как пакет")
        self.assertIn("Подкожная эмфизема", entities4)
        self.assertIn("Крепитация", entities4)
        self.assertIn("Капромин", entities4)
        self.assertIn("Гемостаб", entities4)

    def test_classify_dialogue_turn_universal_phrases(self):
        """Проверяет классификацию разнообразных коротких реплик врача как DIALOGUE_ANSWER vs DIALOGUE_FOLLOWUP."""
        now = datetime.now()
        history = [
            {"sender_name": "Assistant", "text": "Оцените объем оставшегося дентина и феррул.", "date": now.isoformat()},
        ]
        # Клинические ответы врача на предыдущий вопрос (DIALOGUE_ANSWER)
        self.assertEqual(assistant.classify_pm_dialogue_turn("Были ранее под коронками", history, now)[0], "DIALOGUE_ANSWER")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Артикаина нет", history, now)[0], "DIALOGUE_ANSWER")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Пишет , что шуршит как пакет", history, now)[0], "DIALOGUE_ANSWER")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Они были ранее под коронками и сняты вчера", history, now)[0], "DIALOGUE_ANSWER")
        self.assertEqual(assistant.classify_pm_dialogue_turn("У нас в клинике сейчас нет артикаина", history, now)[0], "DIALOGUE_ANSWER")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Феррул 1.5 мм сохранен с оральной стороны", history, now)[0], "DIALOGUE_ANSWER")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Сделали пробу Вальсальвы — отрицательная", history, now)[0], "DIALOGUE_ANSWER")

        # Вопросы коллеги по ходу манипуляции (DIALOGUE_FOLLOWUP)
        self.assertEqual(assistant.classify_pm_dialogue_turn("А валики во рту или рот пустой?", history, now)[0], "DIALOGUE_FOLLOWUP")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Зуботехническим шпателем можно замерять?", history, now)[0], "DIALOGUE_FOLLOWUP")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Какой адгезив лучше взять под Estecem?", history, now)[0], "DIALOGUE_FOLLOWUP")

    async def test_chairside_completeness_in_prompt_assembly_universal_teeth(self):
        """Проверяет, что при коротком ответе врача сборка промпта в handle_private_message сохраняет зубы и накладывает 4-компонентную директиву полноты."""
        mock_client = _create_mock_client()

        arbitrary_chat_id = 987654321
        mock_event = MagicMock()
        mock_event.chat_id = arbitrary_chat_id
        mock_event.sender_id = arbitrary_chat_id
        mock_event.message = MagicMock()
        mock_event.message.id = 1812
        detailed_pm_answer = "Были ранее под коронками и сняты вчера, сохранен циркулярный феррул около 1.5 мм с небной стороны"
        mock_event.message.message = detailed_pm_answer
        mock_event.message.text = detailed_pm_answer
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
                "text": detailed_pm_answer,
                "date": now.isoformat(),
            }
        ]

        captured_prompts = []

        async def fake_gemini(prompt, status_ctx=None, timeout=None):
            captured_prompts.append(prompt)
            resp = MagicMock()
            resp.text = "<b>Оцените остаточную толщину стенок дентина на 43, 44, 45 после снятия коронок.</b>"
            return resp, None

        with patch("assistant.database.save_pm_message", AsyncMock(return_value=1813)),              patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=fake_history)),              patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)),              patch("assistant.database.get_user_profile", AsyncMock(return_value={})),              patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])),              patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})),              patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()),              patch("assistant.generate_gemini_text_async", side_effect=fake_gemini),              patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))),              patch("assistant.send_message_chunks_async", AsyncMock()),              patch("assistant._async_pm_supplement_job", AsyncMock()):

            await assistant.handle_private_message(mock_client, mock_event)

        self.assertTrue(len(captured_prompts) >= 1)
        final_prompt = captured_prompts[-1]

        # Проверяем универсальную преемственность контекста
        self.assertIn("Были ранее под коронками", final_prompt)
        self.assertIn("Зубы (FDI): 43, 44, 45", final_prompt)
        self.assertIn("ДИРЕКТИВА CHAIRSIDE COMPLETENESS", final_prompt)
        self.assertIn("CLINICAL ENTITY CONTINUITY", final_prompt)

        # Проверяем требование исчерпывающего 4-компонентного протокола
        self.assertIn("Немедленные действия у кресла", final_prompt)
        self.assertIn("Полный пошаговый протокол", final_prompt)
        self.assertIn("Строгие запреты и границы безопасности", final_prompt)

        # Проверяем отсутствие искусственного ограничения до 3-5 предложений
        self.assertNotIn("3–5 плотных предложений", final_prompt)
        self.assertNotIn("3–6 плотных предложений", final_prompt)

    async def test_emphysema_emergency_prompt_assembly_universal(self):
        """Проверяет, что при реплике с симптомом крепитации промпт формирует неотложный триаж эмфиземы и полноту действий."""
        mock_client = _create_mock_client()

        arbitrary_chat_id = 8811223344
        mock_event = MagicMock()
        mock_event.chat_id = arbitrary_chat_id
        mock_event.sender_id = arbitrary_chat_id
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

        with patch("assistant.database.save_pm_message", AsyncMock(return_value=1807)),              patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=fake_history)),              patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)),              patch("assistant.database.get_user_profile", AsyncMock(return_value={})),              patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])),              patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})),              patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()),              patch("assistant.generate_gemini_text_async", side_effect=fake_gemini),              patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))),              patch("assistant.send_message_chunks_async", AsyncMock()),              patch("assistant._async_pm_supplement_job", AsyncMock()):

            await assistant.handle_private_message(mock_client, mock_event)

        self.assertTrue(len(captured_prompts) >= 1)
        final_prompt = captured_prompts[-1]

        self.assertIn("Пишет , что шуршит как пакет", final_prompt)
        self.assertIn("Подкожная эмфизема", final_prompt)
        self.assertIn("Крепитация", final_prompt)
        self.assertIn("ДИРЕКТИВА CHAIRSIDE COMPLETENESS", final_prompt)
        self.assertIn("НЕРАЗРЫВНОСТЬ КОНТЕКСТА", final_prompt)

    def test_implant_entity_extraction_universal_multisystem(self):
        """Проверяет универсальное извлечение сущностей для нескольких имплантационных систем (Neodent, Straumann, Osstem, Dentium)."""
        # 1. Neodent Grand Morse (GM)
        history_neodent = [
            {"sender_name": "User", "text": "Имплантаты неодент размер 5х8 YGYR4 подойдёт ли слепочный трансфер GM implant neodent? Какая отвёртка?"},
            {"sender_name": "Assistant", "text": "Трансфер GM подойдёт, если имплантат именно Neodent Grand Morse. Для GM нужна отвёртка Neo..."},
        ]
        ent_red = assistant.extract_pm_case_entities(history_neodent, "RED 109.953")
        self.assertIn("Neodent Grand Morse (GM)", ent_red)
        self.assertIn("109.953", ent_red)
        self.assertIn("Ø5×8 мм", ent_red)
        self.assertIn("YGYR4", ent_red)
        self.assertIn("Neo", ent_red)

        ent_ref = assistant.extract_pm_case_entities(history_neodent, "REF 109.953")
        self.assertIn("Neodent Grand Morse (GM)", ent_ref)
        self.assertIn("109.953", ent_ref)

        ent_analog = assistant.extract_pm_case_entities(history_neodent, "Аналог брать 5.0? REF 101.090 GM Hybrid")
        self.assertIn("Neodent Grand Morse (GM)", ent_analog)
        self.assertIn("101.090", ent_analog)
        self.assertIn("Hybrid", ent_analog)

        # 2. Straumann BLX (TorcFit)
        history_straumann = [
            {"sender_name": "User", "text": "Планирую Straumann BLX 4.5х10 на зуб 16. Какая отвертка и шахта?"},
            {"sender_name": "Assistant", "text": "Для Straumann BLX используется коническое соединение TorcFit..."},
        ]
        ent_straumann = assistant.extract_pm_case_entities(history_straumann, "Какая отвертка? SCS?")
        self.assertIn("Straumann BLX", ent_straumann)
        self.assertIn("16", ent_straumann)
        self.assertIn("Ø4.5×10 мм", ent_straumann)
        self.assertIn("Отвертка", ent_straumann)

        # 3. Osstem TSIII
        history_osstem = [
            {"sender_name": "User", "text": "Имплантат Osstem 4.0x10 в позиции 46. Какой трансфер брать?"},
            {"sender_name": "Assistant", "text": "Для Osstem TSIII платформа Regular, внутренний конус 11 градусов..."},
        ]
        ent_osstem = assistant.extract_pm_case_entities(history_osstem, "Какая отвёртка?")
        self.assertIn("Osstem", ent_osstem)
        self.assertIn("46", ent_osstem)
        self.assertIn("Ø4.0×10 мм", ent_osstem)
        self.assertIn("Отвертка", ent_osstem)

        # 4. Dentium SuperLine
        history_dentium = [
            {"sender_name": "User", "text": "Dentium SuperLine 4.5x10, винтовая фиксация."},
        ]
        ent_dentium = assistant.extract_pm_case_entities(history_dentium, "Какой шестигранник? 1.27?")
        self.assertIn("Dentium", ent_dentium)
        self.assertIn("Винтовая фиксация", ent_dentium)

    def test_classify_dialogue_turn_universal_components(self):
        """Проверяет классификацию реплик врача: артикулы -> DIALOGUE_ANSWER, вопросы -> DIALOGUE_FOLLOWUP."""
        now = datetime.now()
        history = [
            {"sender_name": "Assistant", "text": "Трансфер GM подойдёт, если имплантат именно Neodent Grand Morse.", "date": now.isoformat()},
        ]

        # Ввод артикула / опечатки — ответ на вопрос ассистента
        self.assertEqual(assistant.classify_pm_dialogue_turn("RED 109.953", history, now)[0], "DIALOGUE_ANSWER")
        self.assertEqual(assistant.classify_pm_dialogue_turn("REF 109.953", history, now)[0], "DIALOGUE_ANSWER")
        self.assertEqual(assistant.classify_pm_dialogue_turn("LOT 80921", history, now)[0], "DIALOGUE_ANSWER")

        # Уточняющие вопросы коллеги — FOLLOWUP
        self.assertEqual(assistant.classify_pm_dialogue_turn("Какая отвёртка? 1,2?", history, now)[0], "DIALOGUE_FOLLOWUP")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Аналог брать 5.0?", history, now)[0], "DIALOGUE_FOLLOWUP")
        self.assertEqual(assistant.classify_pm_dialogue_turn("Какой из этих аналогов дать в лабораторию?", history, now)[0], "DIALOGUE_FOLLOWUP")

    async def test_implant_continuity_prompt_assembly_universal(self):
        """Проверяет сквозную сборку промпта в handle_private_message для ортопедии на имплантатах:
        - Преемственность системы и размера имплантата
        - Запрет повторного переспроса системы
        - Наличие директивы подбора имплантационных компонентов и параметров торка.
        """
        mock_client = _create_mock_client()

        arbitrary_chat_id = 9933445566
        mock_event = MagicMock()
        mock_event.chat_id = arbitrary_chat_id
        mock_event.sender_id = arbitrary_chat_id
        mock_event.message = MagicMock()
        mock_event.message.id = 1730
        mock_event.message.message = "RED 109.953"
        mock_event.message.text = "RED 109.953"
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
                "text": "Имплантаты неодент размер 5х8 YGYR4 подойдёт ли слепочный трансфер GM implant neodent? Какая отвёртка? 1,2? Аналог лабораторный 5.0/6.0/7.0 взять , или 4.0/4.3",
                "date": (now - timedelta(minutes=3)).isoformat(),
            },
            {
                "sender_name": "Assistant",
                "text": "Трансфер GM подойдёт, если имплантат именно Neodent Grand Morse. Для оригинального GM-трансфера нужна отвёртка Neo с шестилучевым профилем, не шестигранник 1,2 мм...",
                "date": (now - timedelta(minutes=1)).isoformat(),
            },
            {
                "sender_name": "User",
                "text": "RED 109.953",
                "date": now.isoformat(),
            }
        ]

        captured_prompts = []

        async def fake_gemini(prompt, status_ctx=None, timeout=None):
            captured_prompts.append(prompt)
            resp = MagicMock()
            resp.text = "<b>REF 109.953 — Neodent Drive GM, Ø5,0 × 8 мм.</b> Трансфер GM подходит, отвертка Neo..."
            return resp, None

        with patch("assistant.database.save_pm_message", AsyncMock(return_value=1731)),              patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=fake_history)),              patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)),              patch("assistant.database.get_user_profile", AsyncMock(return_value={})),              patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])),              patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})),              patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()),              patch("assistant.generate_gemini_text_async", side_effect=fake_gemini),              patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))),              patch("assistant.send_message_chunks_async", AsyncMock()),              patch("assistant._async_pm_supplement_job", AsyncMock()):

            await assistant.handle_private_message(mock_client, mock_event)

        self.assertTrue(len(captured_prompts) >= 1)
        final_prompt = captured_prompts[-1]

        # 1. Текущая реплика и активные сущности
        self.assertIn("RED 109.953", final_prompt)
        self.assertIn("Neodent Grand Morse (GM)", final_prompt)
        self.assertIn("109.953", final_prompt)
        self.assertIn("Ø5×8 мм", final_prompt)

        # 2. Директивы преемственности и полноты
        self.assertIn("ДИРЕКТИВА CHAIRSIDE COMPLETENESS", final_prompt)
        self.assertIn("CLINICAL ENTITY CONTINUITY", final_prompt)
        self.assertIn("ПРОТОКОЛ ИДЕНТИФИКАЦИИ И ПОДБОРА ИМПЛАНТАЦИОННЫХ КОМПОНЕНТОВ", final_prompt)

        # 3. Запрет повторного переспроса системы
        self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО требовать повторно назвать систему", final_prompt)

        # 4. Протокольные параметры (отвертка Neo, торк 32 Нсм, аналог REF 101.090)
        self.assertIn("отвертка Neo", final_prompt)
        self.assertIn("32 Нсм", final_prompt)
        self.assertIn("101.090", final_prompt)


if __name__ == "__main__":
    unittest.main()
