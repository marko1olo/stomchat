# -*- coding: utf-8 -*-
"""
tests/test_gnathology_phonetics_ux.py — Тестирование UI/UX и кресельного протокола фонетических проб,
функциональной окклюзии, избирательного пришлифовывания и экспресс-шаблонов для МИС.
"""

import html
import unittest
from unittest.mock import AsyncMock, MagicMock

import clinical_cognitive_core as ccc
import clinical_gnathology_phonetics as cgp
import chairside_tools as ct


class TestGnathologyPhoneticsMasterCard(unittest.TestCase):
    """Тестирование полноты клинической мастер-карточки фонетических проб."""

    def setUp(self):
        self.card = cgp.get_phonetic_test_chairside_card()

    def test_exterior_measurement_protocol_without_fingers_in_mouth(self):
        """Протокол замера снаружи: точки Subnasale и Gnathion, жесткая линейка/шпатель, запрет пальцев во рту."""
        self.assertIn("МЕТОДИКА ЗАМЕРА У КРЕСЛА (БЕЗ РУК ВО РТУ)", self.card)
        self.assertIn("Subnasale", self.card)
        self.assertIn("Gnathion", self.card)
        self.assertIn("В РОТ РУКАМИ НЕ ЛЕЗТЬ", self.card)
        self.assertIn("БЕЗ подголовника", self.card)
        self.assertIn("Этап 1:", self.card)
        self.assertIn("БЕЗ валиков во рту", self.card)
        self.assertIn("Этап 2:", self.card)
        self.assertIn("Этап 3:", self.card)
        self.assertIn("<code>VDO = VDR - (2 ... 4 мм)</code>", self.card)

    def test_sound_m_words_and_freeway_space(self):
        """Звук «М»: контрольные слова «мама», «Эмма», Freeway space 2–4 мм."""
        self.assertIn("Звук «М»", self.card)
        self.assertIn("«мама»", self.card)
        self.assertIn("«Эмма»", self.card)
        self.assertIn("<code>2–4 мм</code>", self.card)
        self.assertIn("Гизи", self.card)
        self.assertIn("без напряжения", self.card)

    def test_sound_s_silverman_clattering_and_words(self):
        """Звук «С»: слова «сосиска», «шестьдесят шесть», зазор 1.0–1.5 мм, стук зубов (завышение)."""
        self.assertIn("Звук «С»", self.card)
        self.assertIn("«шестьдесят шесть»", self.card)
        self.assertIn("«сосиска»", self.card)
        self.assertIn("<code>1.0–1.5 мм</code>", self.card)
        self.assertIn("СТУК ЗУБОВ", self.card)
        self.assertIn("ВНОЛ ЗАВЫШЕНА", self.card)

    def test_sound_fv_words_klein_line_and_errors(self):
        """Звуки «Ф», «В»: слова «Федор», «фото», «Варвара», «Вера», линия Клейна, контакт снаружи vs внутри."""
        self.assertIn("Звуки «Ф», «В»", self.card)
        self.assertIn("«Федор»", self.card)
        self.assertIn("«фото»", self.card)
        self.assertIn("«Варвара»", self.card)
        self.assertIn("«Вера»", self.card)
        self.assertIn("Линия Клейна", self.card)
        self.assertIn("сухо-влажная линия", self.card)
        self.assertIn("Контакт снаружи", self.card)
        self.assertIn("Контакт внутри", self.card)
        self.assertIn("протрузия", self.card)
        self.assertIn("палатально", self.card)

    def test_sound_i_incisal_display_young_and_elderly(self):
        """Звук «И»: видимость в покое 1.5–2.0 мм у молодых, 0.5–1.0 мм у пожилых, линия улыбки."""
        self.assertIn("Звук «И»", self.card)
        self.assertIn("<code>1.5–2.0 мм</code>", self.card)
        self.assertIn("<code>0.5–1.0 мм</code>", self.card)
        self.assertIn("положительная линия улыбки", self.card)

    def test_lisping_etiology_rugae_palatinae(self):
        """Шепелявость: переутолщение базиса в зоне rugae palatinae (истончить до 1.5–2.0 мм), сужение свода."""
        self.assertIn("Шепелявость (Lisping)", self.card)
        self.assertIn("rugae palatinae", self.card)
        self.assertIn("<code>1.5–2.0 мм</code>", self.card)
        self.assertIn("Сужение переднего свода", self.card)
        self.assertIn("Оральный наклон резцов", self.card)
        self.assertIn("Занижение прикуса", self.card)

    def test_pound_line_orientation(self):
        """Линия Паунда: клык к язычной трети ретромолярного бугорка."""
        self.assertIn("Линия Паунда", self.card)
        self.assertIn("клыка", self.card)
        self.assertIn("ретромолярного бугорка", self.card)
        self.assertIn("НЕ ДОЛЖНЫ пересекать", self.card)

    def test_master_card_contains_functional_occlusion_and_interferences(self):
        """Мастер-карточка включает функциональную окклюзию, суперконтакты и шаблоны для МИС."""
        self.assertIn("ФУНКЦИОНАЛЬНАЯ ОККЛЮЗИЯ: КЛЫКОВОЕ ВЕДЕНИЕ VS ГРУППОВАЯ ФУНКЦИЯ", self.card)
        self.assertIn("РИСКИ НА ИМПЛАНТАТАХ", self.card)
        self.assertIn("КАТЕГОРИЧЕСКИ ПРОТИВОПОКАЗАНО", self.card)
        self.assertIn("crestal bone loss", self.card)
        self.assertIn("ПРЕЖДЕВРЕМЕННЫЕ КОНТАКТЫ И СУПЕРКОНТАКТЫ", self.card)
        self.assertIn("Балансирующая сторона", self.card)
        self.assertIn("САМЫЙ ОПАСНЫЙ СУПЕРКОНТАКТ", self.card)
        self.assertIn("НЕМЕДЛЕННОЕ СОШЛИФОВЫВАНИЕ", self.card)
        self.assertIn("Правило BULL", self.card)
        self.assertIn("ЭКСПРЕСС-ШАБЛОН ДЛЯ ВСТАВКИ В КАРТУ ПАЦИЕНТА / МИС", self.card)
        self.assertIn("<code>", self.card)


