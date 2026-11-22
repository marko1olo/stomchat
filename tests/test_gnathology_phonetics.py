# -*- coding: utf-8 -*-
"""
tests/test_gnathology_phonetics.py — Комплексные тесты гнатологического модуля
фонетических проб и Chairside-протоколов (Domain 5).

Проверяет:
1. Числовые константы и клинические интервалы (Freeway Space, Closest Speaking Space, Incisal display).
2. Описание всех 5 тестов (звуки «М», «Ф/В», «С», «И», линия Паунда) и точность биомеханических правил.
3. Расчетные и диагностические функции:
   - calculate_vdo_from_vdr: формулы, завышение/занижение прикуса, границы нормы;
   - diagnose_speaking_space_issue: дифдиагностика соударения (clattering) vs шепелявости (lisping);
   - check_pound_line_compliance: контроль нейтральной зоны и предотвращение сбрасывания протеза.
4. Генерацию форматированной карточки get_phonetic_test_chairside_card.
5. Реэкспорт через clinical_cognitive_core.
6. Интеграцию в Chairside Tools:
   - Наличие chairside:phonetics в разметке хаба;
   - Регистрация в is_chairside_callback и is_chairside_command;
   - Диспетчеризация callback chairside:phonetics;
   - Команды прямого действия (/phonetics, /фонетика, /внол, /vdo).
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

import clinical_cognitive_core as ccc
import clinical_gnathology_phonetics as cgp
import chairside_tools as ct


class TestGnathologyPhoneticConstants(unittest.TestCase):
    """Тестирование строгости клинических констант и числовых интервалов."""

    def test_freeway_space_intervals(self):
        """Межокклюзионный промежуток (проба по Гизи) строго 2.0 – 4.0 мм."""
        self.assertEqual(cgp.FREEWAY_SPACE_MIN_MM, 2.0)
        self.assertEqual(cgp.FREEWAY_SPACE_MAX_MM, 4.0)
        self.assertEqual(cgp.FREEWAY_SPACE_DEFAULT_MM, 3.0)
        self.assertLess(cgp.FREEWAY_SPACE_MIN_MM, cgp.FREEWAY_SPACE_MAX_MM)

    def test_closest_speaking_space_intervals(self):
        """Ближайшее разговорное расстояние по Сильверману (звук «С») строго 1.0 – 1.5 мм."""
        self.assertEqual(cgp.CLOSEST_SPEAKING_SPACE_MIN_MM, 1.0)
        self.assertEqual(cgp.CLOSEST_SPEAKING_SPACE_MAX_MM, 1.5)
        self.assertLess(cgp.CLOSEST_SPEAKING_SPACE_MIN_MM, cgp.CLOSEST_SPEAKING_SPACE_MAX_MM)

    def test_incisal_display_intervals(self):
        """Обнажение резцов из-под верхней губы в покое строго 1.5 – 2.0 мм."""
        self.assertEqual(cgp.INCISAL_DISPLAY_REST_MIN_MM, 1.5)
        self.assertEqual(cgp.INCISAL_DISPLAY_REST_MAX_MM, 2.0)
        self.assertLess(cgp.INCISAL_DISPLAY_REST_MIN_MM, cgp.INCISAL_DISPLAY_REST_MAX_MM)


class TestGnathologyPhoneticRegistry(unittest.TestCase):
    """Тестирование структуры и полноты реестра клинических тестов."""

    def setUp(self):
        self.registry = cgp.get_phonetic_test_registry()

    def test_registry_contains_all_five_tests(self):
        """Реестр содержит 5 обязательных тестов: M, F_V, S, I, POUND."""
        expected_keys = {"M", "F_V", "S", "I", "POUND"}
        self.assertTrue(expected_keys.issubset(set(self.registry.keys())))

    def test_sound_m_gysi_protocol(self):
        """Тест звука «М»: покой по Гизи, freeway space, формула VDO."""
        test_m = self.registry["M"]
        self.assertIn("Гизи", test_m.author)
        self.assertIn("покоя", test_m.target_landmark.lower())
        self.assertIn("2.0 – 4.0", test_m.normal_interval_mm)
        self.assertIn("freeway_too_small", test_m.error_interpretations)
        self.assertIn("freeway_too_large", test_m.error_interpretations)

    def test_sound_f_v_silverman_wet_dry(self):
        """Тесты звуков «Ф», «В»: контакт с линией Клейна (граница сухой и влажной каймы)."""
        test_fv = self.registry["F_V"]
        self.assertIn("Сильверман", test_fv.author)
        self.assertIn("Клейна", test_fv.normal_interval_mm)
        self.assertIn("incisal_edge_too_lingual_or_short", test_fv.error_interpretations)
        self.assertIn("incisal_edge_too_labial_or_long", test_fv.error_interpretations)

    def test_sound_s_closest_speaking_space(self):
        """Тест звука «С»: closest speaking space, clattering (завышение) и lisping (шепелявость)."""
        test_s = self.registry["S"]
        self.assertIn("Сильверман", test_s.author)
        self.assertIn("1.0 – 1.5", test_s.normal_interval_mm)
        self.assertIn("clattering", test_s.error_interpretations)
        self.assertIn("lisping", test_s.error_interpretations)
        # Clattering обязательно указывает на завышение ВНОЛ
        self.assertIn("ЗАВЫШЕНА", test_s.error_interpretations["clattering"])

    def test_sound_i_smile_display(self):
        """Тест звука «И»: видимость резцов в покое и при улыбке."""
        test_i = self.registry["I"]
        self.assertIn("1.5 – 2.0", test_i.normal_interval_mm)
        self.assertIn("reverse_smile_line", test_i.error_interpretations)

    def test_pound_line_and_triangle(self):
        """Линия Паунда: ограничение язычных поверхностей боковых зубов нижней челюсти."""
        test_pound = self.registry["POUND"]
        self.assertIn("Паунд", test_pound.author)
        self.assertIn("клыка", test_pound.protocol_steps[0].lower())
        self.assertIn("ретромолярн", test_pound.protocol_steps[0].lower())
        self.assertIn("lingual_encroachment", test_pound.error_interpretations)
        self.assertIn("buccal_displacement", test_pound.error_interpretations)


class TestGnathologyCalculationsAndDiagnostics(unittest.TestCase):
    """Тестирование клинических расчетных функций."""

    def test_calculate_vdo_from_vdr_normal(self):
        """Корректный расчет VDO в пределах нормы (2–4 мм freeway space)."""
        res = cgp.calculate_vdo_from_vdr(vdr_mm=70.0, freeway_space_mm=3.0)
        self.assertTrue(res.is_valid_range)
        self.assertEqual(res.vdo_mm, 67.0)
        self.assertEqual(res.freeway_space_mm, 3.0)
        self.assertIn("НОРМА", res.diagnostic_comment)

    def test_calculate_vdo_from_vdr_too_small(self):
        """Зазор < 2 мм сигнализирует о завышении прикуса."""
        res = cgp.calculate_vdo_from_vdr(vdr_mm=70.0, freeway_space_mm=1.0)
        self.assertFalse(res.is_valid_range)
        self.assertEqual(res.vdo_mm, 69.0)
        self.assertIn("завышена", res.diagnostic_comment.lower())

    def test_calculate_vdo_from_vdr_too_large(self):
        """Зазор > 4 мм сигнализирует о занижении прикуса."""
        res = cgp.calculate_vdo_from_vdr(vdr_mm=70.0, freeway_space_mm=5.5)
        self.assertFalse(res.is_valid_range)
        self.assertEqual(res.vdo_mm, 64.5)
        self.assertIn("занижена", res.diagnostic_comment.lower())

    def test_calculate_vdo_from_vdr_invalid_negative(self):
        """Защита от некорректного отрицательного VDR."""
        res = cgp.calculate_vdo_from_vdr(vdr_mm=-10.0)
        self.assertFalse(res.is_valid_range)
        self.assertIn("Ошибка", res.diagnostic_comment)

    def test_diagnose_speaking_space_normal(self):
        """Нормальное разговорное расстояние без соударения."""
        diag = cgp.diagnose_speaking_space_issue(observed_s_gap_mm=1.2, has_clattering=False, has_lisping=False)
        self.assertEqual(diag["status"], "PASS")

    def test_diagnose_speaking_space_clattering(self):
        """Стук зубов при звуке «С» -> ВНОЛ завышена."""
        diag = cgp.diagnose_speaking_space_issue(observed_s_gap_mm=0.2, has_clattering=True, has_lisping=False)
        self.assertEqual(diag["status"], "CORRECTION_NEEDED")
        self.assertIn("ЗАВЫШЕНА", diag["summary"])
        self.assertIn("Уменьшить", diag["recommendation"])

    def test_diagnose_speaking_space_lisping(self):
        """Шепелявость при звуке «С» -> занижение или переутолщение базиса."""
        diag = cgp.diagnose_speaking_space_issue(observed_s_gap_mm=2.5, has_clattering=False, has_lisping=True)
        self.assertEqual(diag["status"], "CORRECTION_NEEDED")
        self.assertIn("Шепелявость", diag["summary"])
        self.assertIn("небного", diag["recommendation"].lower())

    def test_pound_line_compliance(self):
        """Проверка выхода за линию Паунда."""
        ok_valid, msg_valid = cgp.check_pound_line_compliance(lingual_overhang_mm=0.0)
        self.assertTrue(ok_valid)
        self.assertIn("корректна", msg_valid)

        ok_invalid, msg_invalid = cgp.check_pound_line_compliance(lingual_overhang_mm=2.5)
        self.assertFalse(ok_invalid)
        self.assertIn("ОШИБКА", msg_invalid)
        self.assertIn("языка", msg_invalid.lower())


class TestChairsideCardGeneration(unittest.TestCase):
    """Тестирование карточки у кресла get_phonetic_test_chairside_card."""

    def test_chairside_card_formatting_and_content(self):
        """Карточка содержит все ключевые звуки, миллиметры и форматирование."""
        card = cgp.get_phonetic_test_chairside_card()
        self.assertIsInstance(card, str)
        self.assertGreater(len(card), 500)
        # Проверяем наличие всех 5 ключевых элементов
        self.assertIn("Звук «М»", card)
        self.assertIn("Звуки «Ф», «В»", card)
        self.assertIn("Звук «С»", card)
        self.assertIn("Звук «И»", card)
        self.assertIn("Линия Паунда", card)
        # Проверяем точные миллиметры
        self.assertIn("2–4 мм", card)
        self.assertIn("1.0–1.5 мм", card)
        self.assertIn("1.5–2.0 мм", card)
        # Проверяем клинические маркеры
        self.assertIn("Линия Клейна", card)
        self.assertIn("Clattering", card)
        self.assertIn("Lisping", card)
        self.assertIn("Freeway Space", card)
        self.assertIn("VDO = VDR - (2 ... 4 мм)", card)


class TestCognitiveCoreReExport(unittest.TestCase):
    """Тестирование реэкспорта через clinical_cognitive_core."""

    def test_cognitive_core_exports_phonetic_tools(self):
        """clinical_cognitive_core успешно экспортирует константы и функции фонетики."""
        self.assertTrue(hasattr(ccc, "FREEWAY_SPACE_MIN_MM"))
        self.assertEqual(ccc.FREEWAY_SPACE_MIN_MM, 2.0)
        self.assertTrue(hasattr(ccc, "CLOSEST_SPEAKING_SPACE_MIN_MM"))
        self.assertEqual(ccc.CLOSEST_SPEAKING_SPACE_MIN_MM, 1.0)
        self.assertTrue(hasattr(ccc, "get_phonetic_test_chairside_card"))
        card = ccc.get_phonetic_test_chairside_card()
        self.assertIn("ВНОЛ", card)


class TestChairsideToolsPhoneticsIntegration(unittest.IsolatedAsyncioTestCase):
    """Тестирование интеграции фонетических проб в Chairside Tools."""

    def test_hub_markup_has_phonetics_button(self):
        """Главное меню Chairside Hub содержит кнопку фонетических проб."""
        buttons = ct.build_chairside_hub_markup()
        flattened = [btn for row in buttons for btn in row]
        phonetics_btns = [btn for btn in flattened if getattr(btn, "data", b"") == b"chairside:phonetics"]
        self.assertEqual(len(phonetics_btns), 1, "Должна присутствовать ровно 1 кнопка chairside:phonetics")
        self.assertIn("Фонетические пробы", phonetics_btns[0].text)

    def test_is_chairside_callback_recognizes_phonetics(self):
        """is_chairside_callback распознает префикс chairside:phonetics."""
        self.assertTrue(ct.is_chairside_callback("chairside:phonetics"))

    def test_is_chairside_command_recognizes_phonetics(self):
        """is_chairside_command распознает команды /phonetics, /внол, /фонетика, /vdo."""
        self.assertTrue(ct.is_chairside_command("/phonetics"))
        self.assertTrue(ct.is_chairside_command("/фонетика"))
        self.assertTrue(ct.is_chairside_command("/внол"))
        self.assertTrue(ct.is_chairside_command("/vdo"))

    async def test_dispatch_chairside_callback_phonetics(self):
        """Диспетчеризация callback chairside:phonetics возвращает карточку без спиннера."""
        mock_client = AsyncMock()
        mock_event = AsyncMock()
        mock_event.answer = AsyncMock()
        mock_event.edit = AsyncMock()

        handled = await ct.dispatch_chairside_callback(mock_client, mock_event, "chairside:phonetics")
        self.assertTrue(handled)
        mock_event.answer.assert_called_once()
        mock_event.edit.assert_called_once()
        args, kwargs = mock_event.edit.call_args
        edited_text = args[0]
        self.assertIn("ВНОЛ", edited_text)
        self.assertIn("Звук «М»", edited_text)
        self.assertIn("Звук «С»", edited_text)

        # Проверяем иерархию кнопок «Назад» под карточкой
        buttons = kwargs.get("buttons") or args[1]
        btn_data = [btn.data.decode("utf-8") if isinstance(btn.data, bytes) else btn.data for row in buttons for btn in row]
        self.assertIn("chairside:hub", btn_data)
        self.assertIn("nav:main", btn_data)

    async def test_handle_if_chairside_command_phonetics(self):
        """Прямой вызов /phonetics и /внол через handle_if_chairside_command."""
        mock_client = AsyncMock()
        mock_client.send_message = AsyncMock()
        mock_event = AsyncMock()
        mock_event.chat_id = 12345
        mock_event.message = MagicMock(id=999)

        handled = await ct.handle_if_chairside_command(mock_client, mock_event, "/phonetics")
        self.assertTrue(handled)
        mock_client.send_message.assert_called_once()
        args, kwargs = mock_client.send_message.call_args
        sent_text = args[1]
        self.assertIn("ВНОЛ", sent_text)
        self.assertIn("Линия Паунда", sent_text)

        mock_client.reset_mock()
        handled_vnol = await ct.handle_if_chairside_command(mock_client, mock_event, "/внол")
        self.assertTrue(handled_vnol)
        mock_client.send_message.assert_called_once()


class TestGnathologyVocabularyIntegration(unittest.TestCase):
    """Тестирование интеграции гнатологических терминов в словарь триажа (dental_vocab)."""

    def test_vocab_recognizes_gnathology_queries(self):
        """Проверка распознавания гнатологических запросов триажем и демоном спасения."""
        import dental_vocab as dv
        queries = [
            "как определить внол?",
            "проба сильвермана при полном съемном протезе",
            "линия паунда на нижней челюсти",
            "определить высоту покоя",
            "расчет vdo и vdr",
            "freeway space в норме 2-4 мм",
            "фонетические пробы для определения прикуса",
            "постановка зубов на бюгеле",
            "линия клейна на нижней губе",
            "проба гизи для покоя",
        ]
        for q in queries:
            with self.subTest(query=q):
                self.assertTrue(dv.has_dental_term(q), f"Запрос '{q}' должен признаваться клиническим!")


if __name__ == "__main__":
    unittest.main()
