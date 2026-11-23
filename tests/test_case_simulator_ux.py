"""
Тесты UI/UX, синхронизации и защиты от race condition для клинического симулятора кейсов.
Проверяет:
1. Синхронизацию команды /case и кнопки case:start (единый 4-шаговый симулятор).
2. Защиту от двойного клика (Double-click / stale buttons) и блокировку устаревших кнопок.
3. Парсинг текстовых ответов врача (буквы A-D, кириллица А-Г, цифры 1-4, фразы).
4. Извлечение полного описания варианта из текста кейса.
5. UI-индикаторы прогресса [🟩⬜⬜⬜], таймаут сессии (> 1 ч) и финальный дебрифинг.
"""

import asyncio
import json
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Убедимся, что корень проекта в sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import assistant
from assistant import (
    CASE_SESSION_TIMEOUT_SECONDS,
    CASE_TOTAL_STEPS,
    _ACTIVE_CASE_USERS,
    clean_case_step_header,
    ensure_case_options,
    extract_option_text_from_case,
    format_case_progress_bar,
    handle_interactive_case_step,
    parse_case_option_choice,
    start_interactive_case,
)


class TestCaseOptionParser(unittest.TestCase):
    """Тестирование нормализации и распознавания ответов врача у кресла."""

    def test_latin_letters(self):
        for letter in ("A", "B", "C", "D"):
            self.assertEqual(parse_case_option_choice(letter), letter)
            self.assertEqual(parse_case_option_choice(letter.lower()), letter)
            self.assertEqual(parse_case_option_choice(f"{letter})"), letter)
            self.assertEqual(parse_case_option_choice(f"{letter}."), letter)

    def test_cyrillic_letters(self):
        cyr_mapping = {
            "А": "A", "а": "A",
            "Б": "B", "б": "B",
            "В": "C", "в": "C",  # 3-я буква русского алфавита
            "С": "C", "с": "C",  # буква С (key-match / visual)
            "Г": "D", "г": "D",  # 4-я буква русского алфавита
            "Д": "D", "д": "D",
        }
        for cyr, lat in cyr_mapping.items():
            self.assertEqual(parse_case_option_choice(cyr), lat)
            self.assertEqual(parse_case_option_choice(f"{cyr})"), lat)
            self.assertEqual(parse_case_option_choice(f"{cyr}."), lat)

    def test_digit_choices(self):
        digit_mapping = {"1": "A", "2": "B", "3": "C", "4": "D"}
        for dig, lat in digit_mapping.items():
            self.assertEqual(parse_case_option_choice(dig), lat)
            self.assertEqual(parse_case_option_choice(f"{dig})"), lat)
            self.assertEqual(parse_case_option_choice(f"{dig}."), lat)

    def test_natural_language_phrases(self):
        phrases = [
            ("вариант 1", "A"),
            ("вариант А", "A"),
            ("вариант а", "A"),
            ("выбираю 1", "A"),
            ("выбираю вариант A", "A"),
            ("ответ: 1", "A"),
            ("п. 1", "A"),
            ("пункт 1", "A"),
            ("вариант 2", "B"),
            ("выбираю Б", "B"),
            ("мой выбор вариант B", "B"),
            ("ответ 2", "B"),
            ("п 2", "B"),
            ("вариант 3", "C"),
            ("выбираю C", "C"),
            ("выбираю В", "C"),
            ("голосую за вариант 3", "C"),
            ("вариант 4", "D"),
            ("выбираю D", "D"),
            ("мой выбор - 4", "D"),
            ("пункт 4", "D"),
        ]
        for phrase, expected in phrases:
            self.assertEqual(parse_case_option_choice(phrase), expected, f"Failed on '{phrase}'")

    def test_free_form_clinical_text_is_not_matched(self):
        free_forms = [
            "Назначу КЛКТ 5х5 зуба 2.6 и прицельный снимок",
            "Проведу инфильтрационную анестезию артикаином 1:100000",
            "Будем удалять зуб по неотложным показаниям",
            "Антибиотики системно не показаны, раскроем полость зуба",
            "Вариант консервативного лечения здесь рискован из-за трещины",
        ]
        for text in free_forms:
            self.assertIsNone(parse_case_option_choice(text), f"Text should not be parsed as option: '{text}'")