class TestGnathologySubCards(unittest.TestCase):
    """Тестирование специализированных интерактивных суб-карточек."""

    def test_vdr_vdo_sub_card(self):
        """Суб-карточка VDR/VDO содержит пошаговый протокол снаружи."""
        card = cgp.format_vdr_vdo_protocol_card()
        self.assertIn("Замер VDR / VDO снаружи у кресла", card)
        self.assertIn("Subnasale", card)
        self.assertIn("Gnathion", card)
        self.assertIn("«мама»", card)
        self.assertIn("<code>VDO = VDR - 3.0 мм</code>", card)

    def test_fv_sub_card(self):
        """Суб-карточка «Ф»/«В» содержит слова и дифдиагностику."""
        card = cgp.format_fv_test_card()
        self.assertIn("Длина резцов и линия Клейна", card)
        self.assertIn("«Федор»", card)
        self.assertIn("vermilion border", card)
        self.assertIn("Контакт снаружи", card)
        self.assertIn("Контакт внутри", card)

    def test_s_sub_card(self):
        """Суб-карточка «С» содержит Closest Speaking Space и стук зубов."""
        card = cgp.format_s_test_card()
        self.assertIn("Closest Speaking Space", card)
        self.assertIn("«шестьдесят шесть»", card)
        self.assertIn("<code>1.0–1.5 мм</code>", card)
        self.assertIn("СТУК ЗУБОВ", card)
        self.assertIn("ВНОЛ ЗАВЫШЕНА", card)

    def test_lisping_sub_card(self):
        """Суб-карточка шепелявости содержит 4 анатомические причины."""
        card = cgp.format_lisping_etiology_card()
        self.assertIn("Шепелявость (Lisping) при полном съемном протезировании", card)
        self.assertIn("rugae palatinae", card)
        self.assertIn("<code>1.5–2.0 мм</code>", card)
        self.assertIn("СУЖЕНИЕ ПЕРЕДНЕГО ОТДЕЛА", card)
        self.assertIn("ОРАЛЬНЫЙ (ПАЛАТАЛЬНЫЙ) НАКЛОН", card)
        self.assertIn("ЗАНИЖЕНИЕ ВЫСОТЫ ПРИКУСА", card)

    def test_i_sub_card(self):
        """Суб-карточка «И» содержит экспозицию резцов."""
        card = cgp.format_i_test_card()
        self.assertIn("Экспозиция резцов и эстетика улыбки", card)
        self.assertIn("Положительная линия улыбки", card)
        self.assertIn("<code>1.5–2.0 мм</code>", card)
        self.assertIn("<code>0.5–1.0 мм</code>", card)

    def test_pound_sub_card(self):
        """Суб-карточка линии Паунда."""
        card = cgp.format_pound_line_card()
        self.assertIn("Линия и треугольник Паунда", card)
        self.assertIn("клыка", card)
        self.assertIn("ретромолярного бугорка", card)
        self.assertIn("сбрасывается с ложа", card)

    def test_functional_occlusion_sub_card(self):
        """Суб-карточка функциональной окклюзии: клыковое ведение vs групповая функция."""
        card = cgp.format_functional_occlusion_card()
        self.assertIn("Клыковое ведение vs Групповая функция", card)
        self.assertIn("Canine-Protected Occlusion", card)
        self.assertIn("Group Function Occlusion", card)
        self.assertIn("КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ОДИНОЧНЫХ ИМПЛАНТАТАХ", card)
        self.assertIn("ЖЕЛЕЗНОЕ ПРАВИЛО БАЛАНСИРУЮЩЕЙ СТОРОНЫ", card)
        self.assertIn("100% полная дизокклюзия", card)

    def test_occlusal_interferences_sub_card(self):
        """Суб-карточка окклюзионных суперконтактов и правил пришлифовывания."""
        card = cgp.format_occlusal_interferences_card()
        self.assertIn("Окклюзионные суперконтакты и избирательное сошлифовывание", card)
        self.assertIn("СУПЕРКОНТАКТ НА БАЛАНСИРУЮЩЕЙ (НЕРАБОЧЕЙ) СТОРОНЕ", card)
        self.assertIn("НЕМЕДЛЕННОЕ СОШЛИФОВЫВАНИЕ", card)
        self.assertIn("ПРАВИЛО BULL", card)
        self.assertIn("ПРАВИЛО DUML", card)
        self.assertIn("Shimstock 8–12 мкм", card)

    def test_clinical_ehr_templates_sub_card(self):
        """Суб-карточка экспресс-шаблонов для МИС содержит все 4 шаблона в <code>."""
        card = cgp.format_clinical_ehr_templates_card()
        self.assertIn("Экспресс-шаблоны для копирования в карту пациента / МИС", card)
        self.assertIn("Протокол окклюзионного анализа", card)
        self.assertIn("Протокол фонетических проб и VDO", card)
        self.assertIn("Протокол окклюзии на имплантатах", card)
        self.assertIn("Протокол избирательного пришлифовывания", card)
        self.assertEqual(card.count("<code>"), 4)
        self.assertEqual(card.count("</code>"), 4)


