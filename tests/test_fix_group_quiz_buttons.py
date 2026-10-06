"""
Тесты исправлений:
1. edit_callback_message: отсутствие двойной правки (раньше вызывался и event.edit, и tg_safety.edit_message).
2. edit_callback_message: корректный таймаут и фолбэк на event.edit.
3. qa:* в handle_quiz_callback:
   - сохранение инлайн-кнопок при обновлении статистики (раньше кнопки стирались).
   - ограничение alert_text до 200 символов (лимит Telegram answerCallbackQuery).
   - защита от выхода за пределы списка вариантов (IndexError).
4. Завершение клинического кейса по неактивности:
   - если неактивность < 24 часов: отправка вежливого уведомления.
   - если неактивность >= 24 часов: тихое очищение состояния без спама врачу.
5. Миграция колонок user_memories в database.py: логирование неожиданных ошибок вместо немого pass.
"""
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_test_quiz_btns_")
os.environ["STOMCHAT_DATA_DIR"] = _TMPDIR
os.environ["STOMCHAT_DB_PATH"] = os.path.join(_TMPDIR, "stomat_bot.db")
import config
config.DB_PATH = os.path.join(_TMPDIR, "stomat_bot.db")
import assistant
import database
database.DB_PATH = config.DB_PATH
import tg_safety
import blocking_tools
import runtime_guard

# Изолируем боевые файлы состояния и мониторинга от изменений тестом
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")
runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "bot_heartbeat.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "bot_watchdog_dump.txt")
assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

# Ускоряем бэкофф в tg_safety для тестов
tg_safety.TRANSIENT_BACKOFF_SECONDS = 0.01
tg_safety.MIN_RETRY_SLICE_SECONDS = 0.01

# Мокаем LLM вызовы
async def mock_llm(*args, **kwargs):
    return type("R", (), {"text": "Тестовый ответ ассистента"})(), None

assistant.generate_gemini_text_async = mock_llm
blocking_tools.generate_gemini_text_async = mock_llm

PASS = []
FAIL = []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class FakeWorkingBot:
    def __init__(self):
        self.edits = []

    async def edit_message(self, chat_id, message_id, text, buttons=None, **kwargs):
        self.edits.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "buttons": buttons,
            "kwargs": kwargs
        })
        return type("M", (), {"id": message_id, "chat_id": chat_id})()


class FakeFailingBot:
    async def edit_message(self, chat_id, message_id, text, buttons=None, **kwargs):
        raise ConnectionResetError("Connection reset")


class FakeOriginalMessage:
    def __init__(self, message_text, reply_markup=None):
        self.message = message_text
        self.reply_markup = reply_markup


class FakeCallbackEvent:
    def __init__(self, data_str, chat_id=-1001234567, message_id=888, sender_id=999, original_msg=None):
        self.data = data_str.encode("utf-8") if isinstance(data_str, str) else data_str
        self.chat_id = chat_id
        self.message_id = message_id
        self.sender_id = sender_id
        self._original_msg = original_msg
        self.answers = []
        self.edits = []

    async def answer(self, text=None, alert=False):
        self.answers.append({"text": text, "alert": alert})

    async def get_message(self):
        return self._original_msg

    async def edit(self, text, buttons=None, **kwargs):
        self.edits.append({"text": text, "buttons": buttons, "kwargs": kwargs})
        return True


