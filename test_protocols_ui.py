"""
Раздел «Протоколы»: кнопки, обработчики и безопасность выдержки.

В тексте /protocols перечислено пять протоколов, а кнопок было четыре —
«Вертикальное препарирование» открыть было нельзя вообще.

Выдержка обрезалась голым срезом [:1500] и уходила в edit_message с
parse_mode='html'. Срез мог попасть внутрь тега или внутрь экранированной
сущности, а Telegram отклоняет такую разметку целиком: врач нажимает кнопку и
не видит ничего. На сегодняшнем корпусе это скрытая угроза, а не срабатывающий
отказ — в базе знаний 0 фактов с markdown-жирным из 12 784 и 4 факта со знаком
«&», — но срез теперь идёт через html_safe.

Запуск: python test_protocols_ui.py
"""
import io
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import html_safe

SOURCE = io.open("assistant.py", encoding="utf-8").read()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def markup_problem(text):
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
    return f"незакрытые {stack}" if stack else None


# Обработчик протоколов вырезаем целиком: keywords_map в файле не один, и
# наивный split находит словарь категорий энциклопедии.
PROTO_HANDLER = SOURCE.split('if data_str.startswith("proto:")', 1)[1].split("# WIKI MAIN MENU BACK", 1)[0]
# Код без строк-комментариев: иначе проверки ловят пояснения о том, как было.
PROTO_CODE = "\n".join(
    line for line in PROTO_HANDLER.split("\n") if not line.lstrip().startswith("#")
)

button_ids = set(re.findall(r'data="proto:(\w+)"', SOURCE))
keyword_ids = set(re.findall(r'"(\w+)": \[', PROTO_HANDLER.split("keywords_map = {", 1)[1].split("}", 1)[0]))
title_ids = set(re.findall(r'"(\w+)": "', PROTO_HANDLER.split("proto_names = {", 1)[1].split("}", 1)[0]))

print("\n[1] Кнопка, ключевые слова и заголовок есть у каждого протокола")
protocols = button_ids - {"back"}
check("протоколов пять", len(protocols) == 5, f"got {sorted(protocols)}")
for proto in sorted(protocols):
    check(f"{proto}: есть ключевые слова", proto in keyword_ids, f"есть только {sorted(keyword_ids)}")
    check(f"{proto}: есть заголовок", proto in title_ids, f"есть только {sorted(title_ids)}")

print("\n[2] Ни один протокол не потерян между списком и кнопками")
check("вертикальное препарирование доступно кнопкой", "vertical" in protocols,
      f"got {sorted(protocols)}")
listed = SOURCE.split("📚 <b>Основные клинические протоколы", 1)[1].split("👇", 1)[0]
check("в тексте перечислено столько же, сколько кнопок",
      listed.count("• <b>") == len(protocols),
      f"в тексте {listed.count('• <b>')}, кнопок {len(protocols)}")

print("\n[3] Кнопка «назад» повторяет тот же набор")
back_block = SOURCE.split('if data_str == "proto:back"', 1)[1].split("await bot_client.edit_message", 1)[0]
back_ids = set(re.findall(r'data="proto:(\w+)"', back_block))
check("возврат к списку показывает все протоколы", back_ids == protocols,
      f"в возврате {sorted(back_ids)}, всего {sorted(protocols)}")

print("\n[4] Выдержка обрезается безопасно, а не голым срезом")
check("голого среза [:1500] в обработчике протоколов нет",
      "wiki_corpus[:1500]" not in PROTO_CODE, "срез всё ещё на месте")
check("используется общий html_safe", "html_safe.safe_truncate_html" in PROTO_CODE)
check("длина вынесена в константу", "PROTOCOL_EXCERPT_MAX_CHARS" in SOURCE)

print("\n[5] Обрезка выдерживает разметку, которую даёт clean_html_formatting")
# clean_html_formatting сохраняет <b>, <i>, <code> и экранирует «&» в «&amp;».
cases = {
    "тег на границе среза": "щ" * 1495 + "<b>важно</b>" + "щ" * 500,
    "сущность на границе": "щ" * 1497 + "&amp;" + "щ" * 500,
    "незакрытый тег в источнике": "<b>" + "щ" * 3000,
    "вложенные теги": "<b><i>" + "щ" * 3000,
    "короткий текст без обрезки": "<b>Протокол ирригации</b> — коротко.",
}
for name, source in cases.items():
    result = html_safe.safe_truncate_html(source, max_len=1500)
    check(f"{name}: разметка валидна", markup_problem(result) is None,
          f"got {markup_problem(result)}")
    check(f"{name}: длина в пределах", len(result) <= 1500, f"got {len(result)}")

print("\n[6] «...» не дописывается к тексту, который не обрезали")
short = "<b>Протокол ирригации</b> — коротко."
check("короткая выдержка осталась как есть",
      html_safe.safe_truncate_html(short, max_len=1500) == short,
      f"got {html_safe.safe_truncate_html(short, max_len=1500)!r}")

print("\n[7] html_safe — один общий модуль, копий больше нет")
summarizer_src = io.open("summarizer.py", encoding="utf-8").read()
check("summarizer использует html_safe", "import html_safe" in summarizer_src)
check("в summarizer нет своей реализации обрезки",
      "def _safe_truncate_html(" not in summarizer_src, "копия осталась")
check("в assistant нет своей реализации обрезки",
      "def _safe_truncate_html(" not in SOURCE and "def safe_truncate_html(" not in SOURCE)
check("assistant подключил html_safe", "import html_safe" in SOURCE)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
