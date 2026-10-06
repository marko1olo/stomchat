"""
Подъём бота против сторожа: может ли он застрелить процесс во время старта.

Сторож убивает процесс, если heartbeat не обновлялся WATCHDOG_STALE_SECONDS.
Он запускается первой строкой start_bot, а сердцебиение, которое его кормит,
запускалось в самом конце — после всего сетевого подъёма.

Бюджет таймаутов этого подъёма складывался в БОЛЬШЕ сторожевого порога:

    client.start()      120 с
    bot_client.start()  120 с
    get_my_id()          60 с
    ------------------------
                        300 с  при пороге 300 с, плюс init_db и init_assistant

На медленной сети — то есть ровно тогда, когда подъём и без того труден, —
сторож стрелял посреди подключения, start.bat поднимал процесс заново, и бот
уходил в цикл перезапусков, ни разу не встав. Особенно вероятно после долгого
простоя: бот не запускался с 22 июня.

Тест проверяет порядок запуска и арифметику бюджета — сеть не трогается.

Запуск: python test_startup_watchdog.py
"""
import io
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import main
import runtime_guard

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCE = io.open("main.py", encoding="utf-8").read()
START_BODY = SOURCE.split("async def start_bot():", 1)[1].split("\nif __name__", 1)[0]
# Комментарии убираем: они упоминают те же вызовы, и проверка порядка ловила бы
# пояснение вместо кода. На этой мелочи я спотыкался уже четырежды.
START_CODE = "\n".join(l for l in START_BODY.split("\n") if not l.lstrip().startswith("#"))


def position(marker):
    index = START_CODE.find(marker)
    return index if index != -1 else 10 ** 9


print("\n[1] Сердцебиение запускается до сетевого подъёма")
check("сторож запускается первым", position("start_watchdog()") < position("heartbeat_task()"))
check("сердцебиение раньше инициализации базы",
      position("heartbeat_task()") < position("database.init_db()"))
check("сердцебиение раньше подключения клиента",
      position("heartbeat_task()") < position("wait_for(client.start()"))
check("сердцебиение раньше подключения бота",
      position("heartbeat_task()") < position("bot_client.start("))
check("сердцебиение раньше синхронизации истории",
      position("heartbeat_task()") < position("sync_history()"))
check("запускается ровно один раз",
      START_CODE.count("heartbeat_task()") == 1,
      f"got {START_CODE.count('heartbeat_task()')}")

print("\n[2] Бюджет подъёма против порога сторожа")
network_budget = (
    main.START_TIMEOUT_SECONDS      # client.start()
    + main.START_TIMEOUT_SECONDS    # bot_client.start()
    + main.TELEGRAM_REQUEST_TIMEOUT_SECONDS  # get_my_id()
)
print(f"      таймауты сетевых шагов: {network_budget} с")
print(f"      порог сторожа:          {runtime_guard.WATCHDOG_STALE_SECONDS} с")
check("бюджет подъёма превышает порог — значит сердцебиение обязано идти с начала",
      network_budget >= runtime_guard.WATCHDOG_STALE_SECONDS,
      "бюджет стал меньше порога; проверка ниже потеряла смысл, пересмотрите тест")
check("сердцебиение действительно идёт с начала",
      position("heartbeat_task()") < position("wait_for(client.start()"))

print("\n[3] Каждый шаг подъёма ограничен по времени")
# Держать сердцебиение с начала безопасно только потому, что ни один шаг не
# может зависнуть навсегда. Если появится шаг без таймаута, сторож перестанет
# быть страховкой от зависшего подъёма.
for step in ("database.init_db()", "client.start()", "get_my_id()", "sync_history()"):
    line = next((l for l in START_CODE.split("\n") if step in l), "")
    guarded = "wait_for" in line or "wait_for" in START_CODE[max(0, position(step) - 120):position(step)]
    check(f"{step} обёрнут таймаутом", guarded, f"строка: {line.strip()[:70]}")

print("\n[4] Интервал сердцебиения укладывается в порог с запасом")
check("интервал заметно меньше порога",
      runtime_guard.HEARTBEAT_INTERVAL_SECONDS * 3 < runtime_guard.WATCHDOG_STALE_SECONDS,
      f"интервал {runtime_guard.HEARTBEAT_INTERVAL_SECONDS}, порог {runtime_guard.WATCHDOG_STALE_SECONDS}")
check("сторож просыпается не реже интервала",
      runtime_guard.HEARTBEAT_INTERVAL_SECONDS <= runtime_guard.WATCHDOG_STALE_SECONDS)

print("\n[5] Синхронизация истории кормит сторожа изнутри")
# Она может идти долго после простоя: бот не запускался больше месяца.
sync_body = SOURCE.split("async def sync_history():", 1)[1].split("\nasync def ", 1)[0]
check("внутри цикла есть write_heartbeat", "write_heartbeat" in sync_body,
      "долгая синхронизация не подаёт признаков жизни")
check("сердцебиение бьётся по ходу выборки, а не однажды",
      re.search(r"count % \d+ == 0", sync_body) is not None,
      "нет периодического обновления")

print("\n[6] Задачи после подъёма на месте")
for task in ("scheduler_task(", "pm_ping_scheduler_task(", "runtime_telemetry_task(",
             "summary_watchdog_task(", "health_watchdog_task("):
    check(f"{task.rstrip('(')} запускается", task in START_CODE)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
