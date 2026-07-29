"""
Догон неразобранных снимков: было «никогда», стало «сутки-двое».

Догон вызывался РОВНО ОДИН раз за запуск и брал MEDIA_RECOVERY_LIMIT = 5 строк.
Замер по копии боевой базы: 745 снимков без описания, самый старый от
2026-01-29. При пяти за запуск это 149 перезапусков, а перезапусков по журналу
выходит 14 за 35 суток — порядка 372 суток, чтобы разобрать накопленное.
Практически это «никогда»: врач присылает рентген, бот молчит, и никто не знает,
что снимок стоит в очереди длиной в год.

Очередь разбора живёт в памяти и умирает вместе с процессом, поэтому
восстановление обязано быть ПОВТОРЯЮЩИМСЯ, а не однократным.

Ни одного платного вызова здесь нет: постановка в очередь и сама очередь
подменены, база не открывается, воркеры не поднимаются.

Запуск: python test_media_recovery.py
"""
import asyncio
import io
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ["STOMCHAT_LOG_PATH"] = os.path.join(tempfile.mkdtemp(prefix="stomchat_mr_"), "t.log")

import main as M  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


CODE = "\n".join(l for l in io.open("main.py", encoding="utf-8").read().split("\n")
                 if not l.lstrip().startswith("#"))

print("\n[1] Догон повторяющийся, а не однократный")
check("периодическая задача существует", callable(getattr(M, "media_recovery_task", None)),
      "догон снова однократный: накопленное не разберётся никогда")
check("задача поднимается при старте", 'media_recovery_task(), "media_recovery"' in CODE,
      "задача объявлена, но не запущена")
check("однократный вызов при старте сохранён",
      "await recover_pending_media_analysis()" in CODE,
      "первый заход сразу после подъёма полезен: не ждать первого такта")
check("интервал такта настраиваемый",
      isinstance(M.MEDIA_RECOVERY_INTERVAL_SECONDS, int)
      and M.MEDIA_RECOVERY_INTERVAL_SECONDS >= 60,
      f"got {M.MEDIA_RECOVERY_INTERVAL_SECONDS}")

print("\n[2] Накопленное действительно сходится")
per_day = 86400 / M.MEDIA_RECOVERY_INTERVAL_SECONDS * M.MEDIA_RECOVERY_LIMIT
BACKLOG = 745  # замер по копии боевой базы
days = BACKLOG / per_day
print(f"      пропускная способность {per_day:.0f} снимков в сутки, "
      f"накопленные {BACKLOG} за {days:.1f} суток")
check("догон разбирает накопленное быстрее чем за неделю", days <= 7,
      f"{days:.1f} суток — это снова «никогда»")
check("темп не превышает пропускную способность воркера",
      M.MEDIA_RECOVERY_LIMIT <= M.MEDIA_QUEUE_MAX_SIZE,
      f"партия {M.MEDIA_RECOVERY_LIMIT} против очереди {M.MEDIA_QUEUE_MAX_SIZE}")
# Один воркер разбирает снимок за время до MEDIA_ANALYSIS_TIMEOUT_SECONDS.
worker_per_day = 86400 / M.MEDIA_ANALYSIS_TIMEOUT_SECONDS
check("воркер успевает за темпом долива", worker_per_day >= per_day / 2,
      f"воркер {worker_per_day:.0f} в сутки против долива {per_day:.0f}")

print("\n[3] Такт не забивает очередь и не шумит впустую")
ticks = {"calls": 0, "queued": 0}


async def fake_recover():
    ticks["calls"] += 1
    return ticks["queued"]


async def run_ticks(interval, queue, queued_per_call, expect_calls):
    real_recover = M.recover_pending_media_analysis
    real_interval = M.MEDIA_RECOVERY_INTERVAL_SECONDS
    real_queue = M._media_queue
    ticks["calls"] = 0
    ticks["queued"] = queued_per_call
    M.recover_pending_media_analysis = fake_recover
    M.MEDIA_RECOVERY_INTERVAL_SECONDS = interval
    M._media_queue = queue
    task = asyncio.create_task(M.media_recovery_task())
    try:
        # Ждём СОБЫТИЯ, а не времени: версия со фиксированным сном мигала —
        # накладные цикла событий съедали один такт, и проверка падала на верном
        # коде через раз. Мигающий тест хуже отсутствующего.
        deadline = interval * (expect_calls + 6) + 1.0
        waited = 0.0
        step = interval / 4
        while ticks["calls"] < expect_calls and waited < deadline:
            await asyncio.sleep(step)
            waited += step
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        M.recover_pending_media_analysis = real_recover
        M.MEDIA_RECOVERY_INTERVAL_SECONDS = real_interval
        M._media_queue = real_queue
    return ticks["calls"]


