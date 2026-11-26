import asyncio
"""
Unit tests for Natural Language Intent Router (Zero-Slash Routing)
and instant anesthesia calculation in StomChat (assistant.py).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_intent_test_")
import assistant
assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

from assistant import (
    INTENT_WEB_SEARCH,
    INTENT_CALCULATOR,
    INTENT_QUIZ,
    INTENT_CASE,
    INTENT_BOOKMARKS,
    INTENT_STYLE,
    INTENT_MENU,
    INTENT_HELP,
    UserIntent,
    detect_user_intent,
    calculate_anesthesia_instant,
    handle_private_message,
)


class TestUserIntentObject(unittest.TestCase):
    """Test UserIntent class behavior."""

    def test_intent_equality_and_unpacking(self):
        intent = UserIntent(INTENT_WEB_SEARCH, "биодентин")
        self.assertEqual(intent.name, INTENT_WEB_SEARCH)
        self.assertEqual(intent.query, "биодентин")
        self.assertEqual(intent, INTENT_WEB_SEARCH)
        self.assertTrue(bool(intent))
        self.assertEqual(str(intent), INTENT_WEB_SEARCH)

        name, query = intent
        self.assertEqual(name, INTENT_WEB_SEARCH)
        self.assertEqual(query, "биодентин")

    def test_none_intent(self):
        intent = UserIntent(None)
        self.assertIsNone(intent.name)
        self.assertEqual(intent.query, "")
        self.assertFalse(bool(intent))
        self.assertEqual(intent, None)
        self.assertNotEqual(intent, INTENT_MENU)


class TestIntentClassifier(unittest.TestCase):
    """Test intent classifier detect_user_intent."""

    def test_user_requested_exact_phrases(self):
        """Verify all exact test phrases specified in user prompt."""
        test_cases = [
            # a) INTENT_WEB_SEARCH
            ("погугли биодентин", INTENT_WEB_SEARCH, "биодентин"),
            ("найди статьи про BOPT", INTENT_WEB_SEARCH, "bopt"),
            ("что говорит pubmed о винирах", INTENT_WEB_SEARCH, "винирах"),
            ("какие свежие исследования по ирригации", INTENT_WEB_SEARCH, "ирригации"),
            ("поищи в интернете протокол фиксации", INTENT_WEB_SEARCH, "протокол фиксации"),
            ("найди протокол BOPT", INTENT_WEB_SEARCH, "bopt"),

            # b) INTENT_CALCULATOR
            ("посчитай анестезию", INTENT_CALCULATOR, "посчитай анестезию"),
            ("сколько карпул артикаина на 70 кг", INTENT_CALCULATOR, "сколько карпул артикаина на 70 кг"),
            ("дозировка скандонеста ребенку", INTENT_CALCULATOR, "дозировка скандонеста ребенку"),
            ("рассчитай дозу", INTENT_CALCULATOR, "рассчитай дозу"),

            # c) INTENT_QUIZ
            ("хочу квиз", INTENT_QUIZ, ""),
            ("давай викторину", INTENT_QUIZ, ""),
            ("проверь мои знания", INTENT_QUIZ, ""),
            ("дай вопрос", INTENT_QUIZ, ""),

            # d) INTENT_CASE
            ("давай кейс", INTENT_CASE, ""),
            ("хочу клинический случай", INTENT_CASE, ""),
            ("сыграем в диагностику", INTENT_CASE, ""),
            ("запусти симулятор", INTENT_CASE, ""),

            # e) INTENT_BOOKMARKS
            ("мои закладки", INTENT_BOOKMARKS, ""),
            ("что я сохранил", INTENT_BOOKMARKS, ""),
            ("покажи сохраненки", INTENT_BOOKMARKS, ""),

            # f) INTENT_STYLE
            ("смени стиль", INTENT_STYLE, ""),
            ("настройки стиля", INTENT_STYLE, ""),
            ("хочу другой тон", INTENT_STYLE, ""),

            # g) INTENT_MENU / INTENT_HELP -> показ главного меню
            ("меню", INTENT_MENU, ""),
            ("главное меню", INTENT_MENU, ""),
            ("помощь", INTENT_MENU, ""),
            ("что ты умеешь", INTENT_MENU, ""),
            ("инструкция", INTENT_MENU, ""),
        ]

        for text, expected_intent, expected_query in test_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertEqual(res.name, expected_intent, f"Failed for '{text}': got {res.name}, expected {expected_intent}")
                if expected_query:
                    self.assertEqual(res.query.lower(), expected_query.lower())

    def test_additional_web_search_variants(self):
        cases = [
            ("загугли протокол препарирования", "протокол препарирования"),
            ("поищи в сети гайдлайн ESE", "гайдлайн ese"),
            ("pubmed: biodentine perforation", "biodentine perforation"),
            ("пабмед: виниры", "виниры"),
            ("есть ли свежие исследования по MTA", "mta"),
        ]
        for text, expected_query in cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertEqual(res.name, INTENT_WEB_SEARCH)
                self.assertEqual(res.query.lower(), expected_query.lower())

    def test_calculator_variations(self):
        cases = [
            "калькулятор",
            "калькулятор анестезии",
            "шпаргалка по анестезии",
            "дозировка артикаина",
            "посчитай дозировку скандонеста",
            "сколько карпул ультракаина на 80 кг",
            "доза мепивакаина",
            "рассчитай карпулы",
            "посчитай мне анестезию",
        ]
        for text in cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertEqual(res.name, INTENT_CALCULATOR)

    def test_quiz_and_case_variations(self):
        quiz_cases = ["викторина", "квиз", "клинический квиз", "проэкзаменуй меня", "задай клинический вопрос", "хочу тест"]
        for text in quiz_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertEqual(res.name, INTENT_QUIZ)

        case_cases = ["клинический симулятор", "интерактивный кейс", "начать симулятор", "сыграть в кейс", "запусти клинический кейс"]
        for text in case_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertEqual(res.name, INTENT_CASE)

    def test_bookmarks_with_arguments(self):
        res = detect_user_intent("мои закладки эндодонтия")
        self.assertEqual(res.name, INTENT_BOOKMARKS)
        self.assertEqual(res.query, "эндодонтия")

        res2 = detect_user_intent("покажи что я сохранил про BOPT")
        self.assertEqual(res2.name, INTENT_BOOKMARKS)
        self.assertEqual(res2.query.lower(), "про bopt")

    def test_false_positive_guards(self):
        """Verify clinical questions are not falsely captured by intent router."""
        # 1. Emergency assistance must not trigger INTENT_HELP
        emergency_cases = [
            "первая помощь при анафилактическом шоке",
            "неотложная помощь при обмороке в кресле",
            "помощь при передозировке анестетиком",
            "доврачебная помощь при аспирации бора",
        ]
        for text in emergency_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertNotEqual(res.name, INTENT_HELP)
                self.assertNotEqual(res.name, INTENT_MENU)

        # 2. Answers to active quiz must not trigger INTENT_QUIZ
        quiz_answers = ["Мой ответ А", "ответ В", "вариант C", "мой ответ Б: нужно КТ"]
        for text in quiz_answers:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertNotEqual(res.name, INTENT_QUIZ)

        # 3. Real patient case consultations must not trigger interactive simulator
        real_cases = [
            "Клинический случай: пациент 45 лет, жалобы на боль при накусывании",
            "у меня пациентка 32 года, зуб 36 глубокий кариес",
            "разбор случая: зуб 47 периодонтит",
            "жалобы на ночные боли в зубе 24",
        ]
        for text in real_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertNotEqual(res.name, INTENT_CASE)

        # 4. Standard clinical queries
        dental_queries = [
            "какой протокол ирригации в 46 зубе?",
            "как лечить верхушечный периодонтит?",
            "чем травить дисиликат лития?",
        ]
        for text in dental_queries:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertIn(res.name, [None, INTENT_WEB_SEARCH])

        # 5. Red-Team: Antibiotics and NSAIDs must not be hijacked by INTENT_CALCULATOR
        pharma_cases = [
            "дозировка амоксиклава при периостите у ребенка 25 кг",
            "доза ибупрофена после удаления ретинированного зуба 3.8",
            "максимальная доза нурофена ребенку 4 года",
            "дозировка линкомицина взрослым",
        ]
        for text in pharma_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertNotEqual(res.name, INTENT_CALCULATOR)

        # 6. Red-Team: Acute cardiac pathology / crisis must not be hijacked by instant calculator
        somatic_cases = [
            "дозировка артикаина при гипертоническом кризе",
            "посчитай мне дозу ультракаина у пациента с инфарктом миокарда в анамнезе 2 месяца назад",
            "максимальная доза мепивакаина если у пациента АВ-блокада 2 степени",
        ]
        for text in somatic_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertNotEqual(res.name, INTENT_CALCULATOR)

        # 5. Red-Team: Antibiotics and NSAIDs must not be hijacked by INTENT_CALCULATOR
        pharma_cases = [
            "дозировка амоксиклава при периостите у ребенка 25 кг",
            "доза ибупрофена после удаления ретинированного зуба 3.8",
            "максимальная доза нурофена ребенку 4 года",
            "дозировка линкомицина взрослым",
        ]
        for text in pharma_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertNotEqual(res.name, INTENT_CALCULATOR)

        # 6. Red-Team: Acute cardiac pathology / crisis must not be hijacked by instant calculator
        somatic_cases = [
            "дозировка артикаина при гипертоническом кризе",
            "посчитай мне дозу ультракаина у пациента с инфарктом миокарда в анамнезе 2 месяца назад",
            "максимальная доза мепивакаина если у пациента АВ-блокада 2 степени",
        ]
        for text in somatic_cases:
            with self.subTest(text=text):
                res = detect_user_intent(text)
                self.assertNotEqual(res.name, INTENT_CALCULATOR)


class TestAnesthesiaInstantCalculator(unittest.TestCase):
    """Test instant anesthesia calculation function calculate_anesthesia_instant."""

    def test_articaine_adult_normal(self):
        # 70 kg * 7 mg/kg = 490 mg (below 500 mg ceiling)
        # 490 / 68 = 7.205 carpules -> safe floor = 7 carpules
        res = calculate_anesthesia_instant("сколько карпул артикаина на 70 кг")
        self.assertIsNotNone(res)
        self.assertIn("Артикаин 4%", res)
        self.assertIn("70 кг", res)
        self.assertIn("490 мг", res)
        self.assertIn("до 7 карпул", res)
        self.assertNotIn("Сработал абсолютный потолок", res)

    def test_articaine_adult_ceiling(self):
        # 85 kg * 7 mg/kg = 595 mg -> triggers 500 mg ceiling
        # 500 / 68 = 7.35 carpules -> safe floor = 7 carpules
        res = calculate_anesthesia_instant("артикаин 4% на 85 кг")
        self.assertIsNotNone(res)
        self.assertIn("Артикаин 4%", res)
        self.assertIn("85 кг", res)
        self.assertIn("Сработал абсолютный потолок 500 мг", res)
        self.assertIn("до 7 карпул", res)

    def test_articaine_child(self):
        # 20 kg * 5 mg/kg = 100 mg
        # 100 / 68 = 1.47 carpules -> safe floor = 1 carpule
        res = calculate_anesthesia_instant("артикаин ребенку 20 кг")
        self.assertIsNotNone(res)
        self.assertIn("ребёнок", res.lower())  # регистр не критичен: "ребёнок" или "Ребёнок"
        self.assertIn("20 кг", res)
        self.assertIn("100 мг", res)
        self.assertIn("до 1 карпулы", res)

    def test_mepivacaine_scandonest_child(self):
        # 20 kg * 4.4 mg/kg = 88 mg
        # 88 / 54 = 1.63 carpules -> safe floor = 1 carpule
        res = calculate_anesthesia_instant("дозировка скандонеста ребенку 20 кг")
        self.assertIsNotNone(res)
        self.assertIn("Мепивакаин 3%", res)
        self.assertIn("88 мг", res)
        self.assertIn("до 1 карпулы", res)

    def test_mepivacaine_adult_ceiling(self):
        # 100 kg * 4.4 = 440 mg -> triggers 400 mg ceiling
        # 400 / 54 = 7.40 carpules -> safe floor = 7 carpules
        res = calculate_anesthesia_instant("мепивакаин на 100 кг")
        self.assertIsNotNone(res)
        self.assertIn("Сработал абсолютный потолок 400 мг", res)
        self.assertIn("до 7 карпул", res)

    def test_lidocaine_adult_normal(self):
        # 60 kg * 7 mg/kg = 420 mg
        # 420 / 36 = 11.66 carpules -> safe floor = 11 carpules
        res = calculate_anesthesia_instant("лидокаин 2% на 60 кг")
        self.assertIsNotNone(res)
        self.assertIn("Лидокаин 2%", res)
        self.assertIn("420 мг", res)
        self.assertIn("до 11 карпул", res)

    def test_missing_weight_returns_drug_guide(self):
        res = calculate_anesthesia_instant("дозировка скандонеста ребенку")
        self.assertIsNotNone(res)
        self.assertIn("Мепивакаин 3%", res)
        self.assertIn("4.4 мг/кг", res)
        self.assertIn("Укажите", res)  # подсказка о весе — любая форма ("вес пациента" / "вес ребенка")

    def test_no_drug_no_weight_returns_none(self):
        res = calculate_anesthesia_instant("посчитай анестезию")
        self.assertIsNone(res)


class TestPrivateMessageRoutingIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration test for PM dispatch via handle_private_message."""

    def setUp(self):
        self._orig_state_path = assistant.STATE_PATH
        self._orig_state_tmp = assistant.STATE_TMP_PATH
        self._orig_state_bak = assistant.STATE_BAK_PATH
        assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
        assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
        assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

    def tearDown(self):
        assistant.STATE_PATH = self._orig_state_path
        assistant.STATE_TMP_PATH = self._orig_state_tmp
        assistant.STATE_BAK_PATH = self._orig_state_bak

    async def test_zero_slash_calc_instant_dispatch(self):
        mock_bot = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 999111
        mock_event.message.message = "сколько карпул артикаина на 70 кг"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.document = None
        mock_event.message.sticker = None

        with patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant.database.save_pm_message", new_callable=AsyncMock), \
             patch("assistant.database.get_user_interactive_state", return_value=None):
            await handle_private_message(mock_bot, mock_event)

        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args.kwargs
        self.assertIn("Артикаин 4%", call_kwargs["message"])
        self.assertIn("до 7 карпул", call_kwargs["message"])

    async def test_zero_slash_menu_dispatch(self):
        mock_bot = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 999222
        mock_event.message.message = "главное меню"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.document = None
        mock_event.message.sticker = None

        with patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant.database.save_pm_message", new_callable=AsyncMock), \
             patch("assistant.database.get_user_interactive_state", return_value=None):
            await handle_private_message(mock_bot, mock_event)

        mock_bot.send_message.assert_called()
        sent_messages = [call.kwargs.get("message", "") for call in mock_bot.send_message.call_args_list]
        self.assertTrue(any("StomChat AI" in m or "Приветствую!" in m or "Клинический навигатор" in m for m in sent_messages))

    async def test_zero_slash_help_dispatch(self):
        mock_bot = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 999333
        mock_event.message.message = "что ты умеешь"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.document = None
        mock_event.message.sticker = None

        with patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant.database.save_pm_message", new_callable=AsyncMock), \
             patch("assistant.database.get_user_interactive_state", return_value=None):
            await handle_private_message(mock_bot, mock_event)

        mock_bot.send_message.assert_called()
        sent_messages = [call.kwargs.get("message", "") for call in mock_bot.send_message.call_args_list]
        self.assertTrue(any("StomChat AI" in m or "База Знаний" in m or "клинический ассистент" in m for m in sent_messages))




