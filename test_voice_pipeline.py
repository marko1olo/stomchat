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

    print("\n[3] Отказ Whisper не теряет сообщение и не остаётся тайной")
    # Раньше здесь стояло «публикации не было» — то есть тест ЗАКРЕПЛЯЛ саму
    # находку: все шесть тупиков голосового пути молчали, врач видел своё
    # голосовое, считал, что бот услышал, диктовку не повторял, а в базе
    # оставалась пустая строка — случай выпадал из дайджеста, из контекста и из
    # поиска. В ЛС бот на этом же месте отвечает, в группе не отвечал.
    # Молчать по-прежнему правильно там, где отказа НЕТ (см. [2]: аудио
    # разобрано, речи в нём нет) — но не там, где сломались мы.
    main.PROCESSED_MSG_IDS.clear()
    SENT.clear()
    main._VOICE_FAILURE_NOTICE.clear()
    set_whisper(None, error="whisper subprocess died")
    msg = attach_download(voice_message(6003))
    await main.handle_new_message(VoiceEvent(msg, FakeSender()))
    check("сообщение в базе есть", await row_exists(6003))
    check("врач узнал об отказе", len(SENT) == 1
          and "не удалось распознать" in SENT[0]["message"].lower(),
          f"got {SENT}")
    check("ложной транскрипции при этом нет",
          all("Транскрипция" not in item["message"] for item in SENT), f"got {SENT}")
    check("жалоба привязана к самому голосовому", SENT and SENT[0]["reply_to"] == 6003,
          f"got {SENT[0].get('reply_to') if SENT else None}")

    # Серия отказов не должна превратиться в спам: причина у них общая (упал ключ,
    # умер подпроцесс, не нашёлся ffmpeg), а по архиву диктовка идёт серями — 68
    # серий из двух и более голосовых подряд, самая длинная 19.
    SENT.clear()
    for burst_id in range(6010, 6019):
        main.PROCESSED_MSG_IDS.clear()
        await main.handle_new_message(VoiceEvent(attach_download(voice_message(burst_id)),
                                                 FakeSender()))
    check("серия отказов не залила чат", len(SENT) == 0,
          f"в окне тишины ушло {len(SENT)} строк: {[i['message'][:40] for i in SENT]}")
    check("подавленные отказы посчитаны",
          main._VOICE_FAILURE_NOTICE.get(-1001234567890, [0, 0])[1] == 9
          or any(rec[1] == 9 for rec in main._VOICE_FAILURE_NOTICE.values()),
          f"got {dict(main._VOICE_FAILURE_NOTICE)}")

    # Когда окно вышло, следующая жалоба обязана назвать число подавленных:
    # «не распознал одно» и «не распознал десять» — разные новости.
    for record in main._VOICE_FAILURE_NOTICE.values():
        record[0] -= main.VOICE_FAILURE_NOTICE_COOLDOWN_SECONDS + 1
    SENT.clear()
    main.PROCESSED_MSG_IDS.clear()
    await main.handle_new_message(VoiceEvent(attach_download(voice_message(6019)),
                                             FakeSender()))
    check("после окна жалоба ушла и назвала число подавленных",
          len(SENT) == 1 and "ещё 9" in SENT[0]["message"], f"got {SENT}")

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
    # Окно тишины из секции [3] надо снять руками. Иначе проверка «ложной
    # транскрипции не опубликовано» пройдёт потому, что бот в этом окне вообще
    # молчит, — и перестанет отличать «не опубликовал выдумку» от «не работает».
    main._VOICE_FAILURE_NOTICE.clear()
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
    # Заглушка Whisper вернула бы текст, если бы её позвали. Значит проверка
    # смотрит именно на то, что подготовленный текст НЕ уехал в чат, а не на
    # общее молчание бота.
    check("ложной транскрипции не опубликовано",
          all("этого не должно случиться" not in item["message"] for item in SENT),
          f"got {SENT}")
    check("таймаут скачивания — наш отказ, и врач о нём знает",
          len(SENT) == 1 and "не удалось распознать" in SENT[0]["message"].lower(),
          f"got {SENT}")

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

print("\n[N] Подготовка аудио не раздувает файл в восемь раз")
# Любой вход безусловно перегонялся в 16 кГц моно PCM. Замер на реальном
# telegram-подобном голосовом (ogg/opus 31 кб/с моно, 5.14 с):
# 20 361 Б -> 164 244 Б, рост в 8.07 раза. Арифметика: wav 16 кГц/16 бит/моно —
# это 32 000 Б/с, 1.92 МБ на минуту речи. При потолке загрузки 25 МБ в лимит
# упирается диктовка длиной 13.7 минуты, тогда как исходный ogg упёрся бы только
# на 109-й. Что провайдер режет по размеру, журнал подтверждает: в bot.log есть
# «HTTP/1.1 413 Payload Too Large» от api.groq.com.
#
# Сценарий отказа: врач диктует разбор случая на 15-20 минут или пересылает
# аудиозапись лекции. Из 5-7 МБ ogg получается 30-40 МБ wav, все ключи отдают
# отказ по размеру, расшифровки нет, врачу — тишина.
import gemini_client as _gc  # noqa: E402
import io as _io  # noqa: E402
import subprocess as _sp  # noqa: E402
import wave  # noqa: E402

