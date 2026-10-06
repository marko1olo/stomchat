"""
Снимки, присланные документом (несжатый рентген/КТ), в основном чате.

Тест гоняет настоящий main.handle_new_message с настоящими telethon-объектами
Message и проверяет результат чтением из настоящей базы SQLite. Заглушены
только постановка в очередь анализа, отправка в Telegram и триггеры ассистента.

Отдельно проверяется, что стикер (документ с mime image/webp) снимком НЕ
считается: иначе каждый стикер в чате уезжал бы в платный vision.

Запуск: python test_snapshot_document.py
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
import runtime_guard

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_snapshot_")
config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")
# Путь обработчика ведёт в генерацию, а та в finally пишет флаг статуса.
# Боевой bot_summary_status.json трогать нельзя.
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")

TEST_CHAT_ID = -1001234567890
config.SOURCE_CHAT_ID = TEST_CHAT_ID

import database
import main
import assistant
import media_tools
from telethon.tl import types

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


ENQUEUED = []
SENT = []


async def fake_enqueue(messages, msg_id, text):
    ENQUEUED.append({"msg_id": msg_id, "count": len(messages), "text": text})


async def fake_send_message(entity=None, message=None, reply_to=None, parse_mode=None, **kw):
    SENT.append({"message": message, "reply_to": reply_to})
    return None


async def _no_trigger(*a, **kw):
    return False


main.enqueue_media_analysis = fake_enqueue
main.bot_client.send_message = fake_send_message
assistant.check_and_trigger_assistant = _no_trigger
assistant.check_bot_mention_trigger = _no_trigger
assistant.check_and_trigger_referee = _no_trigger


class Event(main.TelethonEventAdapter):
    def __init__(self, message, sender=None):
        super().__init__(message)
        self._sender = sender

    async def get_sender(self):
        return self._sender


class Sender:
    first_name = "Пётр"
    last_name = "Сидоров"
    username = "psidorov"
    bot = False


def document(mime, attributes=None):
    return types.Document(
        id=1, access_hash=2, file_reference=b"",
        date=datetime(2026, 7, 27, tzinfo=timezone.utc),
        mime_type=mime, size=4096, dc_id=2,
        attributes=attributes or [],
    )


def doc_message(msg_id, mime, caption="", attributes=None):
    return types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
        from_id=types.PeerUser(555),
        message=caption,
        date=datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
        media=types.MessageMediaDocument(document=document(mime, attributes)),
    )


def sticker_message(msg_id):
    return doc_message(
        msg_id, "image/webp",
        attributes=[types.DocumentAttributeSticker(alt="🦷", stickerset=types.InputStickerSetEmpty())],
    )


async def media_flag(msg_id):
    def operation():
        with database._connection() as db:
            row = db.execute(
                "SELECT has_media, media_type FROM messages WHERE msg_id = ?", (msg_id,)
            ).fetchone()
            return row

    return await database._run_db(operation)


async def run():
    await database.init_db()

    print("\n[1] Классификация вложений на настоящих объектах telethon")
    cases = [
        ("image/jpeg", "рентген JPEG", True),
        ("image/png", "снимок PNG", True),
        ("image/webp", "webp-картинка", True),
        ("application/pdf", "выписка PDF", False),
        ("application/zip", "архив", False),
        ("video/mp4", "видео файлом", False),
        ("audio/ogg", "аудио файлом", False),
    ]
    for mime, label, expected in cases:
        got = media_tools.image_document(doc_message(1, mime)) is not None
        check(f"{label} ({mime}) -> снимок={expected}", got is expected, f"got {got}")

    check("стикер снимком НЕ считается",
          media_tools.image_document(sticker_message(2)) is None)
    check("сообщение без вложений не падает",
          media_tools.image_document(
              types.Message(id=3, peer_id=types.PeerChannel(1),
                            message="просто текст",
                            date=datetime(2026, 7, 27, tzinfo=timezone.utc))) is None)

    print("\n[2] Снимок документом доходит до базы и до анализа")
    main.PROCESSED_MSG_IDS.clear()
    ENQUEUED.clear()
    await main.handle_new_message(
        Event(doc_message(7101, "image/jpeg", caption="прицельный 36, что скажете?"), Sender())
    )
    row = await media_flag(7101)
    check("строка сохранена", row is not None)
    check("has_media выставлен", row is not None and bool(row[0]), f"got {row}")
    check("media_type = photo", row is not None and row[1] == "photo", f"got {row}")
    check("поставлен в очередь анализа", len(ENQUEUED) == 1, f"got {ENQUEUED}")
    check("подпись передана в анализ",
          ENQUEUED and "прицельный 36" in ENQUEUED[0]["text"], f"got {ENQUEUED}")

    print("\n[3] Снимок попадает в очередь необработанного медиа")
    pending = {m[0] for m in await database.get_pending_media_message_ids(limit=20)}
    check("виден как медиа без описания", 7101 in pending, f"got {sorted(pending)}")

    print("\n[4] Стикер в анализ не уходит")
    main.PROCESSED_MSG_IDS.clear()
    ENQUEUED.clear()
    await main.handle_new_message(Event(sticker_message(7102), Sender()))
    row = await media_flag(7102)
    check("стикер сохранён как обычное сообщение", row is not None)
    check("has_media не выставлен", row is not None and not row[0], f"got {row}")
    check("в vision не отправлен", ENQUEUED == [], f"got {ENQUEUED}")

    print("\n[5] PDF не выдаёт себя за снимок")
    main.PROCESSED_MSG_IDS.clear()
    ENQUEUED.clear()
    await main.handle_new_message(Event(doc_message(7103, "application/pdf", caption="выписка"), Sender()))
    row = await media_flag(7103)
    check("has_media не выставлен", row is not None and not row[0], f"got {row}")
    check("в очередь анализа не поставлен", ENQUEUED == [], f"got {ENQUEUED}")

    print("\n[6] Подпись-команда по-прежнему отменяет авто-анализ")
    main.PROCESSED_MSG_IDS.clear()
    ENQUEUED.clear()
    await main.handle_new_message(Event(doc_message(7104, "image/jpeg", caption="/итог"), Sender()))
    check("команда не запускает vision", ENQUEUED == [], f"got {ENQUEUED}")
    row = await media_flag(7104)
    check("но медиа в базе отмечено", row is not None and bool(row[0]), f"got {row}")

    print("\n[7] Обычное фото не сломано")
    main.PROCESSED_MSG_IDS.clear()
    ENQUEUED.clear()
    photo = types.Message(
        id=7105,
        peer_id=types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
        from_id=types.PeerUser(555),
        message="снимок фотографией",
        date=datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
        media=types.MessageMediaPhoto(photo=types.Photo(
            id=9, access_hash=9, file_reference=b"",
            date=datetime(2026, 7, 27, tzinfo=timezone.utc),
            sizes=[], dc_id=2, has_stickers=False,
        )),
    )
    await main.handle_new_message(Event(photo, Sender()))
    row = await media_flag(7105)
    check("фото по-прежнему media_type=photo", row is not None and row[1] == "photo", f"got {row}")
    check("фото по-прежнему уходит в анализ", len(ENQUEUED) == 1, f"got {ENQUEUED}")

    print("\n[8] Синхронизация истории размечает снимки-документы так же")
    synced = doc_message(7106, "image/jpeg", caption="догруженный снимок")
    snapshot = media_tools.image_document(synced)
    check("sync-путь опознаёт тот же документ", snapshot is not None)


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
