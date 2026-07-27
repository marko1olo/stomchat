"""
Голосовые сообщения в группе: порядок сохранения и расшифровки.

Тест гоняет НАСТОЯЩИЙ main.handle_new_message с настоящим telethon-объектом
Message (голосовой документ с DocumentAttributeAudio(voice=True)), обёрнутым в
рабочий TelethonEventAdapter, и настоящей базой SQLite во временном файле.
Заглушены только внешние сервисы — Whisper, отправка в Telegram и триггеры
ассистента; вся проверяемая логика исполняется как в бою.

Главное утверждение: строка попадает в базу ДО начала расшифровки. Раньше
она появлялась только через минуту с лишним, и health_watchdog, сверяющий
максимальный id в чате с максимальным в базе, успевал объявить сообщение
пропущенным и прогнать его через обработчик второй раз.

Запуск: python test_voice_pipeline.py
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

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_voice_")
config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")

TEST_CHAT_ID = -1001234567890
config.SOURCE_CHAT_ID = TEST_CHAT_ID

import database
import main
import assistant
import blocking_tools
from telethon.tl import types

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# --- Внешние сервисы: только они и заглушены -------------------------------
SENT = []


async def fake_send_message(entity=None, message=None, reply_to=None, parse_mode=None, **kw):
    SENT.append({"entity": entity, "message": message, "reply_to": reply_to})
    return types.Message(
        id=90000 + len(SENT),
        peer_id=types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
        message=message or "",
        date=datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
    )


main.bot_client.send_message = fake_send_message


async def _no_trigger(*a, **kw):
    return False


assistant.check_and_trigger_assistant = _no_trigger
assistant.check_bot_mention_trigger = _no_trigger
assistant.check_and_trigger_referee = _no_trigger


class VoiceEvent(main.TelethonEventAdapter):
    """Рабочий адаптер main + подставной отправитель (сети в тесте нет)."""

    def __init__(self, message, sender=None):
        super().__init__(message)
        self._sender = sender

    async def get_sender(self):
        return self._sender


class FakeSender:
    def __init__(self, first_name="Пётр", bot=False):
        self.first_name = first_name
        self.last_name = "Сидоров"
        self.username = "psidorov"
        self.bot = bot


def voice_message(msg_id, sender_id=555):
    doc = types.Document(
        id=1, access_hash=2, file_reference=b"",
        date=datetime(2026, 7, 27, tzinfo=timezone.utc),
        mime_type="audio/ogg", size=2048, dc_id=2,
        attributes=[types.DocumentAttributeAudio(duration=9, voice=True)],
    )
    return types.Message(
        id=msg_id,
        peer_id=types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
        from_id=types.PeerUser(sender_id),
        message="",
        date=datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
        media=types.MessageMediaDocument(document=doc),
    )


def attach_download(message, before=None):
    async def fake_download(file=None, **kw):
        if before is not None:
            await before()
        path = os.path.join(_TMPDIR, f"voice_{message.id}.ogg")
        with open(path, "wb") as handle:
            handle.write(b"\x00" * 32)
        return path

    message.download_media = fake_download
    return message


def set_whisper(text, error=None, corrected=None):
    async def fake_transcribe(path, timeout=None):
        return (text, error)

    async def fake_correct(raw):
        return corrected if corrected is not None else raw

    blocking_tools.transcribe_audio_async = fake_transcribe
    blocking_tools.correct_dental_transcription_async = fake_correct


async def stored_text(msg_id):
    row = await database.get_text_by_id(msg_id)
    return row[1] if row else None


async def row_exists(msg_id):
    return await database.get_text_by_id(msg_id) is not None


async def run():
    await database.init_db()

    print("\n[1] Строка сохраняется ДО расшифровки, а не после неё")
    main.PROCESSED_MSG_IDS.clear()
    SENT.clear()
    set_whisper("канал тридцать шестого запломбирован до апекса")

    downloading = asyncio.Event()
    release = asyncio.Event()

    async def block_until_released():
        downloading.set()
        await release.wait()

    msg = attach_download(voice_message(6001), before=block_until_released)
    handler = asyncio.create_task(main.handle_new_message(VoiceEvent(msg, FakeSender())))

    await asyncio.wait_for(downloading.wait(), timeout=5)
    check("к началу скачивания сообщение уже в базе", await row_exists(6001))
    check("текста пока нет — расшифровка не завершена", await stored_text(6001) == "",
          f"got {await stored_text(6001)!r}")
    check("транскрипция в чат ещё не ушла", SENT == [])

    release.set()
    await asyncio.wait_for(handler, timeout=10)

    check("после расшифровки текст догнан в базу",
          await stored_text(6001) == "канал тридцать шестого запломбирован до апекса",
          f"got {await stored_text(6001)!r}")
    check("транскрипция опубликована ровно один раз", len(SENT) == 1, f"got {len(SENT)}")
    check("опубликован именно расшифрованный текст",
          SENT and "канал тридцать шестого" in SENT[0]["message"])
    check("ответ привязан к исходному голосовому", SENT and SENT[0]["reply_to"] == 6001)

    # Регистрацию отправленного в bot_sent_messages проверяет
    # test_wipe_tracking.py: делает это обёртка patched_send_message, которую
    # этот тест как раз подменяет заглушкой.

    print("\n[2] Галлюцинация Whisper на тишине не публикуется и не пишется в базу")
    main.PROCESSED_MSG_IDS.clear()
    SENT.clear()
    set_whisper("Продолжение следует...")
    msg = attach_download(voice_message(6002))
    await main.handle_new_message(VoiceEvent(msg, FakeSender()))
    check("в чат ничего не ушло", SENT == [], f"got {SENT}")
    check("текст сообщения остался пустым", await stored_text(6002) == "",
          f"got {await stored_text(6002)!r}")
    check("само сообщение при этом сохранено", await row_exists(6002))

    print("\n[3] Отказ Whisper не теряет сообщение")
    main.PROCESSED_MSG_IDS.clear()
    SENT.clear()
    set_whisper(None, error="whisper subprocess died")
    msg = attach_download(voice_message(6003))
    await main.handle_new_message(VoiceEvent(msg, FakeSender()))
    check("сообщение в базе есть", await row_exists(6003))
    check("публикации не было", SENT == [])

    print("\n[4] Пустая правка терминов откатывается к сырой расшифровке")
    main.PROCESSED_MSG_IDS.clear()
    SENT.clear()
    set_whisper("реставрация двадцать шестого", corrected="")
    msg = attach_download(voice_message(6004))
    await main.handle_new_message(VoiceEvent(msg, FakeSender()))
    check("сохранена сырая расшифровка, а не пустота",
          await stored_text(6004) == "реставрация двадцать шестого",
          f"got {await stored_text(6004)!r}")

    print("\n[5] Зависшее скачивание ограничено таймаутом")
    main.PROCESSED_MSG_IDS.clear()
    SENT.clear()
    set_whisper("этого не должно случиться")
    original_timeout = main.VOICE_DOWNLOAD_TIMEOUT_SECONDS
    main.VOICE_DOWNLOAD_TIMEOUT_SECONDS = 0.2

    async def hang():
        await asyncio.sleep(30)

    msg = attach_download(voice_message(6005), before=hang)
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(main.handle_new_message(VoiceEvent(msg, FakeSender())), timeout=10)
    elapsed = asyncio.get_running_loop().time() - started
    main.VOICE_DOWNLOAD_TIMEOUT_SECONDS = original_timeout

    check("обработчик не завис на скачивании", elapsed < 5, f"elapsed={elapsed:.1f}s")
    check("сообщение всё равно сохранено", await row_exists(6005))
    check("ложной транскрипции не опубликовано", SENT == [], f"got {SENT}")

    print("\n[6] Голосовое от бота не расшифровывается и не переотправляется")
    main.PROCESSED_MSG_IDS.clear()
    SENT.clear()
    set_whisper("текст из голосового бота")
    msg = attach_download(voice_message(6006, sender_id=7971556097))
    await main.handle_new_message(VoiceEvent(msg, FakeSender(bot=True)))
    check("расшифровки бота в чате нет", SENT == [], f"got {SENT}")
    check("текст бота в базу не записан", await stored_text(6006) == "",
          f"got {await stored_text(6006)!r}")

    print("\n[7] Повторная доставка того же апдейта обрабатывается один раз")
    main.PROCESSED_MSG_IDS.clear()
    SENT.clear()
    set_whisper("повторяемое голосовое")
    msg = attach_download(voice_message(6007))
    await main.handle_new_message(VoiceEvent(msg, FakeSender()))
    await main.handle_new_message(VoiceEvent(attach_download(voice_message(6007)), FakeSender()))
    check("транскрипция ушла один раз, а не дважды", len(SENT) == 1, f"got {len(SENT)}")

    print("\n[8] Временные файлы голосовых не остаются на диске")
    leftovers = [f for f in os.listdir(_TMPDIR) if f.startswith("voice_")]
    check("каталог чист", leftovers == [], f"осталось: {leftovers}")


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
