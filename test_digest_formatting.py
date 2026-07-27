"""
Сборка текста дайджеста: разметка, заголовки разделов, списки.

То, что врач видит в чате. Проверяется настоящий clean_markdown_to_html на
выводе, похожем на реальный ответ модели.

Главный дефект, который тут закрыт: защита заголовков смотрела только на
ПРЕДЫДУЩУЮ строку, поэтому название раздела всегда затягивалось в конец абзаца
перед ним, если тот не оканчивался точкой. В чат уходило
«...резекция <b>Ортопедия</b> Спор о границах уступа...» — заголовок посреди
предложения.

Запуск: python test_digest_formatting.py
"""
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


DIGEST = """## Итоги дня

**Эндодонтия**
Обсуждали ретрит 36 зуба. Коллега использовал протейперы,
но столкнулся с отломом файла в апикальной трети.
- Совет: убрать под микроскопом
- Альтернатива: резекция

**Ортопедия**
Спор о границах уступа под цирконий. Часть коллег за BOPT,
часть за вертикальный препаринг.

**Оборудование**
Обсуждали сканеры и
цены на печи."""

result = S.clean_markdown_to_html(DIGEST)
lines = result.split("\n")

print("\n[1] Заголовок раздела не оказывается внутри абзаца")
for heading in ("Эндодонтия", "Ортопедия", "Оборудование", "Итоги дня"):
    own_line = any(line.strip() == f"<b>{heading}</b>" for line in lines)
    check(f"«{heading}» на отдельной строке", own_line,
          f"строки: {[l for l in lines if heading in l]}")

print("\n[2] Перед заголовком раздела есть пустая строка")
for heading in ("Эндодонтия", "Ортопедия", "Оборудование"):
    index = next(i for i, line in enumerate(lines) if line.strip() == f"<b>{heading}</b>")
    check(f"«{heading}» отделён от предыдущего блока", index > 0 and lines[index - 1] == "",
          f"перед ним: {lines[index - 1]!r}")

print("\n[3] Пункты списка остаются разными строками")
check("«Совет» и «Альтернатива» не склеены",
      not any("Совет" in line and "Альтернатива" in line for line in lines),
      f"строки: {[l for l in lines if 'Совет' in l]}")
check("«Совет» есть в выводе", any("Совет" in line for line in lines))
check("«Альтернатива» есть в выводе", any("Альтернатива" in line for line in lines))

print("\n[4] Перенос внутри предложения по-прежнему склеивается")
check("«протейперы, но столкнулся» в одной строке",
      any("протейперы, но столкнулся" in line for line in lines),
      f"строки: {[l for l in lines if 'протейперы' in l]}")
check("«за BOPT, часть за вертикальный» в одной строке",
      any("за BOPT, часть за вертикальный" in line for line in lines))
check("строка с маленькой буквы приклеена к предыдущей",
      any("сканеры и цены на печи" in line for line in lines),
      f"строки: {[l for l in lines if 'сканеры' in l]}")

print("\n[5] Ничего из содержания не потерялось")
plain = re.sub(r"<[^>]+>", "", result)
for fragment in ("ретрит 36 зуба", "отломом файла", "микроскопом", "резекция",
                 "уступа под цирконий", "вертикальный препаринг", "цены на печи"):
    check(f"«{fragment}» на месте", fragment in plain, f"не найдено в {plain[:80]!r}")

print("\n[6] Жирный внутри предложения заголовком не считается")
out = S.clean_markdown_to_html("Главное: **уступ** должен быть\nчитаемым на скане.")
check("выделение внутри фразы не разрывает абзац", out.count("\n") == 0, f"got {out!r}")
check("выделение сохранено", "<b>уступ</b>" in out, f"got {out!r}")

print("\n[7] Длинная жирная строка заголовком не считается")
long_bold = "**" + "очень длинная выделенная мысль про протокол препарирования и границы" + "**"
out = S.clean_markdown_to_html("Предыдущая строка без точки\n" + long_bold)
check("длинное выделение не превращается в заголовок", "\n" not in out, f"got {out!r}")

print("\n[8] Жирный заголовок с двоеточием остаётся частью фразы")
out = S.clean_markdown_to_html("**Вывод:**\nставим коронку")
check("после двоеточия строка продолжается", "\n" not in out, f"got {out!r}")

print("\n[9] Краевые случаи не роняют сборку")
check("пустой вход", S.clean_markdown_to_html("") == "")
check("None", S.clean_markdown_to_html(None) == "")
check("только пробелы", S.clean_markdown_to_html("   \n  \n ") == "")
check("только заголовок", S.clean_markdown_to_html("**Итог**") == "<b>Итог</b>")
check("только маркеры списка", S.clean_markdown_to_html("- \n- \n") == "")
check("markdown-заголовки становятся жирными",
      S.clean_markdown_to_html("### Раздел") == "<b>Раздел</b>",
      f"got {S.clean_markdown_to_html('### Раздел')!r}")

print("\n[10] Двойные пробелы схлопываются полностью")
check("три пробела не оставляют двух",
      "  " not in S.clean_markdown_to_html("слово   слово"),
      f"got {S.clean_markdown_to_html('слово   слово')!r}")

print("\n[11] Результат остаётся валидной разметкой для Telegram")
def markup_problem(text):
    for match in re.finditer("<", text):
        if text.find(">", match.start()) == -1:
            return "обрыв тега"
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
    return f"незакрытые {stack}" if stack else None

check("разметка дайджеста валидна", markup_problem(result) is None, f"got {markup_problem(result)}")
check("после обрезки тоже валидна",
      markup_problem(S._safe_truncate_html(result, max_len=200)) is None,
      f"got {markup_problem(S._safe_truncate_html(result, max_len=200))}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