class TestFunctionalOcclusionGuidance(unittest.TestCase):
    """Тестирование клинической биомеханики: клыковое ведение vs групповая функция."""

    def test_canine_guidance_spec_and_implant_overload(self):
        """Клыковое ведение: показания, рефлекторная защита и строгий запрет на имплантатах."""
        spec = cgp.OCCLUSION_GUIDANCE_REGISTRY["CANINE_GUIDANCE"]
        self.assertEqual(spec.key, "CANINE_GUIDANCE")
        self.assertIn("Взаимозащищенная окклюзия", spec.title)
        self.assertTrue(any("I класс" in ind for ind in spec.indications))
        self.assertTrue(any("рефлекторная защита" in ind for ind in spec.indications))
        self.assertIn("КАТЕГОРИЧЕСКИ ПРОТИВОПОКАЗАНО НА ОДИНОЧНЫХ ИМПЛАНТАТАХ", spec.contraindications[0])
        self.assertIn("bending moment", spec.implant_risks)
        self.assertIn("crestal bone loss", spec.implant_risks)
        self.assertIn("chipping", spec.implant_risks)

    def test_group_function_spec_and_implant_safety(self):
        """Групповая функция: показания на имплантатах, распределение нагрузки и дизокклюзия на балансе."""
        spec = cgp.OCCLUSION_GUIDANCE_REGISTRY["GROUP_FUNCTION"]
        self.assertEqual(spec.key, "GROUP_FUNCTION")
        self.assertTrue(any("имплантат" in ind for ind in spec.indications))
        self.assertIn("БЕЗОПАСНАЯ СХЕМА ДЛЯ ИМПЛАНТАТОВ", spec.implant_risks)
        self.assertTrue(any("100% ПОЛНАЯ ДИЗОККЛЮЗИЯ" in step for step in spec.clinical_protocol))

    def test_evaluate_occlusion_scheme_decision_logic(self):
        """Логика выбора окклюзионной концепции в зависимости от клинической ситуации."""
        # Интактные клыки без имплантатов -> Клыковое ведение
        res_natural = cgp.evaluate_occlusion_scheme(missing_canine=False, has_implants_canine_zone=False)
        self.assertEqual(res_natural["recommended_scheme"], "CANINE_GUIDANCE")
        self.assertIsNone(res_natural["implant_warning"])

        # Имплантат в зоне клыка -> Строго Групповая функция
        res_implant = cgp.evaluate_occlusion_scheme(has_implants_canine_zone=True)
        self.assertEqual(res_implant["recommended_scheme"], "GROUP_FUNCTION")
        self.assertIsNotNone(res_implant["implant_warning"])
        self.assertIn("bending moment", res_implant["implant_warning"])

        # Стертость зубов или пародонтит -> Групповая функция
        res_attrition = cgp.evaluate_occlusion_scheme(severe_attrition=True)
        self.assertEqual(res_attrition["recommended_scheme"], "GROUP_FUNCTION")


