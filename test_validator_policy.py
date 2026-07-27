"""
Проверка check_response_quality: политика fail-closed/fail-open и разбор вердикта.
Запуск: python test_validator_policy.py
"""
import asyncio
import importlib.util
import os
import sys
import types

for name in ("vision", "database", "config", "blocking_tools", "runtime_guard"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["config"].DENTAL_KEYWORDS = []
sys.modules["blocking_tools"].generate_gemini_text_async = None

spec = importlib.util.spec_from_file_location(
    "assistant_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant.py")
)
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

CTX = ["Иван: у пациента чувствительность после установки коронки"]
DRAFT = "Проверь окклюзию, чаще всего дело в супраконтакте."

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class Resp:
    def __init__(self, text):
        self.text = text


def stub(text=None, error=None, raises=None):
    """Подменяет LLM-вызов внутри валидатора."""
    async def _fake(prompt, ctx, timeout=None):
        if raises:
            raise raises
        return (Resp(text) if text is not None else None), error
    A.generate_gemini_text_async = _fake


def run(invited, draft=DRAFT):
    return asyncio.run(A.check_response_quality(CTX, draft, invited=invited))


print("\n[1] Явный отказ валидатора глушит черновик на ОБОИХ путях")
stub(text='{"ok": false, "reason": "выдуманная связь патологий"}')
ok_u, r_u = run(invited=False)
ok_i, r_i = run(invited=True)
check("незваный: заглушен", ok_u is False, f"got {ok_u}, {r_u}")
check("запрошенный: заглушен", ok_i is False, f"got {ok_i}, {r_i}")
check("причина отказа сохранена", "выдуманная" in r_i, f"got {r_i!r}")

print("\n[2] Одобрение пропускает черновик")
stub(text='{"ok": true, "reason": "корректно"}')
check("незваный: пропущен", run(invited=False)[0] is True)
check("запрошенный: пропущен", run(invited=True)[0] is True)
stub(text='```json\n{"ok": true, "reason": "ок"}\n```')
check("код-фенсы разобраны", run(invited=False)[0] is True)

print("\n[3] Валидатор НЕДОСТУПЕН -> fail-closed для незваных, fail-open для запрошенных")
for label, kw in [
    ("сетевая ошибка", dict(error="gemini-text timeout")),
    ("пустой ответ", dict(text=None)),
    ("мусор вместо JSON", dict(text="Извините, я не могу выполнить этот запрос.")),
    ("битый JSON", dict(text='{"ok": tru')),
    ("JSON без поля ok", dict(text='{"reason": "не знаю"}')),
    ("исключение", dict(raises=RuntimeError("boom"))),
]:
    stub(**kw)
    u, i = run(invited=False)[0], run(invited=True)[0]
    check(f"{label}: незваный заглушен", u is False, f"got {u}")
    check(f"{label}: запрошенный пропущен", i is True, f"got {i}")

print("\n[4] Пустой черновик отбрасывается без вызова LLM")
def _boom(*a, **k):
    raise AssertionError("LLM не должен вызываться для пустого черновика")
A.generate_gemini_text_async = _boom
check("пустая строка отклонена", run(invited=True, draft="")[0] is False)
check("пробелы отклонены", run(invited=False, draft="   \n ")[0] is False)

print("\n[5] Регресс: старое поведение fail-open больше не проходит")
stub(text="полный мусор без json")
check("недоступный валидатор НЕ одобряет незваный ответ", run(invited=False)[0] is False)

print(f"\n{'='*60}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
