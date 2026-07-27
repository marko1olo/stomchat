"""
Окно дайджеста: сквозная проверка того, что вечерние часы в него попадают.

Регресс на измеренную потерю данных. Telethon отдаёт tz-aware UTC, и
save_message писал именно UTC. Планировщик же строил границы окна из наивного
datetime.now() — локального времени, — и они сравнивались с UTC-строками как
есть. При смещении хоста UTC+4 запрошенное «вчера 20:00 — сейчас» фактически
начиналось с сегодняшних 00:00 локального времени: самые активные вечерние
часы не попадали ни во вчерашний дайджест, ни в сегодняшний.

Проверяется не арифметика _date_text, а результат: сообщение, отправленное
вчера в 21:30 по местному времени, обязано вернуться из запроса дайджеста.
База настоящая, путь записи настоящий, запрос настоящий.

Запуск: python test_digest_window.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_digest_")
config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")

import database

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def local_offset():
    """Смещение хоста от UTC в часах, как его видит наивный datetime.now()."""
    return (datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None))


def as_telethon_utc(local_naive):
    """Локальное время -> то, что Telethon положит в message.date (tz-aware UTC)."""
    return local_naive.astimezone(timezone.utc)


async def seed(msg_id, local_naive, text):
    """Сохраняет сообщение ровно тем путём, которым его сохранил бы обработчик."""
    await database.save_message(
        msg_id=msg_id,
        sender_id=555,
        sender_name="Врач",
        sender_username=None,
        text=text,
        date=as_telethon_utc(local_naive),
    )


async def run():
    await database.init_db()

    offset = local_offset()
    offset_h = round(offset.total_seconds() / 3600)
    print(f"\n[0] Смещение хоста: UTC{offset_h:+d}")
    if offset_h == 0:
        print("      хост в UTC — исходный баг здесь не воспроизводится,")
        print("      но окно всё равно обязано быть корректным")

    # «Сейчас» фиксируем как локальное наивное — ровно так его берёт планировщик.
    now_local = datetime.now().replace(microsecond=0)
    yesterday = (now_local - timedelta(days=1)).date()

    print("\n[1] Наполняем сутки по местному времени врачей")
    plan = [
        (5001, datetime.combine(yesterday, datetime.min.time()).replace(hour=15, minute=0),
         "дневное сообщение (до начала окна)"),
        (5002, datetime.combine(yesterday, datetime.min.time()).replace(hour=20, minute=30),
         "вечерний разбор случая — начало"),
        (5003, datetime.combine(yesterday, datetime.min.time()).replace(hour=21, minute=30),
         "вечерний разбор случая — пик"),
        (5004, datetime.combine(yesterday, datetime.min.time()).replace(hour=23, minute=15),
         "поздний вечер, спор про уступ"),
        (5005, now_local - timedelta(hours=2),
         "сегодняшнее сообщение"),
    ]
    for msg_id, when, text in plan:
        await seed(msg_id, when, text)
    check("все пять сообщений записаны", len(await database.get_messages_for_summary()) == 5)

    print("\n[2] Окно планировщика «вчера 20:00 — сейчас»")
    # Ровно та же арифметика, что в daily_report_task.
    end_time = now_local
    start_time = (now_local - timedelta(days=1)).replace(hour=20, minute=0, second=0)

    rows = await database.get_messages_for_daily_summary(start_time, end_time, min_count=0)
    got = {r[0] for r in rows}

    check("вечер 20:30 попал в дайджест", 5002 in got, f"got {sorted(got)}")
    check("пик 21:30 попал в дайджест", 5003 in got, f"got {sorted(got)}")
    check("поздний вечер 23:15 попал в дайджест", 5004 in got, f"got {sorted(got)}")
    check("сегодняшнее сообщение попало", 5005 in got, f"got {sorted(got)}")
    check("дневное 15:00 осталось за границей окна", 5001 not in got, f"got {sorted(got)}")

    print("\n[3] Сколько бы потерялось при старой (наивной) трактовке границ")
    def naive_text(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def operation():
        with database._connection() as db:
            return [r[0] for r in db.execute(
                "SELECT msg_id FROM messages WHERE date >= ? AND date <= ? ORDER BY date",
                (naive_text(start_time), naive_text(end_time)),
            ).fetchall()]

    naive_got = set(await database._run_db(operation))
    lost = got - naive_got
    if offset_h > 0:
        check(f"старая трактовка теряла вечерние часы ({len(lost)} шт.)", len(lost) > 0,
              f"потерь нет: naive={sorted(naive_got)} fixed={sorted(got)}")
    else:
        check("на UTC-хосте расхождения нет — это ожидаемо", lost == set())

    print("\n[4] Ничто не выпадает между вчерашним и сегодняшним дайджестом")
    # Вчерашнее окно: позавчера 20:00 -> вчера в час отчёта.
    report_hour = getattr(config, "REPORT_HOUR", 10)
    yesterday_end = datetime.combine(yesterday, datetime.min.time()).replace(hour=report_hour)
    yesterday_start = (yesterday_end - timedelta(days=1)).replace(hour=20, minute=0, second=0)

    yrows = {r[0] for r in await database.get_messages_for_daily_summary(
        yesterday_start, yesterday_end, min_count=0)}
    covered = yrows | got
    all_ids = {p[0] for p in plan}
    check("каждое сообщение попало хотя бы в один дайджест",
          all_ids <= covered, f"не покрыты: {sorted(all_ids - covered)}")

    print("\n[5] Недельный отчёт видит те же вечера")
    weekly = {r[0] for r in await database.get_messages_for_range(
        now_local - timedelta(days=7), now_local)}
    check("вечерние сообщения в недельном отчёте есть",
          {5002, 5003, 5004} <= weekly, f"got {sorted(weekly)}")

    print("\n[6] Путь записи не сдвигает время")
    probe = datetime(2026, 6, 21, 19, 36, 17, tzinfo=timezone.utc)
    check("tz-aware UTC сохраняется как есть",
          database._date_text(probe) == "2026-06-21 19:36:17",
          f"got {database._date_text(probe)}")

    naive_probe = datetime(2026, 6, 21, 20, 0, 0)
    expected = naive_probe.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    check("наивное локальное переводится в UTC",
          database._date_text(naive_probe) == expected,
          f"got {database._date_text(naive_probe)}")


try:
    asyncio.run(run())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
