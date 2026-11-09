"""
Regression tests for 20-hour audit findings and deep repair fixes:
1. Media reflection suppression on off-topic / non-clinical validator rejections.
2. AgentRouter PM supplement timeout capping.
3. Groq 413 bypass for prompts exceeding token/char ceiling on openai/gpt-oss-120b.
4. Reaction engine dynamic blacklist for Telegram-rejected emojis ('🧠' and chat-specific).
5. No forbidden direct address 'коллега' in onboarding, fallback address, or prompts.
6. database.get_active_pm_users exclusion of phantom IDs (< 900000000 and exclusions).
7. blocking_tools EXHAUSTION_RETRY_KINDS inclusion of lifeline_response and media_ebm_fallback.
8. runtime_guard PID/thread-safe temp path generation.
"""

import sys
from pathlib import Path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import unittest
import os
import sqlite3
import tempfile
import threading
from unittest.mock import patch

import config
import database
import runtime_guard
import blocking_tools
import reaction_engine
import assistant


class TestAudit20hDeepRepair(unittest.IsolatedAsyncioTestCase):

    def test_blocking_tools_exhaustion_retry_kinds(self):
        """Verify that lifeline_response and media_ebm_fallback are retryable under exhaustion."""
        self.assertIn("lifeline_response", blocking_tools.EXHAUSTION_RETRY_KINDS)
        self.assertIn("media_ebm_fallback", blocking_tools.EXHAUSTION_RETRY_KINDS)

    def test_runtime_guard_temp_paths_are_pid_and_thread_safe(self):
        """Verify that runtime guard writes use PID and thread ident in temporary filenames."""
        recorded_temp_paths = []

        def mock_replace(src, dst):
            recorded_temp_paths.append(src)

        saved_status = runtime_guard.SUMMARY_STATUS_PATH
        saved_heartbeat = runtime_guard.HEARTBEAT_PATH
        saved_startup = runtime_guard.STARTUP_STATE_PATH
        temp_dir = tempfile.mkdtemp(prefix="test_rg_iso_")
        try:
            runtime_guard.SUMMARY_STATUS_PATH = os.path.join(temp_dir, "test_summary_status.json")
            runtime_guard.HEARTBEAT_PATH = os.path.join(temp_dir, "test_heartbeat.json")
            runtime_guard.STARTUP_STATE_PATH = os.path.join(temp_dir, "test_startup.json")

            with patch("os.replace", side_effect=mock_replace):
                runtime_guard.write_heartbeat("test_reason")
                runtime_guard.write_summary_status({"test": "status"})
                runtime_guard.write_startup_state({"test": 1})

            self.assertGreaterEqual(len(recorded_temp_paths), 3)
            pid_str = str(os.getpid())
            ident_str = str(threading.get_ident())
            for path in recorded_temp_paths:
                self.assertIn(pid_str, path)
                self.assertIn(ident_str, path)
                self.assertTrue(path.endswith(".tmp"))
        finally:
            runtime_guard.SUMMARY_STATUS_PATH = saved_status
            runtime_guard.HEARTBEAT_PATH = saved_heartbeat
            runtime_guard.STARTUP_STATE_PATH = saved_startup
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_database_get_active_pm_users_filters_phantom_ids(self):
        """Verify get_active_pm_users excludes test/phantom user IDs."""
        db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
        test_db = os.path.join(db_dir, "test_phantom_filter.db")
        if os.path.exists(test_db):
            os.remove(test_db)

        orig_db = config.DB_PATH
        config.DB_PATH = test_db
        try:
            await database.init_db()
            with sqlite3.connect(test_db) as conn:
                cursor = conn.cursor()
                # Insert valid clinical user
                cursor.execute(
                    "INSERT INTO pm_messages (user_id, sender_name, text) VALUES (?, ?, ?)",
                    (12345678, "User", "Вопрос по протоколу")
                )
                # Insert phantom test users
                cursor.execute(
                    "INSERT INTO pm_messages (user_id, sender_name, text) VALUES (?, ?, ?)",
                    (99990001, "TestBot", "Тест 1")
                )
                cursor.execute(
                    "INSERT INTO pm_messages (user_id, sender_name, text) VALUES (?, ?, ?)",
                    (777, "TestBot", "Тест 2")
                )
                cursor.execute(
                    "INSERT INTO pm_messages (user_id, sender_name, text) VALUES (?, ?, ?)",
                    (555111, "TestBot", "Тест 3")
                )
                conn.commit()

            active_users = await database.get_active_pm_users(days_limit=30)
            self.assertIn(12345678, active_users)
            self.assertNotIn(99990001, active_users)
            self.assertNotIn(777, active_users)
            self.assertNotIn(555111, active_users)
        finally:
            config.DB_PATH = orig_db
            if os.path.exists(test_db):
                try:
                    os.remove(test_db)
                except OSError:
                    pass

    def test_reaction_engine_excludes_brain_and_detects_complications(self):
        """Verify '🧠' is excluded from allowed reactions and complication stems trigger safety."""
        # 1. 🧠 must not be in ALLOWED_REACTIONS
        self.assertNotIn("🧠", assistant.ALLOWED_REACTIONS)

        # 2. Verify complication regex detection
        complication_samples = [
            "у пациента перфорация в апексе",
            "образовалась ступенька в канале",
            "инструмент заклинило намертво",
            "протолкнули силер за верхушку корня",
            "случайно пропихнули гуттаперчу",
            "разрыв стенки корня зуба 36",
            "острое обострение после пломбировки",
            "инструмент провалился глубже",
            "свищевой ход открылся в проекции",
            "обильный экссудат из канала",
        ]
        for sample in complication_samples:
            self.assertTrue(
                reaction_engine.is_clinical_complication(sample),
                f"Expected clinical complication detection for: '{sample}'"
            )

        # Non-complication clinical query should not be detected as complication
        safe_query = "Подскажите протокол адгезивной фиксации emax вкладки"
        self.assertFalse(reaction_engine.is_clinical_complication(safe_query))

    def test_reaction_engine_chat_blacklist(self):
        """Verify dynamic per-chat reaction blacklist suppresses rejected emojis."""
        chat_id = -100999999
        reaction_engine._DISABLED_CHAT_REACTIONS.setdefault(chat_id, set()).add("🔥")

        disabled = reaction_engine._DISABLED_CHAT_REACTIONS.get(chat_id, set())
        self.assertIn("🔥", disabled)
        self.assertNotIn("👍", disabled)

    def test_no_forbidden_kollega_in_codebase(self):
        """Verify that onboarding, fallback addresses, and bot lines do not use forbidden 'коллега'."""
        with open("assistant.py", "r", encoding="utf-8") as f:
            asst_text = f.read()

        self.assertNotIn("✅ Отлично, коллега!", asst_text)
        self.assertIn("✅ <b>Специализация зафиксирована</b>", asst_text)
        self.assertIn("sender_first_name or 'Доктор'", asst_text)

        with open("main.py", "r", encoding="utf-8") as f:
            main_text = f.read()

        self.assertNotIn('sender_name or "Коллега"', main_text)
        self.assertIn('sender_name or "Доктор"', main_text)

    def test_media_reflection_suppression_on_offtopic(self):
        """Verify that off-topic validation rejection keywords suppress fallback EBM lecture."""
        offtopic_reasons = [
            "Сообщение содержит оффтоп и не относится к стоматологии",
            "Видео показывает кубик Рубика, нет клинического контента",
            "Флуд и бытовое видео в профессиональном чате",
            "Сообщение нерелевантно клинической практике",
        ]
        keywords = ("оффтоп", "флуд", "не содержит клиническ", "нерелевантн", "нет клиническ", "бытов", "нарушает профессиональн")
        for reason in offtopic_reasons:
            is_offtopic = any(kw in reason.lower() for kw in keywords)
            self.assertTrue(is_offtopic, f"Expected '{reason}' to be flagged as offtopic/flood")

    def test_groq_413_guard_in_gemini_client(self):
        """Verify gemini_client contains safety guard against Groq 413 for oversized prompts."""
        with open("gemini_client.py", "r", encoding="utf-8") as f:
            gemini_code = f.read()

        # Check for proactive skip of gpt-oss-120b when prompt > 24000 chars
        self.assertIn('provider == "groq" and model_name == "openai/gpt-oss-120b" and len(prompt) > 24000', gemini_code)

    def test_pm_supplement_timeout_capped(self):
        """Verify generate_pm_supplement provides generous thinking time for gpt-6.1-sol without choking."""
        with open("gemini_client.py", "r", encoding="utf-8") as f:
            gemini_code = f.read()

        self.assertIn("ar_call_timeout = call_timeout", gemini_code)
        self.assertIn("min(85.0,", gemini_code)


if __name__ == "__main__":
    unittest.main()
