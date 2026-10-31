# -*- coding: utf-8 -*-
"""
test_visiodent_enhancer.py — Комплексный аудит и верификация модуля рентген-фильтров 2х2 HD (Визиограф).

Проверяемые инварианты:
1. Классификатор прицельных снимков: фильтрация визиографов от КТ, ОПТГ, макрофото и скриншотов.
2. Подтверждение модальности через метаданные Vision: PERIAPICAL_XRAY vs CBCT vs CLINICAL_PHOTO.
3. Валидатор кандидатов: отсечение цветных фото, ОПТГ и КТ перед запуском фильтров.
4. Защита меню кнопок: кнопка 2х2 коллажа НИКОГДА не предлагается для обычных фото (has_media=True, is_xray=False).
5. Детектор сомнений: обнаружение клинических развилок, отломков, перфораций и периапикальных деструкций.
6. Выбор квадранта (CLAHE, Негатив, Шарп).
7. Генерация коллажа 2х2 HD и подписи.
8. Команды /xray, /рентген, /визиограф.
"""

import os
import sys

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import unittest
import numpy as np
import cv2

import visiodent_enhancer
import assistant
import vision


class TestVisiodentEnhancer(unittest.TestCase):

    def test_periapical_xray_heuristic(self):
        """Проверяет классификацию прицельного визиографа против КТ, ОПТГ и клинических фото."""
        # Положительные кейсы (визиограф)
        self.assertTrue(visiodent_enhancer.is_periapical_xray_heuristic("Прицельный снимок зуба 36", "На рентгенограмме корень и канал"))
        self.assertTrue(visiodent_enhancer.is_periapical_xray_heuristic("визиограф после обтурации", "Периапикальные ткани в норме"))
        self.assertTrue(visiodent_enhancer.is_periapical_xray_heuristic("снимок 46", "Радиовизиограмма нижнего моляра"))

        # Отрицательные кейсы (КТ, ОПТГ, фото, мемы)
        self.assertFalse(visiodent_enhancer.is_periapical_xray_heuristic("Вот срез КЛКТ", "КЛКТ пазухи"))
        self.assertFalse(visiodent_enhancer.is_periapical_xray_heuristic("ОПТГ пациента", "Панорамный снимок обеих челюстей"))
        self.assertFalse(visiodent_enhancer.is_periapical_xray_heuristic("фото зуба во рту", "Клиническое фото резцов с десной"))
        self.assertFalse(visiodent_enhancer.is_periapical_xray_heuristic("смешной мем", "Скриншот переписки"))

    def test_is_periapical_xray_confirmed_with_vision_metadata(self):
        """Проверяет строгое разделение визиографа от КТ, ОПТГ и клинических фото по метаданным Vision."""
        # 1. Прицельный визиограф
        vd_xray = vision.VisionDescription("Описание", metadata={"modality": "PERIAPICAL_XRAY"})
        self.assertTrue(visiodent_enhancer.is_periapical_xray_confirmed(vd_xray))

        # 2. КТ / КЛКТ
        vd_cbct = vision.VisionDescription("Описание", metadata={"modality": "CBCT"})
        self.assertFalse(visiodent_enhancer.is_periapical_xray_confirmed(vd_cbct))

        # 3. ОПТГ
        vd_opg = vision.VisionDescription("Описание", metadata={"modality": "PANORAMIC_OPG"})
        self.assertFalse(visiodent_enhancer.is_periapical_xray_confirmed(vd_opg))

        # 4. Цветное клиническое фото
        vd_photo = vision.VisionDescription("Описание", metadata={"modality": "CLINICAL_PHOTO"})
        self.assertFalse(visiodent_enhancer.is_periapical_xray_confirmed(vd_photo))

        # 5. Мем / скриншот
        vd_meme = vision.VisionDescription("Описание", metadata={"modality": "NON_DENTAL"})
        self.assertFalse(visiodent_enhancer.is_periapical_xray_confirmed(vd_meme))

    def test_validate_visiodent_candidate_rejects_color_and_opg(self):
        """Проверяет валидацию изображения на уровне физических параметров (цветность, геометрия)."""
        # 1. Монохромный снимок (Grayscale) -> PASS
        mono_img = np.zeros((150, 100, 3), dtype=np.uint8)
        mono_img[:, :] = 100
        _, buf = cv2.imencode(".jpg", mono_img)
        ok, reason = visiodent_enhancer.validate_visiodent_candidate(buf.tobytes())
        self.assertTrue(ok)
        self.assertEqual(reason, "OK")

        # 2. Цветное фото (RGB различие > 20) -> REJECT
        color_img = np.zeros((150, 100, 3), dtype=np.uint8)
        color_img[:, :] = [20, 40, 220] # красная десна / кровь
        _, buf_col = cv2.imencode(".jpg", color_img)
        ok_col, reason_col = visiodent_enhancer.validate_visiodent_candidate(buf_col.tobytes())
        self.assertFalse(ok_col)
        self.assertIn("Цветное клиническое фото", reason_col)

        # 3. ОПТГ панорама (aspect ratio > 1.82) -> REJECT
        opg_img = np.zeros((100, 250, 3), dtype=np.uint8) # 2.5:1
        _, buf_opg = cv2.imencode(".jpg", opg_img)
        ok_opg, reason_opg = visiodent_enhancer.validate_visiodent_candidate(buf_opg.tobytes())
        self.assertFalse(ok_opg)
        self.assertIn("Панорамный снимок", reason_opg)

        # 4. Явный текстовый запрос КТ -> REJECT
        ok_ct, reason_ct = visiodent_enhancer.validate_visiodent_candidate(buf.tobytes(), user_text="посмотри срез кт")
        self.assertFalse(ok_ct)
        self.assertIn("КТ/КЛКТ", reason_ct)

    def test_build_nba_markup_never_shows_for_non_xray(self):
        """Инвариант защиты: кнопка 2х2 фильтров НИКОГДА не показывается, если is_xray=False, даже при has_media=True!"""
        # С обычным фото (has_media=True, is_xray=False)
        markup_random_photo = assistant.build_nba_markup(has_media=True, msg_id=123, is_xray=False)
        has_btn = any(
            "Рентген-фильтры" in getattr(btn, "text", "")
            for row in markup_random_photo for btn in row
        )
        self.assertFalse(has_btn, "Кнопка рентген-фильтров была ошибочно предложена для обычного фото!")

        # С визиографом (is_xray=True)
        markup_xray = assistant.build_nba_markup(has_media=True, msg_id=123, is_xray=True)
        has_btn_xray = any(
            "Рентген-фильтры" in getattr(btn, "text", "")
            for row in markup_xray for btn in row
        )
        self.assertTrue(has_btn_xray, "Кнопка рентген-фильтров должна присутствовать для визиографа!")

    def test_clinical_doubt_heuristic(self):
        """Проверяет обнаружение сомнений и клинических развилок у кресла."""
        self.assertTrue(visiodent_enhancer.has_clinical_doubt_heuristic("Кажется, тут отломок инструмента в апексе"))
        self.assertTrue(visiodent_enhancer.has_clinical_doubt_heuristic("Подскажите, есть ли тут периодонтит?"))
        self.assertTrue(visiodent_enhancer.has_clinical_doubt_heuristic("Посмотрите, это трещина или артефакт наложения?"))
        self.assertTrue(visiodent_enhancer.has_clinical_doubt_heuristic("Сомневаюсь насчет качества обтурации"))

        # Без сомнений (просто протокольная фиксация)
        self.assertFalse(visiodent_enhancer.has_clinical_doubt_heuristic("Контроль 6 месяцев."))
        self.assertFalse(visiodent_enhancer.has_clinical_doubt_heuristic("Лечение завершено."))

    def test_analyze_xray_context_and_doubt(self):
        """Проверяет выбор оптимального квадранта в зависимости от типа патологии."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            # 1. Отломок -> Квадрант 4 (Шарп)
            r1 = loop.run_until_complete(
                visiodent_enhancer.analyze_xray_context_and_doubt_async(
                    user_text="Зуб 36, сломал инструмент в дистальном канале",
                    vision_description="Прицельный снимок моляра"
                )
            )
            self.assertTrue(r1["is_periapical_xray"])
            self.assertTrue(r1["has_clinical_doubt"])
            self.assertEqual(r1["target_tooth"], "36")
            self.assertEqual(r1["recommended_quadrant"], 4)
            self.assertIn("Квадрант 4", r1["recommendation_text"])

            # 2. Периодонтит / деструкция -> Квадрант 3 (Негатив)
            r2 = loop.run_until_complete(
                visiodent_enhancer.analyze_xray_context_and_doubt_async(
                    user_text="Очаг периодонтита на корне 46, есть ли резорбция?",
                    vision_description="Рентгенограмма верхушки корня"
                )
            )
            self.assertEqual(r2["target_tooth"], "46")
            self.assertEqual(r2["recommended_quadrant"], 3)
            self.assertIn("Квадрант 3", r2["recommendation_text"])

            # 3. Общая анатомия -> Квадрант 2 (CLAHE)
            r3 = loop.run_until_complete(
                visiodent_enhancer.analyze_xray_context_and_doubt_async(
                    user_text="Прицельный снимок зуба 12, контроль каналов",
                    vision_description="Визиограф резца"
                )
            )
            self.assertEqual(r3["target_tooth"], "12")
            self.assertEqual(r3["recommended_quadrant"], 2)
            self.assertIn("Квадрант 2", r3["recommendation_text"])
        finally:
            loop.close()

    def test_generate_visiodent_collage(self):
        """Проверяет синтез 2х2 HD коллажа из искусственного тестового снимка."""
        test_img = np.zeros((150, 100, 3), dtype=np.uint8)
        test_img[:, :] = 80
        test_img[30:130, 25:75] = 160  # Корень зуба
        test_img[40:120, 48:52] = 40   # Канал

        _, buf = cv2.imencode(".jpg", test_img)
        img_bytes = buf.tobytes()

        collage_path, dimensions = visiodent_enhancer.generate_visiodent_collage(
            input_source=img_bytes,
            target_quadrant_width=200
        )
        self.assertIsNotNone(collage_path)
        self.assertIsNotNone(dimensions)
        self.assertTrue(os.path.exists(collage_path))

        w, h = dimensions
        self.assertGreaterEqual(w, 400)
        self.assertGreaterEqual(h, 500)

        loaded = cv2.imread(collage_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.shape[0], h)
        self.assertEqual(loaded.shape[1], w)

        if os.path.exists(collage_path):
            os.remove(collage_path)

    def test_format_xray_collage_caption(self):
        """Проверяет формирование HTML подписи под коллажем."""
        analysis = {
            "focus_area": "Зуб 36: поиск отломка инструмента",
            "recommended_quadrant": 4,
            "recommendation_text": "Используйте Квадрант 4 (Контурная резкость) для детальной оценки краев."
        }
        caption = visiodent_enhancer.format_xray_collage_caption(analysis)
        self.assertIn("Рентген-фильтры 2х2 HD", caption)
        self.assertIn("Зуб 36: поиск отломка", caption)
        self.assertIn("Квадрант 4 (Контурный фильтр / Шарп)", caption)
        self.assertIn("<b>", caption)

    def test_is_xray_command(self):
        """Проверяет триггеры команд /xray и /рентген."""
        self.assertTrue(assistant.is_xray_command("/xray"))
        self.assertTrue(assistant.is_xray_command("/xray 36"))
        self.assertTrue(assistant.is_xray_command("/рентген"))
        self.assertTrue(assistant.is_xray_command("/рентген 46"))
        self.assertTrue(assistant.is_xray_command("/визиограф"))
        self.assertFalse(assistant.is_xray_command("/calc"))
        self.assertFalse(assistant.is_xray_command("обычный текст"))


if __name__ == "__main__":
    unittest.main()
