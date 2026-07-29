"""
Доставка дайджеста: обрезка разметки и запасной путь отправки.

Прогоняются настоящие функции summarizer с подставным Telegram-клиентом,
который считает запросы и умеет отвечать теми же ошибками, что настоящий.

Смысл: раньше битая разметка означала не просто испорченное форматирование, а
недоставленный дайджест. Telegram отклоняет сообщение целиком, планировщик не
помечает день отправленным и каждые 10 минут генерирует отчёт заново
LLM-вызовом — до конца суток это десятки платных генераций и ни одной доставки.

Запуск: python test_summary_delivery.py
"""
import asyncio
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import summarizer as S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def markup_problem(text):
    """Нашёл бы ли Telegram причину отклонить эту разметку."""
    for match in re.finditer("<", text):
        if text.find(">", match.start()) == -1:
            return "обрыв тега"
    if re.search(r"&[a-zA-Z#][a-zA-Z0-9]{0,8}$", text):
        return "обрыв HTML-сущности"
    stack = []
    for closing, name in re.findall(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)", text):
        name = name.lower()
        if name in ("br", "img", "hr"):
            continue
        if closing:
            if name in stack:
                del stack[stack.index(name):]
            else:
                return f"непарный </{name}>"
        else:
            stack.append(name)
    if stack:
        return f"незакрытые {stack}"
    return None


print("\n[1] Обрезка не производит разметку, которую Telegram отклонит")
cases = [
    ("срез приходится внутрь тега", "x" * 3890 + '<a href="https://ex.com/l">ссылка</a>' + "y" * 300, 3900),
    ("срез приходится внутрь сущности", "a" * 3895 + "&nbsp;" + "b" * 300, 3900),
    ("перекрёстные теги", "<b><i>" + "z" * 5000, 3900),
    ("глубокая вложенность", "<b><i><u><s><code>" + "q" * 5000, 3900),
    ("текст ровно на границе", "y" * 3900, 3900),
    ("длинный отчёт для telegraph", "<p>" + "ц" * 20000 + "</p>", 9500),
]
for name, source, limit in cases:
    result = S._safe_truncate_html(source, max_len=limit)
    problem = markup_problem(result)
    check(f"{name}: разметка валидна", problem is None, f"got {problem}")
    check(f"{name}: длина в пределах {limit}", len(result) <= limit, f"got {len(result)}")

print("\n[2] Заявленный лимит соблюдается (до правки 3900 давало 3958)")
for limit in (500, 3900, 9500):
    result = S._safe_truncate_html("<b>" + "щ" * (limit * 3) + "</b>", max_len=limit)
    check(f"лимит {limit} не превышен", len(result) <= limit, f"got {len(result)}")

print("\n[3] Разметка правится и когда обрезка не нужна")
check("незакрытый тег дописан",
      S._safe_truncate_html("<b>Итог дня") == "<b>Итог дня</b>",
      f"got {S._safe_truncate_html('<b>Итог дня')!r}")
check("непарный закрывающий убран",
      S._safe_truncate_html("<b>текст</i></b> ещё") == "<b>текст</b> ещё",
      f"got {S._safe_truncate_html('<b>текст</i></b> ещё')!r}")
check("корректная разметка не тронута",
      S._safe_truncate_html("<b>Итог</b> дня") == "<b>Итог</b> дня")
check("пустой вход не падает", S._safe_truncate_html("") == "")
check("None не падает", S._safe_truncate_html(None) == "")

print("\n[4] Плоский текст сохраняет структуру отчёта")
plain = S._html_to_plain("<b>Заголовок</b><br><br>Первый абзац<br>вторая строка&nbsp;— тире")
check("абзацы сохранены", plain.count("\n\n") == 1, f"got {plain!r}")
check("перевод строки сохранён", "\n" in plain)
check("теги убраны", "<" not in plain, f"got {plain!r}")
check("сущности раскрыты", "&nbsp;" not in plain, f"got {plain!r}")


class FakeClient:
    """Считает запросы и умеет падать так же, как настоящий Telegram."""

    def __init__(self, fail_html=False, fail_other=False, recent=None):
        self.fail_html = fail_html
        self.fail_other = fail_other
        self.recent = recent or []
        self.sends = []

    async def get_messages(self, chat_id, limit=None):
        return self.recent

    async def send_message(self, chat_id, text, **params):
        self.sends.append({"text": text, "params": params})
        if self.fail_other:
            raise RuntimeError("FLOOD_WAIT_X: подождите 30 секунд")
        if self.fail_html and params.get("parse_mode") == "HTML":
            raise RuntimeError(
                "Can't parse entities: unmatched end tag at byte offset 512"
            )
        return type("Sent", (), {"id": 500 + len(self.sends)})()


