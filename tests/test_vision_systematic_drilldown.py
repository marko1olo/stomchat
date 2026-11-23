# -*- coding: utf-8 -*-
import os
import sys
import unittest
import inspect

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import vision
import clinical_cognitive_core
import assistant
import visiodent_enhancer


class TestVisionSystematicDrilldown(unittest.TestCase):
    """
    Ред-тим тесты систематического 4-зонного радиологического алгоритма
    и защиты от рентгенологической «слепоты к деталям» (инцидент 1824-1827: КТ 4.7-4.8).
    """

    def test_vision_cognitive_architecture_has_all_4_zones_and_invariants(self):
        """Проверка наличия всех 4 зон и разделения объективной картины и интерпретации в core-архитектуре."""
        arch = clinical_cognitive_core.VISION_COGNITIVE_ARCHITECTURE

        # Зона 1: Коронка и пульпарная камера
        self.assertIn("ЗОНА 1: КОРОНКА И ПУЛЬПАРНАЯ КАМЕРА", arch)
        self.assertIn("Дентикли и петрификаты пульпы", arch)
        self.assertIn("шарик в центре коронки", arch)
        self.assertIn("Глубина кариозного поражения", arch)
        self.assertIn("Краевая адаптация реставраций", arch)
        self.assertIn("Вторичный и рецидивирующий кариес", arch)

        # Зона 2: Корневая система и каналы
        self.assertIn("ЗОНА 2: КОРНЕВАЯ СИСТЕМА И КАНАЛЫ", arch)
        self.assertIn("Вертуччи", arch)
        self.assertIn("Шнайдер", arch)
        self.assertIn("Облитерация", arch)
        self.assertIn("отломки эндодонтических инструментов", arch)
        self.assertIn("перфорации", arch)

        # Зона 3: Периапикальный периодонт и окружающая кость
        self.assertIn("ЗОНА 3: ПЕРИАПИКАЛЬНЫЙ ПЕРИОДОНТ", arch)
        self.assertIn("lamina dura", arch)
        self.assertIn("Периодонтальная щель", arch)
        self.assertIn("Периапикальные очаги деструкции", arch)
        self.assertIn("Резорбция корней", arch)

        # Зона 4: Смежные анатомические ориентиры
        self.assertIn("ЗОНА 4: СМЕЖНЫЕ АНАТОМИЧЕСКИЕ ОРИЕНТИРЫ", arch)
        self.assertIn("гайморовой", arch)
        self.assertIn("Нижнечелюстной канал", arch)
        self.assertIn("Подбородочное отверстие", arch)
        self.assertIn("Кортикальные пластинки", arch)

        # Разделение объективной картины и интерпретации
        self.assertIn("СТРОГОЕ РАЗДЕЛЕНИЕ ОБЪЕКТИВНЫХ ДАННЫХ И ИНТЕРПРЕТАЦИИ", arch)
        self.assertIn("Рентген-плотность", arch)
        self.assertIn("радиоопаковое", arch)
        self.assertIn("радиолюцентное", arch)
        self.assertIn("Дентикль/петрификат vs суперпозиция", arch)
        self.assertIn("радикулярная киста vs апикальная гранулема", arch)
        self.assertTrue("Внутренняя vs наружная" in arch or "внутренняя vs наружная" in arch.lower())
        self.assertIn("cervical burnout", arch)

    def test_vision_system_prompt_mandates_4_zones_and_anti_blindness(self):
        """Проверка, что внутри describe_image промпт модели зрения содержит 4 зоны и запрет слепоты к дентиклям."""
        src = inspect.getsource(vision.describe_image)

        # Запрет слепоты к деталям и туннельного зрения
        self.assertIn("слепота к деталям", src)
        self.assertIn("ОБЯЗАТЕЛЬНЫЙ СИСТЕМАТИЧЕСКИЙ 4-ЗОННЫЙ РАДИОЛОГИЧЕСКИЙ АЛГОРИТМ", src)

        # Зона 1: коронка и пульпарная камера
        self.assertIn("Зона 1 (Коронка и пульпарная камера)", src)
        self.assertIn("дентиклей/петрификатов пульпы", src)
        self.assertIn("не пропускать дентикли в центре коронки", src)

        # Зона 2: корневая система и каналы
        self.assertIn("Зона 2 (Корневая система и каналы)", src)
        self.assertIn("Вертуччи", src)
        self.assertIn("Шнайдер", src)

        # Зона 3: периодонт и кость
        self.assertIn("Зона 3 (Периапикальный периодонт и окружающая кость)", src)
        self.assertIn("lamina dura", src)
        self.assertIn("периапикальные очаги деструкции", src)

        # Зона 4: смежные ориентиры
        self.assertIn("Зона 4 (Смежные анатомические ориентиры)", src)
        self.assertIn("нижнечелюстной канал", src)

        # Разделение объективного и интерпретации
        self.assertIn("РАЗДЕЛЕНИЕ ОБЪЕКТИВНОЙ КАРТИНЫ И ИНТЕРПРЕТАЦИИ", src)
        self.assertIn("Объективная картина: рентген-плотность", src)
        self.assertIn("Клиническая интерпретация и дифдиагноз", src)

    def test_assistant_prompts_mandate_4_zone_systematic_drilldown(self):
        """Проверка, что assistant.py транслирует 4-зонный алгоритм в директивах разбора снимка."""
        src = inspect.getsource(assistant.handle_private_message)
        self.assertIn("Зона 1", src)
        self.assertIn("Коронка и пульпарная камера", src)
        self.assertIn("Зона 2", src)
        self.assertIn("Корневая система и каналы", src)
        self.assertIn("Зона 3", src)
        self.assertIn("Периапикальный периодонт", src)
        self.assertIn("Зона 4", src)
        self.assertIn("Смежные анатомические ориентиры", src)

        # Проверка группового медиа-ассистента
        src_media = inspect.getsource(assistant.check_and_trigger_assistant_media)
        self.assertIn("Зона 1", src_media)
        self.assertIn("Зона 2", src_media)
        self.assertIn("Зона 3", src_media)
        self.assertIn("Зона 4", src_media)
        self.assertIn("дентиклей/петрификатов пульпы", src_media)

    def test_metadata_extraction_denticle_keywords(self):
        """Проверка распознавания дентиклей, петрификатов и шаровидных включений в метаданных."""
        # Тест 1: фраза врача из инцидента («шарик в центре коронки»)
        text_incident = (
            "[ПОДРОБНОЕ ОПИСАНИЕ]\n"
            "Зона 1: В полости зуба 4.7 визуализируется шарик в центре коронки высокой плотности.\n"
            "Зона 3: Глубокий дистальный карман 4.8.\n\n"
            "[КРАТКАЯ ПОДПИСЬ]\n"
            "КТ 4.7-4.8: дентикль в коронке 4.7 и карман 4.8\n\n"
            "[МЕТАДАННЫЕ]\n"
            '{"modality": "CBCT", "target_tooth": 47, "has_clinical_doubt": null, "doubt_subject": null}'
        )
        meta = vision.extract_vision_metadata(text_incident)
        self.assertEqual(meta.get("modality"), "CBCT")
        self.assertEqual(meta.get("target_tooth"), 47)
        self.assertTrue(meta.get("has_clinical_doubt"))
        self.assertIn("Дентикль", meta.get("doubt_subject"))
        self.assertEqual(meta.get("recommended_quadrant"), 2)

        # Тест 2: не-JSON секция метаданных со свободным дентиклем
        text_non_json = (
            "[ПОДРОБНОЕ ОПИСАНИЕ]\n"
            "На прицельной рентгенограмме зуба 3.7 определяется свободный дентикль пульпарной камеры.\n\n"
            "[КРАТКАЯ ПОДПИСЬ]\n"
            "Прицельный снимок 3.7\n\n"
            "[МЕТАДАННЫЕ]\n"
            "- Модальность: прицельный визиограф\n"
            "- Зуб: 3.7\n"
            "- Находка: интрапульпарный петрификат"
        )
        meta2 = vision.extract_vision_metadata(text_non_json)
        self.assertEqual(meta2.get("modality"), "PERIAPICAL_XRAY")
        self.assertEqual(meta2.get("target_tooth"), 37)
        self.assertTrue(meta2.get("has_clinical_doubt"))
        self.assertIn("Дентикль", meta2.get("doubt_subject"))
        self.assertEqual(meta2.get("recommended_quadrant"), 2)

    def test_metadata_extraction_other_zones(self):
        """Проверка маршрутизации сомнений в другие квадранты для зон 2 и 3."""
        # Зона 2: отломок инструмента -> квадрант 4 (контурная резкость)
        t_zone2 = (
            "[ПОДРОБНОЕ ОПИСАНИЕ]\n"
            "Зона 2: в апикальной трети MB2 зуба 16 определяется отломок файла NiTi длиной 2.5 мм.\n\n"
            "[МЕТАДАННЫЕ]\n"
            '{"modality": "PERIAPICAL_XRAY", "target_tooth": 16, "has_clinical_doubt": true, "doubt_subject": null}'
        )
        m2 = vision.extract_vision_metadata(t_zone2)
        self.assertEqual(m2.get("recommended_quadrant"), 4)
        self.assertIn("Инородное тело", m2.get("doubt_subject"))

        # Зона 3: периапикальный очаг деструкции -> квадрант 3 (негатив)
        t_zone3 = (
            "[ПОДРОБНОЕ ОПИСАНИЕ]\n"
            "Зона 3: у верхушки дистального корня зуба 46 определяется периапикальный очаг деструкции d=5 мм.\n\n"
            "[МЕТАДАННЫЕ]\n"
            '{"modality": "PERIAPICAL_XRAY", "target_tooth": 46, "has_clinical_doubt": true, "doubt_subject": null}'
        )
        m3 = vision.extract_vision_metadata(t_zone3)
        self.assertEqual(m3.get("recommended_quadrant"), 3)
        self.assertIn("очаг", m3.get("doubt_subject"))

    def test_incident_47_48_simulation_full_flow(self):
        """
        Полная симуляция инцидента 1824-1827:
        КТ 4.7-4.8 со сложной анатомией: дентикль в 4.7 + дистальный карман 4.8.
        Проверяем, что разбор двойного формата сохраняет все 4 зоны без купирования.
        """
        raw_model_response = (
            "[ПОДРОБНОЕ ОПИСАНИЕ]\n"
            "На срезе КЛКТ зубов 4.7-4.8 определяется:\n"
            "Зона 1 (Коронка и пульпарная камера): в пульпарной камере 4.7 в центре коронки визуализируется "
            "свободный дентикль — шаровидное рентгеноконтрастное включение высокой плотности d≈1.8 мм с четкими контурами. "
            "На окклюзионно-дистальной поверхности 4.8 определяется глубокая кариозная полость.\n"
            "Зона 2 (Корневая система): каналы 4.7 проходимы, корни 4.8 дистально искривлены по Шнайдеру (угол ≈25°).\n"
            "Зона 3 (Периодонт и кость): у дистальной поверхности 4.7 — глубокий костный карман 5 мм с краевой резорбцией; "
            "периодонтальная щель у верхушек корней 4.7 без расширения, lamina dura интактна.\n"
            "Зона 4 (Смежные ориентиры): верхушки корней 4.8 интимно прилежат к верхнему краю нижнечелюстного канала (<0.5 мм).\n\n"
            "[КРАТКАЯ ПОДПИСЬ]\n"
            "КЛКТ 4.7-4.8: дентикль пульпарной камеры 4.7, глубокий костный карман и близость к canalis mandibulae.\n\n"
            "[МЕТАДАННЫЕ]\n"
            "{\n"
            '  "modality": "CBCT",\n'
            '  "target_tooth": 47,\n'
            '  "has_clinical_doubt": true,\n'
            '  "doubt_subject": "Дентикль пульпарной камеры 4.7 и дистальный карман",\n'
            '  "recommended_quadrant": 2\n'
            "}"
        )

        detailed, short = vision.parse_dual_vision_description(raw_model_response)
        meta = vision.extract_vision_metadata(raw_model_response)

        # 1. Проверяем, что детальное описание сохранило ВСЕ 4 зоны
        self.assertIn("Зона 1 (Коронка и пульпарная камера)", detailed)
        self.assertIn("дентикль", detailed)
        self.assertIn("шаровидное рентгеноконтрастное включение", detailed)

        self.assertIn("Зона 2 (Корневая система)", detailed)
        self.assertIn("Шнайдер", detailed)

        self.assertIn("Зона 3 (Периодонт и кость)", detailed)
        self.assertIn("костный карман", detailed)

        self.assertIn("Зона 4 (Смежные ориентиры)", detailed)
        self.assertIn("нижнечелюстного канала", detailed)

        # 2. Проверяем, что краткая подпись лаконична и пригодна для подписи к иллюстрации
        self.assertIn("КЛКТ 4.7-4.8", short)
        self.assertLessEqual(len(short), 160)
        self.assertNotIn("МЕТАДАННЫЕ", short)
        self.assertNotIn("ПОДРОБНОЕ ОПИСАНИЕ", short)

        # 3. Метаданные корректно извлечены
        self.assertEqual(meta["modality"], "CBCT")
        self.assertEqual(meta["target_tooth"], 47)
        self.assertTrue(meta["has_clinical_doubt"])
        self.assertEqual(meta["recommended_quadrant"], 2)

    def test_systematic_drilldown_4_zones_detailed_protocol_in_system_prompt(self):
        """Проверка полноты 4-зонного протокола в system_prompt vision.py."""
        src = inspect.getsource(vision.describe_image)

        # Зона 1: коронка, скрытый апроксимальный кариес, нависающие края, ликвидация слепоты к шарикам/петрификатам
        self.assertIn("скрытый апроксимальный кариес", src)
        self.assertIn("нависающие края реставраций", src)
        self.assertIn("ликвидировать любую слепоту к «шарикам/петрификатам в пульпарной камере»", src)

        # Зона 2: каналы, Вертуччи, Шнайдер, отломки, перфорации, обтурация
        self.assertIn("Вертуччи", src)
        self.assertIn("Шнайдер", src)
        self.assertIn("отломки инструментов", src)
        self.assertIn("перфорации", src)
        self.assertIn("обтураци", src)

        # Зона 3: периапикальные ткани, lamina dura, периодонтальная щель, PAI 1-5, резорбция
        self.assertIn("lamina dura", src)
        self.assertIn("периодонтальной щели", src)
        self.assertIn("PAI 1-5", src)
        self.assertIn("резорбция корней", src)

        # Зона 4: смежные структуры, дно гайморовой пазухи, нижнечелюстной канал, ментальное отверстие
        self.assertIn("дно гайморовой пазухи", src)
        self.assertIn("нижнечелюстной канал", src)
        self.assertIn("ментальное отверстие", src)

    def test_anti_robotic_clean_ux_mandates_in_system_prompt(self):
        """Проверка запрета роботизированных дежурных фраз и требования чистой клинической картины."""
        src = inspect.getsource(vision.describe_image)

        self.assertIn("КАТЕГОРИЧЕСКИЙ ВЫКОС ДЕЖУРНЫХ РОБОТИЗИРОВАННЫХ ФРАЗ", src)
        self.assertIn("На представленном снимке визуализируется", src)
        self.assertIn("Следует отметить, что", src)
        self.assertIn("В целом можно сказать", src)
        self.assertIn("четкую, емкую клиническую картину для доктора", src)

    def test_calcificates_autorouting_to_quadrant_2_clahe(self):
        """Проверка автомаршрутизации кальцификатов пульпарной камеры («шарик», «дентикль», «петрификат») в Квадрант 2."""
        cases = [
            ("В полости зуба 4.6 определяется плотный шарик в пульпарной камере.", 46),
            ("В зубе 3.7 визуализируется крупный интрапульпарный петрификат.", 37),
            ("Зуб 1.6: свободный дентикль полости зуба d=2мм.", 16),
            ("Зуб 2.6: пульполит в устьевой части пульпарной камеры.", 26),
            ("Зуб 4.7: диффузная облитерация и кальцификаты пульпы.", 47),
        ]
        for desc, tooth in cases:
            raw = (
                f"[ПОДРОБНОЕ ОПИСАНИЕ]\n{desc}\n\n"
                f"[КРАТКАЯ ПОДПИСЬ]\nСнимок зуба {tooth}\n\n"
                f"[МЕТАДАННЫЕ]\n"
                f'{{"modality": "PERIAPICAL_XRAY", "target_tooth": {tooth}, "has_clinical_doubt": null, "doubt_subject": null}}'
            )
            meta = vision.extract_vision_metadata(raw)
            self.assertEqual(meta.get("recommended_quadrant"), 2, f"Failed routing to Q2 for: {desc}")
            self.assertTrue(meta.get("has_clinical_doubt"))
            self.assertIn("Дентикль", meta.get("doubt_subject"))

    def test_visiodent_enhancer_autorouting_and_caption_clean_ux(self):
        """Проверка работы visiodent_enhancer с автомаршрутизацией кальцификатов и чистым UX подписи."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            # 1. Запрос врача с шариком/петрификатом -> Квадрант 2
            res = loop.run_until_complete(
                visiodent_enhancer.analyze_xray_context_and_doubt_async(
                    user_text="Зуб 47, в коронке какой-то шарик, посмотрите",
                    vision_description="Прицельная радиовизиография моляра"
                )
            )
            self.assertTrue(res["is_periapical_xray"])
            self.assertTrue(res["has_clinical_doubt"])
            self.assertEqual(res["target_tooth"], "47")
            self.assertEqual(res["recommended_quadrant"], 2)
            self.assertIn("Квадрант 2", res["recommendation_text"])
            self.assertIn("дентиклей", res["recommendation_text"])

            # 2. Подпись под коллажем не содержит тавтологичного заикания
            caption = visiodent_enhancer.format_xray_collage_caption(res)
            self.assertIn("Рентген-фильтры 2х2 HD", caption)
            self.assertIn("Квадрант 2 (Адаптивный контраст CLAHE)", caption)
            # Проверяем, что нет уродливого повтора "обратите внимание на Квадрант 2 ... — Используйте Квадрант 2 ..."
            self.assertNotIn("— Используйте Квадрант 2", caption)
        finally:
            loop.close()

    def test_incident_doctor_frustration_reproduction_tooth7(self):
        """
        Репродукция реального инцидента из живой базы:
        Доктор в отчаянии пишет: «Нет вопрос в шарике в центре коронки 7го зуба»!
        В снимке упомянуты 4.7 и 4.8. Бот не должен зацикливаться на 4.8!
        Целевой зуб должен быть определен строго как 47, а квадрант — 2 (CLAHE HD).
        """
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            # Описание снимка с двумя зубами: 4.7 и ретенированный 4.8
            vd_desc = vision.VisionDescription(
                "На снимке зубов 4.7 и 4.8: ретенция и дистопия 4.8, коронка 4.7 с тенью в центре",
                metadata={"modality": "PERIAPICAL_XRAY", "target_tooth": 48} # старая модель ошибочно выбрала 48
            )

            # Врач уточняет фокус: «Нет вопрос в шарике в центре коронки 7го зуба»
            res = loop.run_until_complete(
                visiodent_enhancer.analyze_xray_context_and_doubt_async(
                    user_text="Нет вопрос в шарике в центре коронки 7го зуба",
                    vision_description=vd_desc
                )
            )

            # Целевой зуб ОБЯЗАН переключиться на 47 (семерка четвертого квадранта)
            self.assertEqual(res["target_tooth"], "47", "Бот проигнорировал указание врача на 7-й зуб!")
            self.assertEqual(res["recommended_quadrant"], 2, "Шарик в центре коронки обязан маршрутизироваться в Квадрант 2!")
            self.assertTrue(res["has_clinical_doubt"])
            self.assertIn("Зуб 47: ревизия пульпарной камеры (дентикли/петрификаты пульпы)", res["focus_area"])
            self.assertIn("Квадрант 2", res["recommendation_text"])
            self.assertIn("дентиклей", res["recommendation_text"])

            # Проверка альтернативной фразы: «камень в пульпе 4.7»
            res2 = loop.run_until_complete(
                visiodent_enhancer.analyze_xray_context_and_doubt_async(
                    user_text="Посмотри, там камень в пульпе 47?",
                    vision_description="Радиовизиография 4.7"
                )
            )
            self.assertEqual(res2["target_tooth"], "47")
            self.assertEqual(res2["recommended_quadrant"], 2)
            self.assertTrue(res2["has_clinical_doubt"])
        finally:
            loop.close()

    def test_postop_crown_evaluation_prompt_and_zone1(self):
        """Проверка протокола постоперационного аудита коронок в Зоне 1 (излишки цемента, уступ, зазоры)."""
        src = inspect.getsource(vision.describe_image)

        self.assertIn("ПОСТОПЕРАЦИОННЫЙ АУДИТ КОРОНОК ПОСЛЕ ФИКСАЦИИ", src)
        self.assertIn("рентгеноконтрастность излишков цемента", src)
        self.assertIn("точность посадки на уступ", src)
        self.assertIn("отсутствие щелей и микрозазоров", src)
        self.assertIn("open margin", src)

    def test_anti_tunnel_vision_mandate_in_vision_prompt(self):
        """Проверка запрета туннельного зрения и игнорирования центра коронки соседнего зуба."""
        src = inspect.getsource(vision.describe_image)

        self.assertIn("АНТИ-ТУННЕЛЬНОЕ ЗРЕНИЕ", src)
        self.assertIn("ретенция/дистопия 4.8", src)
        self.assertIn("шарик/дентикль 4.7", src)

    def test_error_handling_corrupted_or_missing_images(self):
        """Проверка безопасной обработки ошибок при поврежденных, пустых или отсутствующих файлах."""
        import asyncio

        # 1. Отсутствующий файл в generate_visiodent_collage
        c_path, dims = visiodent_enhancer.generate_visiodent_collage("completely_missing_file_xyz_123.jpg")
        self.assertIsNone(c_path)
        self.assertIsNone(dims)

        # 2. Поврежденные байты (битый заголовок)
        c_path2, dims2 = visiodent_enhancer.generate_visiodent_collage(b"not_an_image_random_corrupted_payload")
        self.assertIsNone(c_path2)
        self.assertIsNone(dims2)

        # 3. Пустые байты b""
        c_path3, dims3 = visiodent_enhancer.generate_visiodent_collage(b"")
        self.assertIsNone(c_path3)
        self.assertIsNone(dims3)

        # 4. Валидация кандидата на поврежденных данных
        ok1, reason1 = visiodent_enhancer.validate_visiodent_candidate(b"garbage")
        self.assertFalse(ok1)
        self.assertIn("Поврежденный", reason1)

        ok2, reason2 = visiodent_enhancer.validate_visiodent_candidate("missing_file.jpg")
        self.assertFalse(ok2)
        self.assertIn("Не удалось", reason2)

        # 5. Парсеры vision на пустых/None строках
        self.assertEqual(vision.extract_vision_metadata(""), {})
        self.assertEqual(vision.extract_vision_metadata(None), {})
        self.assertEqual(vision.parse_dual_vision_description(""), ("", ""))
        self.assertEqual(vision.parse_dual_vision_description(None), ("", ""))

        # 6. describe_image на отсутствующем пути
        loop = asyncio.new_event_loop()
        try:
            res_desc = loop.run_until_complete(
                vision.describe_image(["missing_img_test_123.jpg"], use_cache=False)
            )
            self.assertIsNone(res_desc)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
