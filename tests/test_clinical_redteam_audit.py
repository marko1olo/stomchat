# -*- coding: utf-8 -*-
"""
tests/test_clinical_redteam_audit.py — Тесты клинического и редтим-аудита PM и когнитивного ядра.

Проверяет:
1. Искоренение нейросетевых штампов GPT-6.2 Sol:
   Запрет заголовков «1. Клинико-рентгенологический и анатомический статус»,
   «2. Диагностические развилки», «3. Пошаговая тактика», «4. Завершение»,
   «Рентгенологический статус: 1. Локализация...».
2. Запрет механического деления живого ответа врача на нумерованные списки.
3. Тотальный запрет просачивания академических аббревиатур (ESE, AAE, ITI, Cochrane, EFP, EBM, SEDENTEXCT)
   в практический текст для врача и карусель статусов генерации.
4. Клиническая глубина в когнитивном ядре и директивах PM:
   - Токояма Юниверсал Бонд + подготовка и фиксация СВШ (химическое отверждение, мономер 3D-SR, силан gamma-MPTS,
     нейтрализация NaOCl, 17% EDTA, цемент двойного отверждения Estecem II/Panavia).
   - Обострение периодонтита 3.6 с лихорадкой 38.9°C (дифдиагноз с пульпитом 37, холодовой тест -50°C Endo-Ice,
     запрет открытого зуба, декомпрессия patency #10, антибиотикотерапия Амоксиклав, маршрутизация ЧЛХ/112).
   - Эффект Попова-Годона при имплантации (зубы 16, 17, 26, 27) (формы I и II, межокклюзионная высота >=7-8 мм,
     развилки при отказе от TADs: селективное пришлифовывание vs протетическая редукция с депульпированием и E.max/Zr vs остеотомия vs ВНОЛ).
   - Разбор снимков без роботизированных категорий.
5. Инварианты стилей общения STYLE_PROMPTS (colleague_friendly, clinical_dry, humor_cynic).
"""

import sys
import unittest

# Настройка UTF-8 для вывода
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import assistant
import clinical_cognitive_core


class TestClinicalBoilerplateBan(unittest.TestCase):
    """Тестирование искоренения нейросетевых штампов GPT-6.2 Sol и шаблонных заголовков."""

    def test_gpt_sol_headers_banned_in_ccc_rule22(self):
        """В Правиле 22 когнитивного ядра зафиксирован строгий запрет заголовков GPT-6.2 Sol."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("1. Клинико-рентгенологический и анатомический статус", core)
        self.assertIn("2. Диагностические развилки", core)
        self.assertIn("3. Пошаговая тактика", core)
        self.assertIn("4. Завершение", core)

    def test_gpt_sol_headers_banned_in_vision_cognitive_core(self):
        """В VISION_COGNITIVE_ARCHITECTURE запрещены шаблонные заголовки GPT-6.2 Sol."""
        vision = clinical_cognitive_core.VISION_COGNITIVE_ARCHITECTURE
        self.assertIn("1. Клинико-рентгенологический и анатомический статус", vision)
        self.assertIn("2. Диагностические развилки", vision)
        self.assertIn("3. Пошаговая тактика", vision)
        self.assertIn("4. Завершение", vision)

    def test_gpt_sol_headers_banned_in_pm_directive(self):
        """В PM_CLINICAL_COGNITIVE_DIRECTIVE запрещены шаблонные нейросетевые заголовки."""
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE
        self.assertIn("1. Клинико-рентгенологический и анатомический статус", pm_dir)
        self.assertIn("2. Диагностические развилки", pm_dir)
        self.assertIn("3. Пошаговая тактика", pm_dir)
        self.assertIn("4. Завершение", pm_dir)

    def test_uncaptioned_directive_does_not_enforce_numbered_blocks(self):
        """Директива разбора снимка без подписи не навязывает блочную нумерацию 1., 2., 3., 4."""
        with open("assistant.py", "r", encoding="utf-8") as f:
            code = f.read()

        # Старая шаблонная директива должна отсутствовать
        self.assertNotIn("строго по следующим блокам:\n1. <b>Клинико-рентгенологический", code)
        self.assertNotIn("2. <b>Дифференциальный диагноз:</b>", code)
        self.assertNotIn("3. <b>Пошаговая EBM-тактика и план действий:</b>", code)
        self.assertNotIn("4. <b>Завершение:</b>", code)


class TestMechanicalNumberedListsBan(unittest.TestCase):
    """Тестирование запрета механического деления ответов на нумерованные списки."""

    def test_ccc_rule15_bans_mechanical_lists(self):
        """Правило 15 содержит ТОТАЛЬНЫЙ ЗАПРЕТ МЕХАНИЧЕСКИХ СПИСКОВ В ДИАЛОГЕ."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("ТОТАЛЬНЫЙ ЗАПРЕТ МЕХАНИЧЕСКИХ СПИСКОВ В ДИАЛОГЕ", core)

    def test_pm_directive_rule2_bans_mechanical_lists(self):
        """Пункт 2 PM_CLINICAL_COGNITIVE_DIRECTIVE запрещает механические нумерованные списки."""
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE
        self.assertIn("ТОТАЛЬНЫЙ ЗАПРЕТ МЕХАНИЧЕСКИХ СПИСКОВ В ДИАЛОГЕ (ZERO MECHANICAL LISTS)", pm_dir)


