"""
Бот поднимается целиком — проверка сборки, а не отдельных деталей.

Зачем отдельный набор. Правок в подъём внесено много, а бот работает на другой
машине — проверить его здесь запуском нельзя. Все прочие тесты проверяют куски:
словарь, фильтр, разметку. Ни один не отвечал на главный вопрос — соберётся ли
start_bot вообще и дойдёт ли до момента, когда слушатель чата активен.

Здесь выполняется НАСТОЯЩИЙ start_bot: реальные init_db, init_assistant,
регистрация обработчиков, запуск всех фоновых задач. Заглушены только сами
клиенты Telegram — сеть не трогается. База берётся копией боевой во временном
каталоге, пути сердцебиения и статуса уводятся туда же, сторож с os._exit
отключается: он существует, чтобы убивать процесс, и в тесте это лишнее.

Запуск: python test_startup_boot.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_boot_")

import config  # noqa: E402

for _ext in ("", "-wal", "-shm"):
    if os.path.exists("stomat_bot.db" + _ext):
        shutil.copy2("stomat_bot.db" + _ext, os.path.join(_TMPDIR, "boot.db" + _ext))
config.DB_PATH = os.path.join(_TMPDIR, "boot.db")

import runtime_guard  # noqa: E402

runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "hb.json")
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "status.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "dump.txt")
# Сторож убивает процесс через os._exit. В тесте он не нужен и опасен.
runtime_guard.start_watchdog = lambda *a, **k: None
runtime_guard.stop_watchdog = lambda *a, **k: None

import main  # noqa: E402
import assistant  # noqa: E402
import database  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


print("\n[1] Обработчики Telegram зарегистрированы на настоящих клиентах")
# Декораторы срабатывают при импорте, до всяких заглушек. Если обработчик
# отвалится, бот поднимется молча и просто не будет отвечать.
user_handlers = {getattr(cb, "__name__", "?"): b for cb, b in main.client.list_event_handlers()}
bot_handlers = {getattr(cb, "__name__", "?"): b for cb, b in main.bot_client.list_event_handlers()}
for name in ("handle_new_message", "handle_edited_message", "handle_deleted_messages"):
    check(f"юзербот: {name}", name in user_handlers)
for name in ("handle_private_message", "handle_callback_query"):
    check(f"bot_client: {name}", name in bot_handlers)

check("новые сообщения слушаются в основном чате",
      config.SOURCE_CHAT_ID in (getattr(user_handlers.get("handle_new_message"), "chats", None) or []),
      "основной чат не под наблюдением")
# Правки и удаления применяются ТОЛЬКО из основного чата, и это осознанно:
# в таблице messages нет колонки chat_id, id уникальны лишь внутри чата, и
# удаление #4821 в тестовом чате снесло бы строку основного.
for name in ("handle_edited_message", "handle_deleted_messages"):
    chats = getattr(user_handlers.get(name), "chats", None) or []
    check(f"{name} ограничен основным чатом", list(chats) == list(main.SAVED_CHATS),
          f"got {chats}, ожидалось {main.SAVED_CHATS}")

print("\n[2] Отладочные команды доступны только владельцу аккаунта")
# Они висят без фильтра чатов, поэтому единственная защита — outgoing=True.
for name in ("dump_handler", "get_chat_id", "manual_test_handler", "manual_weekly_test"):
    builder = user_handlers.get(name)
    check(f"{name} только для исходящих", builder is not None and getattr(builder, "outgoing", None) is True,
          "команду сможет вызвать посторонний в личке аккаунта")


def fake_client():
    client = MagicMock()
    client.start = AsyncMock(return_value=None)
    client.disconnect = AsyncMock(return_value=None)
    client.run_until_disconnected = AsyncMock(return_value=None)
    client.get_me = AsyncMock(return_value=MagicMock(id=123456789, username="stomchat_bot"))
    client.get_entity = AsyncMock(return_value=MagicMock(id=config.SOURCE_CHAT_ID))
    client.send_message = AsyncMock(return_value=MagicMock(id=1))
    client.is_connected = MagicMock(return_value=True)

    async def _iter(*args, **kwargs):
        return
        yield  # pragma: no cover

    client.iter_messages = _iter
    return client


async def boot(bot_start_error=None):
    """Проводит настоящий start_bot с подменёнными клиентами Telegram."""
    real_client, real_bot = main.client, main.bot_client
    real_create = runtime_guard.create_task
    started = []
    main.client = fake_client()
    main.bot_client = fake_client()
    if bot_start_error:
        main.bot_client.start = AsyncMock(side_effect=bot_start_error)

    def traced(coro, label):
        started.append(label)
        return real_create(coro, label)

    runtime_guard.create_task = traced
    try:
        await asyncio.wait_for(main.start_bot(), timeout=120)
        return started, None, main.client
    except Exception as err:
        return started, err, main.client
    finally:
        runtime_guard.create_task = real_create
        stubs = (main.client, main.bot_client)
        main.client, main.bot_client = real_client, real_bot
        boot.last_stubs = stubs


async def run():
    print("\n[3] start_bot доходит до конца без ошибок")
    started, err, _ = await boot()
    check("подъём завершился", err is None, f"{type(err).__name__ if err else ''}: {err}")

    print("\n[4] Все фоновые задачи запущены")
    for task in ("heartbeat", "scheduler", "pm_ping_scheduler", "runtime_telemetry",
                 "summary_watchdog", "health_watchdog"):
        check(f"задача {task}", task in started, f"запущены: {started}")
    check("воркер разбора медиа поднят",
          any(t.startswith("media_analysis") for t in started), f"got {started}")

    print("\n[5] Сердцебиение стартует ДО сетевого подъёма")
    # Сторож стреляет, если heartbeat не обновлялся 300 с, а бюджет таймаутов
    # подъёма складывается в 300 с. Раньше сердцебиение запускалось последним,
    # и на медленной сети бот уходил в цикл перезапусков, ни разу не встав.
    check("heartbeat запущен первым", started and started[0] == "heartbeat", f"got {started[:3]}")

    print("\n[6] Личность бота определена")
    check("BOT_ID проставлен", assistant.BOT_ID == 123456789, f"got {assistant.BOT_ID}")
    check("username проставлен", assistant.BOT_USERNAME == "stomchat_bot",
          f"got {assistant.BOT_USERNAME}")

    print("\n[7] База поднята на копии, а не на боевой")
    check("работали на копии", "stomchat_boot_" in config.DB_PATH, config.DB_PATH)
    tables = await database.list_tables() if hasattr(database, "list_tables") else None
    if tables is None:
        import sqlite3
        tables = [r[0] for r in sqlite3.connect(config.DB_PATH).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
    for name in ("messages", "user_profiles", "clinical_bookmarks", "pm_messages"):
        check(f"таблица {name} создана", name in tables, f"got {sorted(tables)}")

    print("\n[8] Отказ bot_client не оставляет процесс висеть")
    # Если бот-клиент не поднялся, start.bat обязан получить исключение и
    # перезапустить процесс. Молчаливое продолжение означало бы живой процесс
    # без единого обработчика личных сообщений.
    started, err, stub_client = await boot(bot_start_error=RuntimeError("нет сети"))
    check("ошибка проброшена наружу", isinstance(err, RuntimeError), f"got {err!r}")
    check("юзербот отключён перед выходом", stub_client.disconnect.await_count >= 1,
          "соединение осталось висеть")


async def run_sync_checks():
    print("\n[9] Догоняющая синхронизация после долгого простоя")
    # Синхронизация обёрнута в wait_for, и если она не укладывается, падает ВЕСЬ
    # подъём: start.bat поднимает процесс заново, и снаружи это выглядит как
    # «бот не запускается».
    #
    # Расчётный долг для проверки берём как месяц простоя при 228 сообщениях в
    # сутки — это порядок величины по локальному снимку базы, а не факт о боевом
    # состоянии: базы в репозитории нет, снимок может отставать. Проверка ловит
    # именно диспропорцию бюджетов, и от точности оценки она не зависит.
    ASSUMED_BACKLOG = 228 * 30
    check("бюджет синхронизации больше бюджета подключения",
          main.SYNC_HISTORY_TIMEOUT_SECONDS > main.START_TIMEOUT_SECONDS,
          f"{main.SYNC_HISTORY_TIMEOUT_SECONDS} против {main.START_TIMEOUT_SECONDS}")
    per_message_budget = main.SYNC_HISTORY_TIMEOUT_SECONDS / ASSUMED_BACKLOG
    check(f"на сообщение остаётся хотя бы 0.05 с при долге в {ASSUMED_BACKLOG} реплик",
          per_message_budget >= 0.05, f"{per_message_budget:.3f} с на сообщение")
    # Запрос автора необязателен: при отказе пишется "Unknown". Его таймаут
    # обязан быть НАМНОГО меньше общего, иначе несколько зависаний съедают
    # весь подъём. При 60 с хватало пяти.
    stalls = main.SYNC_HISTORY_TIMEOUT_SECONDS / main.SYNC_SENDER_TIMEOUT_SECONDS
    check("одно зависание запроса автора не съедает подъём", stalls >= 30,
          f"хватит {stalls:.0f} зависаний, чтобы сорвать синхронизацию")
    check("таймаут автора меньше общего сетевого",
          main.SYNC_SENDER_TIMEOUT_SECONDS < main.TELEGRAM_REQUEST_TIMEOUT_SECONDS)

    print("\n[10] Автор запрашивается один раз на отправителя, а не на сообщение")
    from datetime import datetime, timezone

    lookups = {"count": 0}

    def fake_message(index, sender_id):
        message = MagicMock()
        message.id = 500000 + index
        message.sender_id = sender_id
        message.message = f"клиническая реплика {index}"
        message.reply_to = None
        message.photo = None
        message.video = None
        message.document = None
        message.grouped_id = None
        message.date = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

        async def get_sender():
            lookups["count"] += 1
            sender = MagicMock()
            sender.first_name = f"Врач{sender_id}"
            sender.last_name = ""
            sender.username = f"user{sender_id}"
            del sender.title
            return sender

        message.get_sender = get_sender
        return message

    senders = [900 + i for i in range(20)]
    messages = [fake_message(i, senders[i % len(senders)]) for i in range(400)]

    async def iter_messages(*args, **kwargs):
        for message in messages:
            yield message

    real_client = main.client
    main.client = MagicMock()
    main.client.iter_messages = iter_messages
    try:
        await main.sync_history()
    finally:
        main.client = real_client

    check("запросов ровно по числу отправителей",
          lookups["count"] == len(senders),
          f"{lookups['count']} запросов на {len(messages)} сообщений при {len(senders)} авторах")
    check("экономия запросов не меньше 90%",
          lookups["count"] <= len(messages) * 0.1,
          f"{100 * (1 - lookups['count'] / len(messages)):.0f}%")

    import sqlite3
    saved = sqlite3.connect(config.DB_PATH).execute(
        "SELECT COUNT(*) FROM messages WHERE msg_id >= 500000").fetchone()[0]
    check("все догнанные сообщения сохранены", saved == len(messages),
          f"сохранено {saved} из {len(messages)}")
    names = sqlite3.connect(config.DB_PATH).execute(
        "SELECT DISTINCT sender_name FROM messages WHERE msg_id >= 500000").fetchall()
    check("имена авторов проставлены, а не Unknown",
          all(n[0].startswith("Врач") for n in names), f"got {names[:4]}")


async def run_media_checks():
    print("\n[11] Долг медиа после простоя не топит журнал и не теряется")
    # За 37 дней простоя набирается около 985 сообщений с медиа (26.6 в сутки
    # по живой базе), а очередь разбора вмещает MEDIA_QUEUE_MAX_SIZE=128 при
    # одном воркере. Раньше каждое непоместившееся писало отдельную строку
    # ERROR: около 857 подряд. Разбор — вещь дорогая и небыстрая, поэтому
    # отложить их правильно; неправильно было делать это шумно и молча.
    import logging
    from datetime import datetime, timezone

    errors = []

    class Catch(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append(record.getMessage())

    catcher = Catch()
    logging.getLogger().addHandler(catcher)

    def media_message(index):
        message = MagicMock()
        message.id = 700000 + index
        message.sender_id = 42
        message.message = f"снимок {index}"
        message.reply_to = None
        message.photo = MagicMock()
        message.video = None
        message.document = None
        message.grouped_id = None
        message.date = datetime(2026, 7, 1, tzinfo=timezone.utc)

        async def get_sender():
            sender = MagicMock()
            sender.first_name = "Врач"
            sender.last_name = ""
            sender.username = "u"
            del sender.title
            return sender

        message.get_sender = get_sender
        return message

    messages = [media_message(i) for i in range(200)]
    capacity = 20

    async def iter_messages(*args, **kwargs):
        for message in messages:
            yield message

    real_client = main.client
    real_start = main.start_media_analysis_workers
    real_queue = main._media_queue
    main.client = MagicMock()
    main.client.iter_messages = iter_messages
    main.start_media_analysis_workers = lambda *a, **k: None
    main._media_queue = asyncio.Queue(maxsize=capacity)
    try:
        await main.sync_history()
        queued = main._media_queue.qsize()
    finally:
        main.client = real_client
        main.start_media_analysis_workers = real_start
        main._media_queue = real_queue
        logging.getLogger().removeHandler(catcher)

    overflow_errors = [e for e in errors if "queue full" in e]
    check("очередь заполнена ровно до предела", queued == capacity,
          f"got {queued} при пределе {capacity}")
    check("переполнение не пишет строку ERROR на каждый снимок",
          not overflow_errors, f"{len(overflow_errors)} строк вместо сводной")

    import sqlite3
    pending = sqlite3.connect(config.DB_PATH).execute(
        "SELECT COUNT(*) FROM messages WHERE msg_id >= 700000 AND has_media = 1 "
        "AND (media_description IS NULL OR TRIM(media_description) = '')").fetchone()[0]
    check("отложенные остались в базе и восстановимы", pending == len(messages),
          f"в базе {pending} из {len(messages)}")

    print("\n[12] Механизм догона медиа на месте")
    check("recover_pending_media_analysis существует",
          callable(getattr(main, "recover_pending_media_analysis", None)))
    check("у догона есть настраиваемый предел",
          isinstance(main.MEDIA_RECOVERY_LIMIT, int) and main.MEDIA_RECOVERY_LIMIT >= 0,
          f"got {main.MEDIA_RECOVERY_LIMIT}")
    check("очередь вмещает больше, чем берёт один заход догона",
          main.MEDIA_QUEUE_MAX_SIZE > main.MEDIA_RECOVERY_LIMIT,
          f"очередь {main.MEDIA_QUEUE_MAX_SIZE}, догон {main.MEDIA_RECOVERY_LIMIT}")
    # Живая работа — другое дело: там переполнение означает потерянный снимок
    # коллеги, и молчать о нём нельзя.
    source = io.open("main.py", encoding="utf-8").read()
    enqueue = source.split("async def enqueue_media_analysis", 1)[1].split("\nasync def ", 1)[0]
    check("в живой работе переполнение остаётся ошибкой",
          "if bulk:" in enqueue and "logger.error" in enqueue,
          "тихий отказ распространился на обычную работу")


import io  # noqa: E402

asyncio.run(run())
asyncio.run(run_sync_checks())
asyncio.run(run_media_checks())
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