class TestOcclusalInterferencesEquilibration(unittest.TestCase):
    """Тестирование диагностики интерференций и правил избирательного пришлифовывания."""

    def test_balancing_interference_is_critical_immediate_grinding(self):
        """Балансирующий суперконтакт: рычаг 2-го рода, дистракция ВНЧС, немедленное сошлифовывание."""
        spec = cgp.INTERFERENCE_REGISTRY["BALANCING_SIDE"]
        self.assertIn("рычага 2-го рода", spec.clinical_dangers)
        self.assertIn("дистракцию суставного диска", spec.clinical_dangers)
        self.assertIn("НЕМЕДЛЕННОЕ СОШЛИФОВЫВАНИЕ", spec.equilibration_rule)
        self.assertIn("СТРОГО СОХРАНЯТЬ вершины опорных бугров", spec.equilibration_rule)

    def test_working_interference_bull_rule(self):
        """Суперконтакт на рабочей стороне: правило BULL (Buccal Upper, Lingual Lower)."""
        spec = cgp.INTERFERENCE_REGISTRY["WORKING_SIDE"]
        self.assertIn("Правило BULL", spec.equilibration_rule)
        self.assertIn("КАТЕГОРИЧЕСКИ НЕЛЬЗЯ ТРОГАТЬ опорные бугры", spec.equilibration_rule)

    def test_diagnose_occlusal_interference_routing(self):
        """Функция diagnose_occlusal_interference корректно маршрутизирует клинические ситуации."""
        diag_bal = cgp.diagnose_occlusal_interference("balancing", movement="laterotrusion")
        self.assertEqual(diag_bal["type"], "BALANCING_SIDE")
        self.assertEqual(diag_bal["danger_level"], "CRITICAL")
        self.assertIn("НЕМЕДЛЕННОЕ СОШЛИФОВЫВАНИЕ", diag_bal["urgency"])

        diag_work = cgp.diagnose_occlusal_interference("working")
        self.assertEqual(diag_work["type"], "WORKING_SIDE")
        self.assertIn("BULL", diag_work["urgency"])

        diag_protrus = cgp.diagnose_occlusal_interference("protrusive")
        self.assertEqual(diag_protrus["type"], "PROTRUSIVE")
        self.assertIn("DUML", diag_protrus["urgency"])