class TestAcademicAcronymsBan(unittest.TestCase):
    """Тестирование запрета академических аббревиатур (ESE, AAE, ITI, Cochrane, EFP, EBM)."""

    def test_ccc_rule3_enforces_zero_acronyms(self):
        """Правило 3 когнитивного ядра требует нулевого использования академических аббревиатур."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("ТОТАЛЬНЫЙ ЗАПРЕТ АКАДЕМИЧЕСКИХ АББРЕВИАТУР (ZERO ACRONYMS PROSE)", core)

    def test_pm_directive_rule3_enforces_zero_acronyms(self):
        """Пункт 3 PM_CLINICAL_COGNITIVE_DIRECTIVE запрещает ESE, AAE, ITI, Cochrane, EFP, EBM."""
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE
        self.assertIn("СТРОГИЙ ЗАПРЕТ АКАДЕМИЧЕСКИХ АББРЕВИАТУР (ZERO ACRONYMS PROSE)", pm_dir)
        self.assertIn("ESE, AAE, ITI, Cochrane, EFP, EBM", pm_dir)

    def test_carousel_status_no_academic_acronyms(self):
        """Карусель ожидания генерации ответа не содержит аббревиатур ESE, ITI, AHA."""
        with open("assistant.py", "r", encoding="utf-8") as f:
            code = f.read()
        self.assertNotIn("<i>Сверяю международные гайдлайны (ESE, ITI, AHA)", code)
        self.assertIn("<i>Сверяю клинические протоколы, точные дозировки мг/кг, ", code)


class TestClinicalDepthStandards(unittest.TestCase):
    """Тестирование практической клинической глубины (Tokuyama Universal Bond, СВШ, Flare-up, Попов-Годон)."""

    def test_section_25_exists_in_cognitive_core(self):
        """Раздел 25 присутствует в MASTER_COGNITIVE_ARCHITECTURE."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("25. БЕСКОМПРОМИССНАЯ КЛИНИЧЕСКАЯ ГЛУБИНА", core)

    def test_tokuyama_universal_bond_protocol_depth(self):
        """Протокол Tokuyama Universal Bond содержит ключевые клинические параметры."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("Tokuyama Universal Bond", core)
        self.assertIn("3D-SR", core)
        self.assertIn("γ-MPTS", core)
        self.assertIn("НЕ ТРЕБУЕТ СВЕТОВОЙ ПОЛИМЕРИЗАЦИИ", core)
        self.assertIn("НЕ ТРЕБУЕТ ОТДЕЛЬНОГО СИЛАНА", core)
        self.assertIn("Estecem II", core)

    def test_fiber_post_and_canal_preparation_depth(self):
        """Протокол подготовки СВШ и канала содержит регламент обработки."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("алмазным диском", core)
        self.assertIn("кусачки и бокорезы категорически запрещены", core)
        self.assertIn("17% EDTA", core)
        self.assertIn("свободный кислород, который ингибирует свободнорадикальную полимеризацию", core)

    def test_periodontitis_flare_up_fever_depth(self):
        """Протокол обострения с лихорадкой 38.9°C содержит неотложный регламент."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("38.9°C", core)
        self.assertIn("Endo-Ice", core)
        self.assertIn("patency", core)
        self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО оставлять зуб открытым", core)
        self.assertIn("Амоксиклав", core)
        self.assertIn("ангины Людвига", core)

    def test_popov_godon_mechanics_depth(self):
        """Протокол феномена Попова-Годона содержит классификацию и варианты коррекции."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertTrue("ПОПОВА-ГОДОНА" in core or "Попова-Годона" in core)
        self.assertIn("ПОНОМАРЕВОЙ / ГАВРИЛОВУ", core.upper())
        self.assertIn("7–8 мм", core)
        self.assertIn("Протетическая редукция", core)
        self.assertIn("депульпирование", core)
        self.assertIn("ВНОЛ", core)


