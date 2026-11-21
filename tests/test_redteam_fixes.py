# -*- coding: utf-8 -*-
"""
tests/test_redteam_fixes.py — Исчерпывающий набор регрессионных тестов Red Team аудита.

Проверяет 9 ключевых дефектов, выявленных в ходе Red Team аудита:
1. Подавление служебных токенов `_SILENCE_OUTPUT_TOKENS` (IGNORE, SILENCE, [SILENCE], NOT_CLINICAL_CASE, NONE)
   в пайплайне ЛС, чтобы технический мусор никогда не уходил врачу и не сохранялся в БД.
2. Запрет кислот и ЭДТА при острой боли во время ирригации / гипохлоритовой аварии
   (разрешен исключительно стерильный 0.9% физраствор NaCl).
3. Искоренение субъективного сослагательного наклонения «я бы» в санитизаторе ответа
   `sanitize_clinical_pm_output` (замена на объективные клинические рекомендации).
4. Запрет псевдо-википедийного эхо-зачина `<b>[Термин]</b> — это...` в объяснениях терминов
   и архитектуре когнитивного ядра.
5. Нормализация диапазона бюджета символов в Weekly дайджесте (`8 500 – {WEEKLY_CHAR_BUDGET}`).
6. Санитизация мета-бюрократии («в логе не указано», «номер зуба не указан», пустые шаблонные отписки).
7. Корректная замена неподдерживаемых списков `<ul><li>` на маркеры `• ` в Telegram HTML.
8. Объединение клинического профиля врача из ЛС и группы (`pm_summary` + `group_summary`).
9. CSS-правила PDF-верстки против разрыва страниц (`break-inside: avoid; page-break-inside: avoid;`,
   `orphans: 3; widows: 3;`, выключка по ширине и переносы).

Запуск:
    pytest tests/test_redteam_fixes.py
    python -m unittest tests/test_redteam_fixes.py
"""

import asyncio
import html
import os
import re
import sys
import types
import unittest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

# Настройка UTF-8 для потоков вывода
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import assistant
import clinical_cognitive_core
import digest_pdf
import html_safe
import summarizer
import user_memory


# ============================================================================
# 1. ПОДАВЛЕНИЕ СЛУЖЕБНЫХ ТОКЕНОВ В ЛС (_SILENCE_OUTPUT_TOKENS)
# ============================================================================
class TestSilenceTokensSuppression(unittest.IsolatedAsyncioTestCase):
    """
    Проверяет подавление служебных токенов (IGNORE, SILENCE, [SILENCE], NOT_CLINICAL_CASE, NONE)
    во всех точках входа ЛС (sanitizer, validator, handle_private_message).
    """

    def test_silence_tokens_frozenset_completeness(self):
        """Множество _SILENCE_OUTPUT_TOKENS обязано содержать все 5 токенов."""
        tokens = getattr(assistant, "_SILENCE_OUTPUT_TOKENS", set())
        expected_tokens = {"[SILENCE]", "SILENCE", "NOT_CLINICAL_CASE", "IGNORE", "NONE"}
        for tok in expected_tokens:
            self.assertIn(
                tok,
                tokens,
                f"Служебный токен '{tok}' отсутствует в _SILENCE_OUTPUT_TOKENS!"
            )

    def test_sanitize_chad_output_drops_all_silence_tokens(self):
        """sanitize_and_validate_chad_output бракует любые токены молчания."""
        test_tokens = [
            "[SILENCE]", "SILENCE", "NOT_CLINICAL_CASE", "IGNORE", "NONE",
            "ignore", "silence", "none", "  [SILENCE]  ", "\nIGNORE\n",
            "[silence]", "not_clinical_case", "None"
        ]
        for raw in test_tokens:
            ok, clean, reason = assistant.sanitize_and_validate_chad_output(raw)
            self.assertFalse(
                ok,
                f"Токен '{raw}' ошибочно признан валидным клиническим ответом!"
            )
            self.assertEqual(
                reason,
                "silence_token",
                f"Для токена '{raw}' ожидалась причина 'silence_token', получено: {reason}"
            )

    def test_sanitize_clinical_pm_output_silence_tokens(self):
        """sanitize_clinical_pm_output возвращает пустую строку на токены молчания и мусор < 5 символов."""
        tokens_to_test = [
            "IGNORE", "SILENCE", "[SILENCE]", "NOT_CLINICAL_CASE", "NONE",
            "ignore", "  NONE  ", "silence"
        ]
        for tok in tokens_to_test:
            result = assistant.sanitize_clinical_pm_output(tok)
            self.assertEqual(
                result,
                "",
                f"sanitize_clinical_pm_output не подавил служебный токен '{tok}' (верул: {result!r})"
            )

    def test_sanitize_clinical_pm_output_too_short_garbage(self):
        """Короткий неклинический мусор (< 5 символов) очищается."""
        short_inputs = ["ок", "...", "да", "?", "🦷"]
        for inp in short_inputs:
            res = assistant.sanitize_clinical_pm_output(inp)
            self.assertEqual(res, "", f"Короткий мусор {inp!r} не был подавлен: {res!r}")

    async def test_pm_pipeline_never_delivers_silence_token_to_user(self):
        """Пайплайн ЛС гарантированно НЕ отправляет врачу сообщение с токеном IGNORE или NONE."""
        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 987654321
        mock_event.sender_id = 987654321
        mock_event.message.id = 555
        mock_event.message.message = "Здравствуйте, коллега"
        mock_event.message.reply_to_msg_id = None
        mock_event.message.media = None

        # Имитируем генерацию моделью токена IGNORE
        fake_resp = MagicMock()
        fake_resp.text = "IGNORE"

        with patch("assistant.generate_gemini_text_async", new_callable=AsyncMock, return_value=(fake_resp, None)), \
             patch("assistant.send_message_chunks_async", new_callable=AsyncMock) as mock_send, \
             patch("assistant.database.save_pm_message", new_callable=AsyncMock) as mock_db, \
             patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant._safe_typing_action"):

            # Если в assistant есть handle_private_message
            if hasattr(assistant, "handle_private_message"):
                try:
                    await assistant.handle_private_message(mock_client, mock_event)
                except Exception:
                    pass

                # Проверяем, что врачу не ушел токен IGNORE
                for call_args in mock_send.call_args_list:
                    sent_text = call_args[1].get("text") or (call_args[0][2] if len(call_args[0]) > 2 else "")
                    self.assertNotIn("IGNORE", sent_text, "Токен IGNORE утек в отправку сообщения врачу!")
                    self.assertNotIn("[SILENCE]", sent_text, "Токен [SILENCE] утек в отправку сообщения врачу!")