class TestPhoneticsVDOEnhancements(unittest.TestCase):
    """Тестирование расширенной верификации VDO по звукам «М», «С»/«З», «В»/«Ф»."""

    def test_sound_s_z_constants_and_freeway(self):
        """Звуки «С», «З»: разговорный зазор 1–2 мм, стук зубов (clattering)."""
        self.assertEqual(cgp.CLOSEST_SPEAKING_SPACE_MIN_MM, 1.0)
        self.assertEqual(cgp.CLOSEST_SPEAKING_SPACE_MAX_MM, 1.5)
        self.assertEqual(cgp.SPEAKING_SPACE_S_Z_MAX_MM, 2.0)

    def test_evaluate_vdo_phonetics_normal_pass(self):
        """При нормальных значениях всех проб статус PASS."""
        res = cgp.evaluate_vdo_phonetics(s_gap_mm=1.5, fv_lip_contact="dry_wet_line", m_lips_strain=False, clattering=False)
        self.assertEqual(res["status"], "PASS")
        self.assertIn("идеальны", res["summary"])

    def test_evaluate_vdo_phonetics_clattering_detected(self):
        """При стуке зубов фиксируется завышение VDO и рекомендация снизить прикус."""
        res = cgp.evaluate_vdo_phonetics(s_gap_mm=0.5, clattering=True)
        self.assertEqual(res["status"], "CORRECTION_NEEDED")
        self.assertIn("ВНОЛ ЗАВЫШЕНА", res["summary"])
        self.assertIn("Немедленно сошлифовать", res["recommendation"])

    def test_evaluate_vdo_phonetics_fv_vermilion_contacts(self):
        """Тест касания сухой каймы губы (протрузия/длинные) и влажной слизистой (палатально/короткие)."""
        res_dry = cgp.evaluate_vdo_phonetics(fv_lip_contact="dry_vermilion")
        self.assertIn("слишком длинные или протрузия", res_dry["summary"])

        res_wet = cgp.evaluate_vdo_phonetics(fv_lip_contact="wet_mucosa")
        self.assertIn("слишком короткие или ретрузия", res_wet["summary"])

    def test_evaluate_vdo_phonetics_m_strain(self):
        """Напряжение губ при пробе «М» указывает на завышение высоты."""
        res = cgp.evaluate_vdo_phonetics(m_lips_strain=True)
        self.assertIn("напряжение круговой мышцы рта", res["summary"])


class TestClinicalEHRTemplates(unittest.TestCase):
    """Тестирование экспресс-шаблонов для копирования в карту пациента / МИС."""

    def test_all_templates_present_in_registry(self):
        """Реестр содержит 4 основных клинических шаблона."""
        templates = cgp.get_all_ehr_templates()
        self.assertIn("gnathology_protocol", templates)
        self.assertIn("phonetics_vdo", templates)
        self.assertIn("implant_occlusion", templates)
        self.assertIn("equilibration", templates)

    def test_template_getters_return_monospace_code_blocks(self):
        """Все геттеры шаблонов возвращают текст в тегах <code>...</code>."""
        for fn in (
            cgp.get_ehr_gnathology_protocol_template,
            cgp.get_ehr_phonetics_vdo_template,
            cgp.get_ehr_implant_occlusion_template,
            cgp.get_ehr_equilibration_template,
        ):
            text = fn()
            self.assertTrue(text.startswith("<code>"))
            self.assertTrue(text.endswith("</code>"))
            self.assertGreater(len(text), 100)

    def test_html_safety_in_ehr_templates(self):
        """Внутри <code> нет незаэкранированных угловых скобок, ломающих парсер HTML."""
        for key, spec in cgp.get_all_ehr_templates().items():
            escaped = html.escape(spec.template_text)
            self.assertNotIn("<script", escaped)
            self.assertNotIn("<<", escaped)


