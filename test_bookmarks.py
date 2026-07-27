"""
Закладки: экранирование, обрезка и счётчик результатов поиска.

Текст закладки и имя автора приходят из чата и вставлялись в HTML-сообщение
БЕЗ экранирования. Одна угловая скобка в сохранённом посте («уступ <0.5 мм»)
заставляет Telegram отклонить ВЕСЬ список: врач не видит ни одной своей
закладки, а не только испорченную. В живой базе такой символ пока в одном
сообщении из 30 082 — вероятность мала, радиус поражения полный.

У поиска по закладкам не было счётчика: при 50 совпадениях показывались
первые 10 без единого намёка, что есть ещё.

Запуск: python test_bookmarks.py
"""
import asyncio
import io
import os
import re
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

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_bm_")
config.DB_PATH = os.path.join(_TMPDIR, "test.db")
_ORIGINAL_CWD = os.getcwd()

import database
import assistant

PASS, FAIL = [], []
USER = 777


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def markup_problem(text):
    """Нашёл бы ли Telegram причину отклонить это сообщение."""
    for match in re.finditer("<", text):
        end = text.find(">", match.start())
        if end == -1:
            return "обрыв тега"
        tag = text[match.start() + 1:end].split()[0].lstrip("/").lower()
        if tag not in ("b", "i", "code", "a", "br"):
            return f"неизвестный тег <{tag}>"
    if re.search(r"&[a-zA-Z#][a-zA-Z0-9]{0,8}$", text):
        return "обрыв HTML-сущности"
    stack = []
    for closing, name in re.findall(r"<(/?)([a-zA-Z]+)", text):
        name = name.lower()
        if name == "br":
            continue
        if closing:
            if name in stack:
                del stack[stack.index(name):]
            else:
                return f"непарный </{name}>"
        else:
            stack.append(name)
    return f"незакрытые {stack}" if stack else None


SENT = []


class FakeBot:
    async def send_message(self, entity=None, message=None, parse_mode=None, **kw):
        SENT.append(message)
        return type("M", (), {"id": 1})()


class FakeEvent:
    def __init__(self, text):
        self.chat_id = USER
        self.sender_id = USER
        self.message = type("M", (), {"id": 1, "message": text, "photo": None,
                                      "video": None, "document": None,
                                      "reply_to": None, "sticker": None})()


print("\n[1] Выдержка экранирует и режет в правильном порядке")
check("угловые скобки экранированы",
      assistant._bookmark_snippet("уступ <0.5 мм") == "уступ &lt;0.5 мм",
      f"got {assistant._bookmark_snippet('уступ <0.5 мм')!r}")
check("амперсанд экранирован",
      assistant._bookmark_snippet("Kerr & Co") == "Kerr &amp; Co",
      f"got {assistant._bookmark_snippet('Kerr & Co')!r}")
check("срез не разрывает сущность",
      markup_problem(assistant._bookmark_snippet("a" * 78 + " & длинный хвост")) is None,
      f"got {assistant._bookmark_snippet('a' * 78 + ' & длинный хвост')!r}")
check("многоточие только при реальной обрезке",
      assistant._bookmark_snippet("коротко") == "коротко")
check("длинный текст обрезан с многоточием",
      assistant._bookmark_snippet("щ" * 200).endswith("…"))
check("длина соблюдена",
      len(assistant._bookmark_snippet("щ" * 200)) <= assistant.BOOKMARK_SNIPPET_CHARS + 1)
check("пустое значение не падает", assistant._bookmark_snippet(None) == "")
check("теги из закладки не исполняются как разметка",
      "<b>" not in assistant._bookmark_snippet("<b>жирный</b>"),
      f"got {assistant._bookmark_snippet('<b>жирный</b>')!r}")