_audio_tmp = tempfile.mkdtemp(prefix="stomchat_audio_")


def _fake_audio(name, size):
    path = os.path.join(_audio_tmp, name)
    with open(path, "wb") as handle:
        handle.write(b"\x00" * size)
    return path


# Сверять один результат мало: подложенные байты — не аудио, ffmpeg на них падает,
# и convert_to_wav мягко возвращает исходный путь. Тогда проверка «отдан как есть»
# прошла бы и на безусловной перегонке в PCM — то есть не значила бы ничего.
# Поэтому подменяем сам вызов ffmpeg: он записывает выход и рапортует об успехе,
# а тест смотрит, ВЫЗЫВАЛСЯ ли он вообще и с какими аргументами.
_FFMPEG_CALLS = []
_ffmpeg_real = _gc._ffmpeg_to
_FFMPEG_SUCCEEDS = [True]


def _ffmpeg_spy(file_path, out_path, args):
    _FFMPEG_CALLS.append((file_path, out_path, list(args)))
    if not _FFMPEG_SUCCEEDS[0]:
        return None
    with open(out_path, "wb") as handle:
        handle.write(b"\x00" * 4096)
    return out_path


_gc._ffmpeg_to = _ffmpeg_spy

check("штатные для API форматы перечислены",
      ".ogg" in _gc._WHISPER_NATIVE_EXTENSIONS and ".opus" in _gc._WHISPER_NATIVE_EXTENSIONS,
      "голосовые Telegram — это ogg/opus, они и есть основной вход")
check("потолок загрузки объявлен и ниже провайдерских 25 МБ",
      0 < _gc._WHISPER_MAX_UPLOAD_BYTES <= 25 * 1024 * 1024,
      f"got {_gc._WHISPER_MAX_UPLOAD_BYTES}")

for _label, _name, _size in (("голосовое ogg", "voice.ogg", 20 * 1024),
                             ("диктовка opus", "dict.opus", 1024 * 1024),
                             ("лекция mp3", "lecture.mp3", 5 * 1024 * 1024),
                             ("готовый wav", "already.wav", 2 * 1024 * 1024)):
    _src_path = _fake_audio(_name, _size)
    del _FFMPEG_CALLS[:]
    _out = _gc.convert_to_wav(_src_path)
    check(f"{_label}: перекодировки не было", not _FFMPEG_CALLS,
          f"ffmpeg позван на штатном формате: {_FFMPEG_CALLS[:1]} — рост в 8 раз вернулся")
    check(f"{_label}: отдан исходный путь", _out == _src_path,
          f"got {os.path.basename(_out) if _out else _out!r}")

# Файл сверх потолка обязан ПОПЫТАТЬСЯ сжаться — и именно сжаться, а не раздуться.
_huge = _fake_audio("huge.ogg", _gc._WHISPER_MAX_UPLOAD_BYTES + 1024)
del _FFMPEG_CALLS[:]
_out = _gc.convert_to_wav(_huge)
check("файл сверх потолка перекодируется", bool(_FFMPEG_CALLS),
      "ничего не сделано: 25+ МБ уйдут в API и получат 413")
check("первая попытка — сжатие в opus, а не PCM",
      bool(_FFMPEG_CALLS) and "libopus" in _FFMPEG_CALLS[0][2],
      f"got {_FFMPEG_CALLS[0][2] if _FFMPEG_CALLS else None}")
check("результат меньше потолка",
      bool(_out) and os.path.getsize(_out) <= _gc._WHISPER_MAX_UPLOAD_BYTES,
      f"got {os.path.getsize(_out) if _out and os.path.exists(_out) else '?'}")
check("сжатие моно и 16 кГц — речь не теряет разборчивости",
      bool(_FFMPEG_CALLS) and "-ac" in _FFMPEG_CALLS[0][2] and "-ar" in _FFMPEG_CALLS[0][2])

# Провал ffmpeg (нет кодека, битый файл, таймаут) не должен ронять расшифровку:
# лучше отправить исходник и получить отказ, чем упасть с исключением.
_FFMPEG_SUCCEEDS[0] = False
_broken = _fake_audio("broken.amr", 64 * 1024)
try:
    _out = _gc.convert_to_wav(_broken)
    _soft = _out == _broken
    _detail = f"got {_out!r}"
except Exception as exc:
    _soft, _detail = False, f"{type(exc).__name__}: {exc}"
check("провал ffmpeg отдаёт исходник, а не исключение", _soft, _detail)
_FFMPEG_SUCCEEDS[0] = True
_gc._ffmpeg_to = _ffmpeg_real

