"""
Проверка check_response_quality: политика fail-closed/fail-open и разбор вердикта.
Запуск: python test_validator_policy.py
"""
import asyncio
import importlib.util
import os
import re
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

print("\n[6] Рецензент видит справку, на которой строился ответ")
# Без справки он не мог отличить число из базы знаний от выдуманного: и то и
# другое выглядит одинаково правдоподобно. Архив коллег сюда сознательно НЕ
# передаётся — иначе верный EBM-ответ отклонялся бы за расхождение с чужой
# ошибкой в чате.
CAPTURED = {}


def spy_stub(verdict='{"ok": true, "reason": "ок"}'):
    async def _fake(prompt, ctx, timeout=None):
        CAPTURED["prompt"] = prompt
        return Resp(verdict), None
    A.generate_gemini_text_async = _fake


spy_stub()
asyncio.run(A.check_response_quality(
    CTX, "Ставьте на 45 Нсм.", invited=True,
    reference="[3.2.1] Рекомендуемый торк установки имплантата 30-35 Нсм."))
prompt = CAPTURED["prompt"]
check("справка попала в промпт рецензента", "30-35 Нсм" in prompt)
check("сказано, что справка не исчерпывающая",
      "НЕ исчерпывающая" in prompt, "нет оговорки — рецензент начнёт отклонять лишнее")
check("есть правило про конкретные цифры",
      "КОНКРЕТНЫЕ ЦИФРЫ" in prompt, "правило о выдуманных числах не добавлено")
check("черновик по-прежнему в промпте", "45 Нсм" in prompt)
check("контекст переписки на месте", CTX[0][:20] in prompt)

CAPTURED.clear()
spy_stub()
asyncio.run(A.check_response_quality(CTX, DRAFT, invited=True))
check("без справки блок не вставляется",
      "Справка из Базы Знаний" not in CAPTURED["prompt"])

CAPTURED.clear()
spy_stub()
asyncio.run(A.check_response_quality(CTX, DRAFT, invited=True, reference="   "))
check("пустая справка блок не вставляет",
      "Справка из Базы Знаний" not in CAPTURED["prompt"])

CAPTURED.clear()
spy_stub()
marker = "уступ0.8мм"
asyncio.run(A.check_response_quality(
    CTX, DRAFT, invited=True, reference=marker + "щ" * 9000))
block = CAPTURED["prompt"].split("Справка из Базы Знаний", 1)[1].split("[Справка НЕ", 1)[0]
check("длинная справка обрезана по пределу",
      len(block) <= A.VALIDATOR_REFERENCE_MAX_CHARS + 80,
      f"длина блока {len(block)} при пределе {A.VALIDATOR_REFERENCE_MAX_CHARS}")
check("начало справки не потеряно при обрезке", marker in block)

# Окно рецензента обязано покрывать ВЕСЬ бюджет корпуса вики. Пока оно было
# вдвое меньше (3000 против 6000), рецензент видел медиану 53% справки: от него
# было скрыто 39% фактов и 18% чисел на 294 запросах. А правило 3.1 его промпта
# отклоняет ответ за цифры, которых нет в показанной справке, и явный отказ
# глушит черновик ВСЕГДА — даже когда врач спросил напрямую. То есть каждая
# пятая законная цифра из базы знаний выглядела для него выдуманной.
check("рецензенту видна вся справка, а не половина",
      A.VALIDATOR_REFERENCE_MAX_CHARS >= A._CORPUS_MAX_CHARS,
      f"окно {A.VALIDATOR_REFERENCE_MAX_CHARS} меньше бюджета корпуса {A._CORPUS_MAX_CHARS}")

# Проверяем на НАСТОЯЩЕЙ справке из вики, а не на синтетической строке.
CAPTURED.clear()
spy_stub()
_keys = A.select_search_keywords(A.extract_keywords("протокол ирригации канала гипохлорит"))
_wiki, _ = asyncio.run(A.search_knowledge_corpus(_keys))
if _wiki.strip():
    asyncio.run(A.check_response_quality(CTX, DRAFT, invited=True, reference=_wiki))
    shown = CAPTURED["prompt"].split("Справка из Базы Знаний", 1)[1].split("[Справка НЕ", 1)[0]
    _all_numbers = set(re.findall(r"\d+[.,]?\d*", _wiki))
    _shown_numbers = set(re.findall(r"\d+[.,]?\d*", shown))
    check("ни одно число живой справки от рецензента не скрыто",
          not (_all_numbers - _shown_numbers),
          f"скрыто {len(_all_numbers - _shown_numbers)} из {len(_all_numbers)}")
    check("живая справка дошла целиком",
          len(_wiki.strip()) <= len(shown) + 80,
          f"справка {len(_wiki)}, показано {len(shown)}")
else:
    check("живая справка недоступна — проверка пропущена", True)

CAPTURED.clear()
spy_stub('{"ok": false, "reason": "цифра не подтверждается справкой"}')
ok_num, why_num = asyncio.run(A.check_response_quality(
    CTX, "Ставьте на 95 Нсм.", invited=True,
    reference="[3.2.1] Рекомендуемый торк 30-35 Нсм."))
check("отказ по несоответствию цифры глушит ответ и на прямой вопрос",
      ok_num is False, f"got {ok_num}")
check("причина отказа доносится", "цифра" in why_num, f"got {why_num!r}")

print(f"\n{'='*60}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
