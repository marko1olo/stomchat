import unittest
import os
import sys
import time
import inspect

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

sys.stdout.reconfigure(encoding='utf-8')

import gemini_client
import config

class TestLLMLoadBalanceInquisitor(unittest.TestCase):
    """
    Беспощадный Red Team тест инвариантов балансировки нагрузки, квот и каскадов LLM.
    """

    def test_state_files_are_absolute_paths(self):
        """Инвариант 1: Файлы кулдаунов и банов обязаны иметь абсолютные пути."""
        self.assertTrue(os.path.isabs(gemini_client.BANNED_MODELS_FILE))
        self.assertTrue(os.path.isabs(gemini_client.KEY_COOLDOWN_FILE))
        self.assertTrue(os.path.isabs(gemini_client.MODEL_FAILURES_FILE))

    def test_media_cascade_prioritizes_fast_fallbacks(self):
        """
        Инвариант 2: Для медиа-задач после gemini-3.8-flash ОБЯЗАНЫ идти быстрые
        альтернативы (gemini-3.5-flash-lite и deepseek-v4-flash), исключая
        зависание в череде 6 медленных моделей подряд.
        """
        cascade = gemini_client.cascade_for_context(
            status_context={"kind": "assistant_media", "has_media": True},
            prompt="[МУЛЬТИМОДАЛЬНОЕ ЗРЕНИЕ] Оцени снимок зуба 1.2"
        )
        self.assertGreaterEqual(len(cascade), 3)
        self.assertEqual(cascade[0][0], "gemini-3.8-flash")
        self.assertEqual(cascade[1][0], "gemini-3.5-flash-lite")
        # Если AgentRouter настроен, 3-й должна быть deepseek-v4-flash
        has_ar = bool(gemini_client.provider_pool("agentrouter"))
        if has_ar:
            self.assertEqual(cascade[2], ("deepseek-v4-flash", "agentrouter"))

    def test_chairside_chat_timeout_ceilings(self):
        """
        Инвариант 3: Потолок таймаута одного HTTP-запроса для интерактивного чата
        не должен превышать 50с при HIGH и 35с при MEDIUM, предотвращая 110-секундные зависания.
        """
        src = inspect.getsource(gemini_client.generate_text)
        self.assertIn("chat_max = 50.0 if thinking_level == \"HIGH\" else 35.0", src)
        self.assertIn("chat_ceiling = 50.0 if thinking_level == \"HIGH\" else 35.0", src)
        self.assertNotIn("chat_max = 240.0", src)
        self.assertNotIn("chat_ceiling = 240.0", src)

    def test_rate_limit_cooldown_duration_and_sleep(self):
        """
        Инвариант 4: Ротация ключей при 429 обязана соблюдать паузу не менее 2.5с.
        """
        src = inspect.getsource(gemini_client.generate_text)
        self.assertIn("rate_sleep = 2.5", src)
        whisper_src = inspect.getsource(gemini_client.transcribe_audio_bytes_or_file)
        self.assertIn("rate_delay = 2.5", whisper_src)
        # Multimodal STT cooldown & sleep check
        stt_src = inspect.getsource(gemini_client.transcribe_audio_gemini_multimodal)
        self.assertIn("set_key_cooldown(\"gemini\", gkey", stt_src)
        self.assertIn("await _asyncio.sleep(2.5)", stt_src)

    def test_key_cooldown_registry_clearing_and_setting(self):
        """Инвариант 5: Проверка записи и снятия кулдауна с ключа."""
        test_key = "test_inquisitor_key_12345"
        provider = "gemini"
        
        # Ставим кулдаун
        gemini_client.set_key_cooldown(provider, test_key, seconds=60)
        cooldowns = gemini_client.get_key_cooldowns()
        fp = gemini_client._key_fingerprint(provider, test_key)
        self.assertIn(fp, cooldowns)
        self.assertGreater(cooldowns[fp], time.time())
        
        # Снимаем по успеху
        gemini_client.note_success(provider, test_key)
        cooldowns_after = gemini_client.get_key_cooldowns()
        self.assertNotIn(fp, cooldowns_after)

    def test_strip_reasoning_edge_cases(self):
        """Инвариант 6: Полная срезка рассуждений (think/thought) без утечек в ответ врачу."""
        text1 = "<think>Модель думает о зубе 1.2</think>Диагноз: ECR 3 класса."
        self.assertEqual(gemini_client.strip_reasoning(text1), "Диагноз: ECR 3 класса.")
        
        # Незакрытый тег (обрыв токенов)
        text2 = "Черновик ответа.<thought>Размышления которые не закрылись"
        self.assertEqual(gemini_client.strip_reasoning(text2), "Черновик ответа.")
        
        # Только размышления -> пустая строка
        text3 = "<think>Только размышления</think>"
        self.assertEqual(gemini_client.strip_reasoning(text3), "")

if __name__ == "__main__":
    unittest.main()
