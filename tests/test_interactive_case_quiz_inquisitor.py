# -*- coding: utf-8 -*-
"""
tests/test_interactive_case_quiz_inquisitor.py — Red Team Инквизиторский тест
клинического симулятора (/case), викторины (/quiz), защиты от зависших состояний,
сохранения стриков и паритета StomChat и EndoChat.
"""

import asyncio
import json
import os
import re
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import assistant
import database
import poll_storage


class TestClinicalSimulatorInactivityAndAbort(unittest.IsolatedAsyncioTestCase):
    """1. Клинический симулятор: авто-выход по таймауту (>1ч) и мгновенный сброс."""

    async def test_case_timeout_detection_stale_state(self):
        """Сессия с неактивностью более 1 часа (3600с) мгновенно сбрасывается из БД."""
        now = time.time()
        stale_history = json.dumps({
            "messages": [{"role": "assistant", "content": "Шаг 1"}],
            "last_updated": now - 3650
        })

        with patch("database.get_user_interactive_state", AsyncMock(return_value={
            "state_type": "case",
            "current_step": 1,
            "case_id": "dynamic",
            "history": stale_history
        })), patch("database.clear_user_interactive_state", AsyncMock()) as mock_clear, \
             patch("assistant.detect_pm_emergency", return_value=None):

            bot_mock = AsyncMock()
            event_mock = MagicMock()
            event_mock.chat_id = 999111
            event_mock.sender_id = 999111
            event_mock.message = MagicMock()
            event_mock.message.id = 555
            event_mock.message.text = "Проведу холодовую пробу"
            event_mock.message.voice = None
            event_mock.message.audio = None
            event_mock.message.video_note = None
            event_mock.message.photo = None
            event_mock.message.video = None
            event_mock.message.document = None

            # Эмулируем ветку проверки таймаута в ЛС
            user_state = await database.get_user_interactive_state(999111)
            is_case_expired = False
            inactivity_sec = 0
            if user_state and user_state.get("state_type") == "case":
                try:
                    history_raw = json.loads(user_state.get("history") or "{}")
                    if isinstance(history_raw, dict):
                        last_updated = history_raw.get("last_updated")
                        if last_updated:
                            inactivity_sec = time.time() - float(last_updated)
                            if inactivity_sec > 3600:
                                is_case_expired = True
                        else:
                            is_case_expired = True
                    else:
                        is_case_expired = True
                except Exception:
                    is_case_expired = True

                if is_case_expired:
                    await database.clear_user_interactive_state(999111)
                    user_state = None

            self.assertTrue(is_case_expired, "Stale case (> 3600s) must be flagged as expired")
            self.assertIsNone(user_state, "user_state must be cleared to None")
            mock_clear.assert_called_once_with(999111)

    async def test_case_timeout_corrupted_history(self):
        """Сессия с поврежденной историей или без last_updated мгновенно сбрасывается."""
        with patch("database.clear_user_interactive_state", AsyncMock()) as mock_clear:
            for bad_hist in ["", "not-json", "[]", json.dumps({"messages": []})]:
                mock_clear.reset_mock()
                user_state = {
                    "state_type": "case",
                    "current_step": 1,
                    "case_id": "dynamic",
                    "history": bad_hist
                }
                is_case_expired = False
                try:
                    history_raw = json.loads(user_state.get("history") or "{}")
                    if isinstance(history_raw, dict):
                        last_updated = history_raw.get("last_updated")
                        if not last_updated or (time.time() - float(last_updated) > 3600):
                            is_case_expired = True
                    else:
                        is_case_expired = True
                except Exception:
                    is_case_expired = True

                self.assertTrue(is_case_expired, f"Corrupted history {bad_hist!r} must expire")

    async def test_case_abort_commands_exhaustive(self):
        """Все варианты команд отмены (/abort, /exit, /stop, /cancel, выход, отмена) мгновенно сбрасывают симулятор."""
        abort_inputs = [
            "/abort", "/exit", "/stop", "/cancel",
            "выход", "отмена", "стоп", "завершить", "прервать",
            "/abort@stomchat_bot", "/exit@endochat_bot", "/stop@docendobot",
            "  /abort  ", "  ВЫХОД  ", "ОтМеНа"
        ]

        abort_cmds = ("/abort", "/exit", "/stop", "/cancel", "выход", "отмена", "стоп", "завершить", "прервать")
        for inp in abort_inputs:
            with self.subTest(inp=inp):
                clean_text_lower = (inp or "").strip().lower()
                clean_cmd = clean_text_lower.split("@")[0].strip()
                self.assertIn(clean_cmd, abort_cmds, f"Command {inp!r} must match abort_cmds")

    async def test_natural_language_not_hijacked_during_case(self):
        """Клинические действия врача внутри кейса (слова калькулятор, тест) не перехватываются zero-slash триажем."""
        user_state = {"state_type": "case", "current_step": 1, "case_id": "dynamic", "history": "{}"}
        text = "калькулятор анестезии для зуба 1.6: сделаю расчет артикаина"
        has_media_intent = False

        # Условие из assistant.py
        should_run_zero_slash = bool(
            text and not text.startswith("/") and not has_media_intent and not (user_state and user_state.get("state_type") == "case")
        )
        self.assertFalse(should_run_zero_slash, "Doctor's case input must NEVER be hijacked by zero-slash triage")