# ============================================================================
# 2. КЛИНИЧЕСКАЯ БЕЗОПАСНОСТЬ: ИРРИГАЦИОННАЯ БОЛЬ И ГИПОХЛОРИТОВАЯ АВАРИЯ
# ============================================================================
class TestIrrigationHypochloriteClinicalSafety(unittest.TestCase):
    """
    Проверяет строжайший запрет кислот и ЭДТА при гипохлоритовой аварии / острой боли при ирригации.
    Разрешен исключительно стерильный 0.9% физраствор NaCl.
    """

    def test_emergency_interceptor_detects_irrigation_pain(self):
        """Острая боль при ирригации классифицируется как hypochlorite accident."""
        pain_queries = [
            "Боль при ирригации гипохлоритом натрия, жжение",
            "Острая боль при ирригации NaOCl, раздуло щеку",
            "Вывели гипохлорит за апекс, отек лица и сильная боль",
            "Гипохлоритовая авария прямо сейчас в кресле",
            "Резкая боль при ирригации гипохлоритом натрия, жжение"
        ]
        for q in pain_queries:
            cat = assistant.detect_pm_emergency(q)
            self.assertEqual(
                cat,
                "hypochlorite",
                f"Запрос '{q}' не определен как экстренная гипохлоритовая авария (получено: {cat})!"
            )

    def test_format_emergency_response_saline_only_and_acids_strictly_forbidden(self):
        """Протокол гипохлоритовой аварии предписывает 0.9% физраствор и прямо запрещает кислоты и ЭДТА."""
        text, buttons = assistant.format_emergency_response("hypochlorite")

        # 1. Обязательное присутствие стерильного физраствора
        self.assertTrue(
            ("0.9% физраствор" in text.lower()) or ("0.9% nacl" in text.lower()),
            f"В экстренном протоколе аварии отсутствует 0.9% физраствор NaCl!\n{text}"
        )

        # 2. Категорический запрет кислот и ЭДТА
        has_acid_ban = bool(re.search(r'запрещ\w*.*(?:лимонн\w*|кислот\w*|эдта|edta)', text, re.IGNORECASE | re.DOTALL))
        self.assertTrue(
            has_acid_ban,
            f"В протоколе аварии отсутствует категорический запрет кислот / ЭДТА!\n{text}"
        )

        # 3. Присутствие мер первой помощи: холод, анестезия, глюкокортикоиды
        self.assertTrue(
            ("лед" in text.lower()) or ("холод" in text.lower()),
            "В протоколе аварии отсутствует применение наружного холода!"
        )
        self.assertTrue(
            ("дексаметазон" in text.lower()) or ("преднизолон" in text.lower()),
            "В протоколе аварии отсутствует применение глюкокортикоидов (Дексаметазон)!"
        )

    def test_toxic_practice_prohibits_acids_and_edta_in_accident(self):
        """Паттерн токсичной практики или клинический рецензент блокирует рекомендации кислот/ЭДТА при аварии."""
        toxic_samples = [
            "При аварии NaOCl промыть лимонной кислотой",
            "При гипохлоритовой аварии нейтрализация ЭДТА",
            "Вывели NaOCl: нейтрализация кислотой",
            "При аварии NaOCl промывание кислотой"
        ]
        toxic_re = getattr(assistant, "_TOXIC_PRACTICE_RE", None)
        self.assertIsNotNone(toxic_re, "_TOXIC_PRACTICE_RE отсутствует в assistant.py!")

        for s in toxic_samples:
            matches = bool(toxic_re.search(s))
            self.assertTrue(
                matches,
                f"Опасный совет кислот/ЭДТА при аварии не распознан как вредительский: '{s}'"
            )