class TestCaseTextExtractionAndUI(unittest.TestCase):
    """Тестирование извлечения текста опций, очистки заголовков и прогресс-бара."""

    def setUp(self):
        self.sample_case = (
            "🎮 <b>Клинический симулятор (Шаг 1 из 4):</b>\n\n"
            "<b>Пациент:</b> 34 года, боли в зубе 2.6.\n"
            "❓ <b>Клинический вопрос:</b> Каков ваш первый шаг?\n\n"
            "A) Необратимый пульпит. Анестезия, коффердам, раскрытие, NaOCl 3%.\n"
            "B) Обратимый пульпит. Прямое покрытие пульпы МТА и постоянная пломба.\n"
            "C) Острый периодонтит. Инструментация до апекса, Ca(OH)2.\n"
            "D) Назначить системные антибиотики и отправить на КТ.\n\n"
            "<i>Выберите вариант кнопкой ниже:</i>"
        )

    def test_extract_option_text(self):
        self.assertEqual(
            extract_option_text_from_case(self.sample_case, "A"),
            "Необратимый пульпит. Анестезия, коффердам, раскрытие, NaOCl 3%."
        )
        self.assertEqual(
            extract_option_text_from_case(self.sample_case, "B"),
            "Обратимый пульпит. Прямое покрытие пульпы МТА и постоянная пломба."
        )
        self.assertEqual(
            extract_option_text_from_case(self.sample_case, "C"),
            "Острый периодонтит. Инструментация до апекса, Ca(OH)2."
        )
        self.assertEqual(
            extract_option_text_from_case(self.sample_case, "D"),
            "Назначить системные антибиотики и отправить на КТ."
        )

    def test_progress_bar_format(self):
        self.assertEqual(format_case_progress_bar(1, 4), "[🟩⬜⬜⬜]")
        self.assertEqual(format_case_progress_bar(2, 4), "[🟩🟩⬜⬜]")
        self.assertEqual(format_case_progress_bar(3, 4), "[🟩🟩🟩⬜]")
        self.assertEqual(format_case_progress_bar(4, 4), "[🟩🟩🟩🟩]")

    def test_clean_step_header(self):
        samples = [
            ("🎮 <b>Клинический симулятор (Шаг 2 из 4):</b>\n\nОсмотр показывает...", "Осмотр показывает..."),
            ("<b>Шаг 2 из 4: Клиническая динамика</b>\nПациент отмечает снижение болей...", "Клиническая динамика</b>\nПациент отмечает снижение болей..."),
            ("Шаг 3 из 4.\n\nПроведена инструментация...", "Проведена инструментация..."),
            ("Клинический разбор случая. Шаг 2 из 4:\n\nПосле раскрытия...", "После раскрытия..."),
        ]
        for raw, expected in samples:
            self.assertEqual(clean_case_step_header(raw), expected)


