"""
Tests covering all root-cause fixes identified in the 72h log audit (2026-11-05 to 2026-11-08):
1. Defect 1: Playwright chromium fallback cascade in digest_pdf.py.
2. Defect 2: PM deadline enforcement and retry termination before burst timeout.
3. Defect 3: Video file (.mp4) in media message routes to frame extraction instead of PIL prepare_image.
4. Defect 4: Outbox worker auto opt-out for blocked/deactivated users.
5. Defect 5: Batch reactions LLM timeout headroom >= 15.0s.
6. Defect 6: Zero-slash anesthesia calculator returns None for non-specific unweighted queries.
7. Defect 7: User memory coffee and domestic chatter sanitization while preserving clinical terms.
"""
import ast
import inspect
import io
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class TestInquisitorFixes72h(unittest.TestCase):
    """Test suite validating all deep fixes from the 72h log inquisitor audit."""

    def test_digest_pdf_playwright_fallbacks(self):
        """Playwright launch in digest_pdf.py must include chrome and msedge fallback channels."""
        import digest_pdf
        src = inspect.getsource(digest_pdf.generate_digest_pdf)
        self.assertIn("launch_candidates", src)
        self.assertIn('{"channel": "chrome"}', src)
        self.assertIn('{"channel": "msedge"}', src)

    def test_evaluate_batch_sync_timeout_headroom(self):
        """evaluate_batch_sync default timeout must be at least 15.0s to avoid budget exhaustion."""
        import reaction_engine
        sig = inspect.signature(reaction_engine.evaluate_batch_sync)
        timeout_param = sig.parameters.get("timeout")
        self.assertIsNotNone(timeout_param)
        self.assertGreaterEqual(timeout_param.default, 15.0)

    def test_anesthesia_instant_calculator_guards(self):
        """calculate_anesthesia_instant must return None when no drug and no weight are present."""
        import assistant
        self.assertIsNone(assistant.calculate_anesthesia_instant("посчитай анестезию"))
        self.assertIsNone(assistant.calculate_anesthesia_instant("калькулятор доз"))

        # But specific drug with weight returns detailed calculation
        res = assistant.calculate_anesthesia_instant("сколько карпул артикаина на 70 кг")
        self.assertIsNotNone(res)
        self.assertIn("Артикаин 4%", res)

        # And specific drug without weight returns drug guide
        guide = assistant.calculate_anesthesia_instant("дозировка скандонеста")
        self.assertIsNotNone(guide)
        self.assertIn("Мепивакаин 3%", guide)

    def test_user_memory_offtopic_coffee_filter(self):
        """clean_clinician_facts must filter coffee/culinary domestic chatter but preserve clinical equipment."""
        import user_memory
        facts = [
            "Любит пить кофе перед приемом пациентов",
            "Спрашивает про прогноз погоды в Москве",
            "Интересуется рецептом борща и кулинарией",
            "Использует коффердам Sanctuary для изоляции",
            "Применяет ультразвуковые насадки ProUltra при перелечивании",
            "Работает на микроскопе Leica M320 с увеличением 16x",
            "Применяет постоянную обтурацию биокерамическим силером TotalFill BC Sealer",
        ]
        cleaned = user_memory.clean_clinician_facts(facts)

        # Offtopic removed
        self.assertFalse(any("кофе" in f.lower() for f in cleaned))
        self.assertFalse(any("погод" in f.lower() for f in cleaned))
        self.assertFalse(any("борщ" in f.lower() for f in cleaned))

        # Clinical preserved
        self.assertTrue(any("Sanctuary" in f for f in cleaned))
        self.assertTrue(any("ProUltra" in f for f in cleaned))
        self.assertTrue(any("Leica M320" in f for f in cleaned))
        self.assertTrue(any("TotalFill" in f for f in cleaned))

    def test_process_media_message_video_ext_routing(self):
        """process_media_message must route .mp4/.mov files to extract_first_frame_async, not direct image analysis."""
        import main
        src = inspect.getsource(main.process_media_message)
        self.assertIn("is_video_file", src)
        self.assertIn(".mp4", src)
        self.assertIn("extract_first_frame_async", src)

    def test_group_assistant_reflection_scope(self):
        """run_assistant_safe and group reflection must not reference rem_pm_budget."""
        import assistant
        src = inspect.getsource(assistant.check_and_trigger_assistant)
        self.assertNotIn("rem_pm_budget", src)

    def test_pm_deadline_enforcement_in_code(self):
        """handle_private_message must track pm_start_time and PM_MAX_TOTAL_BUDGET."""
        import assistant
        src = inspect.getsource(assistant.handle_private_message)
        self.assertIn("pm_start_time = time.time()", src)
        self.assertIn("PM_MAX_TOTAL_BUDGET", src)
        self.assertIn("rem_pm_budget", src)

    def test_outbox_worker_auto_opt_out_on_blocked_user(self):
        """outbox_worker_task must call set_ping_opt_out when delivery fails with UserIsBlockedError."""
        import main
        src = inspect.getsource(main.outbox_worker_task)
        self.assertIn("userisblocked", src.lower())
        self.assertIn("set_ping_opt_out", src)


if __name__ == "__main__":
    unittest.main()