async def run():
    await database.init_db()

    print("\n[2] Закладка с угловой скобкой не ломает весь список")
    dangerous = "Уступ <0.5 мм при вертипрепе — норма? Цена < 5000 & без НДС"
    await database.save_clinical_bookmark(
        saved_by_user_id=USER, msg_id=100, chat_id=-1001820467444,
        sender_name="Иванов <главврач>", text=dangerous, has_media=False,
        media_description="", date=datetime(2026, 7, 27, tzinfo=timezone.utc))
    for i in range(3):
        await database.save_clinical_bookmark(
            saved_by_user_id=USER, msg_id=200 + i, chat_id=-1001820467444,
            sender_name="Петров", text=f"обычная закладка про коронки {i}",
            has_media=False, media_description="",
            date=datetime(2026, 7, 26, tzinfo=timezone.utc))

    SENT.clear()
    await assistant.handle_private_message(FakeBot(), FakeEvent("/bookmarks"))
    check("список отправлен", len(SENT) == 1, f"got {len(SENT)}")
    listing = SENT[-1]
    check("разметка валидна — Telegram примет", markup_problem(listing) is None,
          f"got {markup_problem(listing)}")
    check("опасная закладка не потеряна", "0.5 мм" in listing, f"got {listing[:200]}")
    check("скобка экранирована, а не вырезана", "&lt;0.5" in listing, f"got {listing[:200]}")
    check("соседние закладки на месте", listing.count("обычная закладка") == 3,
          f"got {listing.count('обычная закладка')}")
    check("имя автора тоже экранировано", "&lt;главврач&gt;" in listing, f"got {listing[:120]}")

    print("\n[3] Поиск сообщает, сколько всего нашлось")
    for i in range(15):
        await database.save_clinical_bookmark(
            saved_by_user_id=USER, msg_id=300 + i, chat_id=-1001820467444,
            sender_name="Сидоров", text=f"циркониевая коронка, случай {i}",
            has_media=False, media_description="",
            date=datetime(2026, 7, 25, tzinfo=timezone.utc))

    SENT.clear()
    await assistant.handle_private_message(FakeBot(), FakeEvent("/bookmarks циркониевая"))
    result = SENT[-1]
    check("найдено больше страницы — сказано сколько всего",
          "из 15 совпадений" in result, f"хвост: {result[-160:]}")
    check("показано ровно десять", result.count("циркониевая коронка") == 10,
          f"got {result.count('циркониевая коронка')}")
    check("разметка результата валидна", markup_problem(result) is None,
          f"got {markup_problem(result)}")

    SENT.clear()
    await assistant.handle_private_message(FakeBot(), FakeEvent("/bookmarks вертипрепе"))
    few = SENT[-1]
    check("когда совпадений мало — просто их число",
          "Найдено совпадений: 1" in few, f"хвост: {few[-140:]}")

    print("\n[3a] Закладку можно найти по автору, а не только по словам")
    SENT.clear()
    await assistant.handle_private_message(FakeBot(), FakeEvent("/bookmarks Иванов"))
    by_author = SENT[-1]
    check("поиск по имени автора находит закладку",
          "0.5 мм" in by_author, f"got {by_author[:160]}")
    check("разметка результата по автору валидна", markup_problem(by_author) is None)

    SENT.clear()
    await assistant.handle_private_message(FakeBot(), FakeEvent("/bookmarks Сидоров"))
    check("автор с многими закладками находится весь",
          "из 15 совпадений" in SENT[-1], f"хвост: {SENT[-1][-160:]}")

    print("\n[4] Пустой поиск и пустые закладки")
    SENT.clear()
    await assistant.handle_private_message(FakeBot(), FakeEvent("/bookmarks мазерати"))
    check("честно сообщено, что совпадений нет", "не найдено совпадений" in SENT[-1].lower(),
          f"got {SENT[-1][:120]}")

    SENT.clear()
    await assistant.handle_private_message(FakeBot(), FakeEvent("/bookmarks 99"))
    check("несуществующая страница объяснена", "не существует" in SENT[-1], f"got {SENT[-1][:120]}")

    print("\n[5] Постраничный вывод сохранён")
    SENT.clear()
    await assistant.handle_private_message(FakeBot(), FakeEvent("/bookmarks 2"))
    page2 = SENT[-1]
    check("вторая страница отдана", "Страница 2" in page2, f"got {page2[:120]}")
    check("разметка второй страницы валидна", markup_problem(page2) is None,
          f"got {markup_problem(page2)}")


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