class TestStylePromptsIntegrity(unittest.TestCase):
    """Тестирование полноты и тональности стилей общения STYLE_PROMPTS."""

    def test_style_prompts_keys_and_content(self):
        """Стили colleague_friendly, clinical_dry и humor_cynic содержат обновленные требования."""
        styles = getattr(assistant, "STYLE_PROMPTS", {})
        self.assertIn("colleague_friendly", styles)
        self.assertIn("clinical_dry", styles)
        self.assertIn("humor_cynic", styles)

        for name, prompt_text in styles.items():
            self.assertIn("ESE, AAE, ITI, Cochrane", prompt_text, f"В стиле {name} отсутствует запрет академических аббревиатур!")
            self.assertIn("«я бы»", prompt_text, f"В стиле {name} отсутствует запрет сослагательного 'я бы'!")




class TestChairsideInquisitorClinicalDomains(unittest.TestCase):
    """Тестирование 4 новых клинических доменов: эмфизема, перфорация пазухи, починка композита, Zero-Iteration."""

    def test_subcutaneous_emphysema_protocol_in_core_and_pm(self):
        """Подкожная эмфизема лица и шеи: универсальный алгоритм триажа и ведения у кресла."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text in (core, pm_dir):
            self.assertIn("шуршит как пакет", text)
            self.assertIn("крепитация", text)
            self.assertIn("надключичн", text)
            self.assertIn("пневмомедиастинум", text)
            self.assertIn("112", text)
            self.assertIn("Амоксиклав", text)
            self.assertIn("875/125", text)
            self.assertIn("массаж", text)
            self.assertTrue("компресс" in text or "тепло" in text)

    def test_sinus_perforation_oac_protocol_in_core_and_pm(self):
        """Перфорация гайморовой пазухи / Ороантральное сообщение содержит дифференцированный алгоритм."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text in (core, pm_dir):
            self.assertTrue("Вальсальвы" in text or "Вальсальва" in text)
            self.assertIn("< 2 мм", text)
            self.assertTrue("2–5 мм" in text or "2-5 мм" in text or "2 – 5 мм" in text)
            self.assertIn("> 5 мм", text)
            self.assertTrue("Регрману" in text or "Rehrmann" in text)
            self.assertTrue("Оксиметазолин" in text or "Ксилометазолин" in text)
            self.assertIn("Амоксиклав", text)
            self.assertIn("йодоформ", text)

    def test_composite_repair_silane_sandblasting_benchmark(self):
        """Починка старого композита: эталонный адгезивный протокол репарации."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text in (core, pm_dir):
            self.assertTrue("Al2O3" in text or "оксидом алюминия" in text)
            self.assertTrue("27–50 мкм" in text or "27-50 мкм" in text)
            self.assertTrue("H3PO4" in text or "ортофосфорн" in text)
            self.assertTrue("НЕ ТРАВИТ" in text or "НЕ травит" in text or "не травит" in text or "Исключительно для очистки" in text or "ИСКЛЮЧИТЕЛЬНО ДЛЯ ОЧИСТКИ" in text or "ИСКЛЮЧИТЕЛЬНО очистка" in text)
            self.assertTrue("силан" in text or "Силан" in text or "γ-MPTS" in text)
            self.assertTrue("10-MDP" in text)
            self.assertTrue("0.5 мм" in text)

    def test_zero_iteration_chairside_protocol(self):
        """Директива исчерпывающей полноты у кресла: 4-компонентный ответ с Turn 1 и запрет тизеров."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        # Проверка универсального присутствия директивы Zero-Iteration
        self.assertTrue("ZERO ITERATION CHAIRSIDE PROTOCOL" in core or "ДИРЕКТИВА ИСЧЕРПЫВАЮЩЕЙ ПОЛНОТЫ" in core)
        self.assertTrue("ZERO ITERATION CHAIRSIDE PROTOCOL" in pm_dir or "ДИРЕКТИВА ИСЧЕРПЫВАЮЩЕЙ ПОЛНОТЫ" in pm_dir)

        # 1. Диагноз / статус, топография и геометрия
        self.assertIn("стерильных перчатках", core)
        self.assertIn("олива с красной полосой", core)
        self.assertIn("chamfer", core)
        self.assertTrue("1.0–1.2 мм" in core or "1.0-1.2 мм" in core)
        self.assertTrue("1.5–2.0 мм" in core or "1.5-2.0 мм" in core)
        self.assertIn("биологической ширины", core)
        self.assertIn("феррул", core)

        self.assertIn("олива с красной полосой", pm_dir)
        self.assertIn("chamfer", pm_dir)

        # 2. Пошаговый EBM-протокол манипуляции
        self.assertIn("пошаговый ebm-протокол", core.lower())
        self.assertIn("пошаговый ebm-протокол", pm_dir.lower())

        # 3. Фармакотерапия
        self.assertIn("фармакотерапия", core.lower())
        self.assertIn("фармакотерапия", pm_dir.lower())

        # 4. Риски / пределы безопасности / маршрутизация
        self.assertIn("члх", core.lower())
        self.assertIn("члх", pm_dir.lower())

        # 5. Оформление протоколов в <code>...</code> для 1-тап копирования в МИС
        self.assertIn("<code>...</code>", core)
        self.assertIn("<code>...</code>", pm_dir)

    def test_pm_few_shot_contrast_example_5_emphysema(self):
        """В PM_FEW_SHOT_CONTRAST_BLOCK эталонный триаж эмфиземы без привязки к номерам кейсов."""
        fs = assistant.PM_FEW_SHOT_CONTRAST_BLOCK
        self.assertIn("ПРИМЕР 5: Неотложная ситуация у кресла", fs)
        self.assertIn("шуршит как пакет", fs)
        self.assertIn("пневмомедиастинума", fs)
        self.assertIn("112", fs)

    def test_ankylosis_vs_retention_molar_blockade_protocol(self):
        """Протокол: Анкилоз зуба vs ретенция vs механическая блокада."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text in (core, pm_dir):
            self.assertTrue("анкилоз" in text or "Анкилоз" in text)
            self.assertTrue("инфраокклюзи" in text or "Инфраокклюзи" in text)
            self.assertIn("металлический звук", text)
            self.assertIn("звук треснувшей чашки", text)
            self.assertIn("КЛКТ", text)
            self.assertTrue("механическая блокада" in text or "Механическая блокада" in text)
            self.assertTrue("сублюксация" in text or "Сублюксация" in text)
            self.assertIn("24 час", text)
            self.assertTrue("оверлей" in text or "накладка" in text or "коронаропластика" in text)
            self.assertTrue("остеотомия" in text or "Остеотомия" in text)

    def test_pm_few_shot_contrast_example_6_ankylosis(self):
        """В PM_FEW_SHOT_CONTRAST_BLOCK дифдиагноз анкилоза и блокады без привязки к номерам сообщений."""
        fs = assistant.PM_FEW_SHOT_CONTRAST_BLOCK
        self.assertIn("ПРИМЕР 6: Ортодонтия / Инфраокклюзия моляра", fs)
        self.assertIn("металлический звук", fs)
        self.assertIn("звук треснувшей чашки", fs)
        self.assertIn("КЛКТ", fs)
        self.assertIn("сублюксация", fs)




class TestPromptDecontamination(unittest.TestCase):
    """Тестирование деконтаминации промптов: никаких user ID, номеров сообщений, тикетов/инцидентов."""

    def test_no_user_ids_in_prompt_architectures(self):
        """В когнитивном ядре и директивах PM отсутствуют user ID (напр. 1054868515)."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE
        fs = assistant.PM_FEW_SHOT_CONTRAST_BLOCK

        for text, name in [(core, "MASTER_COGNITIVE_ARCHITECTURE"),
                           (pm_dir, "PM_CLINICAL_COGNITIVE_DIRECTIVE"),
                           (fs, "PM_FEW_SHOT_CONTRAST_BLOCK")]:
            self.assertNotIn("1054868515", text, f"User ID 1054868515 найден в {name}!")

    def test_no_message_ids_in_section_headers(self):
        """В заголовках разделов PM_CLINICAL_COGNITIVE_DIRECTIVE отсутствуют номера сообщений (53080, 185553, 185599)."""
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE
        fs = assistant.PM_FEW_SHOT_CONTRAST_BLOCK

        for bad_str in ["СООБЩЕНИЯ 185553–185565", "КЕЙС СООБЩЕНИЙ 185599–185607", "КЕЙС 1054868515", "СООБЩЕНИЯ 53080–53085"]:
            self.assertNotIn(bad_str, pm_dir, f"Загрязнение {bad_str} найдено в PM_CLINICAL_COGNITIVE_DIRECTIVE!")
            self.assertNotIn(bad_str, fs, f"Загрязнение {bad_str} найдено в PM_FEW_SHOT_CONTRAST_BLOCK!")


