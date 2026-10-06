"""
Правки и удаления сообщений: обработчики main + запись в базу.

Тест интеграционный, без заглушек логики:
  * настоящая база SQLite во временном файле, схема из database.init_db();
  * настоящие объекты событий telethon.events, собранные из tl.types.Message;
  * настоящие обработчики main.handle_edited_message / handle_deleted_messages;
  * проверка результата чтением из базы, а не осмотром исходников.

Боевая база не открывается: config.DB_PATH подменяется до первого обращения
и проверяется на непринадлежность рабочему каталогу.

Запуск: python test_edit_delete.py
"""
import asyncio
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

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_editdel_")
_TEST_DB = os.path.join(_TMPDIR, "test_messages.db")
_PROD_DB = os.path.abspath(getattr(config, "DB_PATH", "stomat_bot.db"))
config.DB_PATH = _TEST_DB

TEST_CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009876543210
config.SOURCE_CHAT_ID = TEST_CHAT_ID

import database
import main
from telethon import events
from telethon.tl import types

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def edited_event(msg_id, text, chat_id=TEST_CHAT_ID):
    """Настоящий telethon-эвент правки, а не подделка с нужными атрибутами."""
    peer = types.PeerChannel(abs(chat_id) - 1000000000000)
    message = types.Message(
        id=msg_id,
        peer_id=peer,
        message=text,
        date=datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
    )
    event = events.MessageEdited.Event(message)
    event._entities = {}
    return event


def deleted_event(ids, chat_id=TEST_CHAT_ID):
    peer = types.PeerChannel(abs(chat_id) - 1000000000000)
    return events.MessageDeleted.Event(deleted_ids=list(ids), peer=peer)


async def seed(msg_id, text, sender_id=555, when=None):
    await database.save_message(
        msg_id=msg_id,
        sender_id=sender_id,
        sender_name="Иванов",
        sender_username="ivanov",
        text=text,
        date=when or datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc),
    )


async def text_of(msg_id):
    row = await database.get_text_by_id(msg_id)
    return row[1] if row else None


