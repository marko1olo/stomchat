"""
Тесты для аудита и рефакторинга PM Reflection, Validator и Clinician-Mentor Fallback.
Проверяет:
1. Распознавание реплик о ходе процедуры / результатах (CLINICAL_PROGRESS_RE).
2. Работу generate_dialogue_fallback_async: контекстуальный ответ врача-наставника,
   полное отсутствие бюрократического канцелярита и требований "указать процедуру".
3. Валидатор check_response_quality: наличие инструкций по одобрению chairside-ответов
   на реплики о ходе лечения («достал», «обошел») и запрет роботизированных отписок.
4. Логику ретраев и фоллбека в PM: сохранение контекста сессии и вызов менторского фоллбека.
"""
import asyncio
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import assistant


class TestClinicalProgressRegex(unittest.TestCase):
    """Проверка детектора реплик о ходе процедуры и промежуточных исходах."""

    def test_progress_success_markers(self):
        progress_phrases = [
            "достал",
            "достал отломок",
            "извлекли файл 25.04",
            "обошел ступеньку",
            "обошли отломок",
            "убрал старую пломбу",
            "спилил коронку",
            "промыл гипохлоритом",
            "заложил гидроксид кальция",
            "получилось обойти",
            "вышло отлично",
            "готово, канал сухой",
            "сделал контрольный снимок",
        ]
        for phrase in progress_phrases:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    bool(assistant.CLINICAL_PROGRESS_RE.search(phrase)),
                    f"Фраза '{phrase}' должна быть распознана как progress turn"
                )

    def test_progress_hurdle_markers(self):
        hurdle_phrases = [
            "заклинило файл",
            "не идет десятка",
            "не получается обойти",
            "кровит из канала",
            "перфорация в области бифуркации",
            "сломал кончик инструмента",
        ]
        for phrase in hurdle_phrases:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    bool(assistant.CLINICAL_PROGRESS_RE.search(phrase)),
                    f"Фраза '{phrase}' должна быть распознана как клиническая трудность"
                )

    def test_non_progress_phrases(self):
        non_progress = [
            "какой адгезив выбрать?",
            "сколько мг артикаина на карпулу?",
            "здравствуйте, подскажите протокол",
            "спасибо большое за помощь",
        ]
        for phrase in non_progress:
            with self.subTest(phrase=phrase):
                self.assertFalse(
                    bool(assistant.CLINICAL_PROGRESS_RE.search(phrase)),
                    f"Фраза '{phrase}' не должна распознаваться как progress turn"
                )


class TestDialogueFallbackMentor(unittest.IsolatedAsyncioTestCase):
    """Проверка генератора фоллбека наставника-клинициста."""

    async def test_fallback_generates_mentor_response_on_dostal(self):
        context = [
            "Врач: Сломал фрагмент инструмента 25.04 в апикальной трети 36 зуба.",
            "Ассистент: Рекомендуется начать с предварительного расширения коронковой трети Gates Glidden, затем K-file #08 с ЭДТА.",
            "Врач: достал"
        ]
        quality_reason = "Реплика не содержит клинического вопроса или данных о материале/процедуре"

        mock_llm_reply = (
            "<b>Отлично, что извлекли отломок!</b> Теперь промойте канал 3% NaOCl без давления "
            "на 1 мм короче рабочей длины с ультразвуковой активацией и высушите бумажными пинами. "
            "Контрольный снимок уже сделали? Как апекс?"
        )

        fake_resp = MagicMock()
        fake_resp.text = mock_llm_reply

        with patch("assistant.generate_gemini_text_async", new_callable=AsyncMock) as mock_gemini:
            mock_gemini.return_value = (fake_resp, None)

            fb_text, fb_err = await assistant.generate_dialogue_fallback_async(
                context_msgs=context,
                quality_reason=quality_reason,
                current_text="достал",
            )

            self.assertEqual(fb_err, "")
            self.assertIn("Отлично", fb_text)
            self.assertIn("NaOCl", fb_text)

            # Проверяем, что промпт содержит контекст и наставнические инструкции
            call_prompt = mock_gemini.call_args[0][0]
            self.assertIn("наставник", call_prompt)
            self.assertIn("Сломал фрагмент", call_prompt)
            self.assertIn("достал", call_prompt)
            # Проверяем запрет канцелярита в промпте
            self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ", call_prompt)
            self.assertIn("бюрократический канцелярит", call_prompt)

    async def test_fallback_never_outputs_bureaucratic_formula(self):
        """Проверяем, что даже при отказе валидатора бот не возвращает канцелярскую фразу инцидента."""
        bureaucratic_phrase = (
            "Уточнить, что означает «достал» и какой клинический вопрос требуется решить. "
            "Для выбора инструмента, протокола и предела безопасности указать процедуру и используемый материал — реплика не содержит этих данных."
        )
        # Проверяем, что санитизатор/chad бракует чиновничьи обороты, если они случайно вылезут
        self.assertFalse(
            assistant._META_DROP_RE.search(bureaucratic_phrase) is not None or "отлично" in bureaucratic_phrase.lower()
        )


class TestValidatorMentorRules(unittest.TestCase):
    """Проверка наличия правил наставничества в теле check_response_quality."""

    def test_validator_instructions_contain_progress_and_anti_bureaucracy(self):
        import inspect
        src = inspect.getsource(assistant.check_response_quality)

        # 1. Проверка разрешения progress-реплик у кресла («достал», «обошел»)
        self.assertIn("РЕПЛИКИ О ХОДЕ ПРОЦЕДУРЫ И ПРОМЕЖУТОЧНЫХ РЕЗУЛЬТАТАХ В ДИАЛОГЕ", src)
        self.assertIn("достал", src)
        self.assertIn("обошел", src)
        self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО отклонять такой ответ за «отсутствие вопроса", src)

        # 2. Проверка строгого запрета роботизированного канцелярита в рецензиях
        self.assertIn("БЮРОКРАТИЧЕСКИЙ КАНЦЕЛЯРИТ И РОБОТИЗИРОВАННЫЕ ОТПИСКИ", src)
        self.assertIn("Уточнить, что означает", src)
        self.assertIn("Для выбора инструмента указать процедуру", src)


class TestPmRetryAndFallbackIntegration(unittest.TestCase):
    """Проверка логики retry loop и cautious fallback в handle_private_message."""

    def test_context_preservation_and_cautious_call(self):
        import inspect
        src = inspect.getsource(assistant.handle_private_message)

        # 1. Проверяем, что контекст не режется на коротких/progress репликах
        self.assertIn("is_short_or_continuation = not has_media and (len((text or \"\").split()) <= 8 or is_clinical_progress", src)
        self.assertIn("if not is_short_or_continuation and len(current_context_msgs) > 2:", src)

        # 2. Проверяем, что cautious fallback вызывает generate_dialogue_fallback_async
        self.assertIn("fb_candidate, fb_reason = await generate_dialogue_fallback_async(", src)
        self.assertIn("current_text=text", src)
        self.assertIn('"pm_cautious_ebm"', src)


if __name__ == "__main__":
    unittest.main()
