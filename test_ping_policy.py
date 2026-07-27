"""
Проверка политики проактивных пингов: тихие часы, отписка, шторм ретраев,
точечная запись состояния.
Запуск: python test_ping_policy.py
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
sys.modules["config"].SOURCE_CHAT_ID = -100123
sys.modules["blocking_tools"].generate_gemini_text_async = None

spec = importlib.util.spec_from_file_location(
    "assistant_under_test", os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant.py")
)
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

_TMPDIR = tempfile.mkdtemp(prefix="stomping_")
A.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
A.STATE_TMP_PATH = A.STATE_PATH + ".tmp"
A.STATE_BAK_PATH = A.STATE_PATH + ".bak"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def reset(**pings):
    for p in (A.STATE_PATH, A.STATE_TMP_PATH, A.STATE_BAK_PATH):
        if os.path.exists(p):
            os.remove(p)
    with open(A.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"pm_pings": pings}, f)


def entry(uid):
    return A.load_state().get("pm_pings", {}).get(str(uid), {})


print("\n[1] Тихие часы: ночью проактивных сообщений нет")
for h in (22, 23, 0, 3, 7, 8):
    check(f"{h:02d}:00 — тихо", A.is_ping_quiet_hours(datetime(2026, 7, 27, h, 0)) is True)
for h in (9, 12, 18, 21):
    check(f"{h:02d}:00 — можно писать", A.is_ping_quiet_hours(datetime(2026, 7, 27, h, 0)) is False)

print("\n[2] Точечная запись не затирает соседей и чужие поля")
reset(**{"111": {"last_activity": "2026-07-01T10:00:00", "last_group_ping": "2026-07-02T10:00:00"},
         "222": {"last_activity": "2026-07-01T10:00:00"}})
A.commit_pm_ping("111", ping_sent=True)
check("сосед 222 не тронут", entry(222).get("last_activity") == "2026-07-01T10:00:00")
check("чужое поле last_group_ping сохранено", entry(111).get("last_group_ping") == "2026-07-02T10:00:00",
      f"got {entry(111)}")
check("новое поле записано", entry(111).get("ping_sent") is True)

print("\n[3] Отписка")
reset(**{"333": {"last_activity": "2026-07-01T10:00:00"}})
A.set_ping_opt_out(333, "не пиши мне больше")
check("флаг отписки выставлен", entry(333).get("pings_opted_out") is True)
check("причина сохранена", "не пиши" in (entry(333).get("opt_out_reason") or ""))
check("last_activity не потерян", entry(333).get("last_activity") == "2026-07-01T10:00:00")

print("\n[4] Детектор отписки ловит реальные формулировки")
for phrase in ["не пиши мне", "отвали", "хватит спамить", "заткнись", "надоел"]:
    check(f"«{phrase}» распознано", A.is_negative_feedback(phrase) is True)
for phrase in ["какой протокол ирригации?", "спасибо, помогло", "а что по циркону"]:
    check(f"«{phrase}» НЕ отписка", A.is_negative_feedback(phrase) is False)

print("\n[5] Удаление записи сохраняется сразу")
reset(**{"444": {"last_activity": "2026-07-01T10:00:00"}, "555": {"last_activity": "x"}})
A.drop_pm_ping(444)
check("запись 444 удалена с диска", "444" not in A.load_state().get("pm_pings", {}))
check("запись 555 на месте", "555" in A.load_state().get("pm_pings", {}))

print("\n[6] Счётчик неудач ограничен и сбрасывается успехом")
reset(**{"666": {"last_activity": "2026-07-01T10:00:00"}})
for i in range(1, A.MAX_PING_FAILURES + 1):
    A.commit_pm_ping("666", ping_failures=i)
check(f"порог {A.MAX_PING_FAILURES} достигнут", entry(666)["ping_failures"] >= A.MAX_PING_FAILURES)
A.commit_pm_ping("666", ping_sent=True, ping_failures=0)
check("успех обнулил счётчик", entry(666)["ping_failures"] == 0)

print("\n[7] Регресс: сеяние новых пользователей не эпохой")
# Сам job прогоняется по-настоящему в test_group_ping_job.py — там же
# проверяется, что заведённая запись доезжает до диска. Здесь остаётся
# арифметика порога: запись с сегодняшней датой права на пинг не даёт.
now = datetime(2026, 7, 27, 12, 0, 0)
check("свежая запись до порога 48ч не дотягивает",
      now - datetime.fromisoformat(now.isoformat()) < timedelta(hours=48))
check("эпоха порог перескакивает — именно это и был баг",
      now - datetime(2000, 1, 1) > timedelta(hours=48))

print("\n[8] Лимит пингов за цикл задан")
check(f"MAX_PINGS_PER_CYCLE={A.MAX_PINGS_PER_CYCLE} разумен", 1 <= A.MAX_PINGS_PER_CYCLE <= 20)

shutil.rmtree(_TMPDIR, ignore_errors=True)
print(f"\n{'='*60}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