async def run():
    print("\n[0] Изоляция от боевых данных")
    check("config.DB_PATH уведён во временный каталог",
          os.path.abspath(config.DB_PATH) != _PROD_DB and _TMPDIR in config.DB_PATH,
          f"got {config.DB_PATH}")
    check("боевой файл базы не тронут", not os.path.exists(os.path.join(_TMPDIR, "stomat_bot.db")))

    await database.init_db()
    check("схема создана во временной базе", os.path.exists(_TEST_DB))

    print("\n[1] Правка доезжает до базы")
    await seed(4821, "лечение канала 36 зуба, пломбировка на 2 мм за апекс")
    check("исходный текст сохранён", await text_of(4821) is not None)

    await main.handle_edited_message(
        edited_event(4821, "лечение канала 36 зуба, пломбировка ДО апекса — опечатка выше")
    )
    stored = await text_of(4821)
    check("в базе лежит исправленная редакция", stored is not None and "ДО апекса" in stored,
          f"got {stored!r}")
    check("старая редакция вытеснена", stored is not None and "за апекс" not in stored,
          f"got {stored!r}")

    print("\n[2] Правка не сбрасывает признак попадания в сводку")
    await seed(4822, "первая редакция")
    await database.mark_messages_as_summarized([4822])
    await main.handle_edited_message(edited_event(4822, "вторая редакция"))
    rows = await database.get_messages_for_summary()
    check("уже засуммаризированное не всплывает заново",
          all(r[0] != 4822 for r in rows), f"rows={[r[0] for r in rows]}")
    check("но текст всё равно обновлён", await text_of(4822) == "вторая редакция")

    print("\n[3] Правка чужого чата базу не трогает")
    await seed(4823, "текст основного чата")
    await main.handle_edited_message(edited_event(4823, "подмена из другого чата", chat_id=OTHER_CHAT_ID))
    check("строка основного чата не перезаписана", await text_of(4823) == "текст основного чата",
          f"got {await text_of(4823)!r}")

    print("\n[4] Правка неизвестного сообщения не роняет обработчик")
    main.EDIT_RESAVE_RETRY_SECONDS = 0.01  # не ждать две секунды в тесте
    await main.handle_edited_message(edited_event(999999, "правка поста старше бота"))
    check("обработчик пережил промах", await text_of(999999) is None)

    print("\n[5] Гонка: правка обгоняет сохранение — повтор её догоняет")
    main.EDIT_RESAVE_RETRY_SECONDS = 0.2

    async def late_save():
        await asyncio.sleep(0.05)
        await seed(4830, "текст, записанный с опозданием")

    saver = asyncio.create_task(late_save())
    await main.handle_edited_message(edited_event(4830, "правка, пришедшая раньше записи"))
    await saver
    late = await text_of(4830)
    check("отложенная попытка применила правку", late == "правка, пришедшая раньше записи",
          f"got {late!r}")
    main.EDIT_RESAVE_RETRY_SECONDS = 0.01

    print("\n[6] Удаление вычищает сообщение из базы")
    await seed(4840, "удалённый коллегой пост")
    await seed(4841, "соседнее сообщение")
    check("оба сообщения на месте", await text_of(4840) and await text_of(4841))

    await main.handle_deleted_messages(deleted_event([4840]))
    check("удалённое исчезло", await text_of(4840) is None)
    check("соседнее не задето", await text_of(4841) == "соседнее сообщение")

    print("\n[7] Удалённое больше не попадает ни в сводку, ни в цитаты")
    await seed(4850, "пост, который автор удалил")
    await main.handle_deleted_messages(deleted_event([4850]))
    rows = await database.get_messages_for_summary()
    check("нет в очереди на сводку", all(r[0] != 4850 for r in rows))
    texts = await database.get_texts_by_ids([4850, 4841])
    check("нет в пакетной выборке цитат", 4850 not in texts and 4841 in texts,
          f"got keys {sorted(texts)}")

    print("\n[8] Удаление в другом чате не сносит одноимённую строку основного")
    await seed(4860, "важное сообщение основного чата")
    await main.handle_deleted_messages(deleted_event([4860], chat_id=OTHER_CHAT_ID))
    check("строка с тем же номером уцелела", await text_of(4860) == "важное сообщение основного чата",
          f"got {await text_of(4860)!r}")

    print("\n[9] Собственные сообщения бота вычищаются из очереди /wipe")
    await database.save_bot_sent_message(7001, TEST_CHAT_ID)
    await database.save_bot_sent_message(7002, TEST_CHAT_ID)
    await database.save_bot_sent_message(7003, OTHER_CHAT_ID)
    await main.handle_deleted_messages(deleted_event([7001]))
    left = {r[0] for r in await database.get_last_bot_sent_messages(count=50)}
    check("удалённое вручную сообщение бота выброшено", 7001 not in left, f"got {sorted(left)}")
    check("остальные в очереди остались", {7002, 7003} <= left, f"got {sorted(left)}")

    await main.handle_deleted_messages(deleted_event([7003]))
    left = {r[0] for r in await database.get_last_bot_sent_messages(count=50)}
    check("чистка очереди бота ограничена своим чатом", 7003 in left, f"got {sorted(left)}")

    print("\n[10] Пакетное удаление и мусор во входных данных")
    for i in range(4870, 4875):
        await seed(i, f"пост {i}")
    removed, _ = await database.delete_messages_by_ids([4870, 4871, 4871, None, 0, 4872],
                                                       chat_id=TEST_CHAT_ID)
    check("дубли и пустые id отброшены, удалено ровно три", removed == 3, f"got {removed}")
    check("остальные целы", await text_of(4873) and await text_of(4874))
    check("пустой список — без обращения к базе", await database.delete_messages_by_ids([]) == (0, 0))

    print("\n[11] Обработчики действительно зарегистрированы в клиенте")
    # list_event_handlers отдаёт (callback, builder) именно в таком порядке.
    builders = [type(b).__name__ for _, b in main.client.list_event_handlers()]
    check("MessageEdited подписан", "MessageEdited" in builders, f"got {sorted(set(builders))}")
    check("MessageDeleted подписан", "MessageDeleted" in builders, f"got {sorted(set(builders))}")
    check("NewMessage не потерян", "NewMessage" in builders, f"got {sorted(set(builders))}")

    registered = {type(b).__name__: b for _, b in main.client.list_event_handlers()}
    for name in ("MessageEdited", "MessageDeleted"):
        b = registered.get(name)
        chats = getattr(b, "chats", None)
        check(f"{name} ограничен сохраняемым чатом, а не всеми наблюдаемыми",
              chats is not None and OTHER_CHAT_ID not in (chats or ()),
              f"got {chats}")

    print("\n[12] Правка пустым текстом не ломает строку")
    await seed(4880, "подпись под снимком")
    await main.handle_edited_message(edited_event(4880, ""))
    check("текст обнулён, строка на месте", await text_of(4880) == "",
          f"got {await text_of(4880)!r}")

    print("\n[13] Запись сообщения переживает временный отказ базы")
    # Потеря необратима: sync_history догоняет пропущенное по MAX(msg_id), и
    # если сообщение N не записалось, а N+1 записалось, граница уехала выше
    # дыры навсегда. На Windows типовая причина отказа временная — файл держит
    # антивирус или индексатор.
    check("успешная запись возвращает True",
          await database.save_message(msg_id=4890, sender_id=1, sender_name="Врач",
                                      sender_username=None, text="обычная запись",
                                      date=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)) is True)

    real_run_db = database._run_db
    attempts = {"n": 0}

    async def flaky(operation):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise OSError("файл временно занят другим процессом")
        return await real_run_db(operation)

    database._run_db = flaky
    recovered = await database.save_message(
        msg_id=4891, sender_id=1, sender_name="Врач", sender_username=None,
        text="запись со второй попытки", date=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    )
    database._run_db = real_run_db
    check("временный отказ преодолён повтором", recovered is True, f"got {recovered}")
    check("сообщение реально в базе", await text_of(4891) == "запись со второй попытки",
          f"got {await text_of(4891)!r}")
    check("повтор был, а не одна попытка", attempts["n"] == 2, f"got {attempts['n']}")

    async def always_fail(operation):
        raise OSError("на диске нет места")

    database._run_db = always_fail
    lost = await database.save_message(
        msg_id=4892, sender_id=1, sender_name="Врач", sender_username=None,
        text="это будет потеряно", date=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    )
    database._run_db = real_run_db
    check("окончательный отказ отличим от успеха", lost is False, f"got {lost}")
    check("потерянного в базе нет", await text_of(4892) is None)


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
