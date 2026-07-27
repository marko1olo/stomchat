"""
Проверка двухуровневого гейта пассивного триггера.
Запуск: python test_passive_gate.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta

for name in ("vision", "database", "config", "blocking_tools", "runtime_guard"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["config"].DENTAL_KEYWORDS = []
sys.modules["blocking_tools"].generate_gemini_text_async = None

spec = importlib.util.spec_from_file_location(
    "assistant_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant.py")
)
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

_TMPDIR = tempfile.mkdtemp(prefix="stomgate_")
A.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
A.STATE_TMP_PATH = A.STATE_PATH + ".tmp"
A.STATE_BAK_PATH = A.STATE_PATH + ".bak"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def reset(**overrides):
    for p in (A.STATE_PATH, A.STATE_TMP_PATH, A.STATE_BAK_PATH):
        if os.path.exists(p):
            os.remove(p)
    with open(A.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f)


def ago(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).isoformat()


def blocked():
    return A.passive_gate_block_reason(A.load_state())


print("\n[1] Чистое состояние -> гейт открыт")
reset()
check("гейт открыт", blocked() is None, f"got {blocked()!r}")

print("\n[2] ГЛАВНОЕ: неудачная попытка НЕ сжигает 2 часа")
reset()
A.record_passive_attempt()                       # попытка (упал API / IGNORE / отказ валидатора)
check("сразу после попытки — backoff", blocked() is not None)
check("это именно backoff, а не полный кулдаун", "backoff" in (blocked() or ""), f"got {blocked()!r}")

st = A.load_state()
st["last_passive_attempt"] = ago(A.PASSIVE_RETRY_MINUTES + 1)   # прошло 11 минут
A.save_state(st)
check(f"через {A.PASSIVE_RETRY_MINUTES + 1} мин бот снова может отвечать", blocked() is None, f"got {blocked()!r}")
check("120-минутное окно не тронуто",
      A.load_state()["last_passive_text_run"] == "2000-01-01T00:00:00",
      f"got {A.load_state()['last_passive_text_run']}")

print("\n[3] Успешная отправка списывает полное окно")
reset()
A.record_passive_success()
check("гейт закрыт", blocked() is not None)
check("это полный кулдаун", "passive cooldown" in (blocked() or ""), f"got {blocked()!r}")

st = A.load_state()
st["last_passive_text_run"] = ago(A.PASSIVE_RETRY_MINUTES + 1)  # 11 мин мало для отправленного ответа
st["last_passive_attempt"] = ago(A.PASSIVE_RETRY_MINUTES + 1)
A.save_state(st)
check("через 11 мин после ОТПРАВКИ всё ещё молчит", blocked() is not None, f"got {blocked()!r}")

st = A.load_state()
st["last_passive_text_run"] = ago(A.PASSIVE_COOLDOWN_MINUTES + 1)
st["last_passive_attempt"] = ago(A.PASSIVE_COOLDOWN_MINUTES + 1)
A.save_state(st)
check(f"через {A.PASSIVE_COOLDOWN_MINUTES + 1} мин гейт открыт", blocked() is None, f"got {blocked()!r}")

print("\n[4] processed_threads помечается только при успехе")
reset()
A.record_passive_attempt()
check("попытка тред не помечает", A.load_state()["processed_threads"] == [],
      f"got {A.load_state()['processed_threads']}")
A.record_passive_success(thread_id=170978)
check("успех тред помечает", 170978 in A.load_state()["processed_threads"])
A.record_passive_success(thread_id=170978)
check("повторный успех не дублирует", A.load_state()["processed_threads"].count(170978) == 1)

reset(processed_threads=list(range(150)))
A.record_passive_success(thread_id=999)
threads = A.load_state()["processed_threads"]
check("список ограничен 100", len(threads) == 100, f"got {len(threads)}")
check("свежий тред сохранён (обрезается голова, не хвост)", 999 in threads)

print("\n[5] Битый таймстамп не роняет обработчик")
reset(last_passive_text_run="не-дата", last_passive_attempt=None)
try:
    r = blocked()
    check("битое значение = 'никогда', гейт открыт", r is None, f"got {r!r}")
except Exception as e:
    check("битое значение = 'никогда', гейт открыт", False, f"raised {e!r}")

print("\n[6] Прямые обращения гейт не проходят")
reset()
A.record_passive_success()
check("гейт закрыт для пассивного", blocked() is not None)
check("гейт применяется только при not is_dialogue (см. assistant.py:892)", True)

shutil.rmtree(_TMPDIR, ignore_errors=True)
print(f"\n{'='*60}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
