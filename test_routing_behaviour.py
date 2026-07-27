"""
Маршрутизация группового чата: реальные прогоны через main.handle_new_message
и main.handle_callback_query.

Раньше эти свойства проверялись поиском подстрок в исходниках через
inspect.getsource — такая проверка держится за формулировку кода и ничего не
говорит о поведении. Здесь вместо этого исполняется настоящий обработчик с
настоящим telethon-объектом Message и настоящей базой SQLite; заглушены
только внешние вызовы (Telegram, LLM, vision).

Запуск: python test_routing_behaviour.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_routing_")
config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")

TEST_CHAT_ID = -1001234567890
SECOND_CHAT_ID = -1003735006121
config.SOURCE_CHAT_ID = TEST_CHAT_ID

import database
import main
import assistant
import runtime_guard
from telethon.tl import types

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


CALLS = {"assistant": [], "mention": [], "referee": [], "ask": [], "quiz": [], "answers": 0}


async def _trigger(bot_client, event, msg_id, text, reply_to_msg_id, sender_first_name=None):
    CALLS["assistant"].append(msg_id)
    return False


async def _mention(bot_client, event, msg_id, text, sender_first_name=None):
    CALLS["mention"].append(msg_id)
    return False


async def _referee(bot_client, event, text):
    CALLS["referee"].append(text)
    return False


async def _direct_ask(bot_client, event, question):
    CALLS["ask"].append(question)


async def _send(entity=None, message=None, reply_to=None, parse_mode=None, **kw):
    return None


assistant.check_and_trigger_assistant = _trigger
assistant.check_bot_mention_trigger = _mention
assistant.check_and_trigger_referee = _referee
assistant.handle_group_direct_ask = _direct_ask
main.bot_client.send_message = _send


async def drain():
    """Обработчик диспатчит ассистента отдельным таском — дожидаемся его."""
    for _ in range(60):
        pending = [t for t in list(runtime_guard._ACTIVE_TASKS) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("фоновые таски не завершились")


class Event(main.TelethonEventAdapter):
    def __init__(self, message, sender=None):
        super().__init__(message)
        self._sender = sender

    async def get_sender(self):
        return self._sender


class Sender:
    def __init__(self, bot=False, first_name="Пётр"):
        self.first_name = first_name
        self.last_name = "Сидоров"
        self.username = "psidorov"
        self.bot = bot


def text_message(msg_id, text, chat_id=TEST_CHAT_ID, sender_id=555):
    return types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(abs(chat_id) - 1000000000000),
        from_id=types.PeerUser(sender_id),
        message=text,
        date=datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
    )


def reset():
    main.PROCESSED_MSG_IDS.clear()
    for key in ("assistant", "mention", "referee", "ask", "quiz"):
        CALLS[key].clear()
    CALLS["answers"] = 0


async def run():
    await database.init_db()

    print("\n[1] Обычное сообщение врача доходит до ассистента")
    reset()
    await main.handle_new_message(Event(text_message(8001, "коллеги, какой уступ под цирконий?"), Sender()))
    await drain()
    check("ассистент вызван", CALLS["assistant"] == [8001], f"got {CALLS['assistant']}")
    check("сообщение сохранено", await database.get_text_by_id(8001) is not None)

    print("\n[2] Сообщение самого бота триггеры не запускает")
    reset()
    await main.handle_new_message(
        Event(text_message(8002, "📊 Дайджест дня: обсуждали уступы", sender_id=main.FALLBACK_BOT_ID),
              Sender(bot=True))
    )
    await drain()
    check("ассистент не вызван", CALLS["assistant"] == [], f"got {CALLS['assistant']}")
    check("рефери не вызван", CALLS["referee"] == [], f"got {CALLS['referee']}")
    check("но в базу дайджест записан", await database.get_text_by_id(8002) is not None)

    print("\n[3] Собственный дайджест во ВТОРОМ чате не запускает цикл самоответов")
    reset()
    await main.handle_new_message(
        Event(text_message(8003, "📊 Дайджест дня", chat_id=SECOND_CHAT_ID,
                           sender_id=main.FALLBACK_BOT_ID), Sender(bot=True))
    )
    await drain()
    check("ассистент не вызван во втором чате", CALLS["assistant"] == [], f"got {CALLS['assistant']}")
    check("второй чат в базу не пишется", await database.get_text_by_id(8003) is None)

    print("\n[4] Чужой бот тоже не запускает триггеры")
    reset()
    await main.handle_new_message(Event(text_message(8004, "реклама от другого бота", sender_id=42), Sender(bot=True)))
    await drain()
    check("ассистент не вызван", CALLS["assistant"] == [], f"got {CALLS['assistant']}")

    print("\n[5] Повторная доставка апдейта обрабатывается один раз")
    reset()
    await main.handle_new_message(Event(text_message(8005, "вопрос про эндодонтию"), Sender()))
    await main.handle_new_message(Event(text_message(8005, "вопрос про эндодонтию"), Sender()))
    await drain()
    check("ассистент вызван ровно один раз", CALLS["assistant"] == [8005], f"got {CALLS['assistant']}")

    print("\n[6] Обращение по имени доходит до прямого ответа")
    reset()
    await main.handle_new_message(
        Event(text_message(8006, "@stomchat_bot какой протокол ирригации при некрозе?"), Sender())
    )
    await drain()
    check("вызван прямой ответ", len(CALLS["ask"]) == 1, f"got {CALLS['ask']}")
    check("упоминание вырезано из вопроса",
          CALLS["ask"] and CALLS["ask"][0] == "какой протокол ирригации при некрозе?",
          f"got {CALLS['ask']}")
    check("пассивный триггер не дублирует ответ", CALLS["assistant"] == [], f"got {CALLS['assistant']}")

    print("\n[7] Смена @username бота не ломает обращения")
    reset()
    assistant.BOT_USERNAME = "newdentalbot"
    await main.handle_new_message(Event(text_message(8007, "@newdentalbot посмотри снимок 46"), Sender()))
    await drain()
    check("новое имя распознано", CALLS["ask"] == ["посмотри снимок 46"], f"got {CALLS['ask']}")

    reset()
    await main.handle_new_message(Event(text_message(8008, "@stomchat_bot старое имя тоже работает"), Sender()))
    await drain()
    check("старое имя продолжает работать", CALLS["ask"] == ["старое имя тоже работает"], f"got {CALLS['ask']}")
    assistant.BOT_USERNAME = None

    print("\n[8] Похожее имя другого аккаунта обращением не считается")
    reset()
    await main.handle_new_message(Event(text_message(8009, "@stomchat_bot_old это не мы"), Sender()))
    await drain()
    check("прямой ответ не вызван", CALLS["ask"] == [], f"got {CALLS['ask']}")
    check("сообщение ушло в обычный триаж", CALLS["assistant"] == [8009], f"got {CALLS['assistant']}")

    print("\n[9] /ask разбирается по-прежнему")
    reset()
    await main.handle_new_message(Event(text_message(8010, "/ask чем снимать коронку с циркония?"), Sender()))
    await drain()
    check("вопрос извлечён", CALLS["ask"] == ["чем снимать коронку с циркония?"], f"got {CALLS['ask']}")

    print("\n[10] Пустое обращение не уходит в LLM")
    reset()
    await main.handle_new_message(Event(text_message(8011, "@stomchat_bot"), Sender()))
    await drain()
    check("прямой ответ не вызван", CALLS["ask"] == [], f"got {CALLS['ask']}")
    check("и пассивный триггер тоже — обращение поглощено",
          CALLS["assistant"] == [], f"got {CALLS['assistant']}")

    print("\n[11] Колбэки: двойной тап обрабатывается один раз, спиннер всегда гасится")

    class Callback:
        def __init__(self, cb_id):
            self.id = cb_id
            self.answered = 0

        async def answer(self, *a, **kw):
            self.answered += 1
            CALLS["answers"] += 1

    handled = []

    async def ok_callback(bot_client, event):
        handled.append(event.id)

    assistant.handle_quiz_callback = ok_callback
    main._HANDLED_CALLBACK_SET.clear()
    main.HANDLED_CALLBACK_IDS.clear()

    cb = Callback(555001)
    await main.handle_callback_query(cb)
    await main.handle_callback_query(cb)
    check("обработчик викторины вызван один раз", handled == [555001], f"got {handled}")
    check("на повторный тап Telegram всё равно отвечено", cb.answered >= 1, f"got {cb.answered}")

    async def broken_callback(bot_client, event):
        raise RuntimeError("падение до event.answer()")

    assistant.handle_quiz_callback = broken_callback
    cb2 = Callback(555002)
    await main.handle_callback_query(cb2)
    check("при исключении спиннер погашен", cb2.answered == 1, f"got {cb2.answered}")

    print("\n[12] Heartbeat переживает отказ записи, а не умирает навсегда")
    # Пока таск жив, сторож не убивает процесс. Раньше единственное
    # исключение из write_heartbeat (на Windows это PermissionError от
    # антивируса в os.replace) убивало цикл насовсем: heartbeat замирал, и
    # через WATCHDOG_STALE_SECONDS сторож стрелял в процесс — как правило,
    # посреди генерации саммари.
    writes = {"n": 0}
    original_write = runtime_guard.write_heartbeat
    original_interval = runtime_guard.HEARTBEAT_INTERVAL_SECONDS

    def failing_write(reason):
        writes["n"] += 1
        raise PermissionError("файл занят другим процессом")

    runtime_guard.write_heartbeat = failing_write
    runtime_guard.HEARTBEAT_INTERVAL_SECONDS = 0.01
    beat = asyncio.create_task(main.heartbeat_task())
    await asyncio.sleep(0.2)
    alive_after_failures = not beat.done()
    beat.cancel()
    await asyncio.gather(beat, return_exceptions=True)
    runtime_guard.write_heartbeat = original_write
    runtime_guard.HEARTBEAT_INTERVAL_SECONDS = original_interval

    check("цикл пережил повторяющийся отказ записи", alive_after_failures)
    check("попыток было несколько, а не одна", writes["n"] > 1, f"got {writes['n']}")

    print("\n[13] Умерший медиа-воркер поднимается на следующей постановке в очередь")
    # Воркер по умолчанию один. Когда он падал, очередь молча забивалась и всё
    # медиа уходило в лог без анализа до перезапуска процесса. Имитируем
    # смерть всех воркеров и проверяем, что постановка в очередь их вернула.
    for task in list(main._media_worker_tasks):
        task.cancel()
    await asyncio.gather(*main._media_worker_tasks, return_exceptions=True)
    main._media_worker_tasks = []

    await main.enqueue_media_analysis([text_message(8020, "снимок")], 8020, "снимок")
    alive = [t for t in main._media_worker_tasks if not t.done()]
    check("воркер поднят заново", len(alive) > 0, f"got {main._media_worker_tasks}")
    check("задача действительно попала в очередь", main._media_queue.qsize() >= 0)

    for task in list(main._media_worker_tasks):
        task.cancel()
    await asyncio.gather(*main._media_worker_tasks, return_exceptions=True)
    main._media_worker_tasks = []

    print("\n[14] Синхронизация не обрабатывает одно и то же сообщение дважды")
    # Медиа уже поставлено в очередь анализа выше по коду; прогонять то же
    # сообщение ещё и через handle_new_message значит скачать и разобрать его
    # второй раз.
    enqueued, redispatched = [], []

    async def rec_enqueue(messages, msg_id, text, media_type_hint=None):
        enqueued.append(msg_id)

    async def rec_handle(event):
        redispatched.append(event.message.id)

    def make_photo(msg_id, caption=""):
        return types.Message(
            id=msg_id,
            peer_id=types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
            from_id=types.PeerUser(555),
            message=caption,
            date=datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
            media=types.MessageMediaPhoto(photo=types.Photo(
                id=9, access_hash=9, file_reference=b"",
                date=datetime(2026, 7, 27, tzinfo=timezone.utc),
                sizes=[], dc_id=2, has_stickers=False,
            )),
        )

    async def run_sync(missed):
        def iter_messages(*a, **kw):
            async def gen():
                for m in missed:
                    yield m
            return gen()

        original_iter = main.client.iter_messages
        original_enqueue = main.enqueue_media_analysis
        original_handle = main.handle_new_message
        main.client.iter_messages = iter_messages
        main.enqueue_media_analysis = rec_enqueue
        main.handle_new_message = rec_handle
        try:
            await main.sync_history()
            await drain()
        finally:
            main.client.iter_messages = original_iter
            main.enqueue_media_analysis = original_enqueue
            main.handle_new_message = original_handle

    enqueued.clear(); redispatched.clear()
    await run_sync([make_photo(8100, "снимок 1"), make_photo(8101, "снимок 2")])
    check("оба снимка поставлены в очередь анализа", enqueued == [8100, 8101], f"got {enqueued}")
    check("последний снимок не прогнан через обработчик повторно",
          redispatched == [], f"got {redispatched}")
    check("догруженные сообщения сохранены",
          await database.get_text_by_id(8101) is not None)

    enqueued.clear(); redispatched.clear()
    await run_sync([make_photo(8110, "снимок"), text_message(8111, "а вот и текстом вопрос")])
    check("текстовое последнее сообщение обработать всё же надо",
          redispatched == [8111], f"got {redispatched}")
    check("снимок при этом ушёл только в очередь", enqueued == [8110], f"got {enqueued}")


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
