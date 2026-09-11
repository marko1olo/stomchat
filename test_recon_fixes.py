import unittest
from datetime import datetime, timezone
import assistant

class TestReconFixes(unittest.TestCase):
    def test_parse_db_date_strips_tzinfo(self):
        # 1. Plain naive string
        d1 = assistant._parse_db_date("2026-09-10 19:20:35")
        self.assertIsNone(d1.tzinfo)

        # 2. Offset-aware datetime (e.g. from Telethon API)
        aware_dt = datetime(2026, 9, 10, 19, 20, 35, tzinfo=timezone.utc)
        d2 = assistant._parse_db_date(aware_dt)
        self.assertIsNone(d2.tzinfo)

        # 3. ISO string with timezone
        iso_str = "2026-09-10T19:20:35+00:00"
        d3 = assistant._parse_db_date(iso_str)
        self.assertIsNone(d3.tzinfo)

        # 4. Sorting mixed naive and aware dates must never crash
        rows = [
            (100, None, 1, "DocA", "Msg1", aware_dt),
            (101, None, 2, "DocB", "Msg2", "2026-09-10 19:21:00"),
            (102, None, 3, "DocC", "Msg3", datetime(2026, 9, 10, 19, 19, 0))
        ]
        # This was crashing with TypeError: can't compare offset-naive and offset-aware datetimes
        sorted_rows = sorted(rows, key=lambda r: (assistant._parse_db_date(r[5]), r[0]))
        self.assertEqual([r[0] for r in sorted_rows], [102, 100, 101])

    def test_explicit_non_dental_postcard_detection(self):
        postcard_desc = (
            "На изображении представлена поздравительная открытка с праздником Рош ха-Шана "
            "(Новым годом) от стоматологической клиники «Forest Hills Dental». "
            "Медицинские или анатомические структуры зубочелюстной системы на данном графическом "
            "материале визуально не дифференцируются, за исключением стилизованного контура зуба в логотипе клиники."
        )
        self.assertTrue(assistant.is_explicitly_non_dental_media(postcard_desc))

        # Real clinical photo
        clinical_desc = (
            "На прицельной рентгенограмме зуба 3.6 визуализируется очаг деструкции костной ткани "
            "в области бифуркации и верхушки медиального корня с нечеткими контурами."
        )
        self.assertFalse(assistant.is_explicitly_non_dental_media(clinical_desc))

    def test_strip_vision_negations_removes_non_diff_sentence(self):
        desc = (
            "Праздничный стол и яблоки. "
            "Медицинские или анатомические структуры зубочелюстной системы визуально не дифференцируются. "
            "Вверху виден логотип."
        )
        cleaned = assistant.strip_vision_negations(desc)
        self.assertNotIn("не дифференцируются", cleaned)
        self.assertIn("Праздничный стол", cleaned)

if __name__ == "__main__":
    unittest.main(verbosity=2)
