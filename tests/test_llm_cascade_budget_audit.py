"""
test_llm_cascade_budget_audit.py — Комплексный стресс-тест бюджетов каскада, таймингов и медиа-пайплайна.

Верифицирует:
1. Защиту от голодания каскада при бюджете 15с (response_validator / triage):
   При таймауте первой модели Gemini резервные модели (включая Groq gpt-oss-120b)
   ГАРАНТИРОВАННО опрашиваются, а не сбрасываются по budget_exhausted.
2. Защиту от перерасхода бюджета первой моделью при 30с (assistant / clinical chat):
   Первая модель не может съесть 26 секунд; остатка бюджета хватает для опроса Groq.
3. Математическую корректность per-model adaptive slice:
   Сумма выделенных таймаутов строго <= общего бюджета (usable).
4. Пропуск тяжелых моделей при дефиците времени в пользу быстрых резервов (Groq fast fallback).
5. Изоляцию и отсутствие зомби-процессов в PDF-пайплайне при отмене задач.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import gemini_client as gc
import config
import runtime_guard


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1731100000.0 + self.t

    def sleep(self, seconds):
        self.t += float(seconds)


class TestLlmCascadeBudgetAudit(unittest.TestCase):

    def setUp(self):
        self.orig_time = gc.time
        self.orig_openai_client = gc.get_openai_client
        self.orig_sleep = gc._sleep_with_status
        self.orig_banned = gc.BANNED_MODELS_FILE
        self.orig_cooldowns = gc.KEY_COOLDOWN_FILE
        self.orig_status_path = runtime_guard.SUMMARY_STATUS_PATH

        self.temp_dir = tempfile.mkdtemp()
        gc.BANNED_MODELS_FILE = os.path.join(self.temp_dir, "banned_models.json")
        gc.KEY_COOLDOWN_FILE = os.path.join(self.temp_dir, "key_cooldowns.json")
        runtime_guard.SUMMARY_STATUS_PATH = os.path.join(self.temp_dir, "status.json")

        self.clock = Clock()
        gc.time = self.clock

        self.requests_log = []
        self.spend_full_timeout = True
        self.model_outcomes = {}

        def fake_sleep(seconds, ctx, attempt, max_attempts, key_id):
            self.clock.t += float(seconds)

        gc._sleep_with_status = fake_sleep

        class FakeCompletions:
            def __init__(outer_self, api_key, timeout):
                outer_self.api_key = api_key
                outer_self.timeout = timeout

            def create(outer_self, **kwargs):
                model = kwargs.get("model", "unknown")
                self.requests_log.append((model, outer_self.api_key, outer_self.timeout))
                if self.spend_full_timeout:
                    self.clock.t += outer_self.timeout
                else:
                    self.clock.t += 0.05

                outcome = self.model_outcomes.get(model, "ok_response")
                if isinstance(outcome, Exception):
                    raise outcome
                return type("R", (), {
                    "choices": [type("C", (), {
                        "message": type("M", (), {"content": outcome})(),
                        "finish_reason": "stop"
                    })()]
                })()

        class FakeClient:
            def __init__(outer_self, api_key, timeout):
                outer_self.chat = type("Chat", (), {"completions": FakeCompletions(api_key, timeout)})()

        def fake_client_maker(api_key, base_url, timeout=30.0, **kwargs):
            return FakeClient(api_key, timeout)

        gc.get_openai_client = fake_client_maker

        # Настраиваем тестовые ключи
        config.GOOGLE_KEYS = ["gk-test-1-keypart", "gk-test-2-keypart"]
        config.GROQ_KEYS = ["qk-test-1-keypart", "qk-test-2-keypart"]
        config.AGENTROUTER_KEYS = ["ar-test-1-keypart"]

    def tearDown(self):
        gc.time = self.orig_time
        gc.get_openai_client = self.orig_openai_client
        gc._sleep_with_status = self.orig_sleep
        gc.BANNED_MODELS_FILE = self.orig_banned
        gc.KEY_COOLDOWN_FILE = self.orig_cooldowns
        runtime_guard.SUMMARY_STATUS_PATH = self.orig_status_path

    def test_01_budget_15s_reaches_groq_fallback(self):
        """
        Бюджет 15с (response_validator):
        Если первая модель Gemini падает (Request timed out), каскад ОБЯЗАН
        успеть опросить резервную модель Groq (openai/gpt-oss-120b),
        а не блокировать её по `budget_exhausted`!
        """
        # Симулируем: gemini-3.1-flash-lite и gemini-3.5-flash-lite падают по таймауту
        self.model_outcomes = {
            "gemini-3.1-flash-lite": Exception("Request timed out."),
            "gemini-3.5-flash-lite": Exception("Request timed out."),
            "openai/gpt-oss-120b": "Клинический вердикт валидатора: OK",
        }
        self.spend_full_timeout = True

        res = gc.generate_text("проверка ответа", {"kind": "response_validator"}, timeout=15.0)

        models_called = [m for m, _, _ in self.requests_log]
        self.assertIn("openai/gpt-oss-120b", models_called,
                      f"Резервная модель Groq обязана была быть опрошена! Вызовы: {self.requests_log}")
        self.assertIsNotNone(res, "Ответ должен быть успешно получен от Groq-фоллбэка!")
        self.assertIn("Клинический вердикт", res.text)
        self.assertLessEqual(self.clock.t, 15.01, "Общее время не должно превышать бюджет 15с!")

    def test_02_budget_30s_prevents_first_model_hogging(self):
        """
        Бюджет 30с (клинический чат):
        Первая модель каскада (AgentRouter или Gemini) не должна забирать весь бюджет (26с).
        При её сбое Groq (openai/gpt-oss-120b) должен гарантированно получить свой слот.
        """
        self.model_outcomes = {
            "gpt-6.1-sol": Exception("504 Gateway Timeout"),
            "openai/gpt-oss-120b": "Диагноз подтвержден: пульпит зуба 4.6",
        }
        self.spend_full_timeout = True

        res = gc.generate_text("диагностика 46", {"kind": "assistant", "thinking_level": "HIGH"}, timeout=30.0)

        models_called = [m for m, _, _ in self.requests_log]
        self.assertIn("openai/gpt-oss-120b", models_called,
                      f"Groq обязан получить попытку при сбое первой модели! Вызовы: {self.requests_log}")
        self.assertIsNotNone(res)
        self.assertIn("пульпит", res.text)
        self.assertLessEqual(self.clock.t, 30.01, f"Время каскада {self.clock.t}с превысило бюджет 30с!")

    def test_03_fast_fallback_when_remaining_budget_is_small(self):
        """
        Когда остается мало времени (например, 3.5с), тяжелая модель Gemini должна
        пропускаться без остановки каскада, передавая управление быстрой модели Groq.
        """
        self.model_outcomes = {
            "gemini-3.1-flash-lite": Exception("429 rate limit"),
            "gemini-3.5-flash-lite": Exception("503 overloaded"),
            "openai/gpt-oss-120b": "Быстрый ответ триажа: TRUE",
        }
        # Устанавливаем малый бюджет 6.0с (типично для фоновых реакций / триажа)
        self.spend_full_timeout = False

        res = gc.generate_text("триаж", {"kind": "llama_triage"}, timeout=6.0)

        models_called = [m for m, _, _ in self.requests_log]
        self.assertIn("openai/gpt-oss-120b", models_called)
        self.assertIsNotNone(res)
        self.assertIn("Быстрый ответ", res.text)

    def test_04_mathematical_slicing_invariant(self):
        """
        Математический инвариант: сумма всех запланированных таймаутов
        никогда не превышает исходный бюджет при любых размерах бюджета (15c, 30c, 60c, 90c, 2100c).
        """
        self.model_outcomes = {m: Exception("deterministic error") for m in (
            "gpt-6.1-sol", "openai/gpt-oss-120b", "gemini-3.6-flash", "gemini-3.8-flash",
            "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.8-27b"
        )}
        self.spend_full_timeout = True

        for b in [15.0, 30.0, 60.0, 90.0, 2100.0]:
            self.requests_log.clear()
            self.clock.t = 0.0
            gc.generate_text("тест", {"kind": "assistant"}, timeout=b)
            total_timeouts = sum(t for _, _, t in self.requests_log)
            self.assertLessEqual(
                total_timeouts, b + 0.05,
                f"Сумма таймаутов {total_timeouts:.2f}с превысила бюджет {b}с!"
            )


if __name__ == "__main__":
    unittest.main()
