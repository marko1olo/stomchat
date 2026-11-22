"""
tests/test_insurance_clinical_justification.py — Red Team тест-сьют для аудита
протокола страхового согласования (ДМС) и формы № 043/у (Domain 3).

Проверяет:
1. Экспертную генерацию обоснования для зуба 3.7 с облитерацией каналов и периодонтитом PAI 4
2. Обязательное присутствие ссылок на клинические протоколы СтАР / ESE / МКБ-10 / Приказ 834н / ФЗ-323
3. Аргументацию необходимости операционного микроскопа (Zeiss/Leica) и КЛКТ (Small FOV)
4. Обоснование ультразвуковых микронасадок (Start-X), 17% EDTA, биокерамики и гидроксида кальция
5. Реэкспорт и целостность в clinical_cognitive_core.py
6. Интеграцию в шаблоны 043/у и суперсилу /consultation в assistant.py

Запуск: python -m pytest tests/test_insurance_clinical_justification.py -v
"""

import os
import re
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import clinical_insurance_protocol as cip
import clinical_cognitive_core as ccc
import assistant
from telethon.extensions import html as th_html


class TestInsuranceClinicalJustification(unittest.TestCase):

    def test_tooth_37_obliteration_pai4_generation(self):
        """
        Тест 1: Генерация экспертного согласования для зуба 3.7
        с облитерацией каналов, хроническим апикальным периодонтитом К04.5 и PAI 4.
        """
        tooth = "3.7"
        diagnosis = "K04.5"
        findings = "Жалобы на дискомфорт при накусывании. Облитерация пульповой камеры и устьев каналов, периапикальный очаг деструкции кости PAI 4."
        requested_procedures = ["cbct", "microscope", "ultrasound_chelator", "bioceramics_calcium"]

        report = cip.format_insurance_justification_report(
            tooth_fdi=tooth,
            diagnosis_icd=diagnosis,
            clinical_findings=findings,
            requested_procedures=requested_procedures
        )

        # 1. Локализация по FDI
        self.assertIn("3.7", report)
        self.assertIn("Нижняя челюсть", report)
        self.assertIn("второй моляр", report)

        # 2. Диагноз МКБ-10
        self.assertIn("K04.5", report)
        self.assertIn("Хронический апикальный периодонтит", report)

        # 3. Периапикальный индекс PAI 4
        self.assertIn("PAI 4", report)
        self.assertIn("Orstavik", report)

        # 4. Обоснование микроскопа
        self.assertIn("операционного микроскопа", report.lower())
        self.assertIn("carl zeiss", report.lower())
        self.assertIn("перицервикального дентина", report.lower())
        self.assertIn("перфораци", report.lower())

        # 5. Обоснование КЛКТ
        self.assertIn("клкт", report.lower())
        self.assertIn("деструкция", report.lower())
        self.assertIn("fov", report.lower())

        # 6. Обоснование ультразвука и хелатора
        self.assertIn("start-x", report.lower())
        self.assertIn("edta", report.lower())

        # 7. Обоснование биокерамики и кальция
        self.assertIn("гидроксид", report.lower())
        self.assertIn("биокерамик", report.lower())

        # 8. Финансово-экономическое и клиническое заключение
        self.assertIn("зубосохраняющ", report.lower())
        self.assertIn("имплантаци", report.lower())

    def test_mandatory_citations_star_ese_icd10(self):
        """
        Тест 2: Обязательное присутствие юридических и клинических ссылок
        на клинические протоколы СтАР, ESE, AAE/AAOMR, МКБ-10, Приказ 834н и ФЗ-323.
        """
        report = cip.format_insurance_justification_report(
            tooth_fdi=37,
            diagnosis_icd="K04.5",
            clinical_findings="Облитерация каналов, PAI 4",
            requested_procedures=["cbct", "microscope", "ultrasound", "bioceramics"]
        )

        # СтАР (Стоматологическая Ассоциация России) и Минздрав РФ
        self.assertIn("СтАР", report)
        self.assertIn("Минздрав", report)

        # ESE (European Society of Endodontology)
        self.assertIn("European Society of Endodontology", report)
        self.assertIn("ESE", report)

        # Федеральный закон № 323-ФЗ (ст. 37)
        self.assertIn("323-ФЗ", report)
        self.assertIn("ст. 37", report)

        # Приказ Минздрава РФ № 834н (Форма 043/у)
        self.assertIn("834н", report)
        self.assertIn("043/у", report)

        # МКБ-10
        self.assertIn("МКБ-10", report)

    def test_microscope_and_cbct_clinical_arguments(self):
        """
        Тест 3: Глубокая аргументация необходимости микроскопа и КЛКТ
        с рисками ятрогений при отказе страховой компании.
        """
        cbct_just = cip.get_procedure_justification("cbct", 37, "K04.5")
        self.assertEqual(cbct_just["procedure_category"], "cbct")
        self.assertIn("ALADAIP", cbct_just["ebm_justification"])
        self.assertIn("перфораци", cbct_just["rejection_risk"].lower())
        self.assertIn("нижнечелюстн", cbct_just["rejection_risk"].lower())

        micro_just = cip.get_procedure_justification("микроскоп Carl Zeiss", 37, "K04.5")
        self.assertEqual(micro_just["procedure_category"], "microscope")
        self.assertIn("PCD", micro_just["ebm_justification"])
        self.assertIn("перицервикального дентина", micro_just["ebm_justification"].lower())
        self.assertIn("ложного хода", micro_just["rejection_risk"].lower())

    def test_ultrasound_edta_bioceramics_arguments(self):
        """
        Тест 4: Аргументация ультразвуковых насадок Start-X, 17% EDTA,
        гидроксида кальция и биокерамических силеров.
        """
        us_just = cip.get_procedure_justification("start-x ультразвук", 37, "K04.5")
        self.assertEqual(us_just["procedure_category"], "ultrasound_chelator")
        self.assertIn("17%", us_just["ebm_justification"])
        self.assertIn("EDTA", us_just["ebm_justification"])
        self.assertIn("Start-X", us_just["ebm_justification"])

        bio_just = cip.get_procedure_justification("биокерамический силер bioroot", 37, "K04.5")
        self.assertEqual(bio_just["procedure_category"], "bioceramics_calcium")
        self.assertIn("Ca(OH)2", bio_just["ebm_justification"])
        self.assertIn("гидроксиапатита", bio_just["ebm_justification"].lower())
        self.assertIn("силикатов кальция", bio_just["ebm_justification"].lower())

    def test_reexport_in_clinical_cognitive_core(self):
        """
        Тест 5: Реэкспорт функций и констант страхового протокола
        в клиническое ядро clinical_cognitive_core.py.
        """
        self.assertTrue(hasattr(ccc, "format_insurance_justification_report"))
        self.assertTrue(hasattr(ccc, "build_043_insurance_entry"))
        self.assertTrue(hasattr(ccc, "get_procedure_justification"))
        self.assertTrue(hasattr(ccc, "ICD10_DENTAL_DIAGNOSES"))
        self.assertTrue(hasattr(ccc, "CLINICAL_STANDARDS_CITATIONS"))
        self.assertTrue(hasattr(ccc, "INSTRUMENTAL_JUSTIFICATIONS"))
        self.assertTrue(hasattr(ccc, "INSURANCE_JUSTIFICATION_ARCHITECTURE"))

        # Проверяем вызов через модуль ядра
        report = ccc.format_insurance_justification_report(
            tooth_fdi=37,
            diagnosis_icd="K04.5",
            clinical_findings="Хронический апикальный периодонтит, PAI 4",
            requested_procedures=["cbct", "microscope"]
        )
        self.assertIn("K04.5", report)
        self.assertIn("3.7", report)

    def test_form_043_entry_and_template_integration(self):
        """
        Тест 6: Интеграция шаблона ДМС в форму 043/у (assistant.CLINICAL_RECORD_TEMPLATES).
        Проверяется наличие тегов <code>, читаемость парсером Telethon и полнота данных.
        """
        # Генератор чистой записи 043/у
        entry_text = cip.build_043_insurance_entry(
            tooth_fdi="3.7",
            diagnosis_icd="K04.5",
            clinical_findings="Облитерация каналов, PAI 4",
            requested_procedures=["cbct", "microscope", "ultrasound", "bioceramics"]
        )
        self.assertIn("<code>", entry_text)
        self.assertIn("</code>", entry_text)
        self.assertIn("💡 <i>Нажмите на текст ниже, чтобы скопировать", entry_text)

        # Валидация шаблона в CLINICAL_RECORD_TEMPLATES['endo']['variants']
        endo_variants = assistant.CLINICAL_RECORD_TEMPLATES["endo"]["variants"]
        self.assertIn("v_insurance_dms", endo_variants, "Вариант v_insurance_dms должен быть в шаблоне endo")

        dms_card = endo_variants["v_insurance_dms"]
        dms_text = dms_card.get("text", "")
        self.assertIn("<code>", dms_text)
        self.assertIn("</code>", dms_text)
        self.assertIn("3.7", dms_text)
        self.assertIn("K04.5", dms_text)
        self.assertIn("PAI 4", dms_text)
        self.assertIn("КЛКТ", dms_text)
        self.assertIn("микроскоп", dms_text.lower())

        # Проверка Telethon HTML-парсером на валидность MessageEntityCode
        parsed_text, entities = th_html.parse(dms_text)
        code_entities = [e for e in entities if type(e).__name__ == "MessageEntityCode"]
        self.assertTrue(len(code_entities) >= 1, "Telethon должен обнаружить MessageEntityCode в карточке ДМС")

    def test_consultation_command_routing(self):
        """
        Тест 7: Маршрутизация команды /consultation и ее синонимов в TEXT_MENU_COMMAND_MAP.
        """
        menu_map = assistant.TEXT_MENU_COMMAND_MAP
        self.assertIn("консультация", menu_map)
        self.assertEqual(menu_map["консультация"], "/consultation")
        self.assertIn("согласование", menu_map)
        self.assertEqual(menu_map["согласование"], "/consultation")
        self.assertIn("экспертиза", menu_map)
        self.assertEqual(menu_map["экспертиза"], "/consultation")
        self.assertIn("дмс", menu_map)
        self.assertEqual(menu_map["дмс"], "/consultation")


if __name__ == "__main__":
    unittest.main()