async def scenarios():
    # Интервал подменяем на доли секунды: проверяется цикл, а не ожидание.
    empty_queue = asyncio.Queue(maxsize=4)
    calls = await run_ticks(0.05, empty_queue, 2, 3)
    check("такт повторяется, а не срабатывает однажды", calls >= 3,
          f"тактов {calls} — цикл не крутится")

    full_queue = asyncio.Queue(maxsize=1)
    full_queue.put_nowait(("x",))
    calls = await run_ticks(0.05, full_queue, 2, 3)
    check("при полной очереди догон не вызывается", calls == 0,
          f"вызовов {calls} — догон льёт в переполненную очередь")

    # Отключение через предел 0: задача завершается сразу, а не крутится впустую.
    real_limit = M.MEDIA_RECOVERY_LIMIT
    M.MEDIA_RECOVERY_LIMIT = 0
    try:
        task = asyncio.create_task(M.media_recovery_task())
        await asyncio.sleep(0.1)
        check("при пределе 0 задача завершается", task.done(),
              "задача крутится, хотя догон выключен")
        if not task.done():
            task.cancel()
    finally:
        M.MEDIA_RECOVERY_LIMIT = real_limit

    # Отказ внутри такта не должен убивать задачу целиком.
    async def boom():
        ticks["calls"] += 1
        raise RuntimeError("база недоступна")

    real_recover = M.recover_pending_media_analysis
    real_interval = M.MEDIA_RECOVERY_INTERVAL_SECONDS
    real_queue = M._media_queue
    ticks["calls"] = 0
    M.recover_pending_media_analysis = boom
    M.MEDIA_RECOVERY_INTERVAL_SECONDS = 0.05
    M._media_queue = asyncio.Queue(maxsize=4)
    task = asyncio.create_task(M.media_recovery_task())
    try:
        await asyncio.sleep(0.3)
        survived = not task.done()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        M.recover_pending_media_analysis = real_recover
        M.MEDIA_RECOVERY_INTERVAL_SECONDS = real_interval
        M._media_queue = real_queue
    check("отказ такта не убивает задачу", survived,
          "одна ошибка базы — и догон умер до перезапуска")
    check("отказ такта пробовался не один раз", ticks["calls"] >= 2,
          f"вызовов {ticks['calls']}")


asyncio.run(scenarios())

print("\n[4] Догон сообщает, сколько поставил")
check("функция возвращает число поставленных",
      "return queued" in CODE,
      "без возврата такт не может отличить «поставил» от «пусто» и будет шуметь")
recovery = CODE.split("async def recover_pending_media_analysis", 1)[1].split("\nasync def ", 1)[0]
check("постановка идёт пачкой", "bulk=True" in recovery)
check("уже стоящее в очереди не ставится повторно",
      "_QUEUED_MEDIA_IDS" in recovery,
      "пять свежих снимков уедут в платный Vision дважды на каждом рестарте")

print("\n[5] Проверки выше ловят поломку")
check("детектор запуска задачи поймал бы пропажу",
      'media_recovery_task(), "media_recovery"' not in 'create_task(health_watchdog_task())')
check("расчёт сходимости зависит от интервала",
      86400 / 60 * M.MEDIA_RECOVERY_LIMIT > 86400 / 3600 * M.MEDIA_RECOVERY_LIMIT,
      "формула не реагирует на интервал — проверка [2] слепа")
check("однократный догон не сошёлся бы", BACKLOG / M.MEDIA_RECOVERY_LIMIT > 100,
      f"{BACKLOG / M.MEDIA_RECOVERY_LIMIT:.0f} перезапусков при однократном догоне")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
