"""
Unit tests for Natural Language Intent Router (Zero-Slash Routing)
and instant anesthesia calculation in StomChat (assistant.py).
"""
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.assertIn("Ребёнок", res)
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
        self.assertIn("Укажите вес пациента", res)

    def test_no_drug_no_weight_returns_none(self):
        res = calculate_anesthesia_instant("посчитай анестезию")
        self.assertIsNone(res)


class TestPrivateMessageRoutingIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration test for PM dispatch via handle_private_message."""

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


if __name__ == "__main__":
    unittest.main()
