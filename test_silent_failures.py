"""
Молчание бота: где оно правильное, а где врач остаётся без ответа.

Есть два разных случая, и путать их нельзя.

Врач обратился НАПРЯМУЮ (/ask, упоминание, /что) — ответить обязаны всегда.
Здесь стоял голый return при отказе модели: вопрос уходил в пустоту, и врач не
мог отличить сломанного бота от игнорирующего.

Бот собрался высказаться САМ (пассивный триаж, рефери, проактивные пинги) —
молчать при отказе как раз правильно. Это та же политика, что у валидатора
ответов: неуверен — молчи, если тебя не спрашивали.

Запуск: python test_silent_failures.py
"""
import asyncio
import inspect
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

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_silent_")
config.DB_PATH = os.path.join(_TMPDIR, "test.db")

import database
import assistant

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SENT = []


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeBot:
    async def send_message(self, entity=None, message=None, reply_to=None, parse_mode=None, **kw):
        SENT.append(message)
        return type("M", (), {"id": 1})()

    async def delete_messages(self, chat_id, msg_id):
        pass

    def action(self, chat_id, kind):
        return _Typing()


class FakeEvent:
    def __init__(self, text=""):
        self.chat_id = -1001234567890
        self.sender_id = 555
        self.message = type("M", (), {"id": 7, "message": text, "reply_to": None})()


async def dead_llm(prompt, status_ctx=None, timeout=None):
    return None, "all providers exhausted"


async def empty_corpus(keywords):
    return "", ""


async def run():
    await database.init_db()
    assistant.generate_gemini_text_async = dead_llm
    assistant.search_knowledge_corpus = empty_corpus
    assistant.check_user_cooldown = lambda *a, **kw: 0

    print("\n[1] Прямое обращение: отказ модели объяснён, а не проглочен")
    SENT.clear()
    await assistant.handle_term_explainer(FakeBot(), FakeEvent(), "BOPT")
    check("врач получил ответ на /что", len(SENT) == 1, f"сообщений: {len(SENT)}")
    check("в ответе сказано про недоступность",
          SENT and "недоступны" in SENT[0], f"got {SENT[0][:90] if SENT else None}")

    SENT.clear()
    await assistant.handle_group_direct_ask(FakeBot(), FakeEvent(), "чем снимать цирконий?")
    check("врач получил ответ на прямой вопрос", len(SENT) == 1, f"сообщений: {len(SENT)}")
    check("в ответе сказано про недоступность",
          SENT and "недоступны" in SENT[0], f"got {SENT[0][:90] if SENT else None}")

    print("\n[2] Пустой термин объясняется, а не уходит в модель")
    SENT.clear()
    await assistant.handle_term_explainer(FakeBot(), FakeEvent(), "   ")
    check("подсказан формат команды", SENT and "Укажите термин" in SENT[0],
          f"got {SENT[0][:90] if SENT else None}")

    print("\n[3] Слишком длинный термин обрезается, а не раздувает промпт")
    captured = {}

    async def capture(prompt, status_ctx=None, timeout=None):
        captured["prompt"] = prompt
        return None, "stop"

    assistant.generate_gemini_text_async = capture
    SENT.clear()
    await assistant.handle_term_explainer(FakeBot(), FakeEvent(), "щ" * 4000)
    check("промпт не раздут термином",
          captured.get("prompt") and "щ" * (assistant.TERM_EXPLAINER_MAX_CHARS + 1) not in captured["prompt"],
          "термин попал в промпт целиком")
    assistant.generate_gemini_text_async = dead_llm

    print("\n[4] Инициатива бота при отказе остаётся молчаливой")
    # Пассивные пути молчать ОБЯЗАНЫ: врач их не звал, а выдавать «модели
    # недоступны» в общий чат по своей инициативе — это спам.
    passive = {
        "check_and_trigger_assistant_media": "пассивный разбор медиа",
        "check_and_trigger_referee": "клинический рефери",
        "check_and_send_group_activity_pings": "проактивные приглашения",
    }
    for name, label in passive.items():
        source = inspect.getsource(getattr(assistant, name))
        # первая ветка отказа генерации не должна ничего отправлять
        idx = source.find("if error")
        window = source[idx:idx + 400] if idx != -1 else ""
        speaks = "send_message" in window.split("return")[0] if "return" in window else False
        check(f"{label} при отказе молчит", not speaks, "отправляет сообщение в чат")

    print("\n[5] Разделение сохранено в коде, а не только на словах")
    source = open(os.path.join(_ORIGINAL_CWD, "assistant.py"), encoding="utf-8").read().split("\n")
    silent = []
    for i, line in enumerate(source):
        if not re.search(r"if error or not response|if error:", line):
            continue
        block = source[i + 1:i + 6]
        if any(b.strip() == "return" for b in block) and not any(
                ("send_message" in b or "edit_message" in b) for b in block):
            for j in range(i, 0, -1):
                if source[j].startswith(("async def ", "def ")):
                    silent.append(source[j].split("(")[0].split()[-1])
                    break
    allowed = {"check_and_trigger_assistant_media", "check_and_trigger_referee",
               "check_and_send_group_activity_pings"}
    unexpected = set(silent) - allowed
    check("молчат только пути по инициативе бота", not unexpected,
          f"молчат на прямой вопрос: {sorted(unexpected)}")
    check("пассивные пути действительно молчат", set(silent) == allowed,
          f"got {sorted(set(silent))}")


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