# Незнакомый контейнер всё-таки надо привести к штатному формату.
del _FFMPEG_CALLS[:]
_gc._ffmpeg_to = _ffmpeg_spy
_gc.convert_to_wav(_fake_audio("weird.amr", 32 * 1024))
check("незнакомый контейнер конвертируется", bool(_FFMPEG_CALLS),
      "amr/aac уйдут в API как есть и получат отказ по формату")
_gc._ffmpeg_to = _ffmpeg_real

_gc_src = _io.open("gemini_client.py", encoding="utf-8").read()
check("у ffmpeg есть таймаут",
      "timeout=120" in _gc_src.split("def _ffmpeg_to", 1)[1][:900],
      "без таймаута ffmpeg на битом файле висит бесконечно")

# Размер PCM задан форматом ТОЧНО, поэтому кратность считается, а не берётся из
# комментария: 16 кГц × 16 бит × моно = 32 000 Б/с против ~3.9 кБ/с у opus.
_SPEECH_SECONDS = 5 * 60
_pcm_path = os.path.join(_audio_tmp, "ruler.wav")
with wave.open(_pcm_path, "wb") as _w:
    _w.setnchannels(1)
    _w.setsampwidth(2)
    _w.setframerate(16000)
    _w.writeframes(b"\x00" * (16000 * 2 * _SPEECH_SECONDS))
_pcm_bytes = os.path.getsize(_pcm_path)
_opus_bytes = int(31000 / 8 * _SPEECH_SECONDS)  # битрейт голосового Telegram
check("PCM-раздувание кратно, а не на проценты", _pcm_bytes > _opus_bytes * 4,
      f"{_opus_bytes} -> {_pcm_bytes}")
_min_pcm = _gc._WHISPER_MAX_UPLOAD_BYTES / (_pcm_bytes / _SPEECH_SECONDS) / 60
_min_opus = _gc._WHISPER_MAX_UPLOAD_BYTES / (_opus_bytes / _SPEECH_SECONDS) / 60
print(f"       предельная диктовка: {_min_pcm:.0f} мин в PCM -> {_min_opus:.0f} мин как есть")
check("предельная диктовка выросла в разы", _min_opus > _min_pcm * 4,
      f"{_min_pcm:.1f} -> {_min_opus:.1f}")
check("пятиминутное голосовое в исходном виде проходит с запасом",
      _opus_bytes < _gc._WHISPER_MAX_UPLOAD_BYTES,
      f"{_opus_bytes} против потолка {_gc._WHISPER_MAX_UPLOAD_BYTES}")
# Тот же файл, но уже как wav — штатный формат, значит без второй перегонки.
del _FFMPEG_CALLS[:]
_gc._ffmpeg_to = _ffmpeg_spy
check("настоящий wav отдаётся без повторной обработки",
      _gc.convert_to_wav(_pcm_path) == _pcm_path and not _FFMPEG_CALLS,
      f"позвали ffmpeg: {_FFMPEG_CALLS[:1]}")
_gc._ffmpeg_to = _ffmpeg_real

print("\n[N+1] Отсутствие ffmpeg не тратит процессы и объяснено в журнале")
# На рабочей машине первым в PATH оказался 108-килобайтный шим: "-version" даёт
# код 1 и пустой вывод. Голая проверка «файл существует» такой бинарь принимает,
# и каждый нештатный файл платит за обречённый subprocess.
_probe_real = _gc._probe_ffmpeg
_run_real = _sp.run
_spawned = []


def _no_spawn(*a, **k):
    _spawned.append(a[0] if a else None)
    return _run_real(*a, **k)


_gc._FFMPEG_RESOLVED[:] = []
_gc._probe_ffmpeg = lambda path: False
try:
    check("сломанный бинарь не признаётся рабочим", _gc.ffmpeg_binary() is None)
    # _ffmpeg_to делает import subprocess внутри себя, но модуль в sys.modules
    # тот же самый объект — подмена run перехватывает запуск.
    _sp.run = _no_spawn
    _out = _gc._ffmpeg_to(_fake_audio("x.amr", 1024), os.path.join(_audio_tmp, "x.wav"),
                          ["-ar", "16000"])
    check("обречённый процесс не запускается", not _spawned and _out is None,
          f"spawned={_spawned} out={_out!r}")
finally:
    _sp.run = _run_real
    _gc._probe_ffmpeg = _probe_real
    _gc._FFMPEG_RESOLVED[:] = []
check("результат поиска кэшируется, а не проверяется на каждый файл",
      "_FFMPEG_RESOLVED" in _gc_src and "if _FFMPEG_RESOLVED:" in _gc_src,
      "проба -version на каждый файл — это до 30 с на файл")
check("путь к бинарю можно задать переменной окружения",
      "STOMCHAT_FFMPEG_PATH" in _gc_src,
      "иначе сломанный шим в PATH нечем обойти")
check("в предупреждении названы последствия, а не только факт",
      "расшифрованы НЕ БУДУТ" in _gc_src,
      "«ffmpeg not found» не говорит дежурному, что именно перестало работать")

shutil.rmtree(_audio_tmp, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
