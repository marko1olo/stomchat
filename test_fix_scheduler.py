"""
Планировщик выпусков и медиа-путь: догон пропущенного, кэш, отметки альбома.

Регресс на шесть подтверждённых дефектов main.py. Все они об одном классе цены —
потерянный выпуск, оплаченная дважды генерация, вечная перекачка:

  * дневное окно считалось только от now. Пропущенный день (бот лежал, цель
    отвалилась) означал, что порядка 22 часов переписки не попадали НИ В ОДИН
    выпуск, хотя last_sent_date хранится ровно для этого;
  * DIGEST_WINDOW_START_HOUR и config.REPORT_HOUR были двумя несвязанными
    числами: при REPORT_HOUR < 20 между выпусками возникала дыра до 20 часов
    в сутки (в config.example.py стоит REPORT_HOUR = 0);
  * недельный отчёт выходил только в понедельник и догона не имел вовсе:
    пропущенный понедельник терял выпуск навсегда;
  * кэш дневного текста создавался заново на каждом круге цикла, поэтому одна
    сломанная цель означала полную повторную генерацию каждые 10 минут;
  * pm_ping_scheduler_task не имел ни одного таймаута: один зависший
    send_message останавливал все пинги навсегда и молча;
  * догон медиа при старте ставил в очередь то, что sync_history поставил
    секундой раньше — до пяти снимков на КАЖДОМ рестарте уезжали в платный
    Vision дважды;
  * если ни один файл альбома не скачался, в базу не писалось ничего: строка
    оставалась «ожидающей» и перекачивалась на каждом рестарте вечно;
  * при частичном провале альбома описание одного снимка записывалось во ВСЕ
    строки альбома — в дайджест уходил чужой снимок под чужой подписью.

Проверяется поведение, а не текст комментариев: настоящая база во временном
каталоге, настоящий scheduler_task, настоящий process_media_message. Наружу
уходят только сеть и Vision.

Боевые файлы (stomat_bot.db, bot_state.json, assistant_state.json, bot.log и
прочие) не открываются: путь базы и журнала уводится в tempfile ДО импорта
модулей проекта, а неизменность боевых файлов проверяется отдельно в конце.

Запуск: python test_fix_scheduler.py
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

_REPO = os.path.dirname(os.path.abspath(__file__))
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_fixsched_")

# Журнал уводим ДО первого импорта проекта: runtime_guard.configure_logging()
# вызывается на уровне модуля в main.py, и без этого тест писал бы выдуманные
# строки в боевой bot.log — тот самый файл, по которому разбирают поведение бота.
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")

sys.path.insert(0, _REPO)
os.chdir(_REPO)  # config ищет .env от текущего каталога

import config  # noqa: E402

config.DB_PATH = os.path.join(_TMPDIR, "fixsched.db")

import runtime_guard  # noqa: E402

runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "hb.json")
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "status.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "dump.txt")
# Сторож убивает процесс через os._exit. В тесте он не нужен и опасен.
runtime_guard.start_watchdog = lambda *a, **k: None
runtime_guard.stop_watchdog = lambda *a, **k: None

# Снимок боевых файлов: в конце сверяем, что ни один не тронут.
_LIVE_FILES = (
    "stomat_bot.db", "assistant_state.json", "bot_state.json",
    "bot_summary_status.json", "bot_heartbeat.json", "bot.log",
)
_LIVE_BEFORE = {}
for _name in _LIVE_FILES:
    _path = os.path.join(_REPO, _name)
    if os.path.exists(_path):
        _LIVE_BEFORE[_path] = (os.path.getmtime(_path), os.path.getsize(_path))

# Состояние планировщика, temp_media и сессии Telethon адресуются относительными
# путями — уводим их в temp сменой каталога, иначе тест перепишет bot_state.json.
os.chdir(_TMPDIR)

import main  # noqa: E402
import assistant  # noqa: E402
import database  # noqa: E402
import summarizer  # noqa: E402

# STATE_PATH у ассистента абсолютный (от каталога скрипта), chdir его не спасает.
assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

PASS, FAIL = [], []
_REAL_SLEEP = asyncio.sleep


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def as_telethon_utc(local_naive):
    """Локальное наивное время -> то, что Telethon кладёт в message.date."""
    return local_naive.astimezone(timezone.utc)


async def seed(msg_id, local_naive, text, has_media=False, media_type=None):
    await database.save_message(
        msg_id=msg_id,
        sender_id=555,
        sender_name="Врач",
        sender_username=None,
        text=text,
        date=as_telethon_utc(local_naive),
        has_media=has_media,
        media_type=media_type,
    )


class FakeMessage:
    """Сообщение Telethon настолько, насколько его трогает медиа-путь."""

    def __init__(self, msg_id, photo=False, video=False, download="ok"):
        self.id = msg_id
        self.photo = True if photo else None
        self.video = True if video else None
        self.document = None
        self.sticker = None
        self.message = "снимок"
        self.download_calls = 0
        self._download = download

    async def download_media(self, file=None):
        self.download_calls += 1
        if self._download == "fail":
            raise OSError("соединение сброшено")
        if self._download == "none":
            return None
        path = os.path.join(_TMPDIR, f"media_{self.id}.bin")
        with open(path, "wb") as handle:
            handle.write(b"jpeg-ish")
        return path


async def run():
    await database.init_db()

    print("\n[0] Изоляция: тест работает на копиях, а не на боевых файлах")
    check("база уведена во временный каталог", _TMPDIR in config.DB_PATH, config.DB_PATH)
    check("журнал уведён во временный каталог", _TMPDIR in runtime_guard.LOG_PATH,
          runtime_guard.LOG_PATH)
    check("состояние планировщика пишется рядом с temp", os.getcwd() == _TMPDIR, os.getcwd())
    check("состояние ассистента уведено", _TMPDIR in assistant.STATE_PATH)

    # ------------------------------------------------------------------ дневное
    print("\n[1] Дневное окно догоняет пропущенный день")
    # «Сейчас» — сегодня в час отчёта: ровно так его видит планировщик.
    now = datetime.now().replace(hour=22, minute=30, second=0, microsecond=0)
    yesterday = (now - timedelta(days=1)).date()
    day_before = (now - timedelta(days=2)).date()

    normal = main.daily_window_start(now, yesterday)
    check("обычный день: окно как раньше — с 20:00 вчера",
          normal == now.replace(hour=20, minute=0, second=0, microsecond=0) - timedelta(days=1),
          f"got {normal}")

    missed = main.daily_window_start(now, day_before)
    resume = datetime.combine(day_before, datetime.min.time()).replace(hour=config.REPORT_HOUR)
    check("пропущенный день: окно уезжает к концу прошлого выпуска",
          missed == resume, f"got {missed}, ожидалось {resume}")
    check("догон отыгрывает те самые ~22 часа",
          timedelta(hours=20) <= (normal - missed) <= timedelta(hours=24),
          f"разница {normal - missed}")

    deep = main.daily_window_start(now, (now - timedelta(days=30)).date())
    check("месяц простоя не уезжает в окно целиком",
          deep == now - timedelta(days=main.DIGEST_CATCHUP_MAX_DAYS), f"got {deep}")

    check("без состояния (первый запуск) окно базовое",
          main.daily_window_start(now, None) == normal)

    future = main.daily_window_start(now, (now + timedelta(days=2)).date())
    check("дата из будущего (сбитые часы) окно не ломает", future == normal, f"got {future}")

    print("\n[2] Окно и REPORT_HOUR больше не две несвязанные константы")
    saved_hour = config.REPORT_HOUR
    try:
        config.REPORT_HOUR = 10  # как в config.example.py, где стоит 0
        prev_end = datetime.combine(yesterday, datetime.min.time()).replace(hour=10)
        today_start = main.daily_window_start(now.replace(hour=10), yesterday)
        check("при REPORT_HOUR=10 стык окон без дыры", today_start <= prev_end,
              f"окно начинается {today_start}, прошлый выпуск кончился {prev_end}")
        naive_gap = (now.replace(hour=10) - timedelta(days=1)).replace(
            hour=main.DIGEST_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
        check("старая арифметика на том же REPORT_HOUR дыру давала",
              naive_gap > prev_end, "дыры не было — проверка ничего не значит")
    finally:
        config.REPORT_HOUR = saved_hour

    print("\n[3] Сквозная проверка на настоящей базе: между выпусками ничего не выпало")
    # Заполняем трое суток по одному сообщению в час.
    seeded = {}
    stamp = now - timedelta(days=3)
    msg_id = 41000
    while stamp <= now:
        seeded[msg_id] = stamp
        await seed(msg_id, stamp, f"Реплика про эндодонтию в {stamp:%d.%m %H:%M}")
        msg_id += 1
        stamp += timedelta(hours=1)
    check("трое суток переписки записаны", len(seeded) > 60, f"got {len(seeded)}")

    prev_issue_at = now - timedelta(days=2)          # выпуск позавчера
    # ... вчерашний выпуск не состоялся ...
    today_start = main.daily_window_start(now, prev_issue_at.date())
    today_rows = await database.get_messages_for_daily_summary(today_start, now, min_count=0)
    today_ids = {row[0] for row in today_rows}

    after_prev = {mid for mid, when in seeded.items() if when > prev_issue_at}
    lost = after_prev - today_ids
    check("после пропущенного дня в выпуск попало ВСЁ, что было с прошлого выпуска",
          not lost, f"потеряно {len(lost)} сообщений, например {sorted(lost)[:5]}")

    old_start = (now - timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0)
    old_rows = await database.get_messages_for_daily_summary(old_start, now, min_count=0)
    old_lost = after_prev - {row[0] for row in old_rows}
    check("прежнее окно на этих же данных теряло около суток",
          len(old_lost) >= 18, f"потеряно всего {len(old_lost)} — проверка ничего не значит")

    # --------------------------------------------------------------- недельный
    print("\n[4] Недельный отчёт: пропущенный понедельник не теряется")
    monday = now - timedelta(days=now.weekday())          # понедельник этой недели
    monday_10 = monday.replace(hour=10, minute=5, second=0, microsecond=0)
    check("понедельник, 10:05, неделя прошла -> выпуск",
          main.weekly_report_due(monday_10, (monday - timedelta(days=7)).date()))
    check("понедельник до 10:00 -> ещё рано",
          not main.weekly_report_due(monday_10.replace(hour=9), (monday - timedelta(days=7)).date()))
    check("сегодня уже отправляли -> второй раз не идём",
          not main.weekly_report_due(monday_10, monday_10.date()))

    wednesday = monday_10 + timedelta(days=2)
    check("пропущенный понедельник догоняется в среду",
          main.weekly_report_due(wednesday, (wednesday - timedelta(days=9)).date()),
          "выпуск потерян навсегда")
    check("свежий отчёт в середине недели не переиздаётся",
          not main.weekly_report_due(wednesday, (wednesday - timedelta(days=3)).date()))
    check("без состояния догон не выдумывает выпуск",
          not main.weekly_report_due(wednesday, None))
    sunday_catchup = monday_10 + timedelta(days=6)
    check("догон в воскресенье не даёт вторую газету в понедельник",
          not main.weekly_report_due(sunday_catchup + timedelta(days=1), sunday_catchup.date()),
          "два выпуска и две страницы Telegraph за трое суток")

    weekly_normal = main.weekly_window_start(monday_10, (monday_10 - timedelta(days=7)).date())
    # Точного равенства требовать нельзя: граница выравнивается на ЧАС отчёта
    # прошлого выпуска (минуты обнуляются), а now здесь 10:05. Окно выходит на
    # пять минут ШИРЕ семи суток — то есть захватывает больше, а не теряет, и
    # это ровно то поведение, которое нужно: продолжать от времени прошлого
    # отчёта. Прежняя версия проверки сравнивала на равенство и падала на
    # верном коде. Проверяем СВОЙСТВО: не уже семи суток и не глубже, чем на
    # сутки, иначе это уже догон, а не обычное окно.
    normal_base = monday_10 - timedelta(days=main.WEEKLY_WINDOW_DAYS)
    check("обычное недельное окно покрывает семь суток целиком",
          weekly_normal <= normal_base, f"got {weekly_normal}, надо не позже {normal_base}")
    check("и не растягивается сверх этого больше чем на сутки",
          weekly_normal >= normal_base - timedelta(days=1),
          f"got {weekly_normal}")

    weekly_catchup = main.weekly_window_start(wednesday, (wednesday - timedelta(days=9)).date())
    check("догоняющее окно тянется дальше семи суток",
          weekly_catchup < wednesday - timedelta(days=main.WEEKLY_WINDOW_DAYS),
          f"got {weekly_catchup}")
    check("но не глубже потолка догона",
          weekly_catchup >= wednesday - timedelta(days=main.WEEKLY_CATCHUP_MAX_DAYS),
          f"got {weekly_catchup}")

    # Сквозная: девятидневная реплика обязана попасть в догоняющий недельный.
    await seed(43001, now - timedelta(days=9), "Разбор случая девятидневной давности")
    await seed(43002, now - timedelta(days=8), "Спор про уступ восьмидневной давности")
    weekly_rows = await database.get_messages_for_range(
        main.weekly_window_start(now, (now - timedelta(days=9)).date()), now)
    weekly_ids = {row[0] for row in weekly_rows}
    check("сообщения пропущенной недели попали в догоняющий отчёт",
          {43001, 43002} <= weekly_ids, f"нет: {sorted({43001, 43002} - weekly_ids)}")
    old_weekly_ids = {row[0] for row in await database.get_messages_for_range(
        now - timedelta(days=7), now)}
    check("прежнее окно (ровно 7 суток) их не видело",
          not ({43001, 43002} & old_weekly_ids), "проверка ничего не значит")

    # -------------------------------------------------------------------- кэш
    print("\n[5] Кэш дайджеста живёт между кругами цикла, а не внутри одного")
    saved_targets = getattr(config, "REPORT_TARGETS", [])
    saved_hour = config.REPORT_HOUR
    real_daily_query = database.get_messages_for_daily_summary
    real_range_query = database.get_messages_for_range
    real_mark = database.mark_messages_as_summarized
    real_batch = summarizer.process_summary_batch
    real_weekly = summarizer.process_weekly_batch

    generations = []   # None -> генерация с нуля, текст -> отправка кэша
    marked = []
    cycles = {"n": 0}

    async def fake_daily_query(start_time, end_time, min_count=100):
        return [(i, "Врач", None, f"Реплика {i}", None, "2026-07-01 10:00:00", None, None)
                for i in range(120)]

    async def fake_range_query(start_dt, end_dt):
        return []

    async def fake_mark(ids):
        marked.append(list(ids))

    async def fake_batch(messages, client, chat_id=None, topic_id=None, msg_count=0,
                         cached_message=None, delivery_hook=None):
        generations.append(cached_message)
        if chat_id == -1002:
            return None      # сломанная цель: не доставлено
        return "<b>Дайджест</b> за сутки"

    async def counting_sleep(delay, *args, **kwargs):
        # Длинный сон = конец круга планировщика. Считаем круги и выходим.
        if delay and delay >= 60:
            cycles["n"] += 1
            if cycles["n"] >= 3:
                raise asyncio.CancelledError()
            return await _REAL_SLEEP(0)
        return await _REAL_SLEEP(0)

    try:
        config.REPORT_TARGETS = [{"chat_id": -1001, "topic_id": None},
                                 {"chat_id": -1002, "topic_id": 26}]
        config.REPORT_HOUR = 0            # чтобы дневная ветка сработала сейчас
        database.get_messages_for_daily_summary = fake_daily_query
        database.get_messages_for_range = fake_range_query
        database.mark_messages_as_summarized = fake_mark
        summarizer.process_summary_batch = fake_batch
        # Недельную ветку в этом прогоне глушим: её проверяет раздел [4].
        main.save_scheduler_state((now - timedelta(days=1)).date(), now.date(), {})
        asyncio.sleep = counting_sleep
        task = asyncio.ensure_future(main.scheduler_task(object()))
        done, pending = await asyncio.wait({task}, timeout=20)
        for leftover in pending:
            leftover.cancel()
    finally:
        asyncio.sleep = _REAL_SLEEP
        config.REPORT_TARGETS = saved_targets
        config.REPORT_HOUR = saved_hour
        database.get_messages_for_daily_summary = real_daily_query
        database.get_messages_for_range = real_range_query
        database.mark_messages_as_summarized = real_mark
        summarizer.process_summary_batch = real_batch
        summarizer.process_weekly_batch = real_weekly

    check("планировщик отработал три круга и остановлен", cycles["n"] >= 3, f"got {cycles}")
    check("генерация с нуля произошла РОВНО один раз",
          generations.count(None) == 1,
          f"генераций с нуля {generations.count(None)} на {len(generations)} отправок")
    check("сломанная цель получает кэш, а не новую генерацию",
          len(generations) > 1 and all(g is not None for g in generations[1:]),
          f"got {['cache' if g else 'GEN' for g in generations]}")
    check("сообщения не помечены прочитанными, пока доставлено не во все цели",
          marked == [], f"got {marked}")
    check("доставленная цель во втором круге не переотправляется",
          main.load_sent_targets("daily", now.date()) == {"-1001:main"},
          f"got {main.load_sent_targets('daily', now.date())}")

    # ------------------------------------------------------------------ пинги
    print("\n[6] Зависшая фаза пингов больше не останавливает цикл навсегда")
    real_pm = assistant.check_and_send_pm_pings
    real_group = assistant.check_and_send_group_activity_pings
    saved_ping_timeout = main.PING_PHASE_TIMEOUT_SECONDS
    pm_calls, group_calls, hour_sleeps = [], [], []

    async def hanging_pm(bot):
        pm_calls.append(1)
        await _REAL_SLEEP(30)

    async def quick_group(bot):
        group_calls.append(1)

    async def watching_sleep(delay, *args, **kwargs):
        if delay and delay >= 60:
            hour_sleeps.append(delay)
            return await _REAL_SLEEP(0.01)
        return await _REAL_SLEEP(0)

    try:
        main.PING_PHASE_TIMEOUT_SECONDS = 0.05
        assistant.check_and_send_pm_pings = hanging_pm
        assistant.check_and_send_group_activity_pings = quick_group
        asyncio.sleep = watching_sleep
        ping_task = asyncio.ensure_future(main.pm_ping_scheduler_task(object()))
        await _REAL_SLEEP(0.6)
        ping_task.cancel()
        await asyncio.gather(ping_task, return_exceptions=True)

        check("зависший проход ЛС прерван по таймауту", len(pm_calls) >= 1)
        check("вторая фаза (групповые пинги) всё равно выполнена", len(group_calls) >= 1,
              "цикл встал на первой фазе — пинги мертвы навсегда")
        check("цикл дошёл до часового сна, то есть круг завершился", len(hour_sleeps) >= 1)

        pm_calls.clear(); group_calls.clear(); hour_sleeps.clear()

        async def quick_pm(bot):
            pm_calls.append(1)

        async def hanging_group(bot):
            group_calls.append(1)
            await _REAL_SLEEP(30)

        assistant.check_and_send_pm_pings = quick_pm
        assistant.check_and_send_group_activity_pings = hanging_group
        ping_task = asyncio.ensure_future(main.pm_ping_scheduler_task(object()))
        await _REAL_SLEEP(0.6)
        ping_task.cancel()
        await asyncio.gather(ping_task, return_exceptions=True)
        check("зависшая групповая фаза тоже прерывается, цикл идёт дальше",
              len(pm_calls) >= 2, f"кругов всего {len(pm_calls)}")
    finally:
        asyncio.sleep = _REAL_SLEEP
        main.PING_PHASE_TIMEOUT_SECONDS = saved_ping_timeout
        assistant.check_and_send_pm_pings = real_pm
        assistant.check_and_send_group_activity_pings = real_group

    # ------------------------------------------------------------------- медиа
    print("\n[7] Догон медиа не оплачивает Vision дважды за один снимок")
    real_start_workers = main.start_media_analysis_workers
    real_get_messages = main.client.get_messages
    fetches = []

    async def fake_get_messages(chat, ids=None, **kwargs):
        fetches.append(list(ids or []))
        return [FakeMessage(i, photo=True) for i in (ids or [])]

    try:
        main.start_media_analysis_workers = lambda: None
        main.client.get_messages = fake_get_messages
        main._media_queue = asyncio.Queue(maxsize=64)
        main._QUEUED_MEDIA_IDS.clear()
        main._QUEUED_MEDIA_ORDER.clear()

        await seed(77001, datetime.now(), "снимок 26 зуба", has_media=True, media_type="photo")
        pending_ids = [row[0] for row in await database.get_pending_media_message_ids(5)]
        check("свежий снимок виден как ожидающий разбора", 77001 in pending_ids, f"got {pending_ids}")

        queued = await main.enqueue_media_analysis([FakeMessage(77001, photo=True)], 77001, "снимок")
        check("sync_history поставил снимок в очередь", queued and main._media_queue.qsize() == 1)

        await main.recover_pending_media_analysis()
        check("догон не полез в Telegram за уже поставленным", fetches == [], f"got {fetches}")
        check("очередь не выросла — Vision не оплачен второй раз",
              main._media_queue.qsize() == 1, f"got {main._media_queue.qsize()}")

        album = [FakeMessage(77010 + i, photo=True) for i in range(3)]
        await main.enqueue_media_analysis(album, 77010, "альбом")
        check("альбом запомнен всеми строками, а не только первой",
              all((77010 + i) in main._QUEUED_MEDIA_IDS for i in range(3)))

        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait(("x", 0, "", None))
        main._media_queue = full_queue
        rejected = await main.enqueue_media_analysis([FakeMessage(77020, photo=True)], 77020,
                                                    "не влезло", bulk=True)
        check("непоставленное (очередь полна) НЕ считается поставленным",
              (not rejected) and 77020 not in main._QUEUED_MEDIA_IDS)

        # После рестарта память процесса пуста — догон обязан снимок подобрать.
        main._media_queue = asyncio.Queue(maxsize=64)
        main._QUEUED_MEDIA_IDS.clear()
        main._QUEUED_MEDIA_ORDER.clear()
        await main.recover_pending_media_analysis()
        check("после рестарта догон по-прежнему подбирает необработанное",
              main._media_queue.qsize() >= 1 and fetches, f"queue={main._media_queue.qsize()}")
    finally:
        main.start_media_analysis_workers = real_start_workers
        main.client.get_messages = real_get_messages
        main._QUEUED_MEDIA_IDS.clear()
        main._QUEUED_MEDIA_ORDER.clear()

    print("\n[8] Альбом: неудача скачивания отмечается, описание не расползается")
    DESC = "На снимке перфорация в области 26"
    real_describe = main.vision.describe_image
    real_media_assistant = assistant.check_and_trigger_assistant_media
    real_frame = main.extract_first_frame_async
    vision_calls = []

    async def fake_describe(files, caption=None, is_passive=True):
        vision_calls.append(list(files))
        return DESC

    async def noop_media_assistant(*args, **kwargs):
        return None

    async def failing_frame(path, timeout=None):
        return None

    try:
        main.vision.describe_image = fake_describe
        assistant.check_and_trigger_assistant_media = noop_media_assistant

        print("  -- ни один файл не подготовлен (D)")
        dead = [FakeMessage(78001, photo=True, download="fail"),
                FakeMessage(78002, photo=True, download="none")]
        for message in dead:
            await seed(message.id, datetime.now(), "альбом кейса", has_media=True, media_type="photo")
        await main.process_media_message(dead, 78001, "альбом кейса")
        marks = [await database.get_media_description(m.id) for m in dead]
        check("провал скачивания отмечен в базе",
              all(mark == main.MEDIA_UNAVAILABLE_MARK for mark in marks), f"got {marks}")
        still_pending = [row[0] for row in await database.get_pending_media_message_ids(20)]
        check("снимок больше не висит в вечном догоне",
              not ({78001, 78002} & set(still_pending)), f"got {still_pending}")
        check("Vision на пустом наборе файлов не вызывался", vision_calls == [])

        print("  -- альбом подготовлен частично (E)")
        partial = [FakeMessage(78011, photo=True),
                   FakeMessage(78012, photo=True, download="fail"),
                   FakeMessage(78013, photo=True, download="none")]
        for message in partial:
            await seed(message.id, datetime.now(), "альбом кейса", has_media=True, media_type="photo")
        await main.process_media_message(partial, 78011, "альбом кейса")
        got = [await database.get_media_description(m.id) for m in partial]
        check("Vision получил ровно один файл", len(vision_calls[-1]) == 1, f"got {vision_calls[-1]}")
        check("описание записано снимку, который дошёл до Vision", got[0] == DESC, f"got {got[0]}")
        check("чужим строкам альбома описание НЕ разослано",
              got[1] != DESC and got[2] != DESC, f"got {got}")
        check("и они помечены недоступными, а не оставлены пустыми",
              got[1] == main.MEDIA_UNAVAILABLE_MARK and got[2] == main.MEDIA_UNAVAILABLE_MARK,
              f"got {got}")

        print("  -- целый альбом (регресс: поведение не изменилось)")
        whole = [FakeMessage(78021 + i, photo=True) for i in range(3)]
        for message in whole:
            await seed(message.id, datetime.now(), "альбом кейса", has_media=True, media_type="photo")
        await main.process_media_message(whole, 78021, "альбом кейса")
        got = [await database.get_media_description(m.id) for m in whole]
        check("всем трём строкам записано общее описание альбома",
              got == [DESC, DESC, DESC], f"got {got}")
        check("Vision получил все три файла", len(vision_calls[-1]) == 3)

        print("  -- видео без кадра (кадр не извлёкся)")
        main.extract_first_frame_async = failing_frame
        mixed = [FakeMessage(78031, photo=True), FakeMessage(78032, video=True)]
        for message in mixed:
            await seed(message.id, datetime.now(), "фото и видео", has_media=True, media_type="photo")
        await main.process_media_message(mixed, 78031, "фото и видео")
        got = [await database.get_media_description(m.id) for m in mixed]
        check("фото получило описание", got[0] == DESC, f"got {got[0]}")
        check("видео без кадра описание фотографии не получило",
              got[1] == main.MEDIA_UNAVAILABLE_MARK, f"got {got[1]}")
    finally:
        main.vision.describe_image = real_describe
        assistant.check_and_trigger_assistant_media = real_media_assistant
        main.extract_first_frame_async = real_frame


try:
    asyncio.run(run())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
    os.chdir(_REPO)
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print("\n[9] Боевые файлы не тронуты")
for _path, _before in _LIVE_BEFORE.items():
    _now_state = (os.path.getmtime(_path), os.path.getsize(_path)) if os.path.exists(_path) else None
    check(f"{os.path.basename(_path)} не изменён", _now_state == _before,
          f"было {_before}, стало {_now_state}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
