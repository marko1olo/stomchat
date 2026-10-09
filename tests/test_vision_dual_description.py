import sys
import os
import unittest
import asyncio
import tempfile

sys.path.insert(0, os.path.abspath("."))
import vision
import database
import config
from summarizer import embed_media_into_summary_html


class TestVisionDualDescription(unittest.TestCase):
    def test_parse_dual_vision_description_explicit(self):
        raw = """[ПОДРОБНОЕ ОПИСАНИЕ]
На прицельной внутриротовой рентгенограмме зуба 3.6 визуализируется периапикальный очаг деструкции костной ткани округлой формы диаметром около 4 мм с четкими контурами у дистального корня (PAI 3). Медиальные каналы обтурированы до апекса, в дистальном канале визуализируется недопломбировка на 3 мм. Кортикальная пластинка альвеолы прерывиста.

[КРАТКАЯ ПОДПИСЬ]
Рентгенограмма зуба 3.6: периапикальный очаг у дистального корня, недопломбировка канала."""

        detailed, short = vision.parse_dual_vision_description(raw)
        self.assertIn("дистального корня", detailed)
        self.assertIn("PAI 3", detailed)
        self.assertEqual(short, "Рентгенограмма зуба 3.6: периапикальный очаг у дистального корня, недопломбировка канала.")
        self.assertLessEqual(len(short), 160)

    def test_extract_fallback_short_caption_monolithic(self):
        raw = "Внутриротовая фотография фронтального отдела верхней челюсти. Определяется краевое прилегание коронок на зубах 1.1 и 2.1 с рецессией десны 1-2 мм. Воспалительных явлений в маргинальной десне не выявлено."
        short = vision.extract_fallback_short_caption(raw, max_len=140)
        self.assertTrue(len(short) > 20)
        self.assertLessEqual(len(short), 140)
        self.assertIn("Внутриротовая фотография", short)

    def test_vision_description_class_attributes(self):
        desc = vision.VisionDescription(
            "Подробный клинический разбор",
            image_urls=["data:image/jpeg;base64,123"],
            short_caption="Короткая подпись"
        )
        self.assertEqual(str(desc), "Подробный клинический разбор")
        self.assertEqual(desc.image_urls, ["data:image/jpeg;base64,123"])
        self.assertEqual(desc.short_caption, "Короткая подпись")

    def test_database_media_short_caption_lifecycle(self):
        async def _run():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
                db_path = tf.name

            old_db = config.DB_PATH
            try:
                config.DB_PATH = db_path
                await database.init_db()

                # Insert test message
                await database.save_message(
                    msg_id=777,
                    sender_id=123,
                    sender_name="Доктор Иванов",
                    sender_username="dr_ivanov",
                    text="Кейс по ретроградному пломбированию",
                    date="2026-10-08 10:00:00",
                    has_media=True
                )

                # Update with dual description
                await database.update_media_description(
                    msg_id=777,
                    description="Подробный протокол ретро-пломбирования ProRoot MTA",
                    short_caption="Ретроградное пломбирование корня 2.1"
                )

                # Fetch both
                full_desc = await database.get_media_description(777)
                short_cap = await database.get_media_short_caption(777)

                self.assertEqual(full_desc, "Подробный протокол ретро-пломбирования ProRoot MTA")
                self.assertEqual(short_cap, "Ретроградное пломбирование корня 2.1")
            finally:
                config.DB_PATH = old_db
                if os.path.exists(db_path):
                    try:
                        os.remove(db_path)
                    except Exception:
                        pass

        asyncio.run(_run())

    def test_embed_media_deduplication_and_targeted_matching(self):
        html_input = """<h2>КЛИНИЧЕСКИЕ КЕЙСЫ</h2>
### Кейс 1: Эндодонтия 4.6
Начали лечение зуба [IMG_101]
Повторное упоминание маркера [IMG_101] не должно дублировать фото!

### Кейс 2: Ортопедия 1.1
Здесь рассматривали случай врача в MSG_102. Обошлись без маркера."""

        media_map = {
            101: "https://i.ibb.co/photo101.jpg",
            102: "https://i.ibb.co/photo102.jpg",
            103: "https://i.ibb.co/photo103.jpg", # Unrelated, should NOT be dumped into Case 1!
        }
        media_captions = {
            101: "Снимок зуба 4.6",
            102: "Препарирование под винир 1.1",
            103: "Несвязанный снимок другого врача"
        }

        result_html = embed_media_into_summary_html(html_input, media_map, media_captions, max_images=4)

        # 1. Image 101 must appear EXACTLY ONCE (count=1 check)
        self.assertEqual(result_html.count("https://i.ibb.co/photo101.jpg"), 1)

        # 2. Image 102 must appear because MSG_102 is mentioned in text
        self.assertEqual(result_html.count("https://i.ibb.co/photo102.jpg"), 1)

        # 3. Image 103 must NOT be blindly dumped into Case 1
        self.assertEqual(result_html.count("https://i.ibb.co/photo103.jpg"), 0)

        # 4. Captions in figcaption must be chairside short captions
        self.assertIn("<figcaption>Снимок зуба 4.6</figcaption>", result_html)
        self.assertIn("<figcaption>Препарирование под винир 1.1</figcaption>", result_html)


if __name__ == "__main__":
    unittest.main()
