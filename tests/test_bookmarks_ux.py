"""
Тесты UI/UX клинических закладок:
1. Защита HTML-разметки от срезов в сниппетах (_bookmark_snippet).
2. Безопасность и IDOR-защита в database.py (get_clinical_bookmark_by_id, delete_clinical_bookmark).
3. Пагинация без холостого клика (bm:noop вместо bm:page:1 на кнопке 1/N).
4. Детальный просмотр закладок (bm:view:ID:page).
5. Удаление закладок с подтверждением (bm:del:ID:page -> bm:cdel:ID:page).
6. Валидность HTML-разметки во всех режимах отображения.

Запуск: python tests/test_bookmarks_ux.py
"""
import asyncio
import html
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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_bm_ux_")
import config
config.DB_PATH = os.path.join(_TMPDIR, "test_bm_ux.db")

import database
import assistant
assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

PASS, FAIL = [], []
USER_A = 1111
USER_B = 2222


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def markup_problem(text):
    """Проверяет валидность Telegram HTML разметки."""
    if not text:
        return None
    for match in re.finditer("<", text):
        end = text.find(">", match.start())
        if end == -1:
            return "обрыв тега"
        tag = text[match.start() + 1:end].split()[0].lstrip("/").lower()
        if tag not in ("b", "i", "code", "a", "br", "pre", "u", "s", "blockquote", "tg-spoiler"):
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


class FakeEvent:
    def __init__(self, data_str, sender_id=USER_A, message_id=100):
        self.data = data_str.encode("utf-8") if isinstance(data_str, str) else data_str
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.message_id = message_id
        self.answers = []

    async def answer(self, message=None, alert=False):
        self.answers.append({"message": message, "alert": alert})


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []

    async def send_message(self, entity=None, message=None, buttons=None, parse_mode=None, **kw):
        self.sent_messages.append({
            "entity": entity,
            "message": message,
            "buttons": buttons,
            "parse_mode": parse_mode
        })
        return type("M", (), {"id": 1, "text": message})()

    async def edit_message(self, *args, **kw):
        # Telethon client.edit_message(chat_id, message_id, text, **kw)
        entity = args[0] if len(args) > 0 else kw.get("entity")
        message_id = args[1] if len(args) > 1 else kw.get("message")
        msg_text = args[2] if len(args) > 2 else kw.get("text", kw.get("message"))
        buttons = kw.get("buttons")
        parse_mode = kw.get("parse_mode")
        self.edited_messages.append({
            "entity": entity,
            "message_id": message_id,
            "message": msg_text,
            "buttons": buttons,
            "parse_mode": parse_mode
        })
        return type("M", (), {"id": message_id or 1, "text": msg_text})()


async def test_snippet_html_safety():
    print("\n[1] Защита HTML-разметки в _bookmark_snippet")

    # Срез посреди тега форматирования не ломает разметку
    raw_with_tags = "<b>" + ("А" * 150) + "</b>"
    snip = assistant._bookmark_snippet(raw_with_tags, limit=120)
    check("срез длинного жирного текста не содержит незакрытых тегов",
          markup_problem(snip) is None, f"got {markup_problem(snip)} in {snip!r}")
    check("тег <b> удален из сниппета перед срезом",
          "<b>" not in snip and "&lt;b&gt;" not in snip)
    check("многоточие в конце длинного текста", snip.endswith("…"))

    # Сниппет из энциклопедии с кодом и курсивом
    wiki_sample = "📚 <b>Эндодонтия: NaOCl</b>\n\nПротокол <code>3% NaOCl</code> с ультразвуковой <i>активацией</i>."
    wiki_snip = assistant._bookmark_snippet(wiki_sample, limit=80)
    check("сниппет энциклопедии валиден", markup_problem(wiki_snip) is None)
    check("теги <code> и <i> очищены", "<code>" not in wiki_snip and "<i>" not in wiki_snip)
    check("содержимое не потеряно", "NaOCl" in wiki_snip and "активацией" in wiki_snip)

    # Клинические знаки сравнения сохраняются и экранируются
    clinical_text = "Уступ <0.5 мм при вертипрепе. Толщина стенки > 1.5 мм & без трещин."
    clin_snip = assistant._bookmark_snippet(clinical_text, limit=100)
    check("клинический знак <0.5 мм сохранен и экранирован", "&lt;0.5 мм" in clin_snip)
    check("клинический знак > 1.5 мм сохранен и экранирован", "&gt; 1.5 мм" in clin_snip)
    check("амперсанд экранирован", "&amp;" in clin_snip)
    check("разметка клинического сниппета валидна", markup_problem(clin_snip) is None)