class TestCaseSimulatorFlowAndSafety(unittest.IsolatedAsyncioTestCase):
    """Тестирование синхронизации /case и case:start, защиты от double-click и таймаута."""

    def setUp(self):
        _ACTIVE_CASE_USERS.clear()

    def tearDown(self):
        _ACTIVE_CASE_USERS.clear()

    async def test_double_click_protection_on_start(self):
        """Проверяет, что одновременный второй клик на старт блокируется и не создает гонку."""
        chat_id = 999001
        _ACTIVE_CASE_USERS.add(chat_id)

        mock_event = AsyncMock()
        mock_event.answer = AsyncMock()
        mock_bot = AsyncMock()

        # Попытка запуска симулятора во время уже активного процесса
        await start_interactive_case(mock_bot, chat_id, callback_event=mock_event)

        # Должен сработать моментальный ответ на callback без обращения к LLM и базе
        mock_event.answer.assert_called_once_with(
            "⏳ Симулятор уже подготавливается, подождите...", alert=False
        )
        mock_bot.send_message.assert_not_called()

    async def test_double_click_protection_on_step(self):
        """Проверяет, что второй клик по варианту блокируется, пока идет обработка первого."""
        chat_id = 999002
        _ACTIVE_CASE_USERS.add(chat_id)

        mock_event = AsyncMock()
        mock_event.answer = AsyncMock()
        mock_bot = AsyncMock()

        user_state = {
            "current_step": 1,
            "state_type": "case",
            "history": json.dumps({"messages": [], "last_updated": time.time()})
        }

        await handle_interactive_case_step(
            mock_bot, chat_id, "B", user_state, callback_event=mock_event
        )

        mock_event.answer.assert_called_once_with(
            "⏳ Уже обрабатываю ваш ход, пожалуйста, подождите...", alert=False
        )
        mock_bot.send_message.assert_not_called()

    @patch("database.get_user_interactive_state")
    @patch("database.set_user_interactive_state")
    @patch("database.save_pm_message")
    @patch("assistant.generate_gemini_text_async")
    @patch("assistant.send_message_chunks_async")
    @patch("assistant.check_user_cooldown", return_value=0)
    async def test_case_start_via_command_creates_4_options_and_state(
        self, mock_cooldown, mock_send_chunks, mock_gemini, mock_save_pm, mock_set_state, mock_get_state
    ):
        """Проверяет, что текстовая команда /case запускает полноценный 4-вариантный симулятор."""
        chat_id = 999003
        mock_get_state.return_value = None

        gemini_response = MagicMock()
        gemini_response.text = (
            "<b>Пациент:</b> 45 лет, глубокий кариес 4.6.\n"
            "❓ <b>Вопрос:</b> Тактика препарирования?\n\n"
            "A) Провести витальное сохранение пульпы МТА\n"
            "B) Девитальная экстирпация\n"
            "C) Динамическое наблюдение\n"
            "D) Удаление зуба"
        )
        mock_gemini.return_value = (gemini_response, None)

        sent_msg = MagicMock()
        sent_msg.id = 5551
        outcome = MagicMock()
        outcome.ok = True
        outcome.value = sent_msg
        mock_send_chunks.return_value = outcome

        mock_bot = AsyncMock()
        mock_status = MagicMock()
        mock_status.id = 111
        mock_bot.send_message.return_value = mock_status

        await start_interactive_case(mock_bot, chat_id, callback_event=None)

        # 1. Проверяем удаление спиннера
        mock_bot.delete_messages.assert_called_once_with(chat_id, 111)

        # 2. Проверяем отправку сообщения с 4 кнопками и индикатором прогресса
        self.assertTrue(mock_send_chunks.called)
        sent_args, sent_kwargs = mock_send_chunks.call_args
        msg_text = sent_args[2]
        self.assertIn("🎮 <b>Клинический симулятор: Шаг 1 из 4</b> [🟩⬜⬜⬜]", msg_text)
        self.assertIn("⏱ <i>Активность: 1 ч на решение • Выход: /abort</i>", msg_text)

        buttons = sent_kwargs.get("buttons")
        self.assertIsNotNone(buttons)
        # 3 строки кнопок: [A, B], [C, D], [Завершить, В меню]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0][0].data, b"case:opt:A")
        self.assertEqual(buttons[0][1].data, b"case:opt:B")
        self.assertEqual(buttons[1][0].data, b"case:opt:C")
        self.assertEqual(buttons[1][1].data, b"case:opt:D")
        self.assertEqual(buttons[2][0].data, b"case:abort")
        self.assertEqual(buttons[2][1].data, b"nav:main")

        # 3. Проверяем сохранение состояния в SQLite
        mock_set_state.assert_called_once()
        set_kwargs = mock_set_state.call_args.kwargs
        self.assertEqual(set_kwargs["user_id"], chat_id)
        self.assertEqual(set_kwargs["state_type"], "case")
        self.assertEqual(set_kwargs["current_step"], 1)
        self.assertEqual(set_kwargs["case_id"], "dynamic")

        hist = json.loads(set_kwargs["history"])
        self.assertEqual(hist["last_msg_id"], 5551)
        self.assertIn("last_updated", hist)

    @patch("database.get_user_interactive_state")
    @patch("database.set_user_interactive_state")
    @patch("database.save_pm_message")
    @patch("assistant.generate_gemini_text_async")
    @patch("assistant.send_message_chunks_async")
    @patch("tg_safety.edit_message")
    async def test_stale_buttons_deactivation_on_step(
        self, mock_tg_edit, mock_send_chunks, mock_gemini, mock_save_pm, mock_set_state, mock_get_state
    ):
        """Проверяет деактивацию старых кнопок при переходе на следующий шаг."""
        chat_id = 999004
        last_msg_id = 7771

        user_state = {
            "user_id": chat_id,
            "state_type": "case",
            "current_step": 1,
            "case_id": "dynamic",
            "history": json.dumps({
                "messages": [{
                    "role": "assistant",
                    "content": (
                        "❓ <b>Вопрос:</b> Первый шаг?\n"
                        "A) Провести КТ 5х5\n"
                        "B) Начать эндодонтическое лечение\n"
                        "C) Назначить антибиотики\n"
                        "D) Удалить зуб"
                    )
                }],
                "last_updated": time.time(),
                "last_msg_id": last_msg_id
            })
        }

        mock_event = AsyncMock()
        mock_event.message_id = last_msg_id
        mock_event.answer = AsyncMock()

        gemini_response = MagicMock()
        gemini_response.text = (
            "Биомеханический анализ: выбор КТ 5х5 абсолютно верен.\n"
            "A) Этап 1\nB) Этап 2\nC) Этап 3\nD) Этап 4"
        )
        mock_gemini.return_value = (gemini_response, None)

        sent_msg = MagicMock()
        sent_msg.id = 7772
        outcome = MagicMock()
        outcome.ok = True
        outcome.value = sent_msg
        mock_send_chunks.return_value = outcome

        mock_bot = AsyncMock()
        mock_status = MagicMock()
        mock_status.id = 888
        mock_bot.send_message.return_value = mock_status

        # Врач выбрал "A"
        await handle_interactive_case_step(
            mock_bot, chat_id, "A", user_state, callback_event=mock_event
        )

        # 1. Проверяем, что на предыдущем сообщении кнопки заменены на бейдж выбора
        mock_tg_edit.assert_called_once()
        edit_args, edit_kwargs = mock_tg_edit.call_args
        self.assertEqual(edit_args[2], last_msg_id)  # message_id
        buttons_passed = edit_kwargs.get("buttons")
        self.assertIsNotNone(buttons_passed)
        self.assertEqual(buttons_passed[0][0].text, "✅ Выбран вариант A")
        self.assertEqual(buttons_passed[0][0].data, b"case:stale:A")

        # 2. Проверяем отправку шага 2 с прогресс-баром
        msg_text = mock_send_chunks.call_args[0][2]
        self.assertIn("🎮 <b>Клинический симулятор: Шаг 2 из 4</b> [🟩🟩⬜⬜]", msg_text)

    @patch("database.clear_user_interactive_state")
    @patch("assistant.send_message_chunks_async")
    async def test_session_timeout_cleans_state(self, mock_send_chunks, mock_clear_state):
        """Проверяет завершение сессии по таймауту (> 1 ч неактивности)."""
        chat_id = 999005
        old_time = time.time() - 3650  # 3650 секунд назад (> 1 час)

        user_state = {
            "user_id": chat_id,
            "state_type": "case",
            "current_step": 2,
            "history": json.dumps({
                "messages": [{"role": "assistant", "content": "Вопрос"}],
                "last_updated": old_time
            })
        }

        mock_bot = AsyncMock()
        mock_event = AsyncMock()
        mock_event.answer = AsyncMock()

        await handle_interactive_case_step(
            mock_bot, chat_id, "B", user_state, callback_event=mock_event
        )

        # 1. Состояние должно быть очищено
        mock_clear_state.assert_called_once_with(chat_id)

        # 2. Врач должен получить вежливое уведомление с кнопкой нового кейса
        mock_event.answer.assert_called_once_with(
            "⏳ Сессия симулятора устарела (> 1 ч неактивности).", alert=True
        )
        mock_bot.send_message.assert_called_once()
        msg = mock_bot.send_message.call_args[1]["message"]
        self.assertIn("Сессия клинического симулятора завершена по таймауту", msg)

    @patch("database.clear_user_interactive_state")
    @patch("database.save_pm_message")
    @patch("assistant.generate_gemini_text_async")
    @patch("assistant.send_message_chunks_async")
    @patch("user_memory.update_clinician_memory_async")
    async def test_final_step_debriefing_and_cleanup(
        self, mock_mem_update, mock_send_chunks, mock_gemini, mock_save_pm, mock_clear_state
    ):
        """Проверяет завершение симулятора на шаге 4: дебрифинг, очистка state и кнопки."""
        chat_id = 999006
        user_state = {
            "user_id": chat_id,
            "state_type": "case",
            "current_step": 4,  # Финальный шаг
            "history": json.dumps({
                "messages": [
                    {"role": "assistant", "content": "Вопрос 1"},
                    {"role": "user", "content": "Действие 1"},
                    {"role": "assistant", "content": "Вопрос 2"},
                    {"role": "user", "content": "Действие 2"},
                    {"role": "assistant", "content": "Вопрос 3"},
                    {"role": "user", "content": "Действие 3"},
                    {"role": "assistant", "content": "Вопрос 4"}
                ],
                "last_updated": time.time(),
                "last_msg_id": 9991
            })
        }

        gemini_response = MagicMock()
        gemini_response.text = (
            "Клинический дебрифинг совокупности принятых решений:\n"
            "1. Анатомический исход: сохранность корня и герметичность апикального силера обеспечена.\n"
            "2. Закрытые риски: рецидив периапикального воспаления исключен.\n"
            "3. Долгосрочный прогноз: выживаемость реставрации > 95% на 5 лет."
        )
        mock_gemini.return_value = (gemini_response, None)

        mock_bot = AsyncMock()
        mock_status = MagicMock()
        mock_status.id = 9999
        mock_bot.send_message.return_value = mock_status

        await handle_interactive_case_step(
            mock_bot, chat_id, "Выбираю вариант A", user_state, callback_event=None
        )

        # 1. State должен быть очищен
        mock_clear_state.assert_called_once_with(chat_id)

        # 2. Сообщение должно содержать финишный заголовок и прогресс [🟩🟩🟩🟩]
        sent_args, sent_kwargs = mock_send_chunks.call_args
        final_msg = sent_args[2]
        self.assertIn("🏁 <b>Клинический симулятор: Итоги разбора</b> [🟩🟩🟩🟩]", final_msg)
        self.assertIn("Клинический дебрифинг", final_msg)

        # 3. Кнопки финиша: [🚀 Новый кейс] и [🏠 В меню]
        buttons = sent_kwargs.get("buttons")
        self.assertIsNotNone(buttons)
        self.assertEqual(buttons[0][0].data, b"case:start")
        self.assertEqual(buttons[1][0].data, b"nav:main")

    @patch("database.get_user_interactive_state")
    @patch("database.set_user_interactive_state")
    @patch("database.save_pm_message")
    @patch("assistant.generate_gemini_text_async")
    @patch("assistant.send_message_chunks_async")
    @patch("tg_safety.edit_message")
    async def test_text_answer_extracts_option_detail_and_deactivates_old_button(
        self, mock_tg_edit, mock_send_chunks, mock_gemini, mock_save_pm, mock_set_state, mock_get_state
    ):
        """Проверяет, что при вводе текста 'вариант 2' извлекается текст опции B и старые кнопки гасятся."""
        chat_id = 999007
        old_msg_id = 8811

        user_state = {
            "user_id": chat_id,
            "state_type": "case",
            "current_step": 1,
            "case_id": "dynamic",
            "history": json.dumps({
                "messages": [{
                    "role": "assistant",
                    "content": (
                        "❓ <b>Вопрос:</b> Какова тактика?\n"
                        "A) Удаление зуба\n"
                        "B) Прямое покрытие пульпы Biodentine и реставрация\n"
                        "C) Девитализация мышьяковистой пастой\n"
                        "D) Назначить антибиотики"
                    )
                }],
                "last_updated": time.time(),
                "last_msg_id": old_msg_id
            })
        }

        gemini_response = MagicMock()
        gemini_response.text = "Анализ выбора Biodentine...\nA) 1\nB) 2\nC) 3\nD) 4"
        mock_gemini.return_value = (gemini_response, None)

        mock_bot = AsyncMock()
        mock_status = MagicMock()
        mock_status.id = 8812
        mock_bot.send_message.return_value = mock_status

        # Врач набрал текстом: "вариант 2"
        await handle_interactive_case_step(
            mock_bot, chat_id, "вариант 2", user_state, callback_event=None
        )

        # 1. Проверяем деактивацию старых кнопок по last_msg_id
        mock_tg_edit.assert_called_once()
        edit_args, edit_kwargs = mock_tg_edit.call_args
        self.assertEqual(edit_args[2], old_msg_id)
        btn = edit_kwargs["buttons"][0][0]
        self.assertEqual(btn.text, "✅ Выбран вариант B")
        self.assertEqual(btn.data, b"case:stale:B")

        # 2. Проверяем, что в историю ушло полное клиническое описание опции
        saved_hist = json.loads(mock_set_state.call_args.kwargs["history"])
        user_entry = saved_hist["messages"][1]
        self.assertEqual(user_entry["role"], "user")
        self.assertIn("Выбираю вариант B: Прямое покрытие пульпы Biodentine и реставрация", user_entry["content"])

    @patch("assistant.edit_callback_message")
    @patch("database.get_user_interactive_state")
    @patch("database.clear_user_interactive_state")
    async def test_case_callbacks_dispatch(
        self, mock_clear_state, mock_get_state, mock_edit_cb
    ):
        """Проверяет обработку callback-запросов case:stale и case:abort."""
        mock_bot = AsyncMock()
        mock_event = AsyncMock()
        mock_event.sender_id = 999008
        mock_event.answer = AsyncMock()

        # 1. Проверка клика по устаревшей кнопке (case:stale:B)
        mock_event.data = b"case:stale:B"
        await assistant.handle_quiz_callback(mock_bot, mock_event)
        mock_event.answer.assert_called_with(
            "ℹ️ Этот шаг уже пройден. Актуальный вопрос симулятора находится ниже.",
            alert=True
        )

        # 2. Проверка кнопки сброса (case:abort)
        mock_event.data = b"case:abort"
        await assistant.handle_quiz_callback(mock_bot, mock_event)
        mock_clear_state.assert_called_with(999008)
        mock_edit_cb.assert_called_once()
        edit_args, edit_kwargs = mock_edit_cb.call_args
        self.assertIn("Интерактивная сессия симулятора успешно завершена", edit_args[2])
        buttons = edit_kwargs["buttons"]
        self.assertEqual(buttons[0][0].data, b"case:start")
        self.assertEqual(buttons[1][0].data, b"nav:main")