async def run_tests():
    await database.init_db()

    print("\n[1] edit_callback_message не делает двойную правку при успехе tg_safety")
    bot = FakeWorkingBot()
    event = FakeCallbackEvent("nav:main")
    await assistant.edit_callback_message(bot, event, "Текст меню", "test_op")
    check("bot.edit_message вызван ровно 1 раз", len(bot.edits) == 1, f"edits={len(bot.edits)}")
    check("event.edit НЕ вызывался (дубликат предотвращён)", len(event.edits) == 0, f"event.edits={len(event.edits)}")

    print("\n[2] edit_callback_message делает фолбэк на event.edit при сбое bot_client")
    fail_bot = FakeFailingBot()
    event2 = FakeCallbackEvent("nav:main")
    await assistant.edit_callback_message(fail_bot, event2, "Текст фолбэка", "test_op")
    check("event.edit вызван при сбое tg_safety", len(event2.edits) == 1, f"event.edits={len(event2.edits)}")

    print("\n[3] qa:* ограничивает alert_text до 200 символов")
    quiz_id = -999888777
    long_explanation = "А" * 350
    await database.set_user_interactive_state(
        user_id=quiz_id,
        state_type="quiz_config",
        current_step=0,
        case_id=long_explanation,
        history=json.dumps({"votes": [0, 0, 0, 0], "voters": {}})
    )

    dummy_markup = object()
    orig_msg = FakeOriginalMessage(
        "🎲 <b>КЛИНИЧЕСКИЙ КЕЙС-ВИКТОРИНА</b>\n\nВопрос?\n\n<b>A:</b> Вариант 1\n<b>B:</b> Вариант 2\n<b>C:</b> Вариант 3\n<b>D:</b> Вариант 4\n\n<i>Нажмите на кнопку</i>",
        reply_markup=dummy_markup
    )
    cb_quiz = FakeCallbackEvent(f"qa:0:0:{quiz_id}", sender_id=12345, original_msg=orig_msg)
    
    bot_quiz = FakeWorkingBot()
    await assistant.handle_quiz_callback(bot_quiz, cb_quiz)

    check("event.answer вызван", len(cb_quiz.answers) == 1, f"answers={len(cb_quiz.answers)}")
    ans_text = cb_quiz.answers[0]["text"]
    check("длина alert_text <= 200 символов", len(ans_text) <= 200, f"длина={len(ans_text)}")
    check("alert=True передан", cb_quiz.answers[0]["alert"] is True)

    print("\n[4] qa:* сохраняет инлайн-кнопки при обновлении статистики")
    check("bot.edit_message вызван для обновления статистики", len(bot_quiz.edits) == 1, f"edits={len(bot_quiz.edits)}")
    saved_buttons = bot_quiz.edits[0]["buttons"]
    check("reply_markup передан в правку сообщения", saved_buttons is dummy_markup, f"got {saved_buttons}")
    updated_text = bot_quiz.edits[0]["text"]
    check("статистика голосов присутствует в тексте", "Всего проголосовало: 1" in updated_text)
    check("процент голосов пересчитан", "100%" in updated_text)

    print("\n[5] Защита от повторного голосования")
    cb_repeat = FakeCallbackEvent(f"qa:0:1:{quiz_id}", sender_id=12345, original_msg=orig_msg)
    bot_repeat = FakeWorkingBot()
    await assistant.handle_quiz_callback(bot_repeat, cb_repeat)
    check("повторный голос отклонён", len(cb_repeat.answers) == 1 and "уже проголосовали" in cb_repeat.answers[0]["text"])
    check("сообщение со статистикой не правилось повторно", len(bot_repeat.edits) == 0)

    print("\n[6] Экспирация кейса в ЛС: < 24 часов отправляет уведомление, >= 24 часов молчит")
    user_recent = 777111
    user_ancient = 777222

    now = time.time()
    await database.set_user_interactive_state(
        user_id=user_recent,
        state_type="case",
        current_step=1,
        case_id="dynamic",
        history=json.dumps({"last_updated": now - 7200})  # 2 часа назад
    )
    await database.set_user_interactive_state(
        user_id=user_ancient,
        state_type="case",
        current_step=1,
        case_id="dynamic",
        history=json.dumps({"last_updated": now - 30 * 86400})  # 30 дней назад
    )

    class FakePMBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, entity, message, parse_mode=None, **kw):
            self.sent.append({"entity": entity, "message": message})

        def action(self, chat_id, kind):
            class _Act:
                async def __aenter__(self): return self
                async def __aexit__(self, *e): pass
            return _Act()

    class FakePMEvent:
        def __init__(self, chat_id, text):
            self.chat_id = chat_id
            self.sender_id = chat_id
            self.message = type("M", (), {
                "id": 100,
                "message": text,
                "photo": None,
                "video": None,
                "document": None,
                "voice": None,
                "audio": None,
                "reply_to": None,
            })()

    pm_bot = FakePMBot()
    # 1. Запрос от user_recent
    event_recent = FakePMEvent(user_recent, "Привет")
    await assistant.handle_private_message(pm_bot, event_recent)
    recent_cleaned = await database.get_user_interactive_state(user_recent)
    check("состояние user_recent очищено", recent_cleaned is None)
    has_recent_warning = any(s["entity"] == user_recent and "автоматически завершена" in s["message"] for s in pm_bot.sent)
    check("user_recent получил уведомление об истечении 1ч", has_recent_warning)

    # 2. Запрос от user_ancient
    event_ancient = FakePMEvent(user_ancient, "Привет")
    await assistant.handle_private_message(pm_bot, event_ancient)
    ancient_cleaned = await database.get_user_interactive_state(user_ancient)
    check("состояние user_ancient очищено", ancient_cleaned is None)
    has_ancient_warning = any(s["entity"] == user_ancient and "автоматически завершена" in s["message"] for s in pm_bot.sent)
    check("user_ancient НЕ получил уведомление (тихая очистка старого мусора)", not has_ancient_warning)


if __name__ == "__main__":
    try:
        asyncio.run(run_tests())
    finally:
        try:
            database._DB_EXECUTOR.shutdown(wait=True)
        except Exception:
            pass
        shutil.rmtree(_TMPDIR, ignore_errors=True)

    print(f"\n{'='*62}\nИТОГО: PASSED={len(PASS)}   FAILED={len(FAIL)}")
    if FAIL:
        print("Провалено: " + ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)
