"""
Учёт исходящих сообщений бота — основа команды /wipe.

Каждая отправка проходит через обёртку patched_send_message, которая пишет
(msg_id, chat_id) в bot_sent_messages. От правильности пересчёта peer -> chat_id
зависит, что именно удалит /wipe: ошибка здесь означает либо «нечего удалять»,
либо удаление в чужом чате.

Проверяется настоящая обёртка из main с настоящими объектами telethon и
настоящей базой SQLite. Раньше эту логику не проверял никто.

Запуск: python test_wipe_tracking.py
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

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_wipe_")
config.DB_PATH = os.path.join(_TMPDIR, "test.db")

GROUP_ID = -1001820467444
SECOND_ID = -1003735006121
config.SOURCE_CHAT_ID = GROUP_ID

import database
import main
from telethon import utils as telethon_utils
from telethon.tl import types

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def sent_message(msg_id, peer):
    return types.Message(
        id=msg_id, peer_id=peer, message="сообщение бота",
        date=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc),
    )


def peer_of(chat_id):
    """chat_id в формате Telegram -> объект peer, как его вернёт Telethon."""
    if str(chat_id).startswith("-100"):
        return types.PeerChannel(abs(chat_id) - 1000000000000)
    if chat_id < 0:
        return types.PeerChat(-chat_id)
    return types.PeerUser(chat_id)


async def run():
    await database.init_db()

    print("\n[1] Пересчёт peer -> chat_id совпадает с библиотечным")
    for chat_id in (GROUP_ID, SECOND_ID, -12345, 555000111):
        peer = peer_of(chat_id)
        check(f"{chat_id} восстанавливается из peer",
              telethon_utils.get_peer_id(peer) == chat_id,
              f"got {telethon_utils.get_peer_id(peer)}")

    print("\n[2] Каждая отправка бота попадает в очередь /wipe")
    outgoing = []

    async def fake_original(*args, **kwargs):
        msg = outgoing.pop(0)
        return msg

    # Подменяем только сетевой вызов: patched_send_message читает
    # original_send_message из модуля, поэтому её собственная логика
    # исполняется настоящая.
    main.original_send_message = fake_original

    outgoing.append(sent_message(8001, peer_of(GROUP_ID)))
    await main.patched_send_message(entity=GROUP_ID, message="x")
    rows = {(r[0], r[1]) for r in await database.get_last_bot_sent_messages(count=50)}
    check("сообщение группы записано с верным chat_id", (8001, GROUP_ID) in rows, f"got {sorted(rows)}")

    outgoing.append(sent_message(8002, peer_of(SECOND_ID)))
    await main.patched_send_message(entity=SECOND_ID, message="y")
    outgoing.append(sent_message(8003, peer_of(555000111)))
    await main.patched_send_message(entity=555000111, message="в личку")
    rows = {(r[0], r[1]) for r in await database.get_last_bot_sent_messages(count=50)}
    check("второй чат записан отдельно", (8002, SECOND_ID) in rows, f"got {sorted(rows)}")
    check("личное сообщение записано с положительным id", (8003, 555000111) in rows, f"got {sorted(rows)}")

    print("\n[3] /wipe ограничен своим чатом")
    group_rows = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=GROUP_ID)}
    check("в выборке основного чата только его сообщения", group_rows == {8001},
          f"got {sorted(group_rows)}")
    second_rows = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=SECOND_ID)}
    check("во втором чате только его", second_rows == {8002}, f"got {sorted(second_rows)}")
    pm_rows = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=555000111)}
    check("в личке только её", pm_rows == {8003}, f"got {sorted(pm_rows)}")
    check("без chat_id выборка охватывает все чаты — для /wipe это опасно",
          len({r[0] for r in await database.get_last_bot_sent_messages(count=50)}) == 3)

    print("\n[4] Повторная запись того же сообщения не плодит строк")
    await database.save_bot_sent_message(8001, GROUP_ID)
    await database.save_bot_sent_message(8001, GROUP_ID)
    group_rows = [r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=GROUP_ID)]
    check("дублей нет", group_rows.count(8001) == 1, f"got {group_rows}")

    print("\n[5] Отказ записи не ломает отправку")
    real_save = database.save_bot_sent_message

    async def failing_save(msg_id, chat_id):
        raise RuntimeError("нет такой таблицы")

    database.save_bot_sent_message = failing_save
    outgoing.append(sent_message(8010, peer_of(GROUP_ID)))
    result = None
    try:
        result = await main.patched_send_message(entity=GROUP_ID, message="z")
    except Exception as exc:
        result = f"ИСКЛЮЧЕНИЕ: {exc}"
    database.save_bot_sent_message = real_save
    check("сообщение всё равно отправлено", getattr(result, "id", None) == 8010, f"got {result}")

    print("\n[6] Удаление сообщения снимает его с учёта")
    await main.handle_deleted_messages(
        __import__("telethon").events.MessageDeleted.Event(
            deleted_ids=[8001], peer=peer_of(GROUP_ID))
    )
    group_rows = {r[0] for r in await database.get_last_bot_sent_messages(count=50, chat_id=GROUP_ID)}
    check("удалённое вручную сообщение выброшено из очереди", 8001 not in group_rows,
          f"got {sorted(group_rows)}")
    all_rows = {r[0] for r in await database.get_last_bot_sent_messages(count=50)}
    check("сообщения других чатов не задеты", {8002, 8003} <= all_rows, f"got {sorted(all_rows)}")


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
