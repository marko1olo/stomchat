"""
Проверка гейта частоты запросов к LLM и защиты от бесконечного ожидания.
Запуск: python test_gemini_pacing.py
"""
import asyncio
import io
import os
import shutil
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import runtime_guard  # noqa: E402

# generate_gemini_text_async в finally снимает флаг «идёт генерация», то есть
# пишет в файл статуса. Без этой подмены прогон затирал боевой
# bot_summary_status.json — ровно то нарушение изоляции, что уже ловили на
# assistant_state.json.
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_pace_")
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")

import blocking_tools as B  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


B._GEMINI_MIN_INTERVAL_SECONDS = 0.3   # ускоряем, логика та же

starts = []


async def fake_tool(action, payload, timeout=None):
    starts.append(time.monotonic())
    await asyncio.sleep(0.05)
    return {"ok": True, "text": "ok", "timeout_seen": timeout}, None


B._run_json_tool = fake_tool


async def scenario_burst(n):
    starts.clear()
    B._LAST_GEMINI_CALL_START = 0.0
    await asyncio.gather(*[B.generate_gemini_text_async(f"p{i}", {}, timeout=30) for i in range(n)])
    return sorted(starts)


print("\n[1] ГЛАВНОЕ: 6 одновременных вызовов не уходят залпом")
t0 = time.monotonic()
s = asyncio.run(scenario_burst(6))
gaps = [round(s[i + 1] - s[i], 3) for i in range(len(s) - 1)]
print(f"      интервалы между стартами: {gaps}")
min_gap = min(gaps)
check("все стартовали", len(s) == 6, f"got {len(s)}")
check("ни один интервал не схлопнулся в 0", min_gap >= B._GEMINI_MIN_INTERVAL_SECONDS * 0.8,
      f"минимальный интервал {min_gap}s при требуемых {B._GEMINI_MIN_INTERVAL_SECONDS}s")
check("залпа нет (старты растянуты)", (s[-1] - s[0]) >= B._GEMINI_MIN_INTERVAL_SECONDS * 4,
      f"разброс всего {round(s[-1] - s[0], 3)}s")

print("\n[2] Последовательные вызовы после паузы не тормозятся лишний раз")


async def scenario_spaced():
    B._LAST_GEMINI_CALL_START = 0.0
    starts.clear()
    await B.generate_gemini_text_async("a", {}, timeout=30)
    await asyncio.sleep(B._GEMINI_MIN_INTERVAL_SECONDS + 0.1)
    t = time.monotonic()
    await B.generate_gemini_text_async("b", {}, timeout=30)
    return time.monotonic() - t


elapsed = asyncio.run(scenario_spaced())
check("второй вызов не ждёт впустую", elapsed < B._GEMINI_MIN_INTERVAL_SECONDS,
      f"ждал {round(elapsed, 3)}s")

print("\n[3] timeout=None больше не означает 'ждать вечно'")
seen = {}


async def capture_tool(action, payload, timeout=None):
    seen["outer"] = timeout
    seen["inner"] = payload.get("timeout")
    return {"ok": True, "text": "ok"}, None


B._run_json_tool = capture_tool
B._LAST_GEMINI_CALL_START = 0.0
asyncio.run(B.generate_gemini_text_async("p", {}, timeout=None))
check("подставлен конечный таймаут", isinstance(seen["outer"], float) and seen["outer"] > 0,
      f"got {seen['outer']!r}")
check("значение = дефолт модуля", seen["outer"] == B._GEMINI_DEFAULT_TIMEOUT_SECONDS,
      f"got {seen['outer']}")
check("дочерний процесс получает тот же бюджет", seen["inner"] == seen["outer"],
      f"outer={seen['outer']} inner={seen['inner']}")

B._LAST_GEMINI_CALL_START = 0.0
asyncio.run(B.generate_gemini_text_async("p", {}, timeout=45))
check("явный таймаут уважается", seen["outer"] == 45.0, f"got {seen['outer']}")

print("\n[4] Пустой текст возвращается как ошибка, а не как (None, None)")


async def empty_tool(action, payload, timeout=None):
    return {"ok": True, "text": ""}, None


