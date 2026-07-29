"""
Голосовые, накопившиеся за офлайн, и молчание группы на отказе расшифровки.

Два закрытых дефекта:

  1. Догон офлайн-окна (sync_history) голосовые НЕ расшифровывал. Он сохранял
     строку как есть, а clinical_media_kind для голосового отдаёт None — значит
     has_media=0, значит ни очередь разбора медиа, ни догон неразобранного
     (он выбирает has_media = 1) их не подбирают. Расшифровку получало РОВНО
     ОДНО голосовое окна — последнее сообщение, и только потому, что sync_history
     прогоняет last_synced_message через handle_new_message.
  2. На отказе расшифровки в группе бот не говорил НИЧЕГО (в ЛС говорит), а
     успешная расшифровка могла не дойти до базы: результат update_message_text
     не проверялся, а при отсутствии строки он равен 0.

Здесь гоняются НАСТОЯЩИЕ main.sync_history, main.transcribe_voice_backlog и
main.handle_new_message на настоящих telethon-объектах Message (голосовой
документ с DocumentAttributeAudio(voice=True)) и настоящей базе SQLite во
временном файле. Заглушены только внешние службы: Whisper, отправка в Telegram,
триггеры ассистента и iter_messages клиента. Сети нет, платных вызовов нет.

Проверяется ПОВЕДЕНИЕ: что расшифровано, что легло в базу, что сказано в чате.

Запуск: python test_voice_offline.py
"""
import asyncio
import inspect
import logging
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

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_voffline_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import config  # noqa: E402

config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")
TEST_CHAT_ID = -1001234567890
config.SOURCE_CHAT_ID = TEST_CHAT_ID

import runtime_guard  # noqa: E402

runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "hb.json")
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "status.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "dump.txt")

import assistant  # noqa: E402
import blocking_tools  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
from telethon.tl import types  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# --- Внешние службы: только они и заглушены -------------------------------
# Бюджет правки терминов забираем ДО подмены: он участвует в проверке
# вложенности бюджетов, и брать его из заглушки было бы самообманом.
_REAL_CORRECTION_TIMEOUT = inspect.signature(
    blocking_tools.correct_dental_transcription_async
).parameters["timeout"].default

SENT = []
WHISPER_CALLS = []
WHISPER = {"error": None, "text": None, "delay": 0.0}


async def fake_send_message(entity=None, message=None, reply_to=None, parse_mode=None, **kw):
    SENT.append({"entity": entity, "message": message, "reply_to": reply_to})
    return types.Message(
        id=90000 + len(SENT),
        peer_id=types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
        message=message or "",
        date=datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
    )


main.bot_client.send_message = fake_send_message


async def _no_trigger(*a, **kw):
    return False


assistant.check_and_trigger_assistant = _no_trigger
assistant.check_bot_mention_trigger = _no_trigger
assistant.check_and_trigger_referee = _no_trigger


def _msg_id_from_path(path):
    base = os.path.basename(path)
    digits = "".join(ch for ch in base if ch.isdigit())
    return int(digits) if digits else 0


async def fake_transcribe(path, timeout=None):
    """Whisper. Текст свой у каждого сообщения — иначе не видно, куда он лёг."""
    WHISPER_CALLS.append(_msg_id_from_path(path))
    if WHISPER["delay"]:
        await asyncio.sleep(WHISPER["delay"])
    if WHISPER["error"]:
        return None, WHISPER["error"]
    if WHISPER["text"] is not None:
        return WHISPER["text"], None
    return f"расшифровка голосового {_msg_id_from_path(path)}", None


async def fake_correct(raw):
    return raw


blocking_tools.transcribe_audio_async = fake_transcribe
blocking_tools.correct_dental_transcription_async = fake_correct


def reset_whisper(error=None, text=None, delay=0.0):
    WHISPER["error"] = error
    WHISPER["text"] = text
    WHISPER["delay"] = delay
    del WHISPER_CALLS[:]


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass

    def find(self, needle):
        return [line for line in self.lines if needle in line]


LOG = LogCapture()
logging.getLogger().addHandler(LOG)