class TestChairsidePhoneticsInteractiveUI(unittest.IsolatedAsyncioTestCase):
    """Тестирование разметки инлайн-кнопок, роутинга и отсутствия спиннеров."""

    def test_phonetics_menu_markup_buttons(self):
        """Разметка меню фонетики содержит все подпункты и иерархию назад."""
        markup = ct.build_phonetics_menu_markup()
        all_cbs = [btn.data.decode("utf-8") if isinstance(btn.data, bytes) else btn.data
                   for row in markup for btn in row]

        self.assertIn("phonetics:vdr", all_cbs)
        self.assertIn("phonetics:fv", all_cbs)
        self.assertIn("phonetics:s", all_cbs)
        self.assertIn("phonetics:lisping", all_cbs)
        self.assertIn("phonetics:pound", all_cbs)
        self.assertIn("phonetics:i", all_cbs)
        self.assertIn("chairside:hub", all_cbs)
        self.assertIn("nav:main", all_cbs)

    def test_is_chairside_callback_recognizes_all_phonetics_keys(self):
        """is_chairside_callback распознает все ключи phonetics:*."""
        self.assertTrue(ct.is_chairside_callback("chairside:phonetics"))
        self.assertTrue(ct.is_chairside_callback("phonetics:menu"))
        self.assertTrue(ct.is_chairside_callback("phonetics:vdr"))
        self.assertTrue(ct.is_chairside_callback("phonetics:fv"))
        self.assertTrue(ct.is_chairside_callback("phonetics:s"))
        self.assertTrue(ct.is_chairside_callback("phonetics:lisping"))
        self.assertTrue(ct.is_chairside_callback("phonetics:pound"))
        self.assertTrue(ct.is_chairside_callback("phonetics:i"))

    async def test_dispatch_phonetics_submenu_callbacks(self):
        """Диспетчеризация суб-колбэков: edit вызывается, answer() снимает спиннер."""
        mock_client = AsyncMock()
        mock_event = AsyncMock()
        mock_event.answer = AsyncMock()
        mock_event.edit = AsyncMock()

        sub_callbacks = [
            ("phonetics:vdr", "Замер VDR / VDO снаружи"),
            ("phonetics:fv", "Длина резцов и линия Клейна"),
            ("phonetics:s", "Closest Speaking Space"),
            ("phonetics:lisping", "Шепелявость (Lisping)"),
            ("phonetics:pound", "Линия и треугольник Паунда"),
            ("phonetics:i", "Экспозиция резцов"),
        ]

        for cb_data, expected_text in sub_callbacks:
            mock_event.reset_mock()
            handled = await ct.dispatch_chairside_callback(mock_client, mock_event, cb_data)
            self.assertTrue(handled, f"Callback '{cb_data}' must be handled")
            mock_event.answer.assert_awaited()
            mock_event.edit.assert_awaited()

            args, kwargs = mock_event.edit.call_args
            self.assertIn(expected_text, args[0])

            # Проверяем кнопку «Назад к пробам»
            buttons = kwargs.get("buttons") or args[1]
            btn_data = [btn.data.decode("utf-8") if isinstance(btn.data, bytes) else btn.data
                        for row in buttons for btn in row]
            self.assertIn("chairside:phonetics", btn_data)
            self.assertIn("chairside:hub", btn_data)

    async def test_handle_phonetics_command_with_arguments(self):
        """Команда /phonetics с аргументами (vdr, s, lisping, fv, pound) открывает нужные карточки."""
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock()
        mock_event = AsyncMock()
        mock_event.chat_id = 98765
        mock_event.message = MagicMock(id=111)

        test_args = [
            ("/phonetics vdr", "Замер VDR / VDO снаружи"),
            ("/phonetics s", "Closest Speaking Space"),
            ("/phonetics fv", "Длина резцов и линия Клейна"),
            ("/phonetics lisping", "Шепелявость (Lisping)"),
            ("/phonetics pound", "Линия и треугольник Паунда"),
            ("/phonetics", "ВНОЛ/VDO"),
        ]

        for cmd_str, expected_substring in test_args:
            mock_client.reset_mock()
            handled = await ct.handle_if_chairside_command(mock_client, mock_event, cmd_str)
            self.assertTrue(handled, f"Command '{cmd_str}' must be handled")
            mock_client.send_message.assert_awaited()

            sent_text = mock_client.send_message.call_args[0][1]
            self.assertIn(expected_substring, sent_text)


class TestCognitiveCoreGnathologyCompleteness(unittest.TestCase):
    """Тестирование наличия секции гнатологии и полноты в когнитивной архитектуре."""

    def test_master_cognitive_architecture_contains_section_32(self):
        """MASTER_COGNITIVE_ARCHITECTURE содержит секцию 32 по гнатологии и фонетическим пробам."""
        arch = ccc.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("32. ГНАТОЛОГИЧЕСКИЙ ПРОТОКОЛ ОПРЕДЕЛЕНИЯ ВНОЛ, ФОНЕТИЧЕСКИХ ПРОБ И ДИКЦИИ", arch)
        self.assertIn("GNATHOLOGY & PHONETIC TESTS COMPLETENESS", arch)
        self.assertIn("МЕТОДИКА ЗАМЕРА СНАРУЖИ (БЕЗ РУК ВО РТУ)", arch)
        self.assertIn("Subnasale", arch)
        self.assertIn("Gnathion", arch)
        self.assertIn("«мама»", arch)
        self.assertIn("«сосиска»", arch)
        self.assertIn("«Федор»", arch)
        self.assertIn("Clattering", arch)
        self.assertIn("rugae palatinae", arch)



