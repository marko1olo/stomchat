"""
Тест прогрессивного бэкоффа (30с, 60с, 90с) при вылете всех ключей/моделей для ЛС и группы.
"""
import asyncio
import os
import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import blocking_tools as B

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def run(coro):
    return asyncio.run(coro)


print("\n[1] Определение ошибок исчерпания ключей и моделей (_is_exhaustion_error)")
check("cascade_exhausted в reason считается", B._is_exhaustion_error(None, {"reason": "cascade_exhausted"}))
check("all_keys_on_cooldown в reason считается", B._is_exhaustion_error(None, {"reason": "all_keys_on_cooldown"}))
check("model_overloaded в reason считается", B._is_exhaustion_error(None, {"reason": "model_overloaded"}))
check("503 в тексте ошибки считается", B._is_exhaustion_error("503 Service Unavailable"))
check("429 rate limit в тексте ошибки считается", B._is_exhaustion_error("429 Too Many Requests"))
check("все ключи остывают в тексте ошибки считается", B._is_exhaustion_error("все 7 ключей остывают, ближайший через 69с"))
check("ни одна модель не ответила считается", B._is_exhaustion_error("gemini cascade exhausted: ни одна модель не ответила"))
check("timeout / timed out считается", B._is_exhaustion_error("Request timed out"))
check("обычная синтаксическая ошибка НЕ считается", not B._is_exhaustion_error("ValueError: invalid literal"))
check("пустая ошибка НЕ считается", not B._is_exhaustion_error(None, None))


print("\n[2] Виды работ, подпадающие под повторные шансы (EXHAUSTION_RETRY_KINDS)")
expected_kinds = [
    "pm_chat", "pm_ping", "assistant", "assistant_media",
    "bot_mention_reply", "group_referee", "group_explainer"
]
for k in expected_kinds:
    check(f"вид {k} включен в EXHAUSTION_RETRY_KINDS", k in B.EXHAUSTION_RETRY_KINDS)

check("triage НЕ входит в повторные шансы (быстрый гейт)", "llama_triage" not in B.EXHAUSTION_RETRY_KINDS)
check("group_summary НЕ входит в диалоговые повторные шансы", "group_summary" not in B.EXHAUSTION_RETRY_KINDS)


print("\n[3] Поведение generate_gemini_text_async при вылете ключей (моделирование)")
# Ускоряем задержки для модульного теста: вместо 30, 60, 90 ставим 0.01, 0.02, 0.03
original_delays = B.EXHAUSTION_BACKOFF_DELAYS
B.EXHAUSTION_BACKOFF_DELAYS = (0.01, 0.02, 0.03)
original_interval = B._GEMINI_MIN_INTERVAL_SECONDS
B._GEMINI_MIN_INTERVAL_SECONDS = 0.0

sleep_calls = []
original_sleep = asyncio.sleep

async def fake_sleep(seconds):
    sleep_calls.append(seconds)
    await original_sleep(0.001)

# Тест А: Успех на 2-м шансе (после паузы 30с и 60с)
call_count = 0
async def mock_run_json_tool_recover(action, payload, timeout=None):
    global call_count
    call_count += 1
    if call_count < 3:
        # Первые две попытки падают с исчерпанием ключей
        return {"ok": False, "reason": "cascade_exhausted"}, "gemini cascade exhausted: все ключи остывают"
    # На 3-й попытке (chance 2) ключи остыли и модель ответила
    return {"ok": True, "text": "Клинический ответ после остывания"}, None

B._run_json_tool = mock_run_json_tool_recover
asyncio.sleep = fake_sleep

sleep_calls.clear()
call_count = 0
resp, err = run(B.generate_gemini_text_async("вопрос", {"kind": "pm_chat"}))
check("успешно восстановился после двух пауз", resp is not None and resp.text == "Клинический ответ после остывания")
check("ошибки нет", err is None)
check("было ровно 3 вызова подпроцесса", call_count == 3)
check("были сделаны паузы шансов 1 и 2", len(sleep_calls) == 2 and sleep_calls[0] == 0.01 and sleep_calls[1] == 0.02)


# Тест Б: Все 3 шанса исчерпаны
call_count = 0
sleep_calls.clear()
async def mock_run_json_tool_exhaust(action, payload, timeout=None):
    global call_count
    call_count += 1
    return {"ok": False, "reason": "cascade_exhausted"}, "gemini cascade exhausted: ни одна модель не ответила"

B._run_json_tool = mock_run_json_tool_exhaust
resp, err = run(B.generate_gemini_text_async("вопрос", {"kind": "assistant"}))
check("после всех 3 шансов вернул отказ", resp is None)
check("ошибка сохранена", "cascade exhausted" in err)
check("было ровно 4 вызова (первый + 3 шанса)", call_count == 4)
check("были сделаны все 3 паузы (30, 60, 90)", len(sleep_calls) == 3 and sleep_calls == [0.01, 0.02, 0.03])


# Тест В: Неисчерпаемая ошибка (например, неповторяемый сбой) не тратит паузы
call_count = 0
sleep_calls.clear()
async def mock_run_json_tool_fatal(action, payload, timeout=None):
    global call_count
    call_count += 1
    return {"ok": False, "reason": "bad_json"}, "ValueError: corrupt prompt payload"

B._run_json_tool = mock_run_json_tool_fatal
resp, err = run(B.generate_gemini_text_async("вопрос", {"kind": "pm_chat"}))
check("фатальный сбой сразу выходит", resp is None)
check("паузы не вызывались", len(sleep_calls) == 0)
check("вызов сделан только один", call_count == 1)


# Восстанавливаем оригинальные значения
B.EXHAUSTION_BACKOFF_DELAYS = original_delays
asyncio.sleep = original_sleep

print("\n==============================================================")
print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    sys.exit(1)
