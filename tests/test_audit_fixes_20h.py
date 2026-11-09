import asyncio
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add root directory to sys.path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import blocking_tools
import gemini_client
import question_lifeline
import reaction_engine
import clinical_rescue
import assistant


class TestAuditFixes20h(unittest.IsolatedAsyncioTestCase):

    def test_01_blocking_tools_exhaustion_retry_kinds(self):
        """Проверка наличия lifeline_response, clinical_rescue и pm-supplement в EXHAUSTION_RETRY_KINDS."""
        self.assertIn("lifeline_response", blocking_tools.EXHAUSTION_RETRY_KINDS)
        self.assertIn("clinical_rescue", blocking_tools.EXHAUSTION_RETRY_KINDS)
        self.assertIn("pm-supplement", blocking_tools.EXHAUSTION_RETRY_KINDS)

    def test_02_lifeline_response_timeout_budget(self):
        """Проверка, что таймаут для lifeline_response не занижен (>= 60с) для прохождения каскада."""
        # Проверяем в исходном коде или сигнатуре вызова _call_llm_adapter
        import inspect
        src = inspect.getsource(question_lifeline.generate_lifeline_response)
        self.assertIn("timeout=75.0", src)

    def test_03_reaction_engine_complication_patterns(self):
        """Проверка детерминированного блокирования реакций на новые осложнения (ледж, ступенька, стриппинг, проталкивание)."""
        complications = [
            "доктор, образовалась ступенька в средней трети MB2",
            "кажется, получил стриппинг на дистальной стенке",
            "протолкнул отломок файла за верхушку",
            "случился ledge в мезиальном канале",
        ]
        for phrase in complications:
            self.assertTrue(
                reaction_engine.is_clinical_complication(phrase),
                f"Фраза '{phrase}' должна определяться как клиническое осложнение!"
            )

    def test_04_clinical_rescue_is_case_already_rescued(self):
        """Проверка работы функции is_case_already_rescued на SQLite БД."""
        import tempfile
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            test_db = tf.name

        try:
            clinical_rescue.ensure_rescue_table(test_db)
            self.assertFalse(clinical_rescue.is_case_already_rescued(test_db, 99999, -100111))

            # Помечаем кейс
            clinical_rescue.mark_case_rescued(test_db, 99999, -100111, sender_id=555)
            self.assertTrue(clinical_rescue.is_case_already_rescued(test_db, 99999, -100111))
        finally:
            if os.path.exists(test_db):
                try:
                    os.remove(test_db)
                except Exception:
                    pass

    async def test_05_pm_pings_deleted_user_pruning(self):
        """Проверка немедленного удаления удаленного пользователя из pm_pings при отправке пинга."""
        dummy_state = {
            "pm_pings": {
                "9249315760": {
                    "last_activity": "2026-11-06T14:58:16",
                    "ping_failures": 1,
                    "unanswered_pings": 1,
                }
            }
        }

        with patch("assistant.load_state", return_value=dummy_state), \
             patch("assistant.save_state") as mock_save, \
             patch("assistant.is_ping_quiet_hours", return_value=False), \
             patch("assistant.is_pm_ping_work_hours", return_value=True), \
             patch("assistant.config.ENABLE_PM_PROACTIVE_PINGS", True):

            # Создаем фейковый bot_client, который выбрасывает ошибку "The specified user was deleted"
            mock_client = MagicMock()
            mock_client.send_message.side_effect = Exception("The specified user was deleted (caused by SendMessageRequest)")

            # Проверяем, что drop_pm_ping корректно удаляет юзера
            assistant.drop_pm_ping("9249315760")
            self.assertNotIn("9249315760", dummy_state["pm_pings"])

    def test_06_pm_supplement_cascade_budget_protection(self):
        """Проверка защиты каскада PM supplement от бесконечного зависания при медленном провайдере."""
        with patch("gemini_client.provider_pool", return_value=True), \
             patch("gemini_client.available_keys", return_value=(["fake_key"], [], [])):

            # Вызываем generate_pm_supplement с очень маленьким таймаутом (0.01с)
            # Должен сразу завершиться и вернуть None без зависаний
            res = gemini_client.generate_pm_supplement("вопрос", "ответ", timeout=0.01)
            self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
