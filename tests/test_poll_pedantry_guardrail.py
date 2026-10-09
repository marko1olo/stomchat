"""
Unit tests for anti-pedantry guardrails in poll_engine.py.
Verifies that poll generator prompts, reviewer criteria, and weekly rubricator
strictly avoid metallurgical file trivia, 3Y/5Y zirconium crystal phase pedantry,
and micron/millimeter catching in favor of real chairside clinical dilemmas.
"""
import inspect
import unittest
import poll_engine
from poll_engine import WEEKLY_RUBRICATOR


class TestPollPedantryGuardrail(unittest.TestCase):
    """Test guardrails against metallurgical and millimeter pedantry in polls."""

    def test_rubricator_has_no_alloy_or_micron_pedantry(self):
        """Weekly rubricator must not contain factory metallurgy or micron trivia."""
        forbidden_terms = [
            "m-wire", "cm-wire", "аустенит", "мартенсит", "grade 4", "grade 5",
            "3y-tzp", "5y", "молярн", "кубическ", "1.5–2 мм", "3 мм под углом 90°"
        ]
        for day_idx, day_info in WEEKLY_RUBRICATOR.items():
            topic_text = (day_info.get("topic", "") + " " + " ".join(day_info.get("subtopics", []))).lower()
            for term in forbidden_terms:
                self.assertNotIn(
                    term,
                    topic_text,
                    f"Day {day_idx} ({day_info.get('day_name')}) contains forbidden pedantic term: '{term}'"
                )

    def test_generator_prompt_forbids_pedantry(self):
        """generate_poll_content prompt must strictly forbid metallurgy, 3Y zirconium, and millimeter catching."""
        source = inspect.getsource(poll_engine.generate_poll_content)
        self.assertIn("СТРОЖАЙШИЙ ЗАПРЕТ: МАТЕРИАЛОВЕДЧЕСКАЯ ЗУБРЕЖКА", source)
        self.assertIn("ЗАПРЕЩЕНО тестировать знание металлургии", source)
        self.assertIn("ЗАПРЕЩЕНО заставлять угадывать поколения и фазы диоксида циркония", source)
        self.assertIn("ЗАПРЕЩЕНО заставлять зубрить миллиметры и микроны", source)
        self.assertIn("ГЛАВНЫЙ ФОКУС: ПРАКТИЧЕСКИЕ КЛИНИЧЕСКИЕ РАЗВИЛКИ У КРЕСЛА", source)
        self.assertNotIn("[Ключевой локальный параметр в мм / Нсм / ЭОД]", source)

    def test_reviewer_criterion_12_present(self):
        """review_poll_quality must evaluate Criterion 12 for anti-pedantry guardrail."""
        source = inspect.getsource(poll_engine.review_poll_quality)
        self.assertIn("12 универсальным критериям", source)
        self.assertIn("12. ЗАПРЕТ НА МАТЕРИАЛОВЕДЧЕСКУЮ ЗУБРЕЖКУ И МИЛЛИМЕТРОВЫЙ ФАНАТИЗМ", source)
        self.assertIn("3Y-TZP", source)
        self.assertIn("M-Wire", source)


if __name__ == "__main__":
    unittest.main()