class TestCaseBankAndNeuralEvaluation(unittest.IsolatedAsyncioTestCase):
    """Тестирование банка острых кейсов и системных промптов симулятора."""

    def test_case_bank_subcutaneous_emphysema(self):
        """Проверка наличия кейса ятрогенной подкожной эмфиземы."""
        import clinical_case_simulator as ccs
        case = ccs.get_clinical_case_by_id("subcutaneous_emphysema")
        self.assertIsNotNone(case, "Кейс subcutaneous_emphysema обязан быть в банке")
        self.assertIn("1.4", case["tooth"])
        self.assertIn("пустер", case["description"].lower())
        self.assertIn("крепитация", case["description"].lower())
        opts = case["options"]
        self.assertEqual(len(opts), 4)
        self.assertIn("A", opts)
        self.assertIn("B", opts)
        self.assertIn("C", opts)
        self.assertIn("D", opts)
        self.assertEqual(case["ebm_key"], "B")
        self.assertIn("Амоксиклав", opts["B"])

    def test_case_bank_sinus_perforation(self):
        """Проверка наличия кейса перфорации дна гайморовой пазухи."""
        import clinical_case_simulator as ccs
        case = ccs.get_clinical_case_by_id("sinus_perforation")
        self.assertIsNotNone(case, "Кейс sinus_perforation обязан быть в банке")
        self.assertIn("1.6", case["tooth"])
        self.assertIn("гайморов", case["title"].lower())
        self.assertIn("носоротовая", case["description"].lower())
        opts = case["options"]
        self.assertEqual(len(opts), 4)
        self.assertEqual(case["ebm_key"], "B")
        self.assertIn("MTA", opts["B"])
        self.assertIn("запрет сморкаться", opts["B"])

    def test_case_bank_formatting(self):
        """Форматирование кейсов из банка гарантирует 4 опции A, B, C, D."""
        import clinical_case_simulator as ccs
        for case in ccs.CLINICAL_CASE_BANK:
            text = ccs.format_case_step1_from_dict(case)
            self.assertIn("A)", text)
            self.assertIn("B)", text)
            self.assertIn("C)", text)
            self.assertIn("D)", text)
            self.assertIn("❓ <b>Клинический вопрос:</b>", text)

    def test_neural_prompts_invariants(self):
        """Проверка когнитивных директив в системных промптах симулятора."""
        import clinical_case_simulator as ccs
        # 1. Промпт генерации
        self.assertIn("A) [Тактика 1]", ccs.CASE_SYSTEM_GENERATION_PROMPT)
        self.assertIn("D) [Тактика 4]", ccs.CASE_SYSTEM_GENERATION_PROMPT)
        # 2. Промпт ветвления
        self.assertIn("{doctor_action}", ccs.CASE_STEP_EVALUATION_PROMPT)
        self.assertIn("КЛИНИЧЕСКИЙ АУДИТ", ccs.CASE_STEP_EVALUATION_PROMPT)
        # 3. Финальный дебрифинг
        self.assertIn("ДОКАЗАТЕЛЬНЫЙ ПРОГНОЗ", ccs.CASE_FINAL_DEBRIEF_PROMPT)
        self.assertIn("ТОТАЛЬНЫЙ ЗАПРЕТ СИКОФАНТСТВА", ccs.CASE_FINAL_DEBRIEF_PROMPT)

    @patch("assistant.send_message_chunks_async")
    @patch("database.get_user_interactive_state", return_value=None)
    @patch("database.set_user_interactive_state")
    @patch("database.save_pm_message")
    @patch("clinical_case_simulator.get_random_clinical_case")
    async def test_start_case_delivers_bank_case(
        self, mock_get_case, mock_save_pm, mock_set_state, mock_get_state, mock_send_chunks
    ):
        """start_interactive_case успешно доставляет кейс из банка."""
        import clinical_case_simulator as ccs
        emphysema_case = ccs.get_clinical_case_by_id("subcutaneous_emphysema")
        mock_get_case.return_value = emphysema_case
        mock_bot = AsyncMock()
        mock_send_chunks.return_value = MagicMock(value=MagicMock(id=555001))

        await assistant.start_interactive_case(mock_bot, 999888)

        mock_send_chunks.assert_called_once()
        msg_text = mock_send_chunks.call_args[0][2]
        self.assertIn("Ятрогенная подкожная эмфизема", msg_text)
        self.assertIn("Клинический симулятор: Шаг 1 из 4", msg_text)
        buttons = mock_send_chunks.call_args[1]["buttons"]
        self.assertEqual(buttons[0][0].text, "🔘 Вариант A")
        self.assertEqual(buttons[0][0].data, b"case:opt:A")
        self.assertEqual(buttons[0][1].text, "🔘 Вариант B")
        self.assertEqual(buttons[0][1].data, b"case:opt:B")
        self.assertEqual(buttons[1][0].text, "🔘 Вариант C")
        self.assertEqual(buttons[1][0].data, b"case:opt:C")
        self.assertEqual(buttons[1][1].text, "🔘 Вариант D")
        self.assertEqual(buttons[1][1].data, b"case:opt:D")


if __name__ == "__main__":
    unittest.main()