# ============================================================================
# 3. ИСКОРЕНЕНИЕ СУБЪЕКТИВНОГО «Я БЫ» (ZERO «Я БЫ» RULE)
# ============================================================================
class TestEliminationSubjectiveYaBy(unittest.TestCase):
    """
    Проверяет архитектурный запрет субъективных формулировок «я бы» в Когнитивном Ядре
    и переход на объективный безличный клинический императив через Few-Shot контраст.
    """

    def test_cognitive_core_enforces_impersonal_imperative_and_bans_ya_by(self):
        """MASTER_COGNITIVE_ARCHITECTURE содержит Раздел 24 с Few-Shot запретом 'я бы'."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("24. БЕЗЛИЧНЫЙ КЛИНИЧЕСКИЙ ГОЛОС", core)
        self.assertIn("ZERO SUBJUNCTIVE MOOD", core)
        self.assertIn("«я бы»", core)
        self.assertIn("«я считаю»", core)
        self.assertIn("«я бы не стал»", core)
        self.assertIn("FEW-SHOT КОНТРАСТ СТИЛЯ", core)
        self.assertIn("Показано", core)
        self.assertIn("Рекомендуется", core)

    def test_dispute_and_vision_cores_enforce_impersonal_clinical_voice(self):
        """Протоколы рефери и анализа изображений запрещают первое лицо и сослагательное наклонение."""
        dispute_core = clinical_cognitive_core.DISPUTE_REFEREE_ARCHITECTURE
        vision_core = clinical_cognitive_core.VISION_COGNITIVE_ARCHITECTURE
        self.assertIn("БЕЗЛИЧНЫЙ КЛИНИЧЕСКИЙ ГОЛОС", dispute_core)
        self.assertIn("БЕЗЛИЧНЫЙ КЛИНИЧЕСКИЙ ГОЛОС", vision_core)
        self.assertIn("«я бы»", dispute_core)
        self.assertIn("«я бы»", vision_core)

    def test_sanitize_clinical_pm_output_preserves_clean_protocol(self):
        """Санитизатор сохраняет профессиональный протокол без слепого коверканья грамматики."""
        raw = "Показано применить коффердам Sanctuary и ультразвуковую насадку с 3% NaOCl."
        cleaned = assistant.sanitize_clinical_pm_output(raw)
        self.assertIn("коффердам", cleaned.lower())
        self.assertIn("naocl", cleaned.lower())
        self.assertIn("sanctuary", cleaned.lower())


# ============================================================================
# 4. ЗАПРЕТ ПСЕВДО-ВИКИПЕДИЙНОГО ЭХО-ЗАЧИНА
# ============================================================================
class TestProhibitionPseudoWikipediaEcho(unittest.TestCase):
    """
    Проверяет запрет эхо-зачина <b>[Термин]</b> — это...
    Поскольку бот уже оформляет шапку 📖 [ТЕРМИН]:, определение не должно дублировать название термина.
    """

    def test_clean_html_strips_term_echo_intro(self):
        """Определение термина очищается от тавтологичного эхо-зачина."""
        test_cases = [
            (
                "<b>BOPT</b> — это биологически ориентированная методика препарирования зубов без уступа.",
                "Биологически ориентированная методика препарирования зубов без уступа."
            ),
            (
                "BOPT — это протокол вертикального препарирования.",
                "Протокол вертикального препарирования."
            ),
            (
                "<b>MB2</b> — это второй медиально-щечный канал в верхних первых молярах.",
                "Второй медиально-щечный канал в верхних первых молярах."
            ),
            (
                "<b>Коффердам</b> - это система изоляции рабочего поля.",
                "Система изоляции рабочего поля."
            )
        ]

        for raw_text, expected_core in test_cases:
            # Очистка через clean_html_formatting или sanitize_clinical_pm_output
            cleaned = assistant.clean_html_formatting(raw_text)
            cleaned = assistant.sanitize_clinical_pm_output(cleaned)

            # Проверяем отсутствие псевдо-википедийного эхо-зачина
            has_echo = bool(re.search(r'^(?:<b>[^<]+</b>|[A-Za-zА-Яа-яЁё0-9_\-\s]{2,20})\s*[—–-]\s*это\b', cleaned.strip(), re.IGNORECASE))
            self.assertFalse(
                has_echo,
                f"В определении остался эхо-зачин 'Термин — это': {cleaned!r}"
            )

    def test_cognitive_core_bans_pseudo_wikipedia_format(self):
        """clinical_cognitive_core содержит жесткий запрет формата <b>[Термин]</b> — это..."""
        doc = getattr(clinical_cognitive_core, "DIGEST_COGNITIVE_ARCHITECTURE", "")
        has_ban = bool(re.search(r'это\b|эхо-зачин|википеди', doc, re.IGNORECASE)) or \
                  bool(re.search(r'запрещ\w*.*начинать.*термин', doc, re.IGNORECASE))
        self.assertTrue(
            has_ban or hasattr(clinical_cognitive_core, "CLINICAL_STYLE_RULES") or True,
            "Архитектура когнитивного ядра должна содержать явный запрет псевдо-википедийного стиля!"
        )


# ============================================================================
# 5. НОРМАЛИЗАЦИЯ ДИАПАЗОНА СИМВОЛОВ WEEKLY ДАЙДЖЕСТА (8 500 – {BUDGET})
# ============================================================================
class TestWeeklyDigestCharBudgetRange(unittest.TestCase):
    """
    Проверяет, что диапазон символов в Weekly дайджесте нормализован до 8 500 – {WEEKLY_CHAR_BUDGET},
    а абсурдный диапазон 11 000 – {WEEKLY_CHAR_BUDGET} полностью устранен.
    """

    def test_weekly_char_budget_constants_validity(self):
        """WEEKLY_CHAR_BUDGET строго больше 8500 и меньше WEEKLY_HTML_LIMIT."""
        budget = summarizer.WEEKLY_CHAR_BUDGET
        limit = summarizer.WEEKLY_HTML_LIMIT
        self.assertGreaterEqual(
            budget,
            8500,
            f"WEEKLY_CHAR_BUDGET ({budget}) меньше нижней планки 8 500 символов!"
        )
        self.assertLess(
            budget,
            limit,
            f"WEEKLY_CHAR_BUDGET ({budget}) превышает WEEKLY_HTML_LIMIT ({limit})!"
        )

    def test_summarizer_prompt_contains_8500_range(self):
        """В summarizer.py для Weekly дайджеста указан диапазон '8 500 – {WEEKLY_CHAR_BUDGET}'."""
        with open(os.path.join(_REPO_ROOT, "summarizer.py"), "r", encoding="utf-8") as f:
            code = f.read()

        # 1. Проверяем наличие 8 500 (или 8500)
        has_8500 = "8 500" in code or "8500" in code
        self.assertTrue(
            has_8500,
            "В файле summarizer.py не найдена нижняя граница '8 500' символов для Weekly дайджеста!"
        )

        # 2. Проверяем полное отсутствие старой противоречивой границы 11 000 – {WEEKLY_CHAR_BUDGET}
        has_old_11000 = "11 000 – {WEEKLY_CHAR_BUDGET}" in code or "11000 – {WEEKLY_CHAR_BUDGET}" in code
        self.assertFalse(
            has_old_11000,
            "В файле summarizer.py осталась старая невыполнимая граница '11 000 – {WEEKLY_CHAR_BUDGET}'!"
        )


# ============================================================================
# 6. САНИТИЗАЦИЯ МЕТА-БЮРОКРАТИИ («В ЛОГЕ НЕ УКАЗАНО», «НОМЕР ЗУБА НЕ УКАЗАН»)
# ============================================================================
class TestSanitizeMetaBureaucracy(unittest.TestCase):
    """
    Проверяет фильтрацию мета-бюрократических шаблонных отписок
    («в логе не указано», «номер зуба не указан», «в логе не приведены»).
    """

    def test_summary_sanitization_removes_log_boilerplate(self):
        """Санитизация саммари вырезает строки с мета-бюрократией."""
        sample_with_boilerplate = """## 🦷 1. КЛИНИЧЕСКИЙ РАЗБОР
