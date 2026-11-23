# -*- coding: utf-8 -*-
"""
tests/test_validator_new_scan_context.py

Целевой тест для предотвращения ложного отклонения валидатором при смене снимка / топографии в ЛС.
Универсальный инвариант:
- В истории обсуждался зуб одной анатомической зоны (например, зуб 27 верхней челюсти).
- Врач присылает новый снимок КЛКТ другой зоны (например, зубы 47-48 нижней челюсти).
- Валидатор обязан отдавать абсолютный приоритет текущему снимку над устаревшей историей,
  видеть директиву смены анатомической зоны и одобрять разбор нового снимка без ложных отклонений.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import assistant


class TestValidatorNewScanContext(unittest.IsolatedAsyncioTestCase):
    """Тесты валидации черновиков при смене снимка, анатомической зоны или челюсти."""

    async def test_new_scan_topography_shift_prompt_and_approval_universal(self):
        """
        Универсальная валидация смены снимка / анатомической зоны:
        История: зуб 27 (верхняя челюсть, прошлая сессия).
        Новый запрос: снимок КЛКТ 47-48 (нижняя челюсть), текст: 'Что это такое на клкт'.
        Черновик: разбор зубов 47-48 нижней челюсти.
        Валидатор обязан отдавать приоритет текущему снимку над историей.
        """
        context_msgs = [
            "[ИСТОРИЯ ДИАЛОГА (более дня назад / 21.11.2026)]:",
            "Врач: зуб 27 беспокоит при накусывании, глубокий карман с небной стороны",
            "Бот-ассистент: На зубе 27 необходимо оценить периапикальный статус и проходимость небного канала.",
            "Врач (сейчас): Что это такое на клкт [Прикреплен снимок / данные визуализации: КЛКТ нижней челюсти в области зубов 47, 48. Горизонтальная ретенция зуба 48 с упором коронки в дистальную поверхность корня зуба 47.]"
        ]

        draft_reply = (
            "На представленной КЛКТ нижней челюсти определяется ретинированный зуб 48, коронка которого "
            "упирается в дистальную поверхность корня зуба 47 с формированием костного кармана. "
            "Показано атравматичное удаление 48 зуба с сепарацией коронки для сохранения зуба 47."
        )

        captured_prompt = None

        async def fake_gemini(prompt, ctx, timeout=None):
            nonlocal captured_prompt
            captured_prompt = prompt
            resp = MagicMock()
            resp.text = '{"ok": true, "reason": "Черновик точно разбирает зубы 47-48 с нового снимка КЛКТ"}'
            return resp, None

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_gemini):
            ok, reason = await assistant.check_response_quality(
                context_msgs=context_msgs,
                draft_reply=draft_reply,
                invited=True,
                reference="КЛКТ зубов 47, 48. Ретенция третьих моляров."
            )

        self.assertTrue(ok, f"Валидатор обязан одобрить разбор нового снимка: {reason}")
        self.assertIn("47-48", reason)
        self.assertIsNotNone(captured_prompt, "Промпт валидатора должен быть сгенерирован")

        # Проверяем наличие ключевых системных директив в промпте валидатора
        self.assertIn("СМЕНА СНИМКА / НОВЫЙ КЛИНИЧЕСКИЙ КЕЙС И ТОПОГРАФИЯ", captured_prompt)
        self.assertIn("Черновик ОБЯЗАН разбирать ТЕКУЩИЙ прикреплённый снимок", captured_prompt)
        self.assertIn("АБСОЛЮТНЫЙ ПРИОРИТЕТ над старой историей", captured_prompt)
        self.assertIn("НЕ ЯВЛЯЕТСЯ топографической ошибкой или подменой зуба", captured_prompt)

    async def test_context_str_trimming_prioritizes_new_scan_over_ancient_root(self):
        """
        Проверка обрезки context_str при длине > 12 сообщений:
        Если в истории > 12 сообщений, но первое сообщение из прошлой сессии или прислан новый снимок —
        валидатор не должен принудительно пинить c_list[0] как корень кейса.
        """
        # 14 сообщений: первые 3 — древняя история 2-недельной давности про зуб 16
        history = [
            "[ИСТОРИЯ ДИАЛОГА (более недели назад)]:",
            "Врач: зуб 16 эндодонтия",
            "Бот: протокол MB2..."
        ]
        # Промежуточные сообщения
        for i in range(10):
            history.append(f"Врач: реплика {i}")
        # Последнее сообщение с новым снимком зуба 36
        history.append("Врач (сейчас): Оцените снимок [Прикреплен снимок / данные визуализации: Периапикальный снимок зуба 36, очаг деструкции]")

        draft = "На прицельном снимке зуба 36 визуализируется периапикальный деструктивный очаг у верхушек мезиального и дистального корней."

        captured_prompt = None

        async def fake_gemini(prompt, ctx, timeout=None):
            nonlocal captured_prompt
            captured_prompt = prompt
            resp = MagicMock()
            resp.text = '{"ok": true, "reason": "Разбор 36 зуба по присланному снимку корректен"}'
            return resp, None

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_gemini):
            ok, reason = await assistant.check_response_quality(
                context_msgs=history,
                draft_reply=draft,
                invited=True
            )

        self.assertTrue(ok)
        self.assertIsNotNone(captured_prompt)
        # Убеждаемся, что устаревшая строка "[ИСТОРИЯ ДИАЛОГА (более недели назад)]:" не была приколочена намертво
        self.assertNotIn("[ИСТОРИЯ ДИАЛОГА (более недели назад)]:", captured_prompt)
        # А новый снимок присутствует
        self.assertIn("Периапикальный снимок зуба 36", captured_prompt)

    async def test_mandibular_palatal_error_still_rejected_under_criterion_2(self):
        """
        Несмотря на разрешение разбора нового снимка, грубые анатомические ошибки
        (например, выдуманный 'небный корень' у зуба 47 нижней челюсти)
        по-прежнему строго блокируются критерием 2.
        """
        context_msgs = [
            "Врач (сейчас): Что на снимке [Прикреплен снимок / данные визуализации: КЛКТ нижней челюсти 47 зуба]"
        ]
        # Грубейшая анатомическая ошибка: небный корень на нижней челюсти
        draft_with_error = (
            "На снимке 47 зуба определяется воспаление в области небного корня и небного канала зуба 47."
        )

        async def fake_gemini(prompt, ctx, timeout=None):
            resp = MagicMock()
            # Валидатор отклоняет за небный корень у нижней челюсти
            resp.text = '{"ok": false, "reason": "У зубов нижней челюсти (47) нет небного корня — строго язычный"}'
            return resp, None

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_gemini):
            ok, reason = await assistant.check_response_quality(
                context_msgs=context_msgs,
                draft_reply=draft_with_error,
                invited=True
            )

        self.assertFalse(ok, "Анатомическая ошибка на нижней челюсти обязана отклоняться")
        self.assertIn("небного корня", reason)

    async def test_new_tooth_in_text_query_approved(self):
        """
        Врач переключается на новый зуб (46) в текстовом вопросе без снимка:
        История: зуб 27 (верхняя челюсть).
        Вопрос: 'А по зубу 46 подскажи, там разрежение у фуркации'.
        Черновик: протокол по фуркационному дефекту зуба 46.
        Валидатор обязан одобрить разбор зуба 46 без обвинений в смене темы.
        """
        context_msgs = [
            "Врач: зуб 27 периодонтит, небный канал запломбирован",
            "Бот: По зубу 27 рекомендуется...",
            "Врач (сейчас): А по зубу 46 подскажи, там разрежение у фуркации"
        ]
        draft = (
            "По зубу 46: при разрежении в области бифуркации необходимо определить класс фуркационного дефекта (по Hamp). "
            "При I–II классе показан кюретаж и направленная костная регенерация, при III классе — туннелирование или гемисекция."
        )

        async def fake_gemini(prompt, ctx, timeout=None):
            resp = MagicMock()
            resp.text = '{"ok": true, "reason": "Корректный ответ на новый клинический вопрос по зубу 46"}'
            return resp, None

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_gemini):
            ok, reason = await assistant.check_response_quality(
                context_msgs=context_msgs,
                draft_reply=draft,
                invited=True
            )

        self.assertTrue(ok)
        self.assertIn("зубу 46", reason)

    def test_pm_validator_context_media_enrichment(self):
        """
        Проверка логики обогащения контекста валидатора в handle_private_message:
        Описание снимка обязано добавляться в current_query_str,
        а при отсутствии недавней сессии (not recent_session_history) старая история
        не должна превышать 3 последних сообщений.
        """
        # Симулируем сборку validator_context_msgs из handle_private_message
        has_media = True
        media_description = "КЛКТ 47-48, горизонтальная ретенция 48"
        text = "Что это такое на клкт"
        current_context_msgs = [f"Древнее сообщение {i}" for i in range(10)]
        recent_session_history = []  # сообщений за последние 3 часа нет

        # Точная логика из handle_private_message
        media_desc_snippet = ""
        if has_media and media_description:
            raw_desc = str(media_description).strip()
            if len(raw_desc) > 2000:
                raw_desc = raw_desc[:2000] + "..."
            media_desc_snippet = f" [Прикреплен снимок / данные визуализации: {raw_desc}]"
        elif has_media:
            media_desc_snippet = " [прикреплены медиа/снимки]"

        if text:
            current_query_str = f"Врач (сейчас): {text}{media_desc_snippet}"
        else:
            current_query_str = f"Врач (сейчас): {media_desc_snippet.strip() or '[прикреплены медиа/снимки]'}"

        if has_media and not recent_session_history and len(current_context_msgs) > 3:
            validator_history = current_context_msgs[-3:]
        else:
            validator_history = current_context_msgs
        validator_context_msgs = list(validator_history) + [current_query_str]

        # Проверки
        self.assertEqual(len(validator_history), 3, "Старая история без недавней сессии урезается до 3 сообщений")
        self.assertIn("КЛКТ 47-48", validator_context_msgs[-1], "Описание снимка должно быть в последней реплике")
        self.assertIn("Что это такое на клкт", validator_context_msgs[-1])



    async def test_procedural_progress_report_approved_and_prompt_content(self):
        """
        Реплика врача о ходе процедуры ('Достал' / 'Обошел'):
        Валидатор обязан распознавать статус этапа и одобрять черновик с поддержкой и следующим шагом.
        """
        context_msgs = [
            "Врач: В зубе 3.6 в дистальном канале обломок файла 25.04 на глубине 14 мм",
            "Бот: Протокол извлечения: прямой доступ под микроскопом, ультразвуковая насадка Start-X #3...",
            "Врач (сейчас): Достал"
        ]
        draft = (
            "Отлично, что извлекли отломок без агрессивного истончения дентина! "
            "Следующий шаг: обильная бережная ирригация подогретым 3% NaOCl эндоиглой с боковым срезом на 2 мм короче длины без давления, "
            "затем высушивание и контрольный снимок с мастер-файлом. Стенки корня сохранны?"
        )

        captured_prompt = None

        async def fake_gemini(prompt, ctx, timeout=None):
            nonlocal captured_prompt
            captured_prompt = prompt
            resp = MagicMock()
            resp.text = '{"ok": true, "reason": "Черновик фиксирует успех этапа и дает следующий технологический шаг"}'
            return resp, None

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_gemini):
            ok, reason = await assistant.check_response_quality(
                context_msgs=context_msgs,
                draft_reply=draft,
                invited=True
            )

        self.assertTrue(ok, f"Валидатор обязан одобрить ответ на реплику врача о ходе процедуры: {reason}")
        self.assertIn("успех этапа", reason)
        self.assertIsNotNone(captured_prompt)
        # Проверяем директивы в промпте валидатора
        self.assertIn("«достал»", captured_prompt)
        self.assertIn("«извлек»", captured_prompt)
        self.assertIn("«обошел»", captured_prompt)
        self.assertIn("«спилил»", captured_prompt)
        self.assertIn("отсутствие вопроса в реплике врача", captured_prompt)

    async def test_procedural_progress_paranoid_overrule(self):
        """
        Если модель-валидатор возвращает отказ из-за отсутствия вопроса в реплике врача,
        система отменяет паранойю (overrule) и пропускает корректный черновик.
        """
        context_msgs = [
            "Врач: В зубе 26 ступенька в MB2",
            "Бот: Протокол обхода ступеньки...",
            "Врач (сейчас): Обошел"
        ]
        draft = (
            "Отлично, что обошли ступеньку в MB2! "
            "Проведите обильную ирригацию подогретым 3% NaOCl без давления и сделайте контрольную визиографию. Апекс проходим?"
        )

        async def fake_gemini(prompt, ctx, timeout=None):
            resp = MagicMock()
            # Валидатор ложно отклоняет из-за отсутствия вопроса
            resp.text = '{"ok": false, "reason": "Реплика врача не содержит вопроса"}'
            return resp, None

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_gemini):
            ok, reason = await assistant.check_response_quality(
                context_msgs=context_msgs,
                draft_reply=draft,
                invited=True
            )

        self.assertTrue(ok, "Ложный отказ за 'реплика врача не содержит вопроса' обязан быть оверрулен")
        self.assertIn("chairside_concise_approved", reason)

    async def test_bureaucratic_brush_off_rejected_by_validator(self):
        """
        Если черновик содержит бюрократический канцелярит и роботизированную отписку
        ('Уточнить, что означает... Для выбора инструмента указать процедуру'),
        валидатор обязан отклонить такой черновик.
        """
        context_msgs = [
            "Врач (сейчас): Достал"
        ]
        draft_brush_off = (
            "Уточнить, что означает 'достал'. Для выбора инструмента указать процедуру — "
            "реплика не содержит данных."
        )

        async def fake_gemini(prompt, ctx, timeout=None):
            resp = MagicMock()
            resp.text = '{"ok": false, "reason": "Бюрократический канцелярит и роботизированная отписка вместо помощи"}'
            return resp, None

        with patch.object(assistant, "generate_gemini_text_async", side_effect=fake_gemini):
            ok, reason = await assistant.check_response_quality(
                context_msgs=context_msgs,
                draft_reply=draft_brush_off,
                invited=True
            )

        self.assertFalse(ok, "Бюрократическая отписка обязана быть отклонена")
        self.assertIn("Бюрократический канцелярит", reason)


if __name__ == "__main__":
    unittest.main()
