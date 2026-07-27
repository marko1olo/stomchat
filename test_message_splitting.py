"""
Разбиение длинного ответа на части — основной путь ответа в ЛС.

Прежний разделитель резал по абзацам и не следил за тегами. Замер на
воспроизводимых примерах:

  тег открыт в одном абзаце, закрыт в другом (4713 символов)
      было: 3 части, 2 из них Telegram отклонил бы
             («незакрытые ['b']» и «непарный </b>»)
  тег ровно на границе 4000 (7007 символов)
      было: 2 части, отклонены ОБЕ — ответ терялся целиком

Отклонение части означает не испорченное форматирование, а потерю куска
клинического ответа: врач спросил и получил половину или ничего.

Запуск: python test_message_splitting.py
"""
import asyncio
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import html_safe

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def markup_problem(text):
    """Нашёл бы ли Telegram причину отклонить эту часть."""
    for match in re.finditer("<", text):
        if text.find(">", match.start()) == -1:
            return "обрыв тега"
    if re.search(r"&[a-zA-Z#][a-zA-Z0-9]{0,8}$", text):
        return "обрыв HTML-сущности"
    stack = []
    for closing, name in re.findall(r"<(/?)([a-zA-Z]+)", text):
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
    return f"незакрытые {stack}" if stack else None


def plain(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()


LIMIT = 4000

CASES = {
    "тег открыт в одном абзаце, закрыт в другом":
        "<b>Ключевой вывод по случаю\n\n" + "подробный разбор тактики. " * 180 + "</b>",
    "тег ровно на границе среза":
        "щ" * 3995 + "<b>важно</b>" + "щ" * 3000,
    "HTML-сущность на границе среза":
        "щ" * 3997 + "&amp;" + "щ" * 3000,
    "один абзац без единого пробела":
        "щ" * 9000,
    "вложенные теги через несколько границ":
        "<b><i>" + "текст ответа " * 1500 + "</i></b>",
    "реалистичный длинный ответ":
        "\n\n".join(f"<b>Раздел {i}</b>\nразбор клинической ситуации. " * 30 for i in range(6)),
}

print("\n[1] Ни одна часть не будет отклонена Telegram")
for name, source in CASES.items():
    parts = html_safe.split_html(source, limit=LIMIT)
    broken = [(i, markup_problem(p)) for i, p in enumerate(parts) if markup_problem(p)]
    check(f"{name}: разметка каждой части валидна", not broken, f"got {broken[:2]}")

print("\n[2] Лимит длины соблюдён для каждой части")
for name, source in CASES.items():
    parts = html_safe.split_html(source, limit=LIMIT)
    over = [len(p) for p in parts if len(p) > LIMIT]
    check(f"{name}: превышений нет", not over, f"got {over}")

print("\n[3] Текст не теряется при разбиении")
for name, source in CASES.items():
    parts = html_safe.split_html(source, limit=LIMIT)
    before = plain(source).replace(" ", "")
    after = plain(" ".join(parts)).replace(" ", "")
    check(f"{name}: содержание сохранено", after == before,
          f"было {len(before)} символов, стало {len(after)}")

print("\n[4] Форматирование продолжается в следующей части")
parts = html_safe.split_html(CASES["вложенные теги через несколько границ"], limit=LIMIT)
check("частей больше одной", len(parts) > 1, f"got {len(parts)}")
check("вторая часть открывает теги заново",
      parts[1].startswith("<b><i>"), f"got {parts[1][:40]!r}")
check("первая часть закрывает их за собой",
      parts[0].rstrip().endswith("</i></b>"), f"got {parts[0][-40:]!r}")

print("\n[5] Короткие и пустые ответы")
check("короткий ответ одной частью",
      html_safe.split_html("<b>Коротко</b> и по делу.", LIMIT) == ["<b>Коротко</b> и по делу."])
check("пустой ответ не даёт частей", html_safe.split_html("", LIMIT) == [])
check("None не роняет", html_safe.split_html(None, LIMIT) == [])
check("пробелы не дают пустой части", html_safe.split_html("   \n\n  ", LIMIT) == [])
check("незакрытый тег в коротком ответе закрывается",
      html_safe.split_html("<b>Итог", LIMIT) == ["<b>Итог</b>"],
      f"got {html_safe.split_html('<b>Итог', LIMIT)}")

print("\n[6] Разрыв ищется по границе абзаца, а не посреди слова")
answer = ("Первый абзац разбора клинической ситуации. " * 60 + "\n\n"
          + "Второй абзац с продолжением мысли. " * 60)
parts = html_safe.split_html(answer, limit=LIMIT)
check("разбито на части", len(parts) > 1, f"got {len(parts)}")
check("часть не обрывается посреди слова",
      all(p.endswith((".", "</b>", "</i>")) or p[-1].isspace() for p in parts[:-1]),
      f"хвосты: {[p[-25:] for p in parts[:-1]]}")

print("\n[7] Отправка идёт настоящим разделителем")
import io, os
source = io.open(os.path.join(os.getcwd(), "assistant.py"), encoding="utf-8").read()
sender = source.split("async def send_message_chunks_async", 1)[1].split("\n\n\n", 1)[0]
check("используется html_safe.split_html", "html_safe.split_html" in sender)
# Ищем именно код старого цикла, а не упоминание среза в пояснении к правке.
check("посимвольного цикла нарезки больше нет",
      "for i in range(0, len(p)" not in sender, "старый цикл остался")
check("ручной склейки абзацев больше нет",
      "current_chunk" not in sender, "старая сборка осталась")
check("лимит вынесен в константу", "TELEGRAM_MESSAGE_LIMIT" in source)

print("\n[8] Части действительно уходят по одной")
SENT = []


class FakeBot:
    async def send_message(self, entity=None, message=None, **kw):
        SENT.append(message)


async def drive():
    import assistant
    await assistant.send_message_chunks_async(
        FakeBot(), 1, CASES["вложенные теги через несколько границ"], parse_mode="html")


asyncio.run(drive())
check("отправлено несколько сообщений", len(SENT) > 1, f"got {len(SENT)}")
check("каждое отправленное валидно",
      not [markup_problem(m) for m in SENT if markup_problem(m)],
      f"got {[markup_problem(m) for m in SENT if markup_problem(m)][:2]}")
check("каждое в пределах лимита", all(len(m) <= LIMIT for m in SENT),
      f"got {[len(m) for m in SENT]}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
