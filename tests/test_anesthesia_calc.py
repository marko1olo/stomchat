# -*- coding: utf-8 -*-
"""
tests/test_anesthesia_calc.py — Беспощадный Red Team тест клинических калькуляторов
анестезии, токсических пределов и протоколов экстренной реанимации у кресла врача.

Векторы проверки:
1. Клиническая математика и двойные потолки доз:
   - Артикаин 4%: 7 мг/кг (макс 500 мг), педиатрия 5 мг/кг, стоп-фактор <15 кг.
   - Мепивакаин 3%: 4.4 мг/кг (абсолютный максимум 400 мг).
   - Лидокаин 2%: с адреналином 7 мг/кг (макс 500 мг), без адреналина 4.4 мг/кг (макс 300 мг).
   - Бупивакаин 0.5%: 2 мг/кг (макс 90 мг), запрет детям до 12 лет.
2. Кардио-лимиты адреналина:
   - Здоровый: 0.20 мг (~11 карпул 1:100 000).
   - Кардиопатология: СТРОГО 0.04 мг (ровно 2 карпулы 1:100 000 или 4 карпулы 1:200 000).
3. Парсинг и краевые случаи:
   - Запятые («18,5 кг»), слова («двадцать пять кг»), нули, отрицательные числа, вес > 250 кг.
4. Протоколы SOS-реанимации:
   - Анафилактический шок (адреналин 0.3-0.5 мг в бедро), LAST (Интралипид 20%), коллапс, гипогликемия.
5. Интеграция с Chairside Tools и Telethon callbacks.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import anesthesia_calc as ac
import chairside_tools as ct


class TestClinicalAnesthesiaMath(unittest.TestCase):
    """1. Клиническая математика, токсические дозы и потолки действующих веществ."""

    def test_articaine_adult_normal(self):
        """Взрослый 70 кг: 70 * 7 = 490 мг (< 500 мг), 7 карпул по 68 мг."""
        res = ac.calculate_anesthesia_dose("артикаин 70 кг")
        self.assertTrue(res.is_valid)
        self.assertFalse(res.contraindicated)
        self.assertEqual(res.effective_max_mg, 490.0)
        self.assertFalse(res.hit_mg_ceiling)
        self.assertEqual(res.final_safe_carpules, 7)
        self.assertIn("до 7 карпул", res.html_report)
        self.assertIn("490 мг", res.html_report)

    def test_articaine_adult_ceiling_trigger(self):
        """Взрослый 85 кг: 85 * 7 = 595 мг -> срабатывает потолок 500 мг! Безопасно 7 карпул."""
        res = ac.calculate_anesthesia_dose("артикаин 85 кг")
        self.assertTrue(res.is_valid)
        self.assertEqual(res.effective_max_mg, 500.0)
        self.assertTrue(res.hit_mg_ceiling)
        self.assertEqual(res.final_safe_carpules, 7)
        self.assertIn("Сработал абсолютный потолок 500 мг", res.html_report)

    def test_articaine_extreme_weight(self):
        """Экстремальный вес 250 кг: потолок строго 500 мг, никаких 1750 мг!"""
        res = ac.calculate_anesthesia_dose("артикаин 250 кг")
        self.assertTrue(res.is_valid)
        self.assertEqual(res.effective_max_mg, 500.0)
        self.assertEqual(res.final_safe_carpules, 7)

    def test_articaine_pediatric_allowed(self):
        """Ребенок 20 кг: норма 5 мг/кг -> 100 мг, floor(100/68) = 1 карпула."""
        res = ac.calculate_anesthesia_dose("артикаин ребенку 20 кг")
        self.assertTrue(res.is_valid)
        self.assertTrue(res.is_pediatric)
        self.assertFalse(res.contraindicated)
        self.assertEqual(res.effective_max_mg, 100.0)
        self.assertEqual(res.final_safe_carpules, 1)
        self.assertIn("до 1 карпулы", res.html_report)

    def test_articaine_pediatric_contraindicated_under_15kg(self):
        """Стоп-фактор: артикаин детям до 4 лет (< 15 кг) КАТЕГОРИЧЕСКИ ПРОТИВОПОКАЗАН."""
        res = ac.calculate_anesthesia_dose("артикаин ребенку 12 кг")
        self.assertTrue(res.is_valid)
        self.assertTrue(res.contraindicated)
        self.assertIn("ПРОТИВОПОКАЗАН", res.html_report)
        self.assertIn("Мепивакаин 3%", res.html_report)

    def test_mepivacaine_adult_normal(self):
        """Мепивакаин взрослый 70 кг: 70 * 4.4 = 308 мг, 308/54 = 5.7 -> 5 карпул."""
        res = ac.calculate_anesthesia_dose("мепивакаин 70 кг")
        self.assertTrue(res.is_valid)
        self.assertEqual(res.effective_max_mg, 308.0)
        self.assertEqual(res.final_safe_carpules, 5)
        self.assertIn("до 5 карпул", res.html_report)

    def test_mepivacaine_absolute_ceiling_400mg(self):
        """Мепивакаин взрослый 100 кг: 100 * 4.4 = 440 мг -> потолок строго 400 мг (не 300!) -> 7 карпул."""
        res = ac.calculate_anesthesia_dose("мепивакаин 100 кг")
        self.assertTrue(res.is_valid)
        self.assertEqual(res.effective_max_mg, 400.0)
        self.assertTrue(res.hit_mg_ceiling)
        self.assertEqual(res.final_safe_carpules, 7)
        self.assertIn("Сработал абсолютный потолок 400 мг", res.html_report)

    def test_mepivacaine_pediatric_early_age(self):
        """Мепивакаин ребенок 12 кг: разрешен, доза 12 * 4.4 = 52.8 мг -> 0 целых карпул (менее 1 карпулы)."""
        res = ac.calculate_anesthesia_dose("скандонест ребенку 12 кг")
        self.assertTrue(res.is_valid)
        self.assertFalse(res.contraindicated)
        self.assertAlmostEqual(res.effective_max_mg, 52.8, places=1)
        self.assertEqual(res.final_safe_carpules, 0)

    def test_lidocaine_plain_vs_with_epi(self):
        """Лидокаин чистый: макс 300 мг (4.4 мг/кг). Лидокаин с адреналином: макс 500 мг (7 мг/кг)."""
        res_plain = ac.calculate_anesthesia_dose("чистый лидокаин без адреналина 80 кг")
        self.assertEqual(res_plain.drug.key, "lidocaine_2_plain")
        self.assertEqual(res_plain.effective_max_mg, 300.0)

        res_epi = ac.calculate_anesthesia_dose("лидокаин 2% с адреналином 80 кг")
        self.assertEqual(res_epi.drug.key, "lidocaine_2_epi")
        self.assertEqual(res_epi.effective_max_mg, 500.0)

    def test_bupivacaine_contraindicated_in_children(self):
        """Бупивакаин строго противопоказан детям до 12 лет из-за высокой кардиотоксичности."""
        res = ac.calculate_anesthesia_dose("бупивакаин ребенку 25 кг")
        self.assertTrue(res.is_valid)
        self.assertTrue(res.contraindicated)
        self.assertIn("БУПИВАКАИН (МАРКАИН) ДЕТЯМ ЗАПРЕЩЕН", res.html_report)


class TestEpinephrineCardiacLimits(unittest.TestCase):
    """2. Контроль дозы вазоконстриктора (эпинефрина/адреналина): 0.2 мг vs 0.04 мг."""

    def test_healthy_adult_epinephrine_limit(self):
        """Здоровый взрослый: лимит адреналина 0.2 мг (200 мкг). Артикаин 1:100 000 допускает 7 карпул."""
        res = ac.calculate_anesthesia_dose("артикаин 70 кг")
        self.assertFalse(res.is_cardiac)
        self.assertEqual(res.epinephrine_limit_mg, 0.20)
        self.assertFalse(res.hit_epinephrine_ceiling)
        self.assertEqual(res.final_safe_carpules, 7)

    def test_cardiac_patient_strict_two_carpules_limit_100(self):
        """Кардиопатология: лимит эпинефрина 0.04 мг -> 1:100 000 (0.017 мг/карп) -> СТРОГО 2 КАРПУЛЫ!"""
        res = ac.calculate_anesthesia_dose("артикаин кардиопациент с аритмией 70 кг")
        self.assertTrue(res.is_cardiac)
        self.assertEqual(res.epinephrine_limit_mg, 0.04)
        self.assertTrue(res.hit_epinephrine_ceiling)
        self.assertEqual(res.final_safe_carpules, 2)
        self.assertIn("СТРОГО НЕ БОЛЕЕ 2 КАРПУЛ", res.html_report)
        self.assertIn("КАРДИОПАТОЛОГИЯ", res.html_report)

    def test_cardiac_patient_four_carpules_limit_200(self):
        """Кардиопатология: при 1:200 000 (0.0085 мг/карп) -> СТРОГО 4 КАРПУЛЫ!"""
        res = ac.calculate_anesthesia_dose("артикаин 1:200000 гипертоник давление 70 кг")
        self.assertTrue(res.is_cardiac)
        self.assertEqual(res.epinephrine_limit_mg, 0.04)
        self.assertTrue(res.hit_epinephrine_ceiling)
        self.assertEqual(res.final_safe_carpules, 4)
        self.assertIn("СТРОГО НЕ БОЛЕЕ 4 КАРПУЛ", res.html_report)


class TestInputParsingRobustness(unittest.TestCase):
    """3. Парсинг входных данных, опечаток, запятых и числительных."""

    def test_comma_in_weight(self):
        """Запятая в весе: «18,5 кг», «72,4»."""
        w1 = ac.parse_weight_from_text("артикаин 18,5 кг")
        self.assertEqual(w1, 18.5)
        w2 = ac.parse_weight_from_text("пациент весом 72,4 килограмма")
        self.assertEqual(w2, 72.4)

    def test_words_as_numbers(self):
        """Числительные словами: «двадцать кг», «семьдесят пять килограмм»."""
        w1 = ac.parse_weight_from_text("скандонест двадцать кг")
        self.assertEqual(w1, 20.0)
        w2 = ac.parse_weight_from_text("ультракаин семьдесят пять килограмм")
        self.assertEqual(w2, 75.0)
        w3 = ac.parse_weight_from_text("дозировка на сто двадцать кг")
        self.assertEqual(w3, 120.0)

    def test_zero_and_negative_weight_rejected(self):
        """Нулевой и отрицательный вес бракуются без исключений и деления на 0."""
        r_zero = ac.calculate_anesthesia_dose("артикаин 0 кг")
        self.assertFalse(r_zero.is_valid)
        self.assertIn("строго положительным", r_zero.error_message)

        r_neg = ac.calculate_anesthesia_dose("артикаин -15 кг")
        self.assertFalse(r_neg.is_valid)

    def test_below_minimum_weight_rejected(self):
        """Вес < 3 кг не разрешен в амбулаторной стоматологии."""
        r_tiny = ac.calculate_anesthesia_dose("артикаин 2 кг")
        self.assertFalse(r_tiny.is_valid)
        self.assertIn("недопустима для амбулаторной", r_tiny.error_message)

    def test_above_maximum_weight_rejected(self):
        """Вес > 300 кг бракуется как нереалистичный."""
        r_huge = ac.calculate_anesthesia_dose("артикаин 350 кг")
        self.assertFalse(r_huge.is_valid)
        self.assertIn("нереалистичный вес", r_huge.error_message)

    def test_blood_pressure_not_mistaken_for_weight(self):
        """АД (140/90) не должно извлекаться как вес 140 кг, но должно включать кардиостатус."""
        p = ac.parse_clinical_query_params("пациентка с давлением 140/90 ультракаин")
        self.assertIsNone(p["weight"])
        self.assertTrue(p["is_cardiac"])

    def test_tooth_number_not_mistaken_for_weight(self):
        """Номер зуба (36 зуба, 46 зуб) не должен извлекаться как масса тела."""
        p1 = ac.parse_clinical_query_params("эндодонтия 36 зуба 3 канала ультракаин")
        self.assertIsNone(p1["weight"])
        p2 = ac.parse_clinical_query_params("удаление 46 зуба артикаин")
        self.assertIsNone(p2["weight"])

    def test_pediatric_age_nelson_formula(self):
        """При указании возраста (7 лет) без веса применяется формула Нельсона (7*2+8 = 22 кг)."""
        res = ac.calculate_anesthesia_dose("девочка 7 лет ультракаин")
        self.assertTrue(res.is_valid)
        self.assertEqual(res.weight_kg, 22.0)
        self.assertTrue(res.is_weight_estimated)
        self.assertEqual(res.estimated_age_years, 7.0)
        self.assertIn("формуле Нельсона", res.html_report)

    def test_pregnancy_guidance_alert(self):
        """Беременность детектируется и выводит протокол безопасности FDA B/C."""
        res = ac.calculate_anesthesia_dose("беременная 2 триместр вес 65 кг артикаин")
        self.assertTrue(res.is_valid)
        self.assertTrue(res.is_pregnant)
        self.assertIn("БЕРЕМЕННОСТЬ", res.html_report)
        self.assertIn("FDA B", res.html_report)


class TestChairsideEmergencyProtocols(unittest.TestCase):
    """4. Протоколы неотложной помощи и реанимации (SOS Resuscitation)."""

    def test_anaphylaxis_protocol_ebm(self):
        """Анафилаксия: адреналин 0.3-0.5 мг в бедро, положение Тренделенбурга, кислород."""
        proto = ac.get_emergency_protocol("anaphylaxis")
        self.assertIsNotNone(proto)
        self.assertIn("0.3 – 0.5 мл", proto.card_text)
        self.assertIn("среднюю треть переднебоковой поверхности бедра", proto.card_text)
        self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО", proto.card_text)
        self.assertIn("Сажать пациента", proto.card_text)

    def test_last_protocol_intralipid_rescue(self):
        """LAST: Интралипид 20% болюс 1.5 мл/кг, инфузия 0.25 мл/кг/мин, запрет лидокаина."""
        proto = ac.get_emergency_protocol("last")
        self.assertIsNotNone(proto)
        self.assertIn("1.5 мл/кг", proto.card_text)
        self.assertIn("0.25 мл/кг/мин", proto.card_text)
        self.assertIn("Лидокаин", proto.card_text)
        self.assertIn("СТРОГО ЗАПРЕЩЕНЫ", proto.card_text)

    def test_syncope_collapse_protocol(self):
        """Обморок: Тренделенбург, нашатырь на 5 см, кислород."""
        proto = ac.get_emergency_protocol("syncope_collapse")
        self.assertIsNotNone(proto)
        self.assertIn("Тренделенбурга", proto.card_text)
        self.assertIn("нашатырного спирта", proto.card_text)

    def test_hypoglycemia_protocol(self):
        """Гипогликемия: в сознании 15-20 г углеводов, без сознания 40% глюкоза 40-60 мл в/в."""
        proto = ac.get_emergency_protocol("hypoglycemia")
        self.assertIsNotNone(proto)
        self.assertIn("15–20 г быстрых углеводов", proto.card_text)
        self.assertIn("40% глюкозу 40–60 мл", proto.card_text)
        self.assertIn("В РОТ НИЧЕГО НЕ ЛИТЬ", proto.card_text)

    def test_hypertensive_crisis_protocol(self):
        """Гипертонический криз: полусидя, моксонидин 0.2 мг / каптоприл 25 мг под язык."""
        proto = ac.get_emergency_protocol("hypertensive_crisis")
        self.assertIsNotNone(proto)
        self.assertIn("Полусидячее положение", proto.card_text)
        self.assertIn("Моксонидин 0.2 мг", proto.card_text)

    def test_needle_breakage_protocol(self):
        """Перелом иглы: фиксация открытого рта, запрет слепых разрезов, 3D КЛКТ и ЧЛХ."""
        proto = ac.get_emergency_protocol("needle")
        self.assertIsNotNone(proto)
        self.assertIn("НЕ ДАВАТЬ ЗАКРЫВАТЬ РОТ", proto.card_text)
        self.assertIn("3D КЛКТ", proto.card_text)
        self.assertIn("ЧЛХ", proto.card_text)

    def test_adrenaline_vs_allergy_differential(self):
        """Дифдиагностика адреналиновой реакции: АД повышено, дыхание свободное, адреналин НЕ колоть."""
        proto = ac.get_emergency_protocol("adrenaline")
        self.assertIsNotNone(proto)
        self.assertIn("Адреналиновая реакция vs Анафилаксия", proto.card_text)
        self.assertIn("Адреналин НЕ КОЛОТЬ", proto.card_text)

    def test_pregnancy_emergency_protocol(self):
        """Протокол при беременности: Лидокаин FDA B, поворот на левый бок, запрет НПВП в III триместре."""
        proto = ac.get_emergency_protocol("pregnancy")
        self.assertIsNotNone(proto)
        self.assertIn("FDA B", proto.card_text)
        self.assertIn("левый бок", proto.card_text)
        self.assertIn("Мепивакаин (Скандонест)", proto.card_text)
        self.assertIn("НЕ РЕКОМЕНДОВАН", proto.card_text)


class TestChairsideToolsIntegration(unittest.TestCase):
    """5. Интеграция с Chairside Tools, разметками и командами бота."""

    def test_resuscitation_markup_generation(self):
        """Разметка кнопок реанимации содержит все ключевые протоколы."""
        markup = ct.build_resuscitation_sos_markup()
        self.assertGreaterEqual(len(markup), 4)
        all_data = [btn.data.decode("utf-8") if isinstance(btn.data, bytes) else btn.data for row in markup for btn in row]
        self.assertIn("sos:anaphylaxis", all_data)
        self.assertIn("sos:last", all_data)
        self.assertIn("sos:collapse", all_data)
        self.assertIn("sos:hypoglycemia", all_data)
        self.assertIn("sos:hypertension", all_data)

    def test_quick_card_function(self):
        """Функция calculate_anesthesia_carpules использует новый сводный расчет."""
        card = ct.calculate_anesthesia_carpules(70.0)
        self.assertIn("4% Артикаин", card)
        self.assertIn("3% Мепивакаин", card)
        self.assertIn("2% Лидокаин", card)
        self.assertIn("0.04 мг", card)

    def test_dispatch_resuscitation_callbacks(self):
        """Диспетчер колбэков перехватывает и отображает протоколы реанимации."""
        async def _test():
            bot = MagicMock()
            event = MagicMock()
            event.chat_id = -10012345
            event.edit = AsyncMock()
            event.answer = AsyncMock()

            # sos:anaphylaxis
            ok_ana = await ct.dispatch_chairside_callback(bot, event, "sos:anaphylaxis")
            self.assertTrue(ok_ana)
            self.assertTrue(event.edit.called)

            # sos:last
            event.edit.reset_mock()
            ok_last = await ct.dispatch_chairside_callback(bot, event, "sos:last")
            self.assertTrue(ok_last)
            self.assertTrue(event.edit.called)

            # sos:resuscitation_menu
            event.edit.reset_mock()
            ok_menu = await ct.dispatch_chairside_callback(bot, event, "sos:resuscitation_menu")
            self.assertTrue(ok_menu)
            self.assertTrue(event.edit.called)

            # sos:needle
            event.edit.reset_mock()
            ok_needle = await ct.dispatch_chairside_callback(bot, event, "sos:needle")
            self.assertTrue(ok_needle)
            self.assertTrue(event.edit.called)

            # sos:adrenaline
            event.edit.reset_mock()
            ok_adr = await ct.dispatch_chairside_callback(bot, event, "sos:adrenaline")
            self.assertTrue(ok_adr)
            self.assertTrue(event.edit.called)

            # sos:pregnancy
            event.edit.reset_mock()
            ok_preg = await ct.dispatch_chairside_callback(bot, event, "sos:pregnancy")
            self.assertTrue(ok_preg)
            self.assertTrue(event.edit.called)

        asyncio.run(_test())

    def test_cumulative_remaining_dose_calculation(self):
        """Кумулятивный расчет при комбинировании: 2 карпулы артикаина оставляют ~222 мг мепивакаина (4 карп)."""
        res = ac.calculate_cumulative_remaining_dose(70.0, {"articaine_100": 2.0}, "mepivacaine_3")
        self.assertIn("Кумулятивный расчет", res)
        self.assertIn("до 4 карпул", res)


if __name__ == "__main__":
    unittest.main()
