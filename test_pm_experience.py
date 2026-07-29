"""
Личка с ботом глазами врача: что он видит, когда что-то идёт не так.

Три отказа, каждый из которых оставлял врача без ответа:

1. Видео + неудачное извлечение кадра. В finally стояло
   os.path.exists(file_to_analyze) без проверки на None, а
   extract_first_frame_async возвращает именно None. TypeError летел прямо из
   finally, поверх любой обработки: статус «Скачиваю и анализирую...» висел
   вечно, ответа не приходило.

2. Подвисшее скачивание. У download_media нет своего таймаута, а обработчик
   держит замок на пользователя — все следующие сообщения врача встают в
   очередь за этой загрузкой навсегда.

3. Рейт-лимит. Второе сообщение подряд молча исчезало: врач ждал ответа,
   которого не будет, и не мог понять, дошёл ли вопрос вообще.

Запуск: python test_pm_experience.py
"""
import asyncio
import os
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_pm_")
config.DB_PATH = os.path.join(_TMPDIR, "test.db")

import database
import assistant

# Файл состояния УВОДИМ во временный каталог.
#
# handle_private_message записывает активность врача для проактивных пингов
# через load_state/save_state. Без этой подмены тест писал в БОЕВОЙ
# assistant_state.json и оставлял там фантомного пользователя 4242: в бою по
# нему пошли бы попытки личных сообщений в никуда.
assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

PASS, FAIL = [], []
USER = 4242


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SENT, EDITED, DELETED = [], [], []


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeBot:
    async def send_message(self, entity=None, message=None, parse_mode=None, **kw):
        SENT.append(message)
        return type("M", (), {"id": 500 + len(SENT)})()

    async def edit_message(self, chat_id, msg_id, message, **kw):
        EDITED.append(message)

    async def delete_messages(self, chat_id, msg_id):
        DELETED.append(msg_id)

    def action(self, chat_id, kind):
        return _Typing()


class FakeMessage:
    def __init__(self, text="", video=None, photo=None, download=None):
        self.id = 77
        self.message = text
        self.video = video
        self.photo = photo
        self.document = None
        self.sticker = None
        self.voice = None
        self.audio = None
        self.reply_to = None
        self._download = download

    async def download_media(self, file=None):
        if self._download is not None:
            return await self._download()
        return None


class FakeEvent:
    def __init__(self, message):
        self.chat_id = USER
        self.sender_id = USER
        self.message = message


def reset():
    SENT.clear(); EDITED.clear(); DELETED.clear()
    assistant.USER_COOLDOWNS.clear()


async def stub_llm(prompt, status_ctx=None, timeout=None):
    return type("R", (), {"text": "Ответ ассистента по существу вопроса."})(), None


async def empty_corpus(keywords):
    return "", ""


async def run():
    await database.init_db()
    assistant.generate_gemini_text_async = stub_llm
    assistant.search_knowledge_corpus = empty_corpus

    print("\n[1] Видео, из которого не удалось извлечь кадр")
    reset()
    made = os.path.join(_TMPDIR, "clip.mp4")

    async def download_ok():
        with open(made, "wb") as handle:
            handle.write(b"\x00" * 16)
        return made

    # Ровно тот случай: кадр не извлёкся, вернулся None.
    import media_tools
    original_extract = media_tools.extract_first_frame_async

    async def no_frame(path, timeout=None):
        return None

    media_tools.extract_first_frame_async = no_frame
    sys.modules["media_tools"].extract_first_frame_async = no_frame

    event = FakeEvent(FakeMessage(text="что скажете по видео?", video=object(), download=download_ok))
    crashed = None
    try:
        await assistant.handle_private_message(FakeBot(), event)
    except Exception as exc:
        crashed = f"{type(exc).__name__}: {exc}"
    media_tools.extract_first_frame_async = original_extract

    check("обработчик не упал", crashed is None, f"got {crashed}")
    check("врач получил ответ", len(SENT) >= 2, f"отправлено сообщений: {len(SENT)}")
    check("статус «Скачиваю...» убран", DELETED or EDITED,
          "статус остался висеть на экране")
    check("временный файл удалён", not os.path.exists(made), "файл остался на диске")

    print("\n[2] Подвисшее скачивание ограничено таймаутом")
    reset()
    original_timeout = assistant.PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS
    assistant.PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 0.2

    async def download_hangs():
        await asyncio.sleep(30)
        return "никогда"

    event = FakeEvent(FakeMessage(text="снимок", photo=object(), download=download_hangs))
    started = asyncio.get_running_loop().time()
    crashed = None
    try:
        await asyncio.wait_for(assistant.handle_private_message(FakeBot(), event), timeout=10)
    except Exception as exc:
        crashed = f"{type(exc).__name__}: {exc}"
    elapsed = asyncio.get_running_loop().time() - started
    assistant.PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS = original_timeout

    check("обработчик не завис", crashed is None and elapsed < 5, f"{crashed}, {elapsed:.1f}с")
    check("врач получил ответ, а не тишину", len(SENT) >= 2, f"отправлено: {len(SENT)}")
    check("про непрочитанный файл сказано честно",
          any("не удалось" in s.lower() or "не смог" in s.lower() for s in SENT + EDITED),
          f"got {SENT + EDITED}")

    print("\n[3] Рейт-лимит объясняется, а не глотает сообщение")
    reset()
    first = FakeEvent(FakeMessage(text="смотрите, случай:"))
    await assistant.handle_private_message(FakeBot(), first)
    answered_first = len(SENT)
    check("первое сообщение отвечено", answered_first >= 1, f"got {answered_first}")

    SENT.clear()
    second = FakeEvent(FakeMessage(text="37-й, боль при накусывании, что делать?"))
    await assistant.handle_private_message(FakeBot(), second)
    check("врач предупреждён, а не проигнорирован", len(SENT) == 1, f"отправлено: {len(SENT)}")
    check("в предупреждении объяснено, что делать",
          SENT and ("одним сообщением" in SENT[0] or "Секунду" in SENT[0]),
          f"got {SENT[0][:100] if SENT else None}")

    print("\n[4] Предупреждение не превращается в спам")
    SENT.clear()
    for _ in range(4):
        await assistant.handle_private_message(
            FakeBot(), FakeEvent(FakeMessage(text="и ещё вопрос")))
    check("на серию сообщений предупреждение одно", len(SENT) == 0,
          f"отправлено ещё {len(SENT)} предупреждений")

    print("\n[5] Потерянное сообщение всё равно попадает в историю")
    history = await database.get_last_pm_messages(USER, limit=20)
    texts = " ".join(m["text"] for m in history)
    check("вопрос из-под рейт-лимита сохранён", "накусывании" in texts, f"got {texts[:160]}")
    check("последующие тоже сохранены", "и ещё вопрос" in texts, f"got {texts[:160]}")

    print("\n[6] Обычный текстовый вопрос отвечается")
    reset()
    await assistant.handle_private_message(
        FakeBot(), FakeEvent(FakeMessage(text="какой уступ под цирконий?")))
    check("ответ отправлен", any("Ответ ассистента" in s for s in SENT), f"got {SENT}")


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
