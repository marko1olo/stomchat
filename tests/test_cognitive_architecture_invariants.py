import unittest
import clinical_cognitive_core
import assistant

class TestCognitiveArchitectureInvariants(unittest.TestCase):
    def test_first_principles_and_biomechanics(self):
        master = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        runtime = clinical_cognitive_core.RUNTIME_COGNITIVE_CORE
        
        # Biomechanical & material constants
        self.assertIn("E-modulus", master)
        self.assertIn("18 ГПа", master)
        self.assertIn("110 ГПа", master)
        self.assertIn("210 ГПа", master)
        self.assertIn("C-фактор", master)
        self.assertIn("MMP", master)
        
        # Also in runtime core
        self.assertIn("E-modulus", runtime)
        self.assertIn("C-фактор", runtime)
        self.assertIn("MMP", runtime)

    def test_blind_spots_and_biases_radar(self):
        master = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        runtime = clinical_cognitive_core.RUNTIME_COGNITIVE_CORE
        
        self.assertIn("Туннельное зрение", master)
        self.assertIn("Sunk Cost Fallacy", master)
        self.assertIn("Confirmation Bias", master)
        
        self.assertIn("Туннельное зрение", runtime)
        self.assertIn("Sunk Cost Fallacy", runtime)
        self.assertIn("Предвзятость подтверждения", runtime)

    def test_multi_order_consequences_horizons(self):
        master = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        runtime = clinical_cognitive_core.RUNTIME_COGNITIVE_CORE
        
        self.assertIn("6 месяцев", master)
        self.assertIn("2 года", master)
        self.assertIn("5 лет", master)
        self.assertIn("VRF", master)
        
        self.assertIn("6 мес", runtime)
        self.assertIn("2 года", runtime)
        self.assertIn("5 лет", runtime)

    def test_assumption_audit_and_paradigm_demolition(self):
        master = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        runtime = clinical_cognitive_core.RUNTIME_COGNITIVE_CORE
        
        self.assertIn("ASSUMPTION AUDIT", master)
        self.assertIn("штифт без феррула", master.lower())
        self.assertIn("радикулярную кисту", master.lower())
        
        self.assertIn("Assumption Demolition", runtime)

    def test_proactive_vector_recalibration(self):
        master = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        self.assertIn("PROACTIVE VECTOR RECALIBRATION", master)
        self.assertIn("Вектор А", master)
        self.assertIn("Вектор Б", master)

    def test_strict_anti_interrogation_guard(self):
        master = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
        runtime = clinical_cognitive_core.RUNTIME_COGNITIVE_CORE
        
        self.assertIn("ANTI-INTERROGATION GUARD", master)
        self.assertIn("98%", master)
        self.assertIn("0 встречных вопросов", master)
        
        self.assertIn("ANTI-INTERROGATION", runtime)
        self.assertIn("0 встречных вопросов", runtime)

    def test_pm_ping_prompt_anti_quiz(self):
        import inspect
        src = inspect.getsource(assistant.check_and_send_pm_pings)
        # Check that old quiz question is eradicated
        self.assertNotIn("один конкретный вопрос по исходу", src)
        self.assertIn("БЕЗ экзаменующих вопросов и школярских тестов", src)

if __name__ == "__main__":
    unittest.main()