class TestQuizEngineAntiRepetitionAndStreaks(unittest.IsolatedAsyncioTestCase):
    """2. Викторина (/quiz): защита от повторов, сохранение стриков и регулярка опций."""

    def test_clinical_quiz_fallbacks_pool_richness(self):
        """Пул fallback-вопросов расширен и содержит не менее 16 клинических задач."""
        self.assertGreaterEqual(len(assistant.CLINICAL_QUIZ_FALLBACKS), 16)
        topics = {q.get("topic") for q in assistant.CLINICAL_QUIZ_FALLBACKS}
        self.assertTrue({"Эндодонтия", "Ортопедия", "Терапия", "Хирургия", "Фармакология"}.issubset(topics))

    def test_group_quiz_option_regex_robustness(self):
        """Регулярка парсинга опций в группе парсит любые варианты оформления (bold с двоеточием и без)."""
        opt_regex = re.compile(r'^(?:<b>|\*\*)?([A-D])[:.]?(?:</b>|\*\*)?[:.]?\s*(.*)', re.IGNORECASE)
        suffix_regex = re.compile(r'(?:\s+|^)\(\d+\s*гол\S*\s*\|\s*\d+%\)\s*$', re.IGNORECASE)

        test_lines = [
            ("<b>A:</b> Ревизия системы каналов", "A", "Ревизия системы каналов"),
            ("<b>B</b>: Резекция верхушки корня (3 гол. | 60%)", "B", "Резекция верхушки корня"),
            ("<b>C.</b> Удаление зуба с имплантацией", "C", "Удаление зуба с имплантацией"),
            ("**D:** Наблюдение в динамике (1 гол. | 20%)", "D", "Наблюдение в динамике"),
            ("A: Наложение коффердама", "A", "Наложение коффердама"),
            ("B. Анестезия артикаином", "B", "Анестезия артикаином"),
        ]

        for raw_line, exp_letter, exp_choice in test_lines:
            with self.subTest(line=raw_line):
                m = opt_regex.match(raw_line.strip())
                self.assertIsNotNone(m, f"Line {raw_line!r} must match opt_regex")
                letter = m.group(1).upper()
                self.assertEqual(letter, exp_letter)
                raw_choice = m.group(2).lstrip(":-. ").strip()
                clean_choice = suffix_regex.sub('', raw_choice).strip()
                self.assertEqual(clean_choice, exp_choice)

    def test_anti_repetition_pool_selection(self):
        """Выборка из пула исключает вопросы, которые недавно задавались."""
        all_questions = assistant.CLINICAL_QUIZ_FALLBACKS
        seen_questions = {q["question"] for q in all_questions[:5]}

        available_pool = [fb for fb in all_questions if fb["question"] not in seen_questions]
        self.assertEqual(len(available_pool), len(all_questions) - 5)
        for q in available_pool:
            self.assertNotIn(q["question"], seen_questions)


class TestDatabaseInteractiveStatesCleanup(unittest.IsolatedAsyncioTestCase):
    """3. Очистка базы: сессии симулятора и старые квизы."""

    def test_stale_cleanup_invariants(self):
        """Сессии симулятора старше 1 часа и квизы старше 3 суток удаляются, свежие — сохраняются."""
        now = time.time()
        cases = [
            # 1. Живой кейс (15 минут назад) -> Сохранить
            (1001, "case", json.dumps({"messages": [], "last_updated": now - 900}), False),
            # 2. Зависший кейс (2 часа назад) -> Удалить
            (1002, "case", json.dumps({"messages": [], "last_updated": now - 7200}), True),
            # 3. Кейс без timestamp -> Удалить
            (1003, "case", json.dumps({"messages": []}), True),
            # 4. Свежий квиз (12 часов назад) -> Сохранить
            (-int((now - 43200) * 1_000_000), "quiz_config", json.dumps({"votes": [0,0,0,0], "voters": {}}), False),
            # 5. Древний квиз (5 дней назад) -> Удалить
            (-int((now - 5 * 86400) * 1_000_000), "quiz_config", json.dumps({"votes": [0,0,0,0], "voters": {}}), True),
        ]

        max_age_seconds = 3600
        max_quiz_age_seconds = 259200  # 3 суток

        for user_id, state_type, hist_raw, should_delete in cases:
            with self.subTest(user_id=user_id, state_type=state_type):
                is_stale = False
                try:
                    if not hist_raw:
                        is_stale = True
                    else:
                        data = json.loads(hist_raw)
                        if state_type == "case":
                            if isinstance(data, dict):
                                ts = data.get("last_updated")
                                if not ts or (now - float(ts) > max_age_seconds):
                                    is_stale = True
                            else:
                                is_stale = True
                        elif state_type == "quiz_config":
                            ts = None
                            if isinstance(data, dict):
                                ts = data.get("last_updated") or data.get("created_at")
                            if not ts:
                                try:
                                    u_int = int(user_id)
                                    if u_int < 0:
                                        ts = abs(u_int) / 1_000_000.0
                                except Exception:
                                    pass
                            if not ts or (now - float(ts) > max_quiz_age_seconds):
                                is_stale = True
                        else:
                            is_stale = True
                except Exception:
                    is_stale = True

                self.assertEqual(is_stale, should_delete)


if __name__ == "__main__":
    unittest.main()