class TestProceduralStatusUpdateSupport(unittest.TestCase):
    """Тестирование распознавания процедурных реплик («достал», «извлек», «обошел», «спилил») и запрета канцелярита."""

    def test_procedural_milestones_supported_in_core_and_pm(self):
        """В когнитивном ядре и директивах PM зафиксированы манипуляции («достал», «извлек», «обошел», «спилил»)."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text in (core, pm_dir):
            for verb in ["достал", "извлек", "обошел", "спилил"]:
                self.assertIn(verb, text.lower(), f"Глагол манипуляции '{verb}' отсутствует в директивах!")

    def test_bureaucratic_brush_offs_strictly_banned(self):
        """Канцелярские отписки («Уточнить, что означает...», «Для выбора инструмента указать процедуру») строго запрещены."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text in (core, pm_dir):
            self.assertIn("Уточнить, что означает", text)
            self.assertIn("Для выбора инструмента указать процедуру", text)

    def test_pm_few_shot_contrast_example_9_procedural_report(self):
        """В PM_FEW_SHOT_CONTRAST_BLOCK присутствует Пример 9 с контрастной парой (канцелярит vs поддержка коллеги)."""
        fs = assistant.PM_FEW_SHOT_CONTRAST_BLOCK
        self.assertIn("ПРИМЕР 9: Ход процедуры у кресла", fs)
        self.assertIn("Уточнить, что означает", fs)
        self.assertIn("Отлично, что извлекли", fs)