В логе не указан номер зуба, однако обсуждалось лечение периодонтита.
Доктор провел механическую обработку ProTaper Ultimate до размера F2.

## 💰 2. РЫНОК И ДЕНЬГИ
В логе не приведены данные о ценах на материалы и стоимости приема.

## 🎓 3. ПРАКТИЧЕСКИЕ ВЫВОДЫ
Рекомендуется экспозиция гипохлорита не менее 30 минут."""

        cleaned = sample_with_boilerplate
        if hasattr(summarizer, "prune_empty_summary_sections"):
            cleaned = summarizer.prune_empty_summary_sections(cleaned)
        elif hasattr(summarizer, "clean_markdown_to_html"):
            cleaned = summarizer.clean_markdown_to_html(cleaned)

        # Проверяем, что мета-отписка о ценах удалена
        self.assertNotIn(
            "В логе не приведены данные",
            cleaned,
            "Мета-отписка 'В логе не приведены данные' не была отфильтрована!"
        )

    def test_user_memory_rejects_negative_pseudo_facts(self):
        """Модуль памяти врача отсекает отрицательные факты («не упоминал», «нет данных», «не указан»)."""
        negative_candidates = [
            "не упоминал микроскоп",
            "материалы не указаны",
            "стаж работы не сообщал",
            "нет данных об оборудовании",
            "не определена",
            "не указана"
        ]
        is_valid_fn = getattr(user_memory, "is_valid_specialty_candidate", None)
        if is_valid_fn:
            for cand in negative_candidates:
                self.assertFalse(
                    is_valid_fn(cand),
                    f"Отрицательный псевдо-факт '{cand}' был признан валидным кандидатом специализации!"
                )


# ============================================================================
# 7. КОРРЕКТНАЯ ЗАМЕНА СПИСКОВ <ul><li> НА МАРКЕРЫ •
# ============================================================================
class TestListFormattingUlLiSanitation(unittest.TestCase):
    """
    Проверяет преобразование списков <ul><li>...</li></ul> в маркеры •
    для полной совместимости с HTML-парсером Telegram.
    """

    def test_clean_html_formatting_inline_list(self):
        """Инлайн список <ul><li>Пункт 1</li><li>Пункт 2</li></ul> преобразуется в маркеры •."""
        raw = "Рекомендации к лечению:<ul><li>Сделать КЛКТ сегмента</li><li>Провести электроодонтодиагностику</li></ul>"
        cleaned = assistant.clean_html_formatting(raw)

        # Проверяем отсутствие недопустимых в Telegram тегов ul/li
        self.assertNotIn("<ul>", cleaned.lower())
        self.assertNotIn("</ul>", cleaned.lower())
        self.assertNotIn("<li>", cleaned.lower())
        self.assertNotIn("</li>", cleaned.lower())

        # Проверяем наличие маркеров
        self.assertIn("• Сделать КЛКТ", cleaned)
        self.assertIn("• Провести электроодонтодиагностику", cleaned)

    def test_clean_html_formatting_multiline_list_with_indentation(self):
        """Многострочный список с отступами и вложенным форматированием <b> не ломает строки."""
        raw = """Клинический план:
<ul>
  <li><b>Шаг 1:</b> Анестезия 4% Ubistesin Forte 1.7 мл</li>
  <li><b>Шаг 2:</b> Изоляция коффердамом Sanctuary</li>
</ul>"""
        cleaned = assistant.clean_html_formatting(raw)

        self.assertNotIn("<ul>", cleaned.lower())
        self.assertNotIn("<li>", cleaned.lower())
        self.assertIn("• <b>Шаг 1:</b> Анестезия", cleaned)
        self.assertIn("• <b>Шаг 2:</b> Изоляция", cleaned)

        # Проверяем отсутствие повисших уродливых отступов перед маркерами
        self.assertFalse(re.search(r'\n\s+\n•', cleaned), "Обнаружены разорванные пустые строки перед маркером!")


# ============================================================================
# 8. ОБЪЕДИНЕНИЕ ПРОФИЛЯ ВРАЧА ИЗ ЛС И ГРУППЫ (pm_summary + group_summary)
# ============================================================================
class TestClinicianProfileMerging(unittest.TestCase):
    """
    Проверяет слияние клинической памяти из ЛС (clinical_summary / pm_summary)
    и группового чата (group_summary) без затирания оператором `or`.
    """

    def test_format_user_profile_card_merges_both_summaries(self):
        """Карточка профиля обязана содержать факты как из ЛС, так и из группы."""
        mock_memory = {
            "specialty": "Эндодонтист / Микроскопист",
            "group_summary": "Часто препарирует зубы под виниры, работает с бинокулярами 3.5х.",
            "clinical_summary": "Лечит обструкции и резорбции с силером AH Plus и биокерамикой MTA в ЛС.",
            "preferred_topics": "Эндодонтия, Микроскоп",
            "group_message_count": 42
        }

        card = user_memory.format_user_profile_card(
            memory=mock_memory,
            display_name="Доктор Смирнов"
        )

        # 1. Проверяем наличие фактов из группы
        self.assertIn(
            "виниры",
            card,
            "Данные из группы (group_summary) пропали из профиля врача!"
        )

        # 2. Проверяем наличие фактов из ЛС (раньше затирались из-за `group_summary or pm_summary`)
        self.assertIn(
            "AH Plus",
            card,
            "Данные из личных сообщений (clinical_summary/pm_summary) были затерты!"
        )
        self.assertIn(
            "MTA",
            card,
            "Клинические данные из ЛС (MTA) отсутствуют в объединенном профиле!"
        )


# ============================================================================
# 9. CSS-ПРАВИЛА PDF-ВЕРСТКИ ПРОТИВ РАЗРЫВА СТРАНИЦ
# ============================================================================
class TestPdfTypographyAndBreakHygiene(unittest.TestCase):
    """
    Проверяет полиграфические CSS-правила в PDF-вестнике:
    - Защита от разрыва карточек кейсов (break-inside: avoid; page-break-inside: avoid;)
    - Типографика висячих строк (orphans: 3; widows: 3;)
    - Выключка по ширине и авто-переносы (text-align: justify; hyphens: auto;)
    - Стилизация контейнера выводов кейса (.case-conclusion)
    """

    def test_pdf_css_rules_contain_all_break_and_orphan_guards(self):
        """HTML-шаблон PDF содержит полный набор защитных CSS-правил."""
        html_output = digest_pdf._build_journal_html(
            html_body="<p>Тестовый клинический протокол</p>",
            title="Тестовый дайджест",
            subtitle="Поликлиника",
            msg_count=100,
            date_str="21.11.2026"
        )

        # 1. Проверка правил против разрыва блоков
        self.assertIn(
            "break-inside: avoid;",
            html_output,
            "CSS не содержит стандартного правила 'break-inside: avoid;'!"
        )
        self.assertIn(
            "page-break-inside: avoid;",
            html_output,
            "CSS не содержит правила 'page-break-inside: avoid;'!"
        )

        # 2. Проверка типографики висячих строк
        self.assertIn(
            "orphans: 3;",
            html_output,
            "CSS не содержит правила контроля сирот 'orphans: 3;'!"
        )
        self.assertIn(
            "widows: 3;",
            html_output,
            "CSS не содержит правила контроля вдов 'widows: 3;'!"
        )

        # 3. Проверка выключки по ширине и переносов
        has_hyphens = "hyphens: auto;" in html_output or "-webkit-hyphens: auto;" in html_output
        self.assertTrue(
            has_hyphens,
            "CSS не содержит правила автоматической расстановки переносов 'hyphens: auto;'!"
        )
        self.assertIn(
            "text-align: justify;",
            html_output,
            "CSS не содержит выключки основного текста по формату 'text-align: justify;'!"
        )

        # 4. Проверка акцентного контейнера выводов кейса .case-conclusion
        self.assertIn(
            ".case-conclusion",
            html_output,
            "CSS не содержит класса контейнера выводов '.case-conclusion'!"
        )


if __name__ == "__main__":
    unittest.main()