class FakeSender:
    def __init__(self, first_name="Пётр", bot=False):
        self.first_name = first_name
        self.last_name = "Сидоров"
        self.username = "psidorov"
        self.bot = bot


class FakeClient:
    """Клиент Telegram: из него sync_history берёт только окно сообщений."""

    def __init__(self, messages):
        self.messages = messages
        self.iter_calls = []

    def iter_messages(self, chat, min_id=0, reverse=False, **kw):
        self.iter_calls.append({"chat": chat, "min_id": min_id, "reverse": reverse})

        async def generate():
            for message in self.messages:
                if message.id > min_id:
                    yield message

        return generate()


def _base_message(msg_id, text="", media=None, sender_id=555, minute=0):
    message = types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
        from_id=types.PeerUser(sender_id),
        message=text,
        date=datetime(2026, 7, 29, 9, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute),
        media=media,
    )

    async def get_sender():
        return FakeSender()

    message.get_sender = get_sender
    return message


def voice_message(msg_id, sender_id=555, minute=0):
    document = types.Document(
        id=1, access_hash=2, file_reference=b"",
        date=datetime(2026, 7, 29, tzinfo=timezone.utc),
        mime_type="audio/ogg", size=2048, dc_id=2,
        attributes=[types.DocumentAttributeAudio(duration=9, voice=True)],
    )
    message = _base_message(
        msg_id, media=types.MessageMediaDocument(document=document),
        sender_id=sender_id, minute=minute,
    )

    async def fake_download(file=None, **kw):
        if WHISPER.get("hang_download"):
            await asyncio.sleep(30)
        path = os.path.join(_TMPDIR, f"voice_{msg_id}.ogg")
        with open(path, "wb") as handle:
            handle.write(b"\x00" * 32)
        return path

    message.download_media = fake_download
    return message


def text_message(msg_id, text="проверка", minute=0):
    return _base_message(msg_id, text=text, minute=minute)


class VoiceEvent(main.TelethonEventAdapter):
    """Рабочий адаптер main + подставной отправитель (сети в тесте нет)."""

    def __init__(self, message, sender=None):
        super().__init__(message)
        self._sender = sender or FakeSender()

    async def get_sender(self):
        return self._sender


async def stored_text(msg_id):
    row = await database.get_text_by_id(msg_id)
    return row[1] if row else None


async def row_exists(msg_id):
    return await database.get_text_by_id(msg_id) is not None


