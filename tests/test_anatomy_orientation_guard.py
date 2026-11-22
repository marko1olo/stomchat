# -*- coding: utf-8 -*-
"""
tests/test_anatomy_orientation_guard.py — Тестовый набор для clinical_anatomy_guard.py.

Проверяет:
1. Выявление запрещенного «небного корня / канала / стенки» у нижних моляров (36, 37, 46, 47).
2. Выявление запрещенного «язычного корня / канала / стенки» у верхних моляров (16, 26, 17, 27).
3. Валидацию корректных корней зуба 36 (медиальный MB/ML, дистальный DB/DL, Radix Entomolaris/Paramolaris).
4. Сохранение и одобрение правильных небных корней и каналов у верхних моляров (16, 26).
5. Топографическую проверку передней группы и премоляров (31, 11, 14, 34).
6. Корректную обработку контекстных отрицаний («нет небного корня», «в отличие от небного корня»).
7. Работу генератора системных промпт-директив get_anatomy_guard_prompt().
8. Детерминированную блокировку в валидаторе check_response_quality() из assistant.py.
"""

import os
import sys
import unittest
import asyncio

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import clinical_anatomy_guard as cag
import clinical_cognitive_core as ccc


class TestAnatomyOrientationGuard(unittest.TestCase):
    """Тесты анатомической ориентации корней и поверхностей по системе FDI."""

    # ==========================================================================
    # 1. ТЕСТЫ НА НИЖНИЕ МОЛЯРЫ (36, 37, 46, 47) — ЗАПРЕТ НЕБНОГО КОРНЯ
    # ==========================================================================

    def test_mandibular_36_detects_palatal_root(self):
        """Зуб 36 (нижний левый 1-й моляр): обнаружение недопустимого 'небного корня'."""
        text = "В зубе 36 обнаружена перфорация дна полости зуба в области небного корня, серозный экссудат."
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=36)
        self.assertFalse(is_valid, "Зуб 36 не должен пропускать 'небный корень'")
        self.assertGreater(len(violations), 0)
        self.assertTrue(any("36" in v and "небн" in v.lower() for v in violations))

    def test_mandibular_37_detects_palatal_root(self):
        """Зуб 37 (нижний левый 2-й моляр): извлечение из текста и блокировка 'небного корня'."""
        text = "На снимке 37 зуба визуализируется резорбция небного корня с выходом силера."
        is_valid, violations = cag.verify_and_guard_anatomy(text)
        self.assertFalse(is_valid, "Зуб 37 должен быть извлечен из текста и забракован")
        self.assertTrue(any("37" in v for v in violations))

    def test_mandibular_46_detects_palatal_root(self):
        """Зуб 46 (нижний правый 1-й моляр): блокировка 'палатинального корня' и палатинального канала."""
        text = "Зуб 46: палатинальный корень обтурирован гуттаперчей, палатинальный канал проходим."
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=46)
        self.assertFalse(is_valid, "Зуб 46 не должен пропускать 'палатинальный корень/канал'")
        self.assertTrue(any("палатинальн" in v.lower() or "небн" in v.lower() for v in violations))

    def test_mandibular_47_detects_palatal_canal_and_surface(self):
        """Зуб 47 (нижний правый 2-й моляр): блокировка 'небного канала' и 'небной стенки'."""
        text = "Зуб 4.7: небный канал склерозирован, перфорация небной стенки."
        is_valid, violations = cag.verify_and_guard_anatomy(text)
        self.assertFalse(is_valid, "Зуб 47 не должен допускать небный канал и стенку")
        self.assertGreaterEqual(len(violations), 1)

    def test_mandibular_multilingual_violations(self):
        """Тест украинской и английской терминологии на нижних молярах."""
        # Украинский язык: піднебінний корінь на 36
        uk_text = "Зуб 36: виявлено деструкцію кістки навколо піднебінного кореня."
        ok_uk, viols_uk = cag.verify_and_guard_anatomy(uk_text, tooth_fdi=36)
        self.assertFalse(ok_uk, "Должен распознавать украинский 'піднебінний корінь'")

        # Английский язык: palatal root on tooth 36
        en_text = "Tooth 36 presents with periapical pathology at the palatal root."
        ok_en, viols_en = cag.verify_and_guard_anatomy(en_text, tooth_fdi=36)
        self.assertFalse(ok_en, "Должен распознавать английский 'palatal root'")

    # ==========================================================================
    # 2. ТЕСТЫ НА ВЕРХНИЕ МОЛЯРЫ (16, 26) — ЗАПРЕТ ЯЗЫЧНОГО КОРНЯ
    # ==========================================================================

    def test_maxillary_16_detects_lingual_root(self):
        """Зуб 16 (верхний правый 1-й моляр): блокировка 'язычного корня'."""
        text = "Зуб 16: язычный корень имеет 1 канал, щечные корни без очагов."
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=16)
        self.assertFalse(is_valid, "Зуб 16 не должен иметь 'язычный корень'")
        self.assertTrue(any("16" in v and "язычн" in v.lower() for v in violations))

    def test_maxillary_26_detects_lingual_root_and_surface(self):
        """Зуб 26 (верхний левый 1-й моляр): блокировка 'язычного корня' и 'язычной стенки'."""
        text = "Зуб 26: язычный корень запломбирован, скол язычной стенки ниже десны."
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=26)
        self.assertFalse(is_valid, "Зуб 26 не должен иметь 'язычный корень/стенку'")
        self.assertGreaterEqual(len(violations), 1)

    def test_maxillary_multilingual_violations(self):
        """Тест украинской и английской терминологии на верхних молярах."""
        # Украинский: язиковий корінь на 16
        uk_text = "Зуб 16: язиковий корінь запломбований гутаперчею."
        ok_uk, viols_uk = cag.verify_and_guard_anatomy(uk_text, tooth_fdi=16)
        self.assertFalse(ok_uk, "Должен распознавать украинский 'язиковий корінь'")

        # Английский: lingual root on tooth 26
        en_text = "Tooth 26: lingual root canal is overfilled."
        ok_en, viols_en = cag.verify_and_guard_anatomy(en_text, tooth_fdi=26)
        self.assertFalse(ok_en, "Должен распознавать английский 'lingual root'")

    # ==========================================================================
    # 3. ТЕСТЫ НА КОРРЕКТНЫЕ КОРНИ ЗУБА 36 (M, D, RADIX ENTOMOLARIS)
    # ==========================================================================

    def test_mandibular_36_correct_roots_pass(self):
        """Зуб 36: корректные корни (медиальный MB/ML, дистальный DB/DL) обязаны проходить."""
        text = (
            "Клинический разбор зуба 36: медиальный корень содержит каналы MB и ML, "
            "пройдены до верхушки на длину 21 мм. Дистальный корень имеет широкий овальный канал D. "
            "Ирригация 3% NaOCl с активацией, временное пломбирование гидроксидом кальция."
        )
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=36)
        self.assertTrue(is_valid, f"Корректная анатомия зуба 36 должна одобряться: {violations}")
        self.assertEqual(len(violations), 0)

    def test_mandibular_36_radix_entomolaris_passes(self):
        """Зуб 36: Radix Entomolaris (дистолингвальный корень) обязан одобряться."""
        text = (
            "На визиограмме зуба 36 визуализируется трехкорневая анатомия: "
            "медиальный корень (MB, ML), дистальный корень и дополнительный дистолингвальный корень "
            "(Radix Entomolaris) с выраженной апикальной кривизной."
        )
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=36)
        self.assertTrue(is_valid, f"Radix Entomolaris зуба 36 должен быть одобрен: {violations}")
        self.assertEqual(len(violations), 0)

    # ==========================================================================
    # 4. ТЕСТЫ НА СОХРАНЕНИЕ ПРАВИЛЬНЫХ НЕБНЫХ КОРНЕЙ У ВЕРХНИХ МОЛЯРОВ (16, 26)
    # ==========================================================================

    def test_maxillary_16_correct_palatal_root_passes(self):
        """Зуб 16: небный корень (P) является анатомической нормой и ОБЯЗАН одобряться."""
        text = (
            "Зуб 16: мезиально-щечный корень (MB1, MB2 найден с помощью ультразвука), "
            "дистально-щечный корень (DB) и небный корень (P). Небный канал широкий, "
            "обработан до размера 35.04. Патологических изменений нет."
        )
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=16)
        self.assertTrue(is_valid, f"Небный корень зуба 16 обязан одобряться: {violations}")
        self.assertEqual(len(violations), 0)

    def test_maxillary_26_correct_palatal_root_passes(self):
        """Зуб 26: небный корень и небный канал одобряются."""
        text = (
            "Зуб 26: обнаружен хронический периодонтит в области небного корня. "
            "Небный корень обработан и запломбирован биокерамическим силером с гуттаперчей."
        )
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=26)
        self.assertTrue(is_valid, f"Небный корень зуба 26 обязан одобряться: {violations}")
        self.assertEqual(len(violations), 0)

    # ==========================================================================
    # 5. ТЕСТЫ НА ТОПОГРАФИЮ ПОВЕРХНОСТЕЙ И ДРУГИХ ГРУПП ЗУБОВ
    # ==========================================================================

    def test_anterior_lower_incisor_surface(self):
        """Зуб 31 (нижний резец): поверхность язычная, небная — ошибка."""
        text = "Зуб 31: кариозная полость на небной поверхности."
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=31)
        self.assertFalse(is_valid, "У зуба 31 поверхность не может быть небной")

    def test_anterior_upper_incisor_surface(self):
        """Зуб 11 (верхний резец): поверхность небная, язычная — ошибка."""
        text = "Зуб 11: пломба на язычной поверхности с нарушением прилегания."
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=11)
        self.assertFalse(is_valid, "У зуба 11 поверхность не может быть язычной")

    def test_premolars_root_and_surface(self):
        """Премоляры: 14 (верхний) имеет небный корень, 34 (нижний) не имеет небного."""
        # 14 с небным корнем — норма
        ok_14, _ = cag.verify_and_guard_anatomy("Зуб 14: два корня — щечный и небный.", tooth_fdi=14)
        self.assertTrue(ok_14, "У зуба 14 допустимы щечный и небный корни")

        # 34 с небным корнем — ошибка
        ok_34, _ = cag.verify_and_guard_anatomy("Зуб 34: обнаружен небный корень.", tooth_fdi=34)
        self.assertFalse(ok_34, "У зуба 34 не может быть небного корня")

    # ==========================================================================
    # 6. ТЕСТЫ НА КОНТЕКСТНЫЕ ОТРИЦАНИЯ И ОПРОВЕРЖЕНИЯ
    # ==========================================================================

    def test_negation_lower_molar_has_no_palatal_root(self):
        """Фраза 'у зуба 36 нет небного корня' не должна штрафоваться."""
        text = "У зуба 36 нет небного корня, так как у нижних моляров корни медиальный и дистальный."
        is_valid, violations = cag.verify_and_guard_anatomy(text, tooth_fdi=36)
        self.assertTrue(is_valid, f"Опровержение не должно штрафоваться: {violations}")

    def test_negation_in_contrast_passes(self):
        """Фраза 'в отличие от небного корня' не должна штрафоваться."""
        text = "В отличие от небного корня зуба 16, зуб 46 имеет медиальный и дистальный корни."
        is_valid, violations = cag.verify_and_guard_anatomy(text)
        self.assertTrue(is_valid, f"Контраст не должен штрафоваться: {violations}")

    # ==========================================================================
    # 7. ТЕСТЫ НА ГЕНЕРАТОР ПРОМПТОВ get_anatomy_guard_prompt()
    # ==========================================================================

    def test_prompt_generator_mandible(self):
        """Генератор промпта для зуба 36 формирует директиву для нижней челюсти."""
        prompt = cag.get_anatomy_guard_prompt("36")
        self.assertIn("MANDIBLE", prompt)
        self.assertIn("36", prompt)
        self.assertIn("небный корень", prompt)

    def test_prompt_generator_maxilla(self):
        """Генератор промпта для зуба 16 формирует директиву для верхней челюсти."""
        prompt = cag.get_anatomy_guard_prompt("16")
        self.assertIn("MAXILLA", prompt)
        self.assertIn("16", prompt)
        self.assertIn("язычный корень", prompt)

    def test_prompt_generator_general(self):
        """Общий генератор промпта содержит обе челюсти."""
        prompt = cag.get_anatomy_guard_prompt()
        self.assertIn("НИЖНЯЯ ЧЕЛЮСТЬ", prompt)
        self.assertIn("ВЕРХНЯЯ ЧЕЛЮСТЬ", prompt)

    # ==========================================================================
    # 8. ИНТЕГРАЦИОННЫЙ ТЕСТ С check_response_quality В assistant.py
    # ==========================================================================

    def test_assistant_check_response_quality_blocks_hallucination(self):
        """Интеграционный тест: check_response_quality блокирует черновик с небным корнем у 36."""
        import assistant

        async def run_check():
            draft = "Разбор снимка зуба 36: видна перфорация в области небного корня."
            ok, reason = await assistant.check_response_quality(
                context_msgs=["Доктор прислал снимок зуба 36"],
                draft_reply=draft,
                invited=True
            )
            return ok, reason

        ok, reason = asyncio.run(run_check())
        self.assertFalse(ok, "check_response_quality обязан заблокировать черновик")
        self.assertIn("anatomy_violation", reason)
        self.assertIn("36", reason)

    def test_assistant_check_response_quality_allows_valid_draft(self):
        """Интеграционный тест: черновик с правильной анатомией 36 не блокируется анатомическим чекером."""
        # Проверяем, что черновик с валидной анатомией зуба 36 успешно проходит анатомическую часть
        draft = (
            "На снимке 3.6: медиальный корень (MB, ML) и дистальный корень (D). "
            "Ирригация 3% NaOCl, временная повязка с гидроксидом кальция."
        )
        is_valid, violations = cag.verify_and_guard_anatomy(
            draft,
            context="Доктор прислал снимок зуба 36"
        )
        self.assertTrue(is_valid, f"Анатомический фильтр должен пропустить валидный черновик: {violations}")


if __name__ == "__main__":
    unittest.main()
