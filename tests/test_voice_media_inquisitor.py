# -*- coding: utf-8 -*-
"""
RED TEAM INQUISITOR SUITE: PM Voice, Audio & Media Error-Tolerance
Проверяет инварианты обработки голосовых сообщений, видеокружочков, аудиофайлов,
детерминированного стоматологического спеллчекера и фильтрации галлюцинаций Whisper.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

# Убедимся в UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import blocking_tools
import media_tools
import gemini_client


class TestVoiceMediaInquisitor(unittest.TestCase):
    """Набор тестов для верификации устойчивости голосовых и медиа функций."""

    def test_audio_document_detection(self):
        """Вектор 1: Проверка распознавания аудиофайлов, присланных как Document."""
        # 1. Обычный документ с mime audio/ogg
        msg_ogg = MagicMock()
        msg_ogg.sticker = None
        msg_ogg.document = MagicMock()
        msg_ogg.document.mime_type = "audio/ogg"
        msg_ogg.document.file_name = "voice.ogg"
        self.assertIsNotNone(media_tools.audio_document(msg_ogg))

        # 2. Документ m4a с application/octet-stream, но расширением .m4a в атрибутах
        msg_m4a = MagicMock()
        msg_m4a.sticker = None
        msg_m4a.document = MagicMock()
        msg_m4a.document.mime_type = "application/octet-stream"
        msg_m4a.document.file_name = None
        attr = MagicMock()
        attr.file_name = "clinical_dictation.m4a"
        msg_m4a.document.attributes = [attr]
        self.assertIsNotNone(media_tools.audio_document(msg_m4a))

        # 3. Документ mp3
        msg_mp3 = MagicMock()
        msg_mp3.sticker = None
        msg_mp3.document = MagicMock()
        msg_mp3.document.mime_type = "audio/mpeg"
        msg_mp3.document.file_name = "record.mp3"
        self.assertIsNotNone(media_tools.audio_document(msg_mp3))

        # 4. Стикер не должен распознаваться как аудио
        msg_sticker = MagicMock()
        msg_sticker.sticker = MagicMock()
        self.assertIsNone(media_tools.audio_document(msg_sticker))

        # 5. Картинка не должна быть аудиодокументом
        msg_img = MagicMock()
        msg_img.sticker = None
        msg_img.document = MagicMock()
        msg_img.document.mime_type = "image/jpeg"
        msg_img.document.file_name = "xray.jpg"
        self.assertIsNone(media_tools.audio_document(msg_img))

    def test_gemini_multimodal_no_unbound_local(self):
        """Вектор 1: Проверка отсутствия UnboundLocalError в transcribe_audio_gemini_multimodal."""
        # Создаем временный файл больше 32 байт
        temp_file = os.path.join(media_tools.MEDIA_TEMP_DIR, "test_b64_check.ogg")
        os.makedirs(media_tools.MEDIA_TEMP_DIR, exist_ok=True)
        try:
            with open(temp_file, "wb") as f:
                f.write(b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00" * 4)  # 40 байт
            
            # Патчим httpx и config, чтобы не дергать реальную сеть
            with patch("gemini_client.config") as mock_cfg, \
                 patch("gemini_client._httpx.AsyncClient") as mock_hc:
                mock_cfg.GOOGLE_KEYS = ["dummy_key_12345"]
                mock_client_instance = AsyncMock()
                mock_hc.return_value.__aenter__.return_value = mock_client_instance
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "candidates": [{"content": {"parts": [{"text": "тестовая расшифровка"}]}}]
                }
                mock_client_instance.post.return_value = mock_resp

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                res, err = loop.run_until_complete(
                    gemini_client.transcribe_audio_gemini_multimodal(temp_file, duration_secs=5.0)
                )
                loop.close()

                self.assertIsNone(err)
                self.assertEqual(res, "тестовая расшифровка")
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    def test_dental_spelling_normalization(self):
        """Вектор 2: Детерминированный спеллчекер стоматологических терминов."""
        # 1. Бренды
        norm1 = blocking_tools.normalize_dental_transcription("применил оптибонд фл и аш плюс")
        self.assertIn("OptiBond FL", norm1)
        self.assertIn("AH Plus", norm1)

        norm2 = blocking_tools.normalize_dental_transcription("для обтурации взяли биорут и протейпер либо реципрок")
        self.assertIn("BioRoot", norm2)
        self.assertIn("ProTaper", norm2)
        self.assertIn("Reciproc", norm2)

        # 2. Инструменты и диагностика
        norm3 = blocking_tools.normalize_dental_transcription(
            "наложили рубер дам и кафердам, промыли гипохлорид, закрыли перфорацию мета, "
            "провели де по форез, включили эндамотор и апекс локатор, задействовали ультразвуковой эндо чак, "
            "замерили эод м, сняли био пленка"
        )
        self.assertIn("раббердам", norm3)
        self.assertIn("коффердам", norm3)
        self.assertIn("гипохлорит натрия", norm3)
        self.assertIn("МТА", norm3)
        self.assertIn("депофорез", norm3)
        self.assertIn("эндомотор", norm3)
        self.assertIn("апекслокатор", norm3)
        self.assertIn("эндочак", norm3)
        self.assertIn("ЭОДМ", norm3)
        self.assertIn("биопленка", norm3)

        # 3. Числа зубов по FDI (включая диктовку "зуб 3.6", "зуб 3,6" и "зуб три точка шесть")
        norm4 = blocking_tools.normalize_dental_transcription("болит зуб сорок шесть и зуб один один, а также зуб сорок семь")
        self.assertIn("зуб 46", norm4)
        self.assertIn("зуб 11", norm4)
        self.assertIn("зуб 47", norm4)

        norm4_dot = blocking_tools.normalize_dental_transcription("пациент 35 лет, зуб 3.6 глубокий кариес, зуб 3,7 и зуб три точка шесть")
        self.assertIn("зуб 36", norm4_dot)
        self.assertIn("зуб 37", norm4_dot)

        norm4_order = blocking_tools.normalize_dental_transcription("в области сорок шестого зуба")
        self.assertIn("46", norm4_order)

        # 4. Формулы и концентрации
        norm5 = blocking_tools.normalize_dental_transcription("анестетик один к ста тысячам, силер один к двумстам тысячам, гипохлорит три процента и пять и двадцать пять сотых процента")
        self.assertIn("1:100 000", norm5)
        self.assertIn("1:200 000", norm5)
        self.assertIn("3%", norm5)
        self.assertIn("5.25%", norm5)

    def test_async_spellchecker_guardrails(self):
        """Вектор 2: Асинхронный спеллчекер, отсечение преамбул и срывов LLM."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Сценарий 1: LLM недоступна (error) -> мгновенный фоллбэк на нормализатор
        with patch("blocking_tools.generate_gemini_text_async", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = (None, "503 service unavailable")
            res = loop.run_until_complete(
                blocking_tools.correct_dental_transcription_async("зуб сорок шесть оптибонд фл кафердам")
            )
            self.assertIn("зуб 46", res)
            self.assertIn("OptiBond FL", res)
            self.assertIn("коффердам", res)

        # Сценарий 2: LLM выдала преамбулу "Исправленный текст: ..."
        with patch("blocking_tools.generate_gemini_text_async", new_callable=AsyncMock) as mock_llm:
            mock_resp = MagicMock()
            mock_resp.text = "Исправленный текст: Зуб 46, изоляция коффердам, OptiBond FL."
            mock_llm.return_value = (mock_resp, None)
            res = loop.run_until_complete(
                blocking_tools.correct_dental_transcription_async("зуб сорок шесть коффердам оптибонд фл")
            )
            self.assertFalse(res.lower().startswith("исправленный текст"))
            self.assertIn("Зуб 46", res)

        # Сценарий 3: LLM сорвалась в ответ на вопрос вместо спеллчекера (длина выросла в 4 раза)
        with patch("blocking_tools.generate_gemini_text_async", new_callable=AsyncMock) as mock_llm:
            mock_resp = MagicMock()
            mock_resp.text = (
                "Здравствуйте! В случае если у пациента болит зуб сорок шесть, вам следует провести "
                "рентгенографию, проверить ЭОДМ, провести термопробу, сделать анестезию артикаином 1:100000 "
                "и раскрыть полость зуба под коффердамом с обильной ирригацией гипохлоритом натрия."
            )
            mock_llm.return_value = (mock_resp, None)
            res = loop.run_until_complete(
                blocking_tools.correct_dental_transcription_async("болит зуб сорок шесть что делать")
            )
            # Должен сработать предохранитель и вернуть нормализованный вопрос, а не консультацию
            self.assertIn("зуб 46", res)
            self.assertNotIn("Здравствуйте! В случае", res)

        loop.close()

    def test_silence_and_whisper_hallucinations(self):
        """Вектор 3: Обнаружение тишины и галлюцинаций Whisper."""
        hallucinations = [
            "дима торжок",
            "Dima Torzhok",
            "Субтитры сделал Dima Torzhok",
            "Субтитры создавал DimaTorzok",
            "Редактор субтитров: Дима Торжок. Корректор: Анна Синецкая. Спасибо за просмотр!",
            "Редактор субтитров: DimaTorzok",
            "Субтитры: DimaTorzok",
            "Субтитры делал DimaTorzok",
            "Субтитры подготовил Dimatorzhok специально для сайта",
            "Редактор субтитров: Синецкая",
            "Редактор субтитров А. Синецкая",
            "Продолжение следует...",
            "Спасибо за просмотр!",
            "спасибо за просмотр.",
            "Спасибо за внимание!",
            "благодарю за внимание",
            "подписывайтесь на канал",
            "ставьте лайки",
            "до новых встреч",
            "[тишина]",
            "(тишина)",
            "(шум)",
            "тишина...",
            ".....",
            "да да да да да",
            "ладно ладно ладно ладно",
        ]
        for h in hallucinations:
            self.assertTrue(
                blocking_tools.is_silence_or_whisper_hallucination(h),
                f"Failed to identify silence hallucination: '{h}'"
            )

        # Реальные запросы врача НЕ должны отсекаться
        real_messages = [
            "Зуб 46, деструктивный периодонтит, протокол ирригации",
            "Подскажите дозировку ультракаина ребенку 25 кг",
            "OptiBond FL или AH Plus?",
            "Спасибо, коллега, всё понял",
        ]
        for msg in real_messages:
            self.assertFalse(
                blocking_tools.is_silence_or_whisper_hallucination(msg),
                f"False positive silence filter on: '{msg}'"
            )

    def test_chairside_clinical_dictation_invariants(self):
        """Вектор 1 и 2: Клиническая диктовка у кресла и очистка галлюцинаций."""
        import dental_vocab

        # 1. Проверка клинического кейса у кресла
        raw_case = "Пациент 35 лет, зуб 3.6, глубокий кариес, на холодовую пробу резкая затяжная боль, перкуссия безболезненна. Что делать?"
        self.assertTrue(dental_vocab.has_dental_term(raw_case))
        norm_case = blocking_tools.normalize_dental_transcription(raw_case)
        self.assertIn("зуб 36", norm_case)

        # 2. Вырезание галлюцинации Whisper при наличии реальной речи врача
        mixed_voice = "Пациент 35 лет, зуб 3.6, глубокий кариес. Спасибо за просмотр! Подписывайтесь на канал."
        stripped = blocking_tools.strip_whisper_hallucinations(mixed_voice)
        norm_stripped = blocking_tools.normalize_dental_transcription(stripped)
        self.assertIn("зуб 36", norm_stripped)
        self.assertNotIn("Спасибо за просмотр", norm_stripped)
        self.assertNotIn("Подписывайтесь", norm_stripped)
        self.assertFalse(blocking_tools.is_silence_or_whisper_hallucination(mixed_voice))

        # 3. Наличие терминов в словаре
        self.assertTrue(dental_vocab.has_dental_term("депофорез"))
        self.assertTrue(dental_vocab.has_dental_term("эндочак"))
        self.assertTrue(dental_vocab.has_dental_term("раббердам"))
        self.assertTrue(dental_vocab.has_dental_term("холодовая проба"))
        self.assertTrue(dental_vocab.has_dental_term("к-файл"))
        self.assertTrue(dental_vocab.has_dental_term("h-файл"))
        self.assertTrue(dental_vocab.has_dental_term("c-pilot"))
        self.assertTrue(dental_vocab.has_dental_term("силер"))
        self.assertTrue(dental_vocab.has_dental_term("байпас"))
        self.assertTrue(dental_vocab.has_dental_term("пафф"))

    def test_primary_teeth_and_endo_instruments_and_notes(self):
        """Вектор 1 и 3: Молочные зубы 51-85, эндодонтический жаргон и музыкальные ноты Whisper."""
        # 1. Временный прикус (детская стоматология)
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("пациент 5 лет, зуб 5.4 глубокий кариес"),
            "пациент 5 лет, зуб 54 глубокий кариес"
        )
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("зуб 6,1 пульпит, зуб 7.5 периодонтит, зуб 8.3 свищ"),
            "зуб 61 пульпит, зуб 75 периодонтит, зуб 83 свищ"
        )
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("зуб пять точка четыре и зуб шестьдесят пять"),
            "зуб 54 и зуб 65"
        )
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("зуб восемьдесят пять и зуб семь один"),
            "зуб 85 и зуб 71"
        )
        # 5.8 во временном прикусе не существует (только 1-5)
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("зуб 5.8 некариозное"),
            "зуб 5.8 некариозное"
        )

        # 2. Жаргон и эндодонтические инструменты
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("промыли хлоркой 3% и сделали ирригация хлоркой"),
            "промыли гипохлоритом натрия 3% и сделали ирригация гипохлоритом натрия"
        )
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("прошли к-файл 10, затем k-файлом 15 и h-файл 20"),
            "прошли К-файл 10, затем К-файлом 15 и H-файл 20"
        )
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("использовали c-pilot 08 и си-пилот 10"),
            "использовали C-Pilot 08 и C-Pilot 10"
        )
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("ввели силлер в канал, разогретая гутаперча, паф силлера"),
            "ввели силер в канал, разогретая гуттаперча, пафф силера"
        )
        self.assertEqual(
            blocking_tools.normalize_dental_transcription("выполнили бай-пас ступеньки"),
            "выполнили байпас ступеньки"
        )

        # 3. Музыкальные ноты Whisper при шуме турбины/слюноотсоса
        hallucinations_notes = [
            "♪♪♪",
            "♪ Music ♪",
            "[Music]",
            "(Ударные и музыка)",
            "Subtitles by the Amara.org community",
            "Приятного просмотра!",
            "Приятного вам просмотра",
        ]
        for hn in hallucinations_notes:
            self.assertTrue(
                blocking_tools.is_silence_or_whisper_hallucination(hn),
                f"Failed to identify note/hallucination: '{hn}'"
            )

        speech_notes = "Промыли зуб 36 гипохлоритом ♪♪♪"
        self.assertFalse(blocking_tools.is_silence_or_whisper_hallucination(speech_notes))
        self.assertEqual(
            blocking_tools.strip_whisper_hallucinations(speech_notes),
            "Промыли зуб 36 гипохлоритом"
        )

    def test_media_download_timeout_invariants(self):
        """Вектор 4: Инварианты таймаутов на download_media (потолок 35с, предотвращение зависания очереди)."""
        import assistant
        self.assertLessEqual(assistant.PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS, 35)
        self.assertGreaterEqual(assistant.PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS, 5)
        self.assertLessEqual(media_tools.MEDIA_DOWNLOAD_TIMEOUT_SECONDS, 35.0)

    def test_guaranteed_temp_media_cleanup_on_timeout_and_errors(self):
        """Вектор 5: Гарантированная очистка временных файлов и отсутствие утечек диска."""
        import tempfile
        test_dir = tempfile.mkdtemp(prefix="test_temp_media_")
        orig_dir = media_tools.MEDIA_TEMP_DIR
        try:
            media_tools.MEDIA_TEMP_DIR = test_dir
            # Имитируем:
            # 1. Завершенное скачивание (конкретный путь)
            f_done = os.path.join(test_dir, "pm_999_.jpg")
            with open(f_done, "wb") as f:
                f.write(b"full_image_data")

            # 2. Оборванное по таймауту/ошибке скачивание (Telethon создал файл, но путь не вернул)
            f_part = os.path.join(test_dir, "pm_1000_.part")
            with open(f_part, "wb") as f:
                f.write(b"partial_data")

            # 3. Извлеченный первый кадр из видео (cv2.imwrite)
            f_frame = os.path.join(test_dir, "pm_1001_.mp4.jpg")
            with open(f_frame, "wb") as f:
                f.write(b"frame_data")

            # 4. Посторонний файл, который не должен задеваться
            f_other = os.path.join(test_dir, "other_unrelated_file.txt")
            with open(f_other, "wb") as f:
                f.write(b"keep_me")

            # Проверяем комплексную очистку по путям и префиксам (как в блоке finally)
            removed = media_tools.cleanup_temp_files(
                {f_done},
                ["pm_1000_", "pm_1001_"]
            )
            self.assertEqual(removed, 3)
            self.assertFalse(os.path.exists(f_done))
            self.assertFalse(os.path.exists(f_part))
            self.assertFalse(os.path.exists(f_frame))
            self.assertTrue(os.path.exists(f_other))
        finally:
            media_tools.MEDIA_TEMP_DIR = orig_dir
            for fn in os.listdir(test_dir):
                try:
                    os.remove(os.path.join(test_dir, fn))
                except Exception:
                    pass
            try:
                os.rmdir(test_dir)
            except Exception:
                pass


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestVoiceMediaInquisitor)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print("\n[ВЕРИФИКАЦИЯ УСПЕШНА] Все инварианты аудио, спеллчекера и тишины доказаны на 100%!")