async def drain_background(timeout=30):
    """Дожидается задач, поставленных runtime_guard.create_task."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        pending = [task for task in list(runtime_guard._ACTIVE_TASKS) if not task.done()]
        if not pending:
            return
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            raise AssertionError(f"фоновые задачи не завершились: {[t.get_name() for t in pending]}")
        await asyncio.wait(pending, timeout=left)


def clear_state():
    del SENT[:]
    del LOG.lines[:]
    main.PROCESSED_MSG_IDS.clear()
    main._TRANSCRIBED_MSG_IDS.clear()
    main._TRANSCRIBED_ORDER.clear()
    # Окно тишины между жалобами на отказ (ключ — чат плюс автор) для теста тоже
    # состояние сценария: без сброса второй сценарий подряд от того же автора
    # получил бы подавление вместо ответа. Саму серию отказов и схлопывание
    # проверяет test_voice_pipeline.py, там окно НЕ сбрасывается специально.
    main._VOICE_FAILURE_NOTICE.clear()
    WHISPER["hang_download"] = False


async def run_sync(messages):
    """Настоящий sync_history на подставном клиенте плюс ожидание фоновых задач."""
    real_client = main.client
    main.client = FakeClient(messages)
    try:
        await asyncio.wait_for(main.sync_history(), timeout=60)
        await drain_background()
    finally:
        main.client = real_client


async def run():
    await database.init_db()

    print("\n[1] Догон офлайн-окна расшифровывает ВСЕ голосовые, а не только последнее")
    # ЗАМЕР. stomat_archive.db, 117 847 сообщений: 395 строк голосовых/аудио
    # (media_type='file' без подписи — дампер deppd.py пишет 'file' для любого
    # документа, а голосовое Telegram это документ). Размеры окон догона взяты из
    # боевых журналов: bot.log (июль) — 170 непустых окон, 6240 догнанных
    # сообщений, максимум 587, среднее 36.7; bot.log.1 (май) — 22 окна, 386
    # сообщений, максимум 134, среднее 17.5.
    #
    # Симуляция прежней логики (шанс есть только у последнего сообщения окна):
    # при окне 17 расшифровку получали 20 голосовых из 395, терялось 375 (94.9%);
    # при окне 37 терялось 389; при окне 587 — все 395. Диктовка идёт серями:
    # 68 серий из двух и более голосовых подряд, самая длинная — 19.
    #
    # СЦЕНАРИЙ ОТКАЗА. Бот лежал десять минут; врач за это время продиктовал три
    # голосовых с разбором случая. После подъёма в базе три строки с text='',
    # в чат не ушло ни одной транскрипции, и случай не попал ни в дайджест, ни в
    # контекст ответов, ни в поиск по истории. Восстановить нечем: строка
    # голосового (text='', has_media=0, media_type=NULL) неотличима от стикера.
    clear_state()
    reset_whisper()
    await database.save_message(
        msg_id=7000, sender_id=555, sender_name="Пётр Сидоров", sender_username="psidorov",
        text="последнее до падения", date=datetime(2026, 7, 29, 8, 0, 0, tzinfo=timezone.utc),
    )
    window = [
        text_message(7001, "коллеги, вопрос по 36", minute=1),
        voice_message(7002, minute=2),
        voice_message(7003, minute=3),
        voice_message(7004, minute=4),
    ]
    await run_sync(window)

    check("все три голосовых расшифрованы", sorted(WHISPER_CALLS) == [7002, 7003, 7004],
          f"в Whisper ушли {sorted(WHISPER_CALLS)} — потеряны "
          f"{sorted({7002, 7003, 7004} - set(WHISPER_CALLS))}")
    for msg_id in (7002, 7003, 7004):
        check(f"расшифровка msg_id={msg_id} легла в свою строку базы",
              await stored_text(msg_id) == f"расшифровка голосового {msg_id}",
              f"got {await stored_text(msg_id)!r}")
    check("текстовое сообщение окна не тронуто",
          await stored_text(7001) == "коллеги, вопрос по 36", f"got {await stored_text(7001)!r}")
    check("в чат ушли ровно три транскрипции", len(SENT) == 3,
          f"got {len(SENT)}: {[s['message'][:40] for s in SENT]}")
    check("каждая транскрипция привязана к своему голосовому",
          sorted(s["reply_to"] for s in SENT) == [7002, 7003, 7004],
          f"got {sorted(s['reply_to'] for s in SENT)}")
    check("транскрипции ушли в исходный чат",
          all(s["entity"] == TEST_CHAT_ID for s in SENT), f"got {[s['entity'] for s in SENT]}")
    check("опубликован именно расшифрованный текст",
          all(f"расшифровка голосового {s['reply_to']}" in s["message"] for s in SENT))
    check("последнее голосовое не расшифровано дважды",
          WHISPER_CALLS.count(7004) == 1, f"got {WHISPER_CALLS.count(7004)}")
    # Вторая страховка от того же дубля: последнее сообщение окна вообще не
    # уходит в живой обработчик, если оно уже отдано в догон расшифровки.
    check("последнее голосовое окна не прогоняется через живой обработчик",
          not LOG.find("Обрабатываем последнее пропущенное сообщение msg_id=7004"),
          "оно уже в догоне расшифровки: второй прогон — второй платный Whisper "
          "и вторая транскрипция в чате")
    check("догон голосовых виден в журнале", LOG.find("voice backlog done"),
          "заход догона не оставил следа — разбирать отказы будет нечем")

    print("\n[2] Очередь догона ограничена, и отброшенное названо по id")
    # ЗАМЕР. Максимальное окно догона по bot.log — 587 сообщений; самая длинная
    # серия подряд идущих голосовых по архиву — 19. Одно голосовое стоит до
    # VOICE_ITEM_BUDGET_SECONDS (225 с) и платного вызова Whisper, поэтому
    # неограниченная очередь означала бы часы расшифровки на каждом подъёме.
    #
    # СЦЕНАРИЙ ОТКАЗА при БЕСшумном обрезании: бот берёт первые сколько-то,
    # остальные молча выбрасывает — и никто, включая дежурного, не знает, что
    # диктовки за половину окна не существует. Поэтому предел есть, но
    # отброшенное уходит в журнал по id И получает отметку в базе.
    clear_state()
    reset_whisper()
    real_max = main.VOICE_BACKLOG_MAX_ITEMS
    main.VOICE_BACKLOG_MAX_ITEMS = 3
    try:
        window = [voice_message(7100 + i, minute=i) for i in range(25)]
        await run_sync(window)
    finally:
        main.VOICE_BACKLOG_MAX_ITEMS = real_max

    expected_done = [7122, 7123, 7124]
    check("расшифровано ровно столько, сколько разрешено пределом",
          sorted(WHISPER_CALLS) == expected_done, f"got {sorted(WHISPER_CALLS)}")
    check("взяты самые свежие голосовые, а не первые попавшиеся",
          sorted(s["reply_to"] for s in SENT) == expected_done,
          f"got {sorted(s['reply_to'] for s in SENT)}")
    truncated = LOG.find("voice backlog truncated")
    check("обрезание очереди записано в журнал", bool(truncated),
          "молчаливое обрезание: 22 диктовки исчезли бы без следа")
    check("в журнале названо число отброшенных", truncated and "dropped=22" in truncated[0],
          f"got {truncated[:1]}")
    check("в журнале названы id отброшенных", truncated and "7100" in truncated[0],
          f"got {truncated[:1]}")
    check("отброшенное помечено в базе, а не осталось пустой строкой",
          await stored_text(7100) == main.VOICE_UNRECOGNIZED_MARK,
          f"got {await stored_text(7100)!r}")
    check("помеченная строка отличима от стикера и пустого сообщения",
          "голосовое" in (await stored_text(7101) or ""), f"got {await stored_text(7101)!r}")
    check("отброшенное в чат не отправлялось", len(SENT) == 3, f"got {len(SENT)}")

    print("\n[3] Бюджеты вложены, и общий бюджет реально останавливает заход")
    # ЗАМЕР/ПРИЧИНА. Внутренний бюджет, не влезающий во внешний, — единственный
    # класс дефекта, который в этом проекте всплывал четыре раза
    # (test_budget_nesting.py): сторож сводки 1800 против генерации 2100, родитель
    # подпроцесса без запаса на импорты, каскад зрения 891 против потолка 180,
    # перебор 7 ключей whisper 222 с против родительских 70 с.
    #
    # СЦЕНАРИЙ ОТКАЗА. Догон встал на первом же голосовом (DC отдаёт файл
    # рывками): без общего бюджета задача висит часами и держит очередь, а
    # остальные диктовки не расшифрованы и никем не посчитаны.
    slack = getattr(blocking_tools, "_SUBPROCESS_STARTUP_SLACK_SECONDS", 0)
    worst_item = (main.VOICE_DOWNLOAD_TIMEOUT_SECONDS
                  + main.VOICE_TRANSCRIBE_TIMEOUT_SECONDS
                  + slack + _REAL_CORRECTION_TIMEOUT)
    print(f"       худшее одно голосовое {worst_item:g} с "
          f"(скачивание {main.VOICE_DOWNLOAD_TIMEOUT_SECONDS} + whisper "
          f"{main.VOICE_TRANSCRIBE_TIMEOUT_SECONDS} + запас подпроцесса {slack:g} + "
          f"правка терминов {_REAL_CORRECTION_TIMEOUT}), бюджет одного "
          f"{main.VOICE_ITEM_BUDGET_SECONDS} с, общий {main.VOICE_BACKLOG_TOTAL_SECONDS} с")
    check("бюджет одного голосового покрывает все его шаги",
          worst_item <= main.VOICE_ITEM_BUDGET_SECONDS,
          f"{worst_item:g} с против {main.VOICE_ITEM_BUDGET_SECONDS} с — расшифровку убьют "
          f"на середине, и попытка будет выброшена")
    check("бюджет одного голосового вложен в общий бюджет захода",
          main.VOICE_ITEM_BUDGET_SECONDS < main.VOICE_BACKLOG_TOTAL_SECONDS,
          f"{main.VOICE_ITEM_BUDGET_SECONDS} против {main.VOICE_BACKLOG_TOTAL_SECONDS}")
    check("в общий бюджет влезает минимум два голосовых",
          main.VOICE_BACKLOG_TOTAL_SECONDS >= main.VOICE_ITEM_BUDGET_SECONDS * 2,
          "догон, в который не влезает даже одна диктовка, — это не догон")
    check("порог остатка меньше бюджета одного голосового",
          main.VOICE_BACKLOG_MIN_ITEM_SECONDS < main.VOICE_ITEM_BUDGET_SECONDS,
          f"{main.VOICE_BACKLOG_MIN_ITEM_SECONDS} против {main.VOICE_ITEM_BUDGET_SECONDS}")
    check("бюджет одного выведен из слагаемых, а не задан числом",
          "VOICE_DOWNLOAD_TIMEOUT_SECONDS + VOICE_TRANSCRIBE_TIMEOUT_SECONDS" in MAIN_SRC,
          "два независимых числа снова разъедутся")

    clear_state()
    reset_whisper(delay=30)
    real_total = main.VOICE_BACKLOG_TOTAL_SECONDS
    real_item = main.VOICE_ITEM_BUDGET_SECONDS
    real_min = main.VOICE_BACKLOG_MIN_ITEM_SECONDS
    main.VOICE_BACKLOG_TOTAL_SECONDS = 0.6
    main.VOICE_ITEM_BUDGET_SECONDS = 0.4
    main.VOICE_BACKLOG_MIN_ITEM_SECONDS = 0.3
    hangers = [voice_message(7200 + i, minute=i) for i in range(5)]
    for message in hangers:
        await database.save_message(
            msg_id=message.id, sender_id=555, sender_name="Пётр Сидоров",
            sender_username="psidorov", text="", date=message.date,
        )
    started = asyncio.get_running_loop().time()
    try:
        done = await asyncio.wait_for(main.transcribe_voice_backlog(hangers, source="test"), timeout=20)
    finally:
        elapsed = asyncio.get_running_loop().time() - started
        main.VOICE_BACKLOG_TOTAL_SECONDS = real_total
        main.VOICE_ITEM_BUDGET_SECONDS = real_item
        main.VOICE_BACKLOG_MIN_ITEM_SECONDS = real_min

    check("зависшая расшифровка не держит заход дольше общего бюджета", elapsed < 5,
          f"elapsed={elapsed:.1f} с при пяти зависших по 30 с")
    check("на зависших расшифровках успешных нет", done == 0, f"got {done}")
    check("после исчерпания бюджета следующие голосовые не начинались",
          WHISPER_CALLS == [7200],
          f"в Whisper ушли {WHISPER_CALLS} — срок одного голосового не ограничен остатком "
          f"общего, и заход тратит время сверх своего бюджета")
    check("исчерпание бюджета записано в журнал", LOG.find("voice backlog out of budget"),
          "заход просто оборвался бы, и остаток окна никто не пересчитал")
    check("нерасшифрованный остаток помечен в базе",
          await stored_text(7204) == main.VOICE_UNRECOGNIZED_MARK,
          f"got {await stored_text(7204)!r}")
    check("зависшая расшифровка не выдумала текст",
          await stored_text(7200) == main.VOICE_UNRECOGNIZED_MARK,
          f"got {await stored_text(7200)!r}")

    print("\n[4] Повторная доставка не расшифровывает и не публикует дважды")
    # ЗАМЕР/ПРИЧИНА. Дубль транскрипции пользователи уже видели: sync_history
    # прогонял последнее сообщение окна через handle_new_message, и голосовое
    # расшифровывалось второй раз — второй платный Whisper и вторая «🎤
    # Транскрипция» в чате. health_watchdog запускает sync_history каждые 5 минут
    # (по bot.log — 195 проходов), то есть шанс на дубль не редкий.
    #
    # СЦЕНАРИЙ ОТКАЗА. Врач продиктовал случай; после такта сторожа в чате висят
    # две одинаковые транскрипции его голосового, а бюджет Whisper потрачен вдвое.
    clear_state()
    reset_whisper()
    window = [text_message(7300, "текст перед голосовым", minute=1), voice_message(7301, minute=2)]
    await run_sync(window)
    first_calls = list(WHISPER_CALLS)
    first_sent = len(SENT)

    # Тот же апдейт прилетает снова — уже как живое сообщение.
    main.PROCESSED_MSG_IDS.clear()
    await main.handle_new_message(VoiceEvent(voice_message(7301, minute=2)))
    await drain_background()
    check("повторная доставка не пошла в Whisper второй раз",
          WHISPER_CALLS == first_calls, f"было {first_calls}, стало {WHISPER_CALLS}")
    check("вторая транскрипция в чат не ушла", len(SENT) == first_sent,
          f"было {first_sent}, стало {len(SENT)}: {[s['message'][:30] for s in SENT]}")
    check("текст в базе остался расшифровкой", await stored_text(7301) == "расшифровка голосового 7301",
          f"got {await stored_text(7301)!r}")
    # И повторный заход догона тем же списком тоже пустой.
    again = await main.transcribe_voice_backlog([voice_message(7301, minute=2)], source="test")
    check("повторный заход догона ничего не расшифровывает", again == 0, f"got {again}")

    print("\n[5] На отказе расшифровки группа больше не молчит")
    # ЗАМЕР/ПРИЧИНА. Шесть тупиков голосового пути отдавали одно и то же None и
    # не отправляли врачу ничего: пустое скачивание, ошибка Whisper, пустая
    # расшифровка, галлюцинация тишины, таймаут скачивания, исключение. В ЛС на
    # том же месте бот отвечает (assistant.py: «❌ Не удалось распознать аудио»).
    # По bot.log и bot.log.1 успешных расшифровок не подтверждено ни одной, то
    # есть в проде врач видел именно тишину.
    #
    # СЦЕНАРИЙ ОТКАЗА. Врач продиктовал в группу разбор случая, Groq ответил 429
    # по всем ключам. Врач видит своё голосовое, уверен, что бот его услышал,
    # диктовку не повторяет. В базе text='' — случай выпал из дайджеста, из
    # контекста ответов и из поиска, и по строке уже не понять, что это была
    # диктовка.
    clear_state()
    reset_whisper(error="429 rate limit on all keys")
    await main.handle_new_message(VoiceEvent(voice_message(7400, minute=1)))
    await drain_background()
    check("на отказе Whisper в чат ушла ровно одна строка", len(SENT) == 1,
          f"got {len(SENT)}: {[s['message'][:40] for s in SENT]}")
    check("врачу сказано, что распознать не удалось",
          SENT and "Не удалось распознать" in SENT[0]["message"], f"got {SENT[:1]}")
    check("ответ привязан к его голосовому", SENT and SENT[0]["reply_to"] == 7400,
          f"got {SENT[0]['reply_to'] if SENT else None}")
    check("ложная транскрипция не опубликована",
          all("Транскрипция" not in s["message"] for s in SENT), f"got {SENT}")
    check("строка голосового помечена в базе, а не осталась пустой",
          await stored_text(7400) == main.VOICE_UNRECOGNIZED_MARK,
          f"got {await stored_text(7400)!r}")
    check("причина отказа названа в журнале", LOG.find("429 rate limit"),
          "разбирать отказ будет нечем")

    clear_state()
    reset_whisper()
    WHISPER["hang_download"] = True
    real_download = main.VOICE_DOWNLOAD_TIMEOUT_SECONDS
    main.VOICE_DOWNLOAD_TIMEOUT_SECONDS = 0.2
    try:
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(main.handle_new_message(VoiceEvent(voice_message(7401, minute=2))), timeout=15)
        elapsed = asyncio.get_running_loop().time() - started
        await drain_background()
    finally:
        main.VOICE_DOWNLOAD_TIMEOUT_SECONDS = real_download
        WHISPER["hang_download"] = False
    check("зависшее скачивание ограничено таймаутом", elapsed < 5, f"elapsed={elapsed:.1f} с")
    check("на таймауте скачивания врач тоже получил ответ",
          len(SENT) == 1 and "Не удалось распознать" in SENT[0]["message"], f"got {SENT}")
    check("сообщение при этом сохранено", await row_exists(7401))

    clear_state()
    reset_whisper(text="Продолжение следует...")
    await main.handle_new_message(VoiceEvent(voice_message(7402, minute=3)))
    await drain_background()
    # Галлюцинация тишины — единственный случай, когда группа молчит осознанно:
    # аудио разобрано, речи в нём нет, терять нечего, а реплика бота на каждый
    # чужой шум в чате 749 врачей — это шум. В ЛС врач диалог с ботом ведёт сам,
    # поэтому там ответ уместен.
    check("на тишине и шуме в чат не уходит ничего", SENT == [], f"got {SENT}")
    check("текст сообщения при тишине остался пустым", await stored_text(7402) == "",
          f"got {await stored_text(7402)!r}")
    check("отметки «не распознано» на тишине не ставится",
          await stored_text(7402) != main.VOICE_UNRECOGNIZED_MARK)

    print("\n[6] Успешная расшифровка доходит до базы даже при провале записи строки")
    # ЗАМЕР/ПРИЧИНА. Результат database.update_message_text не проверялся, а он
    # возвращает rowcount и 0, когда строки нет, гася исключения внутрь лога базы.
    # То есть при провалившемся save_message (рядом на этот случай стоит громкий
    # MESSAGE NOT PERSISTED) транскрипция уходила в чат, а в базу не попадала
    # ВООБЩЕ, и ни одной строки об этом в журнале не появлялось.
    #
    # СЦЕНАРИЙ ОТКАЗА. Антивирус на секунду держит файл базы (типовая причина
    # отказа записи на этой машине — тот же случай разбирает database.py в
    # комментарии про повторные попытки). Врач видит в чате свою транскрипцию и
    # считает случай записанным, а в дайджест, в контекст и в поиск он не попал.
    clear_state()
    reset_whisper()
    real_save = database.save_message
    failures = {"left": 1}

    async def flaky_save(**kw):
        if failures["left"] > 0:
            failures["left"] -= 1
            return False
        return await real_save(**kw)

    database.save_message = flaky_save
    try:
        await main.handle_new_message(VoiceEvent(voice_message(7500, minute=1)))
        await drain_background()
    finally:
        database.save_message = real_save

    check("провал записи строки замечен громко", LOG.find("MESSAGE NOT PERSISTED"),
          "отказ save_message снова неотличим от успеха")
    check("пропажа строки под расшифровкой замечена", LOG.find("VOICE TEXT NOT PERSISTED"),
          "update_message_text вернул 0 строк, и это опять никого не смутило")
    check("расшифровка всё равно доехала до базы",
          await stored_text(7500) == "расшифровка голосового 7500",
          f"got {await stored_text(7500)!r}")
    check("транскрипция при этом опубликована",
          len(SENT) == 1 and "расшифровка голосового 7500" in SENT[0]["message"], f"got {SENT}")

    print("\n[7] Проверки выше ловят поломку")
    check("подставной Whisper действительно считает вызовы", WHISPER_CALLS == [7500],
          f"got {WHISPER_CALLS}")
    check("чтение базы отличает отсутствие строки", await stored_text(999999) is None)
    check("перехват журнала не всеяден", not LOG.find("такой строки в журнале нет"))
    check("отметка непустой текст не затирает",
          await stored_text(7500) != main.VOICE_UNRECOGNIZED_MARK,
          "иначе отметка перезаписала бы готовую расшифровку")
    check("голосовое опознаётся, а текстовое — нет",
          main.is_voice_message(voice_message(7900)) is True
          and main.is_voice_message(text_message(7901)) is False,
          "правило опознания слепо: догон брал бы в Whisper обычные реплики")


MAIN_SRC = open("main.py", encoding="utf-8").read()

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