class FakeMessage:
    def __init__(self, text, reply_to=None):
        self.message = text
        self.raw_text = text
        self.reply_to = reply_to


async def run():
    print("\n[5] Отказ разбора разметки — отчёт всё равно доставляется")
    client = FakeClient(fail_html=True)
    sent = await S._send_message_once(
        client, -100123, None,
        "<b>Дайджест</b><br>тело отчёта&nbsp;с деталями",
        {"parse_mode": "HTML", "link_preview": True},
        "daily_test",
    )
    check("сообщение отправлено, а не потеряно", sent is not None)
    check("была вторая попытка", len(client.sends) == 2, f"got {len(client.sends)}")
    check("повтор ушёл без parse_mode",
          "parse_mode" not in client.sends[-1]["params"], f"got {client.sends[-1]['params']}")
    check("повтор ушёл плоским текстом",
          "<b>" not in client.sends[-1]["text"], f"got {client.sends[-1]['text']!r}")
    check("содержание отчёта сохранилось",
          "тело отчёта" in client.sends[-1]["text"], f"got {client.sends[-1]['text']!r}")
    check("link_preview не потерян", client.sends[-1]["params"].get("link_preview") is True)

    print("\n[6] Прочие ошибки НЕ маскируются запасным путём")
    client = FakeClient(fail_other=True)
    raised = False
    try:
        await S._send_message_once(client, -100123, None, "<b>x</b>",
                                   {"parse_mode": "HTML"}, "daily_test")
    except RuntimeError as exc:
        raised = "FLOOD_WAIT" in str(exc)
    check("ошибка проброшена наверх", raised is True)
    check("второй попытки не было", len(client.sends) == 1, f"got {len(client.sends)}")

    print("\n[7] Успешная отправка идёт одним запросом")
    client = FakeClient()
    sent = await S._send_message_once(client, -100123, None, "<b>Дайджест</b>",
                                      {"parse_mode": "HTML"}, "daily_test")
    check("отправлено", sent is not None)
    check("ровно один запрос", len(client.sends) == 1, f"got {len(client.sends)}")
    check("parse_mode сохранён", client.sends[0]["params"].get("parse_mode") == "HTML")

    print("\n[8] Защита от дубля: уже опубликованный отчёт повторно не уходит")
    text = "<b>Дайджест</b> уже в чате"
    client = FakeClient(recent=[FakeMessage("Дайджест уже в чате")])
    sent = await S._send_message_once(client, -100123, None, text,
                                      {"parse_mode": "HTML"}, "daily_test")
    check("вернулось найденное сообщение", sent is not None)
    check("новых отправок не было", client.sends == [], f"got {client.sends}")

    print("\n[9] Дедупликация узнаёт свой отчёт — с разметкой и без")
    # Telegram отдаёт текст сообщения уже без тегов. Пока тег вырезался пустой
    # строкой, «<b>Дайджест</b>тело» нормализовалось в «Дайджесттело» и с
    # присланным «Дайджест тело» не совпадало — защита не срабатывала никогда.
    html_text = "<b>Дайджест</b><br>тело"
    check("html и плоский текст нормализуются одинаково",
          S._normalize_delivery_text(html_text) == S._normalize_delivery_text(S._html_to_plain(html_text)),
          f"{S._normalize_delivery_text(html_text)!r} vs {S._normalize_delivery_text(S._html_to_plain(html_text))!r}")
    check("слова не склеиваются",
          S._normalize_delivery_text("<b>Дайджест</b>тело") == "Дайджест тело",
          f"got {S._normalize_delivery_text('<b>Дайджест</b>тело')!r}")

    real_digest = "🎓 <b>Дайджест из чата (27 июля)</b>\n\nОбсуждали <b>уступ</b> под цирконий."
    from_telegram = "🎓 Дайджест из чата (27 июля)\n\nОбсуждали уступ под цирконий."
    check("реальный отчёт узнаётся в присланном Telegram виде",
          S._normalize_delivery_text(real_digest) == S._normalize_delivery_text(from_telegram),
          f"{S._normalize_delivery_text(real_digest)!r} vs {S._normalize_delivery_text(from_telegram)!r}")

    client = FakeClient(recent=[FakeMessage(from_telegram)])
    sent = await S._send_message_once(client, -100123, None, real_digest,
                                      {"parse_mode": "HTML"}, "daily_test")
    check("повторная публикация отчёта с тегами предотвращена",
          sent is not None and client.sends == [], f"got {client.sends}")


asyncio.run(run())

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
