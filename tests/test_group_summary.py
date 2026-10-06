"""
Команда /итог в группе: область разбора.

Параметр reply_to_msg_id передавался вызывающей стороной и НЕ использовался
нигде. Врач отвечал «/итог» на конкретный спор и получал выжимку последних
тридцати сообщений чата — часто про совсем другое. Указание на сообщение
теперь задаёт начало разбираемой ветки, а область называется в ответе.

Запуск: python test_group_summary.py
"""
import asyncio
import io
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

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_sum_")
config.DB_PATH = os.path.join(_TMPDIR, "test.db")
_ORIGINAL_CWD = os.getcwd()

import database
import assistant

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SENT, EDITED = [], []
PROMPTS = []


class FakeBot:
    async def send_message(self, entity=None, message=None, reply_to=None, parse_mode=None, **kw):
        SENT.append(message)
        return type("M", (), {"id": 999})()

    async def edit_message(self, chat_id, msg_id, message, **kw):
        EDITED.append(message)


class FakeEvent:
    def __init__(self):
        self.chat_id = -1001234567890
        self.sender_id = 555
        self.message = type("M", (), {"id": 100500})()


async def fake_llm(prompt, status_ctx=None, timeout=None):
    PROMPTS.append(prompt)
    return type("R", (), {"text": "<b>Суть:</b> спор о границе препарирования."})(), None


assistant.generate_gemini_text_async = fake_llm


def reset():
    SENT.clear(); EDITED.clear(); PROMPTS.clear()
    assistant.USER_COOLDOWNS.clear()


async def seed():
    """Старая ветка про уступ и свежая, ничем с ней не связанная."""
    base = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    for i in range(12):
        await database.save_message(
            msg_id=1000 + i, sender_id=500 + i % 3, sender_name=f"Врач{i % 3}",
            sender_username=None,
            text=f"обсуждаем ГРАНИЦУ УСТУПА, метка-у{i:02d}",
            date=base + timedelta(minutes=i))
    later = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)
    for i in range(40):
        await database.save_message(
            msg_id=5000 + i, sender_id=700, sender_name="Другой",
            sender_username=None,
            text=f"совершенно другая тема про ЦЕНЫ НА ПЕЧИ, реплика {i}",
            date=later + timedelta(minutes=i))


async def run():
    await database.init_db()
    await seed()

    print("\n[1] Без ответа на сообщение разбираются последние реплики")
    reset()
    await assistant.handle_group_summary(FakeBot(), FakeEvent(), None)
    check("сводка выдана", len(EDITED) == 1, f"got {len(EDITED)}")
    check("область названа",
          EDITED and "последние" in EDITED[0], f"got {EDITED[0][:120] if EDITED else None}")
    check("в промпт попала свежая тема",
          PROMPTS and "ЦЕНЫ НА ПЕЧИ" in PROMPTS[0], "свежих реплик в промпте нет")
    check("старая ветка про уступ не притянута",
          PROMPTS and "ГРАНИЦУ УСТУПА" not in PROMPTS[0],
          "в промпт попала посторонняя ветка")

    print("\n[2] Ответ на сообщение задаёт начало ветки")
    reset()
    await assistant.handle_group_summary(FakeBot(), FakeEvent(), 1003)
    check("сводка выдана", len(EDITED) == 1, f"got {len(EDITED)}")
    check("в промпт попала именно указанная ветка",
          PROMPTS and "ГРАНИЦУ УСТУПА" in PROMPTS[0], "указанная ветка не попала в промпт")
    # «От указанного сообщения» означает его и всё последующее — так врач и
    # формулирует «подведи итог с этого места». Проверяем осмысленное: реплики
    # ДО указанной в разбор не попадают.
    check("реплики до указанной не берутся",
          PROMPTS and "метка-у00" not in PROMPTS[0]
          and "метка-у01" not in PROMPTS[0] and "метка-у02" not in PROMPTS[0],
          "взяты реплики раньше указанной")
    check("указанная реплика включена",
          PROMPTS and "метка-у03" in PROMPTS[0], "указанное сообщение не попало в разбор")
    check("область названа как указанная",
          EDITED and "с указанного сообщения" in EDITED[0],
          f"got {EDITED[0][:140] if EDITED else None}")
    check("число разобранных реплик показано",
          EDITED and "реплик)" in EDITED[0], f"got {EDITED[0][:140] if EDITED else None}")

    print("\n[3] Области действительно разные")
    reset()
    await assistant.handle_group_summary(FakeBot(), FakeEvent(), 1003)
    thread_prompt = PROMPTS[0]
    reset()
    await assistant.handle_group_summary(FakeBot(), FakeEvent(), None)
    recent_prompt = PROMPTS[0]
    check("ответ на сообщение и без него дают разный материал",
          thread_prompt != recent_prompt, "область не изменилась — параметр снова игнорируется")

    print("\n[4] Ответ на сообщение старше базы не оставляет без сводки")
    reset()
    await assistant.handle_group_summary(FakeBot(), FakeEvent(), 1)
    # msg_id=1 -> в базе есть всё начиная с 1000, поэтому ветка не пуста;
    # проверяем случай, когда указанного и последующих нет вовсе.
    reset()
    await assistant.handle_group_summary(FakeBot(), FakeEvent(), 10 ** 9)
    check("откат к последним репликам сработал", len(EDITED) == 1, f"got {len(EDITED)}")
    check("врач не остался без ответа",
          EDITED and "последние" in EDITED[0], f"got {EDITED[0][:140] if EDITED else None}")
    check("сводка не пустая", EDITED and "Суть" in EDITED[0], f"got {EDITED[0][:140] if EDITED else None}")

    print("\n[5] Потолок ветки соблюдён")
    reset()
    rows = await database.get_messages_from(1000, limit=assistant.SUMMARY_THREAD_LIMIT)
    check("выборка не превышает потолок",
          len(rows) <= assistant.SUMMARY_THREAD_LIMIT, f"got {len(rows)}")
    check("порядок по возрастанию msg_id",
          all(rows[i][0] <= rows[i + 1][0] for i in range(len(rows) - 1)))
    check("указанное сообщение включено", rows and rows[0][0] == 1000, f"got {rows[0][0] if rows else None}")

    print("\n[6] Параметр больше не игнорируется в коде")
    source = io.open(os.path.join(_ORIGINAL_CWD, "assistant.py"), encoding="utf-8").read()
    handler = source.split("async def handle_group_summary", 1)[1].split("\nasync def ", 1)[0]
    body = "\n".join(l for l in handler.split("\n") if not l.lstrip().startswith("#"))
    check("reply_to_msg_id используется в теле", body.count("reply_to_msg_id") >= 2,
          f"упоминаний в коде: {body.count('reply_to_msg_id')}")
    check("вызывается выборка по ветке", "get_messages_from" in body)
    check("потолки вынесены в константы",
          "SUMMARY_THREAD_LIMIT" in body and "SUMMARY_RECENT_LIMIT" in body)


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
