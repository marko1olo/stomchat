"""
Проверка load_state/save_state: слияние конкурентных правок, атомарность записи,
восстановление после обрыва. Запуск: python test_state_atomicity.py
"""
import json
import os
import shutil
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="stomstate_")

# Подменяем пути состояния ДО импорта assistant, чтобы не трогать боевой файл.
import importlib.util

spec = importlib.util.spec_from_file_location(
    "assistant_under_test",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant.py"),
)


def _load_module():
    """Импортирует assistant.py, глуша тяжёлые зависимости."""
    import types

    for name in ("vision", "database", "config", "blocking_tools", "runtime_guard"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            sys.modules[name] = stub
    sys.modules["config"].DENTAL_KEYWORDS = []
    sys.modules["blocking_tools"].generate_gemini_text_async = None

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load_module()
A.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
A.STATE_TMP_PATH = A.STATE_PATH + ".tmp"
A.STATE_BAK_PATH = A.STATE_PATH + ".bak"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def write_raw(text):
    with open(A.STATE_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def read_raw():
    with open(A.STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def reset(payload):
    for p in (A.STATE_PATH, A.STATE_TMP_PATH, A.STATE_BAK_PATH):
        if os.path.exists(p):
            os.remove(p)
    write_raw(json.dumps(payload, ensure_ascii=False, indent=2))


# --- Тест 1: ГЛАВНЫЙ. Гонка silenced_until -------------------------------
# Воспроизводит продовый сценарий: таск A читает state, уходит на 25s LLM-триаж,
# в это время таск B ставит silenced_until, затем A сохраняет свой протухший dict.
print("\n[1] Гонка: silenced_until, выставленный во время LLM-триажа")
reset({"last_passive_text_run": "2026-01-01T00:00:00", "pm_pings": {"111": {"ping_sent": False}}})

task_a = A.load_state()                      # таск A читает состояние (строка 540)
# ... 25 секунд await check_llm_triage() ...
task_b = A.load_state()                      # таск B: пользователь написал "хватит"
task_b["silenced_until"] = "2026-07-27T23:00:00"
task_b["pm_pings"]["111"] = {"ping_sent": True}
A.save_state(task_b)
# ... таск A просыпается и пишет свою копию (строка 781) ...
task_a["last_passive_text_run"] = "2026-07-27T20:00:00"
A.save_state(task_a)

final = read_raw()
check("silenced_until пережил конкурентную запись", final.get("silenced_until") == "2026-07-27T23:00:00",
      f"got {final.get('silenced_until')!r}")
check("pm_pings таска B не затёрты", final["pm_pings"]["111"]["ping_sent"] is True,
      f"got {final['pm_pings']}")
check("собственная правка таска A применена", final["last_passive_text_run"] == "2026-07-27T20:00:00",
      f"got {final['last_passive_text_run']}")

# --- Тест 2: мутация списка на месте -------------------------------------
print("\n[2] processed_threads: in-place append обеих сторон")
reset({"processed_threads": [1, 2]})
a, b = A.load_state(), A.load_state()
b["processed_threads"].append(99)   # таск B пометил тред
A.save_state(b)
a["last_referee_run"] = "2026-07-27T21:00:00"   # таск A список не трогал
A.save_state(a)
check("тред таска B не потерян", 99 in read_raw()["processed_threads"], f"got {read_raw()['processed_threads']}")

reset({"processed_threads": [1, 2]})
a, b = A.load_state(), A.load_state()
b["processed_threads"].append(99)
A.save_state(b)
a["processed_threads"].append(77)   # оба трогают список -> побеждает последний писатель
A.save_state(a)
check("явная правка списка вызывающим применяется", 77 in read_raw()["processed_threads"])

# --- Тест 3: битый файл ---------------------------------------------------
print("\n[3] Восстановление после обрыва записи")
reset({"silenced_until": "2026-07-27T23:00:00", "pm_pings": {"111": {"ping_sent": True}}})
s = A.load_state(); s["last_passive_run"] = "2026-07-27T10:00:00"; A.save_state(s)  # создаём .bak
check(".bak создан", os.path.exists(A.STATE_BAK_PATH))

write_raw('{"silenced_until": "2026-07-27T23:00:00", "pm_pi')   # обрыв на середине
recovered = A.load_state()
check("silenced_until поднят из .bak", recovered.get("silenced_until") == "2026-07-27T23:00:00",
      f"got {recovered.get('silenced_until')!r}")
check("pm_pings подняты из .bak", recovered.get("pm_pings", {}).get("111", {}).get("ping_sent") is True,
      f"got {recovered.get('pm_pings')}")

# --- Тест 4: ключи по умолчанию ------------------------------------------
print("\n[4] Ключи никогда не пропадают")
reset({})
s = A.load_state()
missing = [k for k in ("last_passive_run", "last_passive_text_run", "last_passive_media_run",
                       "last_referee_run", "processed_threads", "pm_pings") if k not in s]
check("все дефолтные ключи присутствуют", not missing, f"missing {missing}")

for p in (A.STATE_PATH, A.STATE_BAK_PATH):
    if os.path.exists(p):
        os.remove(p)
s = A.load_state()
check("файла нет -> дефолты, без исключения", s["processed_threads"] == [] and s["pm_pings"] == {})

# --- Тест 5: временный файл не остаётся ----------------------------------
print("\n[5] Гигиена .tmp")
reset({"a": 1})
s = A.load_state(); s["b"] = 2; A.save_state(s)
check(".tmp удалён после записи", not os.path.exists(A.STATE_TMP_PATH))
check("файл — валидный JSON", read_raw()["b"] == 2)

shutil.rmtree(_TMPDIR, ignore_errors=True)
print(f"\n{'='*60}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