async def test_database_layer():
    print("\n[2] База данных: получение с ID, IDOR-защита и удаление")
    await database.init_db()

    dt = datetime.now(timezone.utc)
    await database.save_clinical_bookmark(USER_A, 101, -100123, "Д-р Иванов", "Пост про КТ 1", False, "", dt)
    await database.save_clinical_bookmark(USER_A, 102, -100123, "Д-р Петров", "Пост про КТ 2", False, "", dt)
    await database.save_clinical_bookmark(USER_B, 201, -100123, "Д-р Сидоров", "Пост Чужой", False, "", dt)

    # get_clinical_bookmarks с include_id=True
    rows_with_id = await database.get_clinical_bookmarks(USER_A, include_id=True)
    check("include_id=True возвращает id первым полем",
          len(rows_with_id) == 2 and isinstance(rows_with_id[0][0], int), f"got {rows_with_id}")
    bm_a_id = rows_with_id[0][0]

    # get_clinical_bookmarks по умолчанию без id (обратная совместимость)
    rows_compat = await database.get_clinical_bookmarks(USER_A)
    check("include_id=False (дефолт) сохраняет кортеж из 6 элементов",
          len(rows_compat[0]) == 6 and rows_compat[0][0] == 102)

    # get_clinical_bookmark_by_id с проверкой владельца
    bm_found = await database.get_clinical_bookmark_by_id(bm_a_id, saved_by_user_id=USER_A)
    check("владелец может получить свою закладку по ID", bm_found is not None and bm_found[0] == bm_a_id)

    bm_idor = await database.get_clinical_bookmark_by_id(bm_a_id, saved_by_user_id=USER_B)
    check("IDOR: чужой пользователь НЕ может получить закладку", bm_idor is None)

    # delete_clinical_bookmark с IDOR-защитой
    del_idor = await database.delete_clinical_bookmark(bm_a_id, saved_by_user_id=USER_B)
    check("IDOR: попытка удалить чужую закладку возвращает False", del_idor is False)

    still_exists = await database.get_clinical_bookmark_by_id(bm_a_id, saved_by_user_id=USER_A)
    check("закладка не была удалена чужим пользователем", still_exists is not None)

    del_success = await database.delete_clinical_bookmark(bm_a_id, saved_by_user_id=USER_A)
    check("владелец успешно удаляет свою закладку", del_success is True)

    after_del = await database.get_clinical_bookmark_by_id(bm_a_id, saved_by_user_id=USER_A)
    check("удаленная закладка больше не существует", after_del is None)


async def test_pagination_ux_and_noop():
    print("\n[3] UX пагинации: отсутствие холостого клика (bm:noop)")
    bot = FakeBot()

    # Заполним базу 7 закладками (при per_page=5 это 2 страницы)
    dt = datetime.now(timezone.utc)
    for i in range(1, 8):
        await database.save_clinical_bookmark(USER_A, 500 + i, -100123, f"Автор {i}", f"Клиническая мысль #{i}", False, "", dt)

    ev_page1 = FakeEvent("bm:page:1", sender_id=USER_A)
    await assistant.handle_quiz_callback(bot, ev_page1)

    # Проверяем кнопки на странице 1
    check("сообщение списка закладок отправлено", len(bot.edited_messages) > 0)
    last_msg = bot.edited_messages[-1]
    check("разметка списка валидна", markup_problem(last_msg["message"]) is None)

    buttons = last_msg["buttons"]
    # Ищем кнопку-счётчик страниц
    counter_btn = None
    for row in buttons:
        for b in row:
            if "/" in getattr(b, "text", ""):
                counter_btn = b
                break

    check("кнопка-счётчик страниц найдена", counter_btn is not None)
    if counter_btn:
        check("кнопка 1/N шлет bm:noop, а не bm:page:1",
              getattr(counter_btn, "data", b"").decode("utf-8") == "bm:noop",
              f"got {getattr(counter_btn, 'data', None)}")

    # Клик по bm:noop гасится ответом на callback без перерисовки
    ev_noop = FakeEvent("bm:noop", sender_id=USER_A)
    prev_edits = len(bot.edited_messages)
    await assistant.handle_quiz_callback(bot, ev_noop)
    check("bm:noop отвечает на событие Telegram", len(ev_noop.answers) == 1)
    check("bm:noop не вызывает холостой перезагрузки сообщения", len(bot.edited_messages) == prev_edits)