class TestPediatricPreventiveCariologyAudit(unittest.TestCase):
    """Тестирование практических EBM-ответов по детской стоматологии, кариесологии и профилактике."""

    def test_stephan_curve_in_core_and_pm(self):
        """Кривая Стефана: критический pH 5.5, 20-30 минут деминерализации, частота атак, сладкое после еды."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text, name in [(core, "MASTER_COGNITIVE_ARCHITECTURE"), (pm_dir, "PM_CLINICAL_COGNITIVE_DIRECTIVE")]:
            self.assertIn("5.5", text, f"pH 5.5 отсутствует в {name}")
            self.assertTrue("20–30 минут" in text or "20-30 минут" in text, f"Время деминерализации 20-30 мин отсутствует в {name}")
            self.assertTrue("ЧАСТОТА" in text or "частота" in text, f"Частота атак отсутствует в {name}")
            self.assertTrue("СРАЗУ ПОСЛЕ" in text or "сразу после" in text, f"Правило приема сладкого после еды отсутствует в {name}")

    def test_acid_erosion_vs_abrasion_and_30min_pause(self):
        """Кислотная эрозия (pH < 4.0): размягчение эмали, запрет чистки щеткой, пауза 30 минут."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text, name in [(core, "MASTER_COGNITIVE_ARCHITECTURE"), (pm_dir, "PM_CLINICAL_COGNITIVE_DIRECTIVE")]:
            self.assertIn("ПАУЗА 30 МИНУТ", text, f"Пауза 30 мин отсутствует в {name}")
            self.assertTrue("эрози" in text.lower(), f"Термин эрозия отсутствует в {name}")
            self.assertTrue("абрази" in text.lower(), f"Термин абразия отсутствует в {name}")

    def test_who_child_sugar_limits(self):
        """Рекомендации ВОЗ/AAPD по сахару: <5% калорийности, 19-24 г (4-5 ложек) для 5-6 лет, 0 г до 2 лет."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text, name in [(core, "MASTER_COGNITIVE_ARCHITECTURE"), (pm_dir, "PM_CLINICAL_COGNITIVE_DIRECTIVE")]:
            self.assertIn("19–24", text, f"Норма 19-24г отсутствует в {name}")
            self.assertTrue("5%" in text, f"Лимит 5% отсутствует в {name}")
            self.assertTrue("0 грамм" in text or "0 г" in text, f"Лимит 0г до 2 лет отсутствует в {name}")

    def test_enzyme_toothpastes_ebm_status(self):
        """Ферментные пасты (лактопероксидаза, лизоцим) и EBM-статус фторидов/nHAP."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text, name in [(core, "MASTER_COGNITIVE_ARCHITECTURE"), (pm_dir, "PM_CLINICAL_COGNITIVE_DIRECTIVE")]:
            self.assertTrue("лактопероксидаза" in text or "лизоцим" in text, f"Ферменты отсутствуют в {name}")
            self.assertIn("1000–1450 ppm", text, f"Стандарт 1000-1450 ppm фтора отсутствует в {name}")
            self.assertTrue("nHAP" in text or "гидроксиапатит" in text, f"nHAP отсутствует в {name}")

    def test_nhap_vs_fluorides_and_splat_rationale(self):
        """Фторапатит (pH 4.5) vs nHAP, маркетинг паст без фтора (Splat) и дозировки детям."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text, name in [(core, "MASTER_COGNITIVE_ARCHITECTURE"), (pm_dir, "PM_CLINICAL_COGNITIVE_DIRECTIVE")]:
            self.assertIn("4.5", text, f"Критический pH 4.5 фторапатита отсутствует в {name}")
            self.assertTrue("хемофоби" in text.lower() or "splat" in text.lower(), f"Анализ паст без фтора отсутствует в {name}")
            self.assertTrue("рисовое зерно" in text.lower(), f"Дозировка рисовое зерно отсутствует в {name}")
            self.assertTrue("горошина" in text.lower(), f"Дозировка горошина отсутствует в {name}")

    def test_spit_dont_rinse_protocol(self):
        """Международный протокол Spit don't rinse («Сплюнь, не полощи»): выполоскать крошки ДО чистки."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

        for text, name in [(core, "MASTER_COGNITIVE_ARCHITECTURE"), (pm_dir, "PM_CLINICAL_COGNITIVE_DIRECTIVE")]:
            self.assertTrue("spit, don't rinse" in text.lower() or "сплюнь, не полощи" in text.lower(), f"Spit don't rinse отсутствует в {name}")
            self.assertTrue("крошек" in text.lower() or "до чистки" in text.lower(), f"Инструкция по крошкам/полосканию ДО чистки отсутствует в {name}")

    def test_parent_memo_and_kindergarten_lesson_plan(self):
        """Готовая памятка родителям и 20-минутный сценарий урока гигиены в детском саду."""
        core = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("Памятка для родителей: здоровая улыбка ребенка", core)
        self.assertIn("Сценарий интерактивного урока гигиены в детском саду (20 минут)", core)
        self.assertIn("2х2", core)
        self.assertIn("до 8–9 лет", core)

    def test_pm_few_shot_contrast_example_10(self):
        """В PM_FEW_SHOT_CONTRAST_BLOCK присутствует Пример 10 по детской гигиене и кариесологии."""
        fs = assistant.PM_FEW_SHOT_CONTRAST_BLOCK
        self.assertIn("ПРИМЕР 10: Профилактика, кариесология и детский прием", fs)
        self.assertIn("Кривая Стефана", fs)
        self.assertIn("Spit don't rinse", fs)
        self.assertIn("Памятка для родителей", fs)


if __name__ == "__main__":
    unittest.main()