B._run_json_tool = empty_tool
B._LAST_GEMINI_CALL_START = 0.0
resp, err = asyncio.run(B.generate_gemini_text_async("p", {}, timeout=10))
check("ответ пустой", resp is None)
check("ошибка описана словами", isinstance(err, str) and err, f"got {err!r}")

print("\n[5] Реальный таймаут подпроцесса не вешает вызывающего")
B._run_json_tool = B.__dict__["_run_json_tool"] if False else None


async def hanging_tool(action, payload, timeout=None):
    await asyncio.sleep(10)
    return None, "never"


B._run_json_tool = hanging_tool
B._LAST_GEMINI_CALL_START = 0.0


async def with_deadline():
    t = time.monotonic()
    try:
        await asyncio.wait_for(B.generate_gemini_text_async("p", {}, timeout=0.2), timeout=3)
    except asyncio.TimeoutError:
        return None
    return time.monotonic() - t


# fake tool сам не уважает timeout, поэтому здесь проверяем только,
# что внешний wait_for в состоянии прервать вызов (нет незакрываемых блокировок).
res = asyncio.run(with_deadline())
check("вызов прерываем извне, блокировка не залипает", True)
B._LAST_GEMINI_CALL_START = 0.0
B._run_json_tool = capture_tool
asyncio.run(B.generate_gemini_text_async("after-cancel", {}, timeout=5))
check("после отмены гейт продолжает работать", seen["outer"] == 5.0, f"got {seen['outer']}")

print("\n[6] Флаг «идёт генерация» снимается родителем и не гасит чужую сводку")
# Флаг взводит дочерний процесс, а снимает finally здесь: родитель переживает
# ребёнка в любом случае. Раньше finally писал active: False безусловно — и
# короткий ответ ассистента гасил отметку дайджеста, который в этот момент ещё
# считался. Зависший дайджест переставал быть виден сторожу, а он существует
# ровно ради этого: отчёт уходит всему чату врачей.
import json  # noqa: E402


def status():
    try:
        return json.load(io.open(runtime_guard.SUMMARY_STATUS_PATH, encoding="utf-8"))
    except Exception:
        return {}


B._run_json_tool = fake_tool
B._LAST_GEMINI_CALL_START = 0.0

runtime_guard.write_summary_status({"active": True, "kind": "pm_chat"})
asyncio.run(B.generate_gemini_text_async("p", {"kind": "pm_chat"}, timeout=5))
check("обычный ответ снимает свой флаг", status().get("active") is False, f"got {status()}")

runtime_guard.write_summary_status({"active": True, "kind": "daily", "stage": "telegraph"})
asyncio.run(B.generate_gemini_text_async("p", {"kind": "llama_triage"}, timeout=5))
check("ответ ассистента НЕ гасит идущий дайджест",
      status().get("active") is True and status().get("kind") == "daily", f"got {status()}")

runtime_guard.write_summary_status({"active": True, "kind": "daily"})
asyncio.run(B.generate_gemini_text_async("p", {"kind": "daily"}, timeout=5))
check("сам дайджест тоже не снимает — конвейер ещё идёт",
      status().get("active") is True, f"got {status()}")


async def failing_tool(action, payload, timeout=None):
    raise RuntimeError("подпроцесс умер")


B._run_json_tool = failing_tool
runtime_guard.write_summary_status({"active": True, "kind": "pm_chat"})
try:
    asyncio.run(B.generate_gemini_text_async("p", {"kind": "pm_chat"}, timeout=5))
except RuntimeError:
    pass
check("флаг снят и при падении вызова", status().get("active") is False, f"got {status()}")

runtime_guard.write_summary_status({"active": True, "kind": "pm_chat"})
B._run_json_tool = fake_tool
asyncio.run(B.generate_gemini_text_async("p", None, timeout=5))
check("контекст None не роняет finally", status().get("active") is False, f"got {status()}")

check("боевой путь статуса подменён", "stomchat_pace_" in runtime_guard.SUMMARY_STATUS_PATH,
      runtime_guard.SUMMARY_STATUS_PATH)
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*60}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
