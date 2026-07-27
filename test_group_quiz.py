"""
Групповая викторина: валидация ответа модели и ключ состояния.

Разобранный JSON ещё не значит пригодный. options[3] читался безусловно —
три варианта от модели давали IndexError и молчание после «Конструирую
задачу...». А correct не проверялся на диапазон: при correct=7 кнопки уходили
с data="qa:7:...", ни один клик не совпадал, и КАЖДОМУ ответившему врачу
сообщалось, что он неправ.

Состояние викторины лежит в user_interactive_states, где ключ — user_id.
Прежний диапазон 100000..999999 пересекается с id старых аккаунтов Telegram:
совпадение затёрло бы врачу активный /case, а его /abort убил бы живую
викторину в группе.

Запуск: python test_group_quiz.py
"""
import asyncio
import io
import json
import os
import re
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_quiz_")
config.DB_PATH = os.path.join(_TMPDIR, "test.db")

import database
import assistant

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SENT = []


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeBot:
    async def send_message(self, entity=None, message=None, buttons=None, reply_to=None, parse_mode=None, **kw):
        SENT.append({"message": message, "buttons": buttons})
        return type("M", (), {"id": 900 + len(SENT)})()

    async def delete_messages(self, chat_id, msg_id):
        pass


class FakeEvent:
    def __init__(self):
        self.chat_id = -1001234567890
        self.sender_id = 555
        self.message = type("M", (), {"id": 42})()


BOT = FakeBot()
_reply = {"text": ""}


async def fake_llm(prompt, status_ctx=None, timeout=None):
    return FakeResponse(_reply["text"]), None


assistant.generate_gemini_text_async = fake_llm
assistant.check_user_cooldown = lambda *a, **kw: 0

GOOD = json.dumps({
    "question": "Боль при накусывании в 3.6, недопломбировка 2 мм. Тактика?",
    "options": ["Резекция", "Перелечивание", "Наблюдение", "Удаление"],
    "correct": 1,
    "explanation": "Перелечивание — метод первого выбора.",
}, ensure_ascii=False)

FALLBACK_MARKER = "недопломбировка язычного канала"


def last_quiz():
    return SENT[-1] if SENT else None


def button_data():
    out = []
    for row in (last_quiz()["buttons"] or []):
        for button in row:
            out.append(button.data.decode() if isinstance(button.data, bytes) else button.data)
    return out


async def run_quiz(payload):
    SENT.clear()
    _reply["text"] = payload
    await assistant.handle_group_quiz(BOT, FakeEvent())


async def run():
    await database.init_db()

    print("\n[1] Корректный ответ модели используется как есть")
    await run_quiz(GOOD)
    check("викторина отправлена", last_quiz() is not None)
    check("вопрос модели на месте", "накусывании" in last_quiz()["message"], f"got {last_quiz()['message'][:60]}")
    check("фолбэк не подставлен", FALLBACK_MARKER not in last_quiz()["message"])
    check("четыре кнопки", len(button_data()) == 4, f"got {button_data()}")
    check("верный индекс проставлен во все кнопки",
          all(d.split(":")[1] == "1" for d in button_data()), f"got {button_data()}")

    print("\n[2] Три варианта вместо четырёх — фолбэк, а не падение")
    await run_quiz(json.dumps({"question": "Вопрос?", "options": ["A", "B", "C"],
                               "correct": 1, "explanation": "..."}, ensure_ascii=False))
    check("обработчик не упал, викторина отправлена", last_quiz() is not None)
    check("подставлен запасной вопрос", FALLBACK_MARKER in last_quiz()["message"],
          f"got {last_quiz()['message'][:80]}")
    check("кнопок всё равно четыре", len(button_data()) == 4, f"got {button_data()}")

    print("\n[3] Индекс верного ответа вне диапазона — фолбэк")
    for bad_index in (4, 7, -1):
        await run_quiz(json.dumps({"question": "Вопрос?", "options": ["A", "B", "C", "D"],
                                   "correct": bad_index, "explanation": "..."}, ensure_ascii=False))
        check(f"correct={bad_index} отвергнут", FALLBACK_MARKER in last_quiz()["message"],
              f"got {last_quiz()['message'][:60]}")
        indices = {int(d.split(":")[1]) for d in button_data()}
        check(f"correct={bad_index}: в кнопках допустимый индекс",
              indices and all(0 <= i <= 3 for i in indices), f"got {indices}")

    print("\n[4] Пустые варианты и пустой вопрос отвергаются")
    await run_quiz(json.dumps({"question": "Вопрос?", "options": ["A", "", "C", "D"],
                               "correct": 0, "explanation": "..."}, ensure_ascii=False))
    check("пустой вариант отвергнут", FALLBACK_MARKER in last_quiz()["message"])
    await run_quiz(json.dumps({"question": "   ", "options": ["A", "B", "C", "D"],
                               "correct": 0, "explanation": "..."}, ensure_ascii=False))
    check("пустой вопрос отвергнут", FALLBACK_MARKER in last_quiz()["message"])

    print("\n[5] Невалидный JSON и лишние варианты")
    await run_quiz("это вообще не json")
    check("мусор вместо JSON — фолбэк", FALLBACK_MARKER in last_quiz()["message"])
    await run_quiz(json.dumps({"question": "Вопрос?", "options": ["A", "B", "C", "D", "E", "F"],
                               "correct": 2, "explanation": "..."}, ensure_ascii=False))
    check("лишние варианты обрезаются, вопрос модели сохранён",
          FALLBACK_MARKER not in last_quiz()["message"], f"got {last_quiz()['message'][:60]}")
    check("кнопок ровно четыре", len(button_data()) == 4, f"got {button_data()}")

    print("\n[6] Ответ в ```json``` разбирается")
    await run_quiz("```json\n" + GOOD + "\n```")
    check("обёртка снята, вопрос модели использован",
          "накусывании" in last_quiz()["message"], f"got {last_quiz()['message'][:60]}")

    print("\n[7] Ключ состояния викторины не может совпасть с id врача")
    await run_quiz(GOOD)
    quiz_ids = {int(d.split(":")[3]) for d in button_data()}
    check("id викторины отрицательный", all(qid < 0 for qid in quiz_ids), f"got {quiz_ids}")
    for qid in quiz_ids:
        state = await database.get_user_interactive_state(qid)
        check("состояние сохранено под этим id", state is not None, f"id {qid}")
        check("тип состояния — викторина", state and state["state_type"] == "quiz_config")

    print("\n[8] Состояние викторины не мешает /case врача")
    await database.set_user_interactive_state(555000111, "case", 1, "dynamic", "[]")
    doctor = await database.get_user_interactive_state(555000111)
    check("кейс врача цел", doctor is not None and doctor["state_type"] == "case", f"got {doctor}")

    print("\n[9] Память диалога описана одним числом")
    source = io.open(os.path.join(_ORIGINAL_CWD, "assistant.py"), encoding="utf-8").read()
    check("константа существует", "PM_HISTORY_LIMIT" in source)
    check("в /help подставляется она же",
          "{PM_HISTORY_LIMIT} сообщений" in source, "в памятке снова литерал")
    check("захардкоженного «30 сообщений» не осталось",
          "<b>30 сообщений</b>" not in source)
    check("запрос истории использует константу",
          "get_last_pm_messages(chat_id, limit=PM_HISTORY_LIMIT)" in source)


_ORIGINAL_CWD = os.getcwd()
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
