import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import runtime_guard

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_agentrouter_test_")
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")

import gemini_client as gc

gc.BANNED_MODELS_FILE = os.path.join(_TMPDIR, "banned_models.json")
gc.KEY_COOLDOWN_FILE = os.path.join(_TMPDIR, "key_cooldowns.json")
gc.MODEL_FAILURES_FILE = os.path.join(_TMPDIR, "model_failures.json")

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))

print("\n[1] AgentRouter: Конфигурация провайдера и заголовки")
check("agentrouter зарегистрирован в PROVIDER_BASE_URLS", "agentrouter" in gc.PROVIDER_BASE_URLS)
check("PROVIDER_KEY_ATTRS связывает agentrouter с AGENTROUTER_KEYS", gc.PROVIDER_KEY_ATTRS.get("agentrouter") == "AGENTROUTER_KEYS")

config.AGENTROUTER_KEYS = ["sk-test-agentrouter-key-12345"]
pool = gc.provider_pool("agentrouter")
check("provider_pool возвращает настроенные ключи", pool == ["sk-test-agentrouter-key-12345"])

# Проверка scrubbing ключа
scrubbed_keys = gc._all_known_keys()
check("AGENTROUTER_KEYS включен в _all_known_keys для очистки логов", "sk-test-agentrouter-key-12345" in scrubbed_keys)

print("\n[2] AgentRouter: Маршрутизация каскада при наличии ключа")
ar_model = getattr(config, "AGENTROUTER_MODEL", "gpt-6.1-sol")
cascade_chat = gc.cascade_for_context({"kind": "pm_chat", "thinking_level": "MEDIUM"})
check(f"В CHAT_KINDS (pm_chat) {ar_model} идет первым", cascade_chat[0] == (ar_model, "agentrouter"))

cascade_assistant = gc.cascade_for_context({"kind": "assistant", "thinking_level": "HIGH"})
check(f"В assistant {ar_model} идет первым", cascade_assistant[0] == (ar_model, "agentrouter"))

cascade_summary = gc.cascade_for_context({"kind": "daily"})
check("В daily сводке Gemini идет первым для длинного контекста", cascade_summary[0][1] == "gemini")
check(f"В daily сводке {ar_model} присутствует как резерв", (ar_model, "agentrouter") in cascade_summary)

cascade_review = gc.cascade_for_context({"kind": "poll_clinical_review"})
check(f"В poll_clinical_review {ar_model} идет первым", cascade_review[0] == (ar_model, "agentrouter"))

cascade_media = gc.cascade_for_context({"kind": "assistant_media", "has_media": True})
check("При наличии медиа (assistant_media) Gemini идет первым", cascade_media[0][1] == "gemini")
check(f"При наличии медиа {ar_model} доступен как резерв", (ar_model, "agentrouter") in cascade_media)

cascade_prompt_media = gc.cascade_for_context({"kind": "pm_chat"}, prompt="[МУЛЬТИМОДАЛЬНОЕ ЗРЕНИЕ: фото зуба 4.6]")
check("Авто-детект медиа ставит Gemini первым", cascade_prompt_media[0][1] == "gemini")
check(f"Авто-детект медиа сохраняет {ar_model} как резерв", (ar_model, "agentrouter") in cascade_prompt_media)

cascade_triage = gc.cascade_for_context({"kind": "llama_triage"})
check(f"В triage {ar_model} присутствует как резерв", (ar_model, "agentrouter") in cascade_triage)

# При отсутствии ключей каскад откатывается на Gemini
config.AGENTROUTER_KEYS = []
cascade_no_key = gc.cascade_for_context({"kind": "pm_chat"})
check("При пустых AGENTROUTER_KEYS agentrouter не вставляется в начало", cascade_no_key[0][1] != "agentrouter")

print("\n[3] AgentRouter: Ошибка 402 Budget pool не банит ключ")
config.AGENTROUTER_KEYS = ["sk-test-agentrouter-key-12345"]
for f in (gc.BANNED_MODELS_FILE, gc.KEY_COOLDOWN_FILE):
    if os.path.exists(f): os.remove(f)

err_402 = "Error code: 402 - {'error': {'message': 'Budget pool quota has been exhausted. Please ask an administrator to increase the limit'}}"
res_failure = gc.note_key_failure("agentrouter", config.AGENTROUTER_KEYS[0], err_402, model_name="gpt-6-astra")

check("402 классифицируется как model_overloaded", res_failure == "model_overloaded")
check("Модель gpt-6-astra забанена", "gpt-6-astra" in gc.get_banned_models())

# Ключ НЕ должен быть на кулдауне
fresh, cooling, _ = gc.available_keys("agentrouter", config.AGENTROUTER_KEYS)
check("Ключ остался активным (не попал под key_rate_limited)", len(fresh) == 1 and not cooling)

print("\n[4] AgentRouter: Генерация с щедрым max_tokens для reasoning")
CAPTURED_REQUESTS = []

class MockHttpResponse:
    status_code = 200
    text = '{"output":[{"type":"message","content":[{"text":"Отличный ответ"}]}]}'
    def json(self):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"text": "Отличный ответ"}]
                }
            ]
        }

import requests
_orig_post = requests.post

def mock_post(url, headers=None, json=None, timeout=None, **kwargs):
    CAPTURED_REQUESTS.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
    return MockHttpResponse()

requests.post = mock_post

# Сброс банов и кулдаунов перед тестом генерации
for f in (gc.BANNED_MODELS_FILE, gc.KEY_COOLDOWN_FILE):
    if os.path.exists(f): os.remove(f)

res = gc.generate_text("Клинический вопрос", status_context={"kind": "assistant"})
requests.post = _orig_post

check("Генерация завершилась успешно", res is not None and res.text == "Отличный ответ")
check(f"Запрос был отправлен к {ar_model}", len(CAPTURED_REQUESTS) > 0 and CAPTURED_REQUESTS[0].get("json", {}).get("model") == ar_model)
check("Таймаут запроса соответствует архитектурному запасу (>= 30с)", len(CAPTURED_REQUESTS) > 0 and (CAPTURED_REQUESTS[0].get("timeout") or 0) >= 30.0)

if __name__ == "__main__":
    print("\n==============================================================")
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print(f"Провалено: {', '.join(FAIL)}")
        sys.exit(1)
    sys.exit(0)