class TestIntentRouterGroupTriageAndParity(unittest.TestCase):
    """Verify intent_router.py parity, group triage, dispute referee, and lock manager."""

    def test_intent_router_zero_slash_parity(self):
        import intent_router
        phrases = [
            ("погугли биодентин", intent_router.INTENT_WEB_SEARCH, "биодентин"),
            ("посчитай анестезию", intent_router.INTENT_CALCULATOR, "посчитай анестезию"),
            ("хочу квиз", intent_router.INTENT_QUIZ, ""),
            ("давай кейс", intent_router.INTENT_CASE, ""),
            ("мои закладки", intent_router.INTENT_BOOKMARKS, ""),
            ("смени стиль", intent_router.INTENT_STYLE, ""),
            ("главное меню", intent_router.INTENT_MENU, ""),
        ]
        for t, exp_intent, exp_query in phrases:
            res = intent_router.detect_user_intent(t)
            self.assertEqual(res.name, exp_intent, f"Failed for {t}")
            if exp_query:
                self.assertEqual(res.query.lower(), exp_query.lower())

    def test_intent_router_anesthesia_calculator(self):
        import intent_router
        res = intent_router.calculate_anesthesia_instant("сколько карпул артикаина на 70 кг")
        self.assertIsNotNone(res)
        self.assertIn("Артикаин 4%", res)
        self.assertIn("до 7 карпул", res)

    def test_should_passively_engage_filters_noise_and_respects_config(self):
        import intent_router
        import config
        # 1. Trivial noise is filtered
        noise_samples = ["ок", "привет", "лол", "+", "да", "спасибо"]
        for noise in noise_samples:
            should_engage, reason = intent_router.should_passively_engage(
                text=noise, has_media=False, is_group=True, chat_id=-100, sender_id=123
            )
            self.assertFalse(should_engage)

        # 2. Addressed to other user (@username) is filtered (with active passive mode)
        with patch.object(config, "SUMMON_ONLY_GROUP_MODE", False),              patch.object(config, "ENABLE_PASSIVE_TEXT", True):
            should_engage, reason = intent_router.should_passively_engage(
                text="@doctor_ivanov посмотри этот клинический снимок",
                has_media=False, is_group=True, chat_id=-100, sender_id=123
            )
            self.assertFalse(should_engage)
            self.assertEqual(reason, "addressed_to_other_user")

        # 3. Not in group
        should_engage, reason = intent_router.should_passively_engage(
            text="Клинический разбор зуба 46", has_media=False, is_group=False
        )
        self.assertFalse(should_engage)
        self.assertEqual(reason, "not_a_group")

    def test_detect_clinical_dispute_distinguishes_healthy_debate_from_toxic_conflict(self):
        import intent_router
        # 1. Toxic conflict
        toxic_text = "Вы вообще стоматолог? Руки оторвать за такую препаровку, полная лажа и бракодел!"
        res_toxic = intent_router.detect_clinical_dispute(toxic_text)
        self.assertTrue(res_toxic.is_dispute)
        self.assertTrue(res_toxic.should_intervene)
        self.assertEqual(res_toxic.dispute_type, "TOXIC_CONFLICT")

        # 2. Dangerous pseudoscience
        quack_text = "Коллеги, закладывайте резорцин-формалин в каналы, проверенный метод"
        res_quack = intent_router.detect_clinical_dispute(quack_text)
        self.assertTrue(res_quack.is_dispute)
        self.assertTrue(res_quack.should_intervene)
        self.assertEqual(res_quack.dispute_type, "DANGEROUS_PSEUDOSCIENCE")

        # 3. Healthy debate
        healthy_chain = [
            "Доктор 1: Я считаю, здесь показана культевая вкладка и коронка",
            "Доктор 2: А я бы рекомендовал стекловолоконный штифт и композитный билдап",
            "Доктор 1: При ферруле меньше 1.5 мм штифт даст расклинивающий эффект",
            "Доктор 2: Но современные адгезивы 8 поколения компенсируют нагрузку"
        ]
        res_healthy = intent_router.detect_clinical_dispute("Что думаете?", context_msgs=healthy_chain)
        self.assertTrue(res_healthy.is_dispute)
        self.assertFalse(res_healthy.should_intervene, "Bot mistakenly intervened in healthy, respectful peer debate!")
        self.assertEqual(res_healthy.dispute_type, "HEALTHY_DEBATE")

    def test_dispute_referee_system_instructions_bans_mentor_tone_and_stamps(self):
        import intent_router
        instructions = intent_router.DISPUTE_REFEREE_SYSTEM_INSTRUCTIONS
        self.assertIn("ЗАПРЕТ МЕНТОРСТВА", instructions)
        self.assertIn("Вердикт", instructions)
        self.assertIn("EBM-Арбитраж", instructions)
        self.assertIn("КОНТРАСТНЫЕ ПРИМЕРЫ", instructions)
        self.assertIn("ПЛОХО", instructions)
        self.assertIn("ХОРОШО", instructions)

    def test_group_triage_lock_manager_prevents_race_conditions(self):
        import intent_router
        async def _test():
            mgr = intent_router.GroupTriageLockManager()
            chat_id = -100777
            slot1 = await mgr.acquire_triage_slot(chat_id, sender_id=1, cooldown_seconds=10.0)
            self.assertTrue(slot1)
            # Parallel request in same chat while slot1 is in-flight must be rejected
            slot2 = await mgr.acquire_triage_slot(chat_id, sender_id=2, cooldown_seconds=10.0)
            self.assertFalse(slot2)
            # Release slot
            await mgr.release_triage_slot(chat_id, mark_success=True)
            # Immediate next request must be blocked by cooldown
            slot3 = await mgr.acquire_triage_slot(chat_id, sender_id=3, cooldown_seconds=10.0)
            self.assertFalse(slot3)
        asyncio.run(_test())

    def test_false_positive_regression_guards(self):
        import config
        import intent_router
        import inflight_triage
        import reaction_engine
        import clinical_anatomy_guard

        # 1. intent_router: бытовые/клинические слова не должны провоцировать арбитраж или блокироваться как жалоба на бота
        self.assertFalse(intent_router.detect_clinical_dispute("У пациентки ужасно болит зуб 36").should_intervene)
        self.assertFalse(intent_router.detect_clinical_dispute("Выброси старую лампу, купи нормальную").should_intervene)
        with patch.object(config, "SUMMON_ONLY_GROUP_MODE", False), patch.object(config, "ENABLE_PASSIVE_TEXT", True):
            should_eng, reason = intent_router.should_passively_engage(
                text="Хватит ли 15 минут экспозиции гипохлорита в канале?",
                has_media=False,
                is_group=True,
            )
            self.assertTrue(should_eng, f"Unexpectedly blocked clinical question: {reason}")

            # 2. Обращение к другому @username блокируется даже при пустом BOT_USERNAME
            with patch.object(config, "BOT_USERNAME", ""):
                ok, rsn = intent_router.should_passively_engage(
                    text="@colleague посмотри снимок 36 зуба пожалуйста",
                    has_media=False,
                    is_group=True,
                )
                self.assertFalse(ok)
                self.assertEqual(rsn, "addressed_to_other_user")

        # 3. inflight_triage: перенос неразобранных фоллоу-апов не падает с AttributeError
        async def _check_inflight():
            mgr = inflight_triage.InFlightManager()
            s1 = await mgr.register_session(-1001, 42, 100, "Зуб 36")
            s1.record_followup(101, "и еще свищ на десне", has_media=False)
            s2 = await mgr.register_session(-1001, 42, 102, "Зуб 36 и свищ")
            restored = s2.pop_unprocessed_followups()
            self.assertTrue(any(f["msg_id"] == 101 and "свищ" in f["text"] for f in restored))

            from types import SimpleNamespace

            async def _mock_llm(prompt, ctx, timeout=25.0):
                return SimpleNamespace(text='{"action":"SUPPLEMENT","merged_query":"Адентия 3.6: имплант или мост?","reason":"added clinical choice"}'), None

            triage_res = await inflight_triage.run_inflight_followup_triage(
                original_query="Пациент с адентией 3.6",
                followups=[{"text": "ну что, имплант ставить или мостовидный?"}],
                generate_gemini_fn=_mock_llm,
            )
            self.assertEqual(triage_res["action"], "SUPPLEMENT")
        asyncio.run(_check_inflight())

        # 4. reaction_engine: клинические вопросы, уступы и JSON с хвостом
        self.assertFalse(reaction_engine.is_meta_or_bot_complaint("Хватит ли 2 мм феррула под коронку?"))
        self.assertTrue(reaction_engine.is_meta_or_bot_complaint("Хватит!"))
        self.assertFalse(reaction_engine.is_clinical_complication("Отпрепарировали ступенчатый уступ под циркон"))
        self.assertTrue(reaction_engine.is_clinical_complication("Образовалась ступенька в МВ2"))
        self.assertFalse(reaction_engine.is_question_or_doubt("Как же круто лег композит"))
        self.assertFalse(reaction_engine.is_question_or_doubt("https://example.com/path?utm_source=tg посмотрите кейс"))
        self.assertTrue(reaction_engine.is_question_or_doubt("Как обойти ступеньку? У зуба 36 изгиб"))
        parsed = reaction_engine.parse_batch_reactions_json('[{"msg_id": 55, "reaction": "👍", "reason": "ok"}] [note]')
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["msg_id"], 55)

        # 5. clinical_anatomy_guard: смешанный кейс верхней и нижней челюсти в одном тексте
        ok_mixed, viol_mixed = clinical_anatomy_guard.verify_and_guard_anatomy(
            "Зуб 16: пройден небный канал. Зуб 36: обработаны медиальный и дистальный каналы."
        )
        self.assertTrue(ok_mixed, f"False positive cross-jaw violations: {viol_mixed}")
        ok_bad, viol_bad = clinical_anatomy_guard.verify_and_guard_anatomy(
            "Зуб 16: пройден небный канал. Зуб 36: обнаружен небный корень."
        )
        self.assertFalse(ok_bad)
        self.assertTrue(viol_bad)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(_TMPDIR, ignore_errors=True)
