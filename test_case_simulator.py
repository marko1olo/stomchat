"""
Интерактивный клинический кейс (/case): реальные прогоны шагов симулятора.

Раньше эти свойства проверялись поиском подстрок в исходнике через
inspect.getsource. Здесь исполняется настоящий assistant.handle_interactive_case_step
с настоящей базой SQLite; заглушены только внешний LLM, поиск по корпусу и
отправка в Telegram.

Запуск: python test_case_simulator.py
"""
import asyncio
import json
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

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_case_")
config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")

import database
import assistant

PASS, FAIL = [], []
USER_ID = 4242


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


EVENTS = []          # хронология: что и в каком порядке произошло
PROMPTS = []
DELETED = []


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeBot:
    async def send_message(self, entity=None, message=None, parse_mode=None, **kw):
        EVENTS.append(("send", message))
        return type("Sent", (), {"id": 70000 + len(EVENTS)})()

    async def delete_messages(self, chat_id, msg_id):
        DELETED.append(msg_id)
        EVENTS.append(("delete", msg_id))


BOT = FakeBot()

_corpus_result = ("Справка: препарирование под цирконий, уступ 0.8 мм.", None)
_corpus_error = None


async def fake_corpus(keywords):
    EVENTS.append(("corpus", tuple(keywords)))
    if _corpus_error:
        raise _corpus_error
    return _corpus_result


async def fake_llm(prompt, status_ctx=None, timeout=None):
    PROMPTS.append(prompt)
    EVENTS.append(("llm", len(prompt)))
    return FakeResponse("<b>Оценка действий врача.</b> Тактика верная."), None


assistant.search_knowledge_corpus = fake_corpus
assistant.generate_gemini_text_async = fake_llm


def reset():
    EVENTS.clear()
    PROMPTS.clear()
    DELETED.clear()


def state(step, history=None):
    return {
        "state_type": "case",
        "current_step": step,
        "case_id": "dynamic",
        "history": json.dumps(history or [], ensure_ascii=False),
    }


def sent_texts():
    return [payload for kind, payload in EVENTS if kind == "send"]


def kinds():
    return [kind for kind, _ in EVENTS]


async def run():
    await database.init_db()

    print("\n[1] Пустой ход не доходит до LLM")
    reset()
    await assistant.handle_interactive_case_step(BOT, USER_ID, "", state(1))
    check("LLM не вызван", "llm" not in kinds(), f"got {kinds()}")
    check("поиск по базе не запускался", "corpus" not in kinds(), f"got {kinds()}")
    check("врачу объяснили, что нужен текст",
          any("читаю только текст" in t for t in sent_texts()), f"got {sent_texts()}")

    reset()
    await assistant.handle_interactive_case_step(BOT, USER_ID, "   \n  ", state(1))
    check("пробелы тоже считаются пустым ходом", "llm" not in kinds(), f"got {kinds()}")

    print("\n[2] Статус «Анализирую» отправляется ПОСЛЕ поиска по базе")
    reset()
    await assistant.handle_interactive_case_step(BOT, USER_ID, "снимаю оттиск", state(1))
    order = kinds()
    check("поиск раньше статуса", order.index("corpus") < order.index("send"), f"got {order}")
    check("LLM после статуса", order.index("send") < order.index("llm"), f"got {order}")
    check("статус удалён после ответа", DELETED, f"got {DELETED}")

    print("\n[3] Отказ поиска не оставляет вечный «Анализирую…»")
    global _corpus_error
    reset()
    _corpus_error = sqlite_error = RuntimeError("database is locked")
    try:
        await assistant.handle_interactive_case_step(BOT, USER_ID, "препарирую 26", state(1))
    except RuntimeError:
        pass
    _corpus_error = None
    check("статус не отправлялся вовсе", "send" not in kinds(), f"got {kinds()}")
    check("висящего сообщения нет", sent_texts() == [], f"got {sent_texts()}")

    print("\n[4] Шаги 1..3 продолжают кейс, а не обрывают его")
    for step in (1, 2, 3):
        reset()
        await database.clear_user_interactive_state(USER_ID)
        await assistant.handle_interactive_case_step(BOT, USER_ID, f"действие на шаге {step}", state(step))
        saved = await database.get_user_interactive_state(USER_ID)
        check(f"шаг {step}: состояние сохранено", saved is not None, f"got {saved}")
        check(f"шаг {step}: счётчик продвинулся до {step + 1}",
              saved is not None and saved["current_step"] == step + 1, f"got {saved}")
        check(f"шаг {step}: финального экрана нет",
              not any("Разбор случая завершен" in t for t in sent_texts()), f"got {sent_texts()}")

    print("\n[5] Заголовок шага согласован с CASE_TOTAL_STEPS")
    reset()
    await assistant.handle_interactive_case_step(BOT, USER_ID, "ставлю коффердам", state(2))
    check(f"в промпте «Шаг 3 из {assistant.CASE_TOTAL_STEPS}»",
          PROMPTS and f"Шаг 3 из {assistant.CASE_TOTAL_STEPS}" in PROMPTS[0],
          "заголовок не найден в промпте")

    print("\n[6] Последний шаг завершает кейс и чистит состояние")
    reset()
    await database.set_user_interactive_state(USER_ID, "case", assistant.CASE_TOTAL_STEPS, "dynamic", "[]")
    await assistant.handle_interactive_case_step(
        BOT, USER_ID, "фиксирую коронку", state(assistant.CASE_TOTAL_STEPS)
    )
    check("показан финальный экран",
          any("Разбор случая завершен" in t for t in sent_texts()), f"got {sent_texts()}")
    cleared = await database.get_user_interactive_state(USER_ID)
    check("состояние симулятора очищено", cleared is None, f"got {cleared}")

    print("\n[7] Кейс не обрывается раньше обещанного числа шагов")
    reset()
    await database.clear_user_interactive_state(USER_ID)
    step_state = state(1)
    finished_at = None
    for turn in range(1, assistant.CASE_TOTAL_STEPS + 1):
        reset()
        await assistant.handle_interactive_case_step(BOT, USER_ID, f"ход {turn}", step_state)
        if any("Разбор случая завершен" in t for t in sent_texts()):
            finished_at = turn
            break
        saved = await database.get_user_interactive_state(USER_ID)
        step_state = {
            "state_type": "case",
            "current_step": saved["current_step"],
            "case_id": "dynamic",
            "history": saved["history"],
        }
    check(f"кейс завершился ровно на ходу {assistant.CASE_TOTAL_STEPS}",
          finished_at == assistant.CASE_TOTAL_STEPS, f"got {finished_at}")

    print("\n[8] История переписки накапливается, а не теряется")
    await database.clear_user_interactive_state(USER_ID)
    reset()
    await assistant.handle_interactive_case_step(BOT, USER_ID, "первый ход врача", state(1))
    saved = await database.get_user_interactive_state(USER_ID)
    history = json.loads(saved["history"])
    messages = history["messages"] if isinstance(history, dict) else history
    roles = [m["role"] for m in messages]
    check("в истории и ход врача, и ответ экзаменатора", roles == ["user", "assistant"], f"got {roles}")
    check("текст врача сохранён дословно",
          messages[0]["content"] == "первый ход врача", f"got {messages[0]}")


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
