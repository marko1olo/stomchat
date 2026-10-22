"""
Проверка политики проактивных пингов: тихие часы (Europe/Moscow, UTC+3),
клиническое рабочее окно (11:00-17:00), отписка по фразам ('стоп', 'хватит', 'не пиши'),
шторм ретраев, точечная запись состояния и строгий антиспам (1 пинг).
Запуск: python tests/test_ping_policy.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

for name in ("vision", "database", "config", "blocking_tools", "runtime_guard"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["config"].DENTAL_KEYWORDS = []
sys.modules["config"].SOURCE_CHAT_ID = -100123
sys.modules["config"].DB_PATH = "messages.db"
sys.modules["blocking_tools"].generate_gemini_text_async = None

spec = importlib.util.spec_from_file_location(
    "assistant_under_test", os.path.join(ROOT_DIR, "assistant.py")
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


print("\n[1] Тихие часы: ночью проактивных сообщений нет (строго Europe/Moscow, UTC+3)")
for h in (22, 23, 0, 2, 3, 7, 8):
    check(f"{h:02d}:00 MSK — тихо", A.is_ping_quiet_hours(datetime(2026, 7, 27, h, 0)) is True)
for h in (9, 12, 14, 18, 21):
    check(f"{h:02d}:00 MSK — можно писать", A.is_ping_quiet_hours(datetime(2026, 7, 27, h, 0)) is False)

# Проверка инцидента Дарьи (02:51:15 ночи по Москве на сервере в UTC):
# 02:51:15 MSK соответствует 23:51:15 UTC
daria_utc_time = datetime(2026, 10, 22, 23, 51, 15, tzinfo=timezone.utc)
check("02:51:15 MSK (23:51 UTC на VPS) — строго ТИХИЕ ЧАСЫ", A.is_ping_quiet_hours(daria_utc_time) is True)

# 22:15 MSK соответствует 19:15 UTC (раньше сервер считал, что это вечер 19:15 и слал пинг)
night_utc_time = datetime(2026, 10, 22, 19, 15, 0, tzinfo=timezone.utc)
check("22:15 MSK (19:15 UTC на VPS) — строго ТИХИЕ ЧАСЫ", A.is_ping_quiet_hours(night_utc_time) is True)

# 14:00 MSK соответствует 11:00 UTC (день)
day_utc_time = datetime(2026, 10, 22, 11, 0, 0, tzinfo=timezone.utc)
check("14:00 MSK (11:00 UTC на VPS) — дневное разрешенное время", A.is_ping_quiet_hours(day_utc_time) is False)

print("\n[2] Рабочее клиническое окно персонального follow-up (11:00-17:00 MSK)")
for h in (11, 13, 15, 16):
    check(f"{h:02d}:00 MSK — рабочее время для follow-up", A.is_pm_ping_work_hours(datetime(2026, 7, 27, h, 0)) is True)
for h in (2, 8, 9, 10, 17, 20, 22):
    check(f"{h:02d}:00 MSK — вне рабочего окна follow-up", A.is_pm_ping_work_hours(datetime(2026, 7, 27, h, 0)) is False)

print("\n[3] Точечная запись не затирает соседей и чужие поля")
ts_act = (datetime.now() - timedelta(days=2)).replace(microsecond=0).isoformat()
ts_gp = (datetime.now() - timedelta(days=1)).replace(microsecond=0).isoformat()
reset(**{"111": {"last_activity": ts_act, "last_group_ping": ts_gp},
         "222": {"last_activity": ts_act}})
A.commit_pm_ping("111", ping_sent=True)
check("сосед 222 не тронут", entry(222).get("last_activity") == ts_act)
check("чужое поле last_group_ping сохранено", entry(111).get("last_group_ping") == ts_gp, f"got {entry(111)}")
check("новое поле записано", entry(111).get("ping_sent") is True)

print("\n[4] Отписка")
reset(**{"333": {"last_activity": ts_act}})
A.set_ping_opt_out(333, "не пиши мне больше")
check("флаг отписки выставлен", entry(333).get("pings_opted_out") is True)
check("причина сохранена", "не пиши" in (entry(333).get("opt_out_reason") or ""))
check("last_activity не потерян", entry(333).get("last_activity") == ts_act)

print("\n[5] Детектор отписки ловит короткие команды и фразы в ЛС")
for phrase in ["стоп", "stop", "хватит", "отстань", "не пиши", "не пиши мне",
               "хватит писать", "перестань писать", "отключи напоминания",
               "без напоминаний", "хватит спамить", "ты надоел", "бот надоел"]:
    check(f"«{phrase}» распознано как отписка", A.is_ping_opt_out_request(phrase) is True)

for phrase in ["какой протокол ирригации?", "спасибо, помогло", "а что по циркону",
               "эта работа надоела", "надо удалить 36 зуб", "коронка отваливается",
               "хватит ли 2% раствора гипохлорита для растворения органики?"]:
    check(f"«{phrase}» НЕ отписка (клинический вопрос сохранен)", A.is_ping_opt_out_request(phrase) is False)

print("\n[6] Удаление записи сохраняется сразу")
reset(**{"444": {"last_activity": ts_act}, "555": {"last_activity": ts_act}})
A.drop_pm_ping(444)
check("запись 444 удалена с диска", "444" not in A.load_state().get("pm_pings", {}))
check("запись 555 на месте", "555" in A.load_state().get("pm_pings", {}))

print("\n[7] Счётчик неудач ограничен и сбрасывается успехом")
reset(**{"666": {"last_activity": ts_act}})
for i in range(1, A.MAX_PING_FAILURES + 1):
    A.commit_pm_ping("666", ping_failures=i)
check(f"порог {A.MAX_PING_FAILURES} достигнут", entry(666)["ping_failures"] >= A.MAX_PING_FAILURES)
A.commit_pm_ping("666", ping_sent=True, ping_failures=0)
check("успех обнулил счётчик", entry(666)["ping_failures"] == 0)

print("\n[8] Клинические параметры задержки follow-up")
check("Минимальная задержка follow-up 72ч (3 дня)", A.MIN_CLINICAL_PING_DELAY_HOURS == 72)
check("Максимальная задержка follow-up 168ч (7 дней)", A.MAX_CLINICAL_PING_DELAY_HOURS == 168)
check("Рабочее окно начала 11:00", A.PM_PING_WORK_START_HOUR == 11)
check("Рабочее окно конца 17:00", A.PM_PING_WORK_END_HOUR == 17)

shutil.rmtree(_TMPDIR, ignore_errors=True)
print(f"\n{'='*60}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