class TestRedTeamDoctorQueriesInquisitor(unittest.TestCase):
    """Тестирование ответов на 6 реальных запросов врачей-стоматологов из живой базы."""

    def test_query_1_sounds_and_words_for_vdo_summary(self):
        """Запрос 1: «Напиши звуки и слова которые помогают определять высоту нижней трети лица»."""
        q = "Напиши звуки и слова которые помогают определять высоту нижней трети лица"
        res = cgp.answer_gnathology_doctor_query(q)
        self.assertIsNotNone(res)
        self.assertEqual(res["key"], "speech_tests")
        card = res["card"]

        # Конкретные речевые тесты и слова
        self.assertIn("«мама»", card)
        self.assertIn("«шестьдесят шесть»", card)
        self.assertIn("«миссисипи»", card)
        self.assertIn("«Февраль»", card)
        self.assertIn("«Валентин»", card)
        self.assertIn("«И-и-и»", card)

        # Что видит и слышит врач при гипер- и гипоокклюзии
        self.assertIn("ГИПЕРОККЛЮЗИЯ", card)
        self.assertIn("ГИПООККЛЮЗИЯ", card)
        self.assertIn("m. mentalis", card)
        self.assertIn("симптом наперстка", card)
        self.assertIn("clattering", card)
        self.assertIn("ангулярный хейлит", card)

        # 1-тап шаблон для МИС
        self.assertIn("<code>", card)
        self.assertIn("</code>", card)
        self.assertIn("ПРОТОКОЛ РЕЧЕВЫХ ПРОБ VDO", card)

    def test_query_2_lisping_etiology_full_reasons(self):
        """Запрос 2: «Шепелявость при полном съемном протезировании причины»."""
        q = "Шепелявость при полном съемном протезировании причины"
        res = cgp.answer_gnathology_doctor_query(q)
        self.assertIsNotNone(res)
        self.assertEqual(res["key"], "lisping")
        card = res["card"]

        # Все 4 причины
        self.assertIn("rugae palatinae", card)
        self.assertIn("1.5–2.0 мм", card)
        self.assertIn("СУЖЕНИЕ ПЕРЕДНЕГО ОТДЕЛА", card)
        self.assertIn("кончика языка", card)
        self.assertIn("ЗАВЫШЕНИЕ ИЛИ ЗАНИЖЕНИЕ", card)
        self.assertIn("ПРОТРУЗИЯ", card)
        self.assertIn("НАКЛОН РЕЗЦОВ", card)

        # 1-тап шаблон для МИС
        self.assertIn("<code>", card)
        self.assertIn("ДИФДИАГНОСТИКА ШЕПЕЛЯВОСТИ", card)

    def test_query_3_sound_s_how_to_verify(self):
        """Запрос 3: «Звук с Как проверить»."""
        q = "Звук с Как проверить"
        res = cgp.answer_gnathology_doctor_query(q)
        self.assertIsNotNone(res)
        self.assertEqual(res["key"], "s_test")
        card = res["card"]

        # Контрольные слова и счет
        self.assertIn("«шестьдесят шесть»", card)
        self.assertIn("«миссисипи»", card)
        self.assertIn("«сосиска»", card)

        # Разговорный зазор и стук зубов (завышение VDO)
        self.assertIn("1–2 мм", card)
        self.assertIn("СТУК ЗУБОВ", card)
        self.assertIn("ВНОЛ ЗАВЫШЕНА", card)
        self.assertIn("гиперокклюзия", card)

        # 1-тап шаблон для МИС
        self.assertIn("<code>", card)
        self.assertIn("ПРОБА «С»/«З»", card)

    def test_query_4_sounds_fv_words_and_interpretation(self):
        """Запрос 4: «Звуки ф и в какие слова сказать И как интерпритировать»."""
        q = "Звуки ф и в какие слова сказать И как интерпритировать"
        res = cgp.answer_gnathology_doctor_query(q)
        self.assertIsNotNone(res)
        self.assertEqual(res["key"], "fv_test")
        card = res["card"]

        # Слова
        self.assertIn("«Февраль»", card)
        self.assertIn("«Валентин»", card)
        self.assertIn("«Федор»", card)
        self.assertIn("«Вера»", card)

        # Линия Клейна и интерпретация контактов снаружи vs внутри
        self.assertIn("Линия Клейна", card)
        self.assertIn("сухо-влажная линия", card)
        self.assertIn("Контакт снаружи", card)
        self.assertIn("слишком длинные", card)
        self.assertIn("протрузия", card)
        self.assertIn("Контакт внутри", card)
        self.assertIn("слишком короткие", card)

        # 1-тап шаблон для МИС
        self.assertIn("<code>", card)
        self.assertIn("ПРОБА «Ф»/«В»", card)

    def test_query_5_sound_i_edentulous_verification(self):
        """Запрос 5: «Звук и При полном отсутствии зубов что поможет нам проверить»."""
        q = "Звук и При полном отсутствии зубов что поможет нам проверить"
        res = cgp.answer_gnathology_doctor_query(q)
        self.assertIsNotNone(res)
        self.assertEqual(res["key"], "i_test")
        card = res["card"]

        # Что помогает проверить звук «И» при ПСП:
        self.assertIn("полном отсутствии зубов", card)
        self.assertIn("Натяжение мимических мышц", card)
        self.assertIn("m. risorius", card)
        self.assertIn("m. zygomaticus", card)
        self.assertIn("Стабильность и фиксация протеза", card)
        self.assertIn("зрачковой линии", card)
        self.assertIn("Положительная линия улыбки", card)
        self.assertIn("Экспозиция резцов", card)

        # 1-тап шаблон для МИС
        self.assertIn("<code>", card)
        self.assertIn("ПРОБА «И»", card)

    def test_query_6_optimal_vdo_criteria(self):
        """Запрос 6: «Высота нижней трети оптимальна если...»."""
        q = "Высота нижней трети оптимальна если..."
        res = cgp.answer_gnathology_doctor_query(q)
        self.assertIsNotNone(res)
        self.assertEqual(res["key"], "optimal_vdo")
        card = res["card"]

        # Все 5 объективных критериев
        self.assertIn("Freeway Space", card)
        self.assertIn("2.0–4.0 мм", card)
        self.assertIn("БЕЗ напряжения мимических мышц", card)
        self.assertIn("m. orbicularis oris", card)
        self.assertIn("m. mentalis", card)
        self.assertIn("«шестьдесят шесть»", card)
        self.assertIn("БЕЗ стука зубов", card)
        self.assertIn("сухо-влажной линии", card)
        self.assertIn("Мышечно-суставной комфорт", card)

        # 1-тап шаблон для МИС
        self.assertIn("<code>", card)
        self.assertIn("ВЕРИФИКАЦИЯ ВЫСОТЫ ПРИКУСА", card)

    def test_unknown_query_returns_none(self):
        """Неизвестный запрос возвращает None."""
        self.assertIsNone(cgp.answer_gnathology_doctor_query(""))
        self.assertIsNone(cgp.answer_gnathology_doctor_query("как готовить пиццу"))

    def test_all_sub_cards_have_monospace_ehr_templates(self):
        """Все специализированные суб-карточки содержат блок шаблона <code>...</code>."""
        cards = [
            cgp.format_vdr_vdo_protocol_card(),
            cgp.format_fv_test_card(),
            cgp.format_s_test_card(),
            cgp.format_lisping_etiology_card(),
            cgp.format_i_test_card(),
            cgp.format_pound_line_card(),
            cgp.format_functional_occlusion_card(),
            cgp.format_occlusal_interferences_card(),
            cgp.format_speech_tests_summary_card(),
            cgp.format_optimal_vdo_criteria_card(),
        ]
        for card in cards:
            self.assertIn("<code>", card)
            self.assertIn("</code>", card)
            self.assertEqual(card.count("<code>"), card.count("</code>"))


if __name__ == "__main__":
    unittest.main()
