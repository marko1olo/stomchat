"""
Тесты сохранения щедрых таймаутов для рассуждающих моделей (gpt-6.2-sol / AgentRouter).

Проверяет:
1. compute_call_timeout для agentrouter при бюджетах 300с и 480с возвращает >= 280с.
2. Отсутствие деления времени первой попытки на число ключей (attempts_planned) для agentrouter.
3. Дефолтные таймауты get_openai_client и get_provider_client >= 480.0с.
4. Отсутствие урезанных таймаутов (< 120с) на клинических разборах и ретраях в assistant.py.
"""
import inspect
import os
import re
import sys
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import gemini_client
import assistant


def test_agentrouter_compute_call_timeout_budget_300():
    """При бюджете 300с gpt-6.2-sol получает >= 280с (не режется до 180с или 90с)."""
    active_cascade = [
        ("openai/gpt-6.2-sol", "agentrouter"),
        ("llama-3.3-70b-versatile", "groq"),
    ]
    t = gemini_client.compute_call_timeout(
        provider="agentrouter",
        model_name="openai/gpt-6.2-sol",
        model_index=0,
        active_cascade=active_cascade,
        remaining_time=300.0,
        timeout=300.0,
        is_triage=False,
        is_chatbot=True,
        thinking_level="HIGH",
        summary_kinds=frozenset(),
        kind="pm_chat",
        attempt=0,
        attempts_planned=2,  # 2 ключа в пуле!
    )
    assert t >= 280.0, f"Expected >= 280.0s for 300s budget, got {t}s (artificially throttled!)"
    assert t <= 300.0


def test_agentrouter_compute_call_timeout_budget_480():
    """При бюджете 480с gpt-6.2-sol получает щедрый таймаут >= 280с (до 465-480с)."""
    active_cascade = [
        ("openai/gpt-6.2-sol", "agentrouter"),
        ("llama-3.3-70b-versatile", "groq"),
    ]
    t = gemini_client.compute_call_timeout(
        provider="agentrouter",
        model_name="openai/gpt-6.2-sol",
        model_index=0,
        active_cascade=active_cascade,
        remaining_time=480.0,
        timeout=480.0,
        is_triage=False,
        is_chatbot=True,
        thinking_level="HIGH",
        summary_kinds=frozenset(),
        kind="pm_chat",
        attempt=0,
        attempts_planned=2,
    )
    assert t >= 280.0, f"Expected >= 280.0s for 480s budget, got {t}s"
    assert t >= 450.0, f"Expected >= 450.0s for 480s budget, got {t}s"


def test_agentrouter_attempts_division_eliminated():
    """Наличие нескольких ключей в пуле AgentRouter не делит таймаут первой попытки пополам."""
    active_cascade = [
        ("openai/gpt-6.2-sol", "agentrouter"),
        ("llama-3.3-70b-versatile", "groq"),
    ]
    t_1key = gemini_client.compute_call_timeout(
        provider="agentrouter",
        model_name="openai/gpt-6.2-sol",
        model_index=0,
        active_cascade=active_cascade,
        remaining_time=300.0,
        timeout=300.0,
        is_triage=False,
        is_chatbot=True,
        thinking_level="HIGH",
        summary_kinds=frozenset(),
        kind="pm_chat",
        attempt=0,
        attempts_planned=1,
    )
    t_3keys = gemini_client.compute_call_timeout(
        provider="agentrouter",
        model_name="openai/gpt-6.2-sol",
        model_index=0,
        active_cascade=active_cascade,
        remaining_time=300.0,
        timeout=300.0,
        is_triage=False,
        is_chatbot=True,
        thinking_level="HIGH",
        summary_kinds=frozenset(),
        kind="pm_chat",
        attempt=0,
        attempts_planned=3,
    )
    # При 3 ключах таймаут первой попытки не должен резаться до 300 / 3 = 100с
    assert t_3keys == t_1key, f"Timeout with 3 keys ({t_3keys}s) must equal 1 key ({t_1key}s) for agentrouter!"
    assert t_3keys >= 280.0


def test_default_client_timeouts():
    """Дефолтные таймауты get_openai_client и get_provider_client должны быть >= 480.0с."""
    sig_oai = inspect.signature(gemini_client.get_openai_client)
    assert sig_oai.parameters["timeout"].default >= 480.0, (
        f"get_openai_client default timeout is {sig_oai.parameters['timeout'].default}, expected >= 480.0"
    )

    sig_prov = inspect.signature(gemini_client.get_provider_client)
    assert sig_prov.parameters["timeout"].default >= 480.0, (
        f"get_provider_client default timeout is {sig_prov.parameters['timeout'].default}, expected >= 480.0"
    )


def test_assistant_clinical_timeouts():
    """Проверяет отсутствие урезанных таймаутов (< 120с) на клинических разборах и ретраях в assistant.py."""
    assistant_path = os.path.join(_REPO_ROOT, "assistant.py")
    with open(assistant_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Рецензентский ретрай (строка ~6600): timeout=300.0 (не 45.0)
    assert "timeout=45.0" not in content, "Found obsolete timeout=45.0 in assistant.py!"
    assert re.search(r"generate_gemini_text_async\(current_prompt,\s*status_ctx,\s*timeout=300\.0\)", content), (
        "Clinical reviewer retry must have timeout=300.0"
    )

    # 2. EBM фоллбек медиа (строка ~7475): timeout=180.0 (не 60)
    assert re.search(r"generate_gemini_text_async\(ebm_prompt,\s*ebm_ctx,\s*timeout=180\.0\)", content), (
        "EBM media fallback must have timeout=180.0"
    )

    # 3. PM бюджет (строка ~14453): PM_MAX_TOTAL_BUDGET = 650.0, attempt_timeout до 650.0
    assert "PM_MAX_TOTAL_BUDGET = 650.0" in content, "PM_MAX_TOTAL_BUDGET must be 650.0"
    assert re.search(r"attempt_timeout\s*=\s*max\(60\.0,\s*min\(rem_pm_budget\s*-\s*15\.0,\s*650\.0\)\)", content), (
        "attempt_timeout ceiling must be 650.0"
    )

    # 4. Консилиум ретрай (строка ~15219): timeout=240.0 (не 60)
    assert re.search(r"generate_gemini_text_async\(cons_prompt,\s*status_ctx,\s*timeout=240\.0\)", content), (
        "Consilium reviewer retry must have timeout=240.0"
    )

    # 5. Экспертный пинг (строка ~20193): timeout=180.0 (не 90)
    assert re.search(r"generate_gemini_text_async\(expert_prompt,\s*status_ctx,\s*timeout=180\.0\)", content), (
        "Expert PM ping must have timeout=180.0"
    )


def test_agentrouter_compute_call_timeout_budget_650():
    """При максимальном бюджете 650с gpt-6.2-sol сохраняет щедрый таймаут >= 600с (до 650с)."""
    active_cascade = [
        ("openai/gpt-6.2-sol", "agentrouter"),
        ("llama-3.3-70b-versatile", "groq"),
    ]
    t = gemini_client.compute_call_timeout(
        provider="agentrouter",
        model_name="openai/gpt-6.2-sol",
        model_index=0,
        active_cascade=active_cascade,
        remaining_time=650.0,
        timeout=650.0,
        is_triage=False,
        is_chatbot=True,
        thinking_level="HIGH",
        summary_kinds=frozenset(),
        kind="pm_chat",
        attempt=0,
        attempts_planned=2,
    )
    assert t >= 600.0, f"Expected >= 600.0s for 650s budget, got {t}s"
    assert t <= 650.0