async def test_detailed_view_and_delete_flow():
    print("\n[4] Детальный просмотр (bm:view) и удаление с подтверждением (bm:del -> bm:cdel)")
    bot = FakeBot()

    dt = datetime.now(timezone.utc)
    await database.save_clinical_bookmark(
        USER_A, 888, -1001999999999, "Д-р Смирнов",
        "Полный протокол ревизии:\n1. Распломбировка гуттаперчи растворителем.\n2. Пассивная ультразвуковая ирригация NaOCl 3%.",
        False, "", dt
    )

    rows = await database.get_clinical_bookmarks(USER_A, include_id=True)
    bm_id = rows[0][0]

    # 1. Открытие детального просмотра: bm:view:ID:1
    ev_view = FakeEvent(f"bm:view:{bm_id}:1", sender_id=USER_A)
    await assistant.handle_quiz_callback(bot, ev_view)

    view_msg = bot.edited_messages[-1]
    check("детальный просмотр открылся", "Клиническая закладка" in view_msg["message"])
    check("разметка детального просмотра валидна", markup_problem(view_msg["message"]) is None)
    check("полный текст протокола присутствует", "Пассивная ультразвуковая ирригация" in view_msg["message"])
    check("ссылка на оригинальный пост есть", "Перейти к оригинальному сообщению" in view_msg["message"])

    # Проверяем кнопки детального просмотра: [🗑 Удалить закладку] и [⬅️ К списку]
    v_buttons = view_msg["buttons"]
    del_btn = next((b for row in v_buttons for b in row if "Удалить" in getattr(b, "text", "")), None)
    back_btn = next((b for row in v_buttons for b in row if "К списку" in getattr(b, "text", "")), None)
    check("кнопка 'Удалить' присутствует в детальном просмотре", del_btn is not None)
    check("кнопка 'К списку' присутствует", back_btn is not None)
    check("кнопка удаления шлет bm:del:ID:page",
          getattr(del_btn, "data", b"").decode("utf-8") == f"bm:del:{bm_id}:1")

    # 2. Нажатие на кнопку удаления: bm:del:ID:1 (Запрос подтверждения)
    ev_del_req = FakeEvent(f"bm:del:{bm_id}:1", sender_id=USER_A)
    await assistant.handle_quiz_callback(bot, ev_del_req)

    confirm_msg = bot.edited_messages[-1]
    check("открылось окно подтверждения удаления", "Подтверждение удаления" in confirm_msg["message"])
    check("разметка подтверждения валидна", markup_problem(confirm_msg["message"]) is None)

    c_buttons = confirm_msg["buttons"]
    cdel_btn = next((b for row in c_buttons for b in row if "Да, удалить" in getattr(b, "text", "")), None)
    cancel_btn = next((b for row in c_buttons for b in row if "Отмена" in getattr(b, "text", "")), None)
    check("кнопка подтверждения 'Да, удалить' есть", cdel_btn is not None)
    check("кнопка подтверждения шлет bm:cdel:ID:page",
          getattr(cdel_btn, "data", b"").decode("utf-8") == f"bm:cdel:{bm_id}:1")
    check("кнопка 'Отмена' возвращает к списку",
          getattr(cancel_btn, "data", b"").decode("utf-8") == "bm:page:1")

    # 3. Подтверждение удаления: bm:cdel:ID:1
    ev_confirm = FakeEvent(f"bm:cdel:{bm_id}:1", sender_id=USER_A)
    await assistant.handle_quiz_callback(bot, ev_confirm)

    check("событие подтверждения отвечено уведомлением",
          any("удалена" in (a.get("message") or "") for a in ev_confirm.answers))

    # Проверяем, что в БД закладка удалена
    check_db = await database.get_clinical_bookmark_by_id(bm_id, saved_by_user_id=USER_A)
    check("закладка действительно удалена из базы", check_db is None)


async def test_clean_html_with_broken_tags():
    print("\n[5] Защита от битых HTML-тегов в детальном просмотре")
    bot = FakeBot()

    dt = datetime.now(timezone.utc)
    # Сохраняем пост с незакрытыми тегами
    broken_html = "Внимание: <b>Незакрытый жирный текст и <code>незакрытый код без конца"
    await database.save_clinical_bookmark(USER_A, 999, -100123, "Хирург", broken_html, False, "", dt)

    rows = await database.get_clinical_bookmarks(USER_A, include_id=True)
    bm_broken = next(r for r in rows if r[1] == 999)
    bm_id = bm_broken[0]

    # Открываем детальный просмотр с битым HTML
    ev_broken = FakeEvent(f"bm:view:{bm_id}:1", sender_id=USER_A)
    await assistant.handle_quiz_callback(bot, ev_broken)

    view_msg = bot.edited_messages[-1]
    prob = markup_problem(view_msg["message"])
    check("битые теги сбалансированы clean_html_formatting", prob is None, f"got problem: {prob}")
    check("Telegram примет сообщение без HTML parse error", "</b>" in view_msg["message"] and "</code>" in view_msg["message"])


async def main():
    try:
        await test_snippet_html_safety()
        await test_database_layer()
        await test_pagination_ux_and_noop()
        await test_detailed_view_and_delete_flow()
        await test_clean_html_with_broken_tags()
    finally:
        shutil.rmtree(_TMPDIR, ignore_errors=True)

    print(f"\n{'=' * 60}")
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print("Провалены тесты: " + ", ".join(FAIL))
        sys.exit(1)
    else:
        print("Все тесты успешно пройдены!")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
