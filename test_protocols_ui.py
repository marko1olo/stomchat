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
import json

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
PROTO_HANDLER = SOURCE.split('if data_str.startswith("proto:")', 1)[-1].split("# WIKI MAIN MENU BACK", 1)[0]
PROTO_CODE = "\n".join(
    line for line in PROTO_HANDLER.split("\n") if not line.lstrip().startswith("#")
)

keyword_ids = set(re.findall(r'"(\w+)": \[', PROTO_HANDLER.split("keywords_map = {", 1)[1].split("}", 1)[0]))
title_ids = set(re.findall(r'"(\w+)": "', PROTO_HANDLER.split("proto_names = {", 1)[1].split("}", 1)[0]))
fallback_protocols = {"irrigation", "bopt", "etching", "obturation", "vertical"}

print("\n[1] Ключевые слова и заголовок есть у каждого резервного протокола")
check("резервных протоколов пять", len(fallback_protocols) == 5, f"got {sorted(fallback_protocols)}")
for proto in sorted(fallback_protocols):
    check(f"{proto}: есть ключевые слова", proto in keyword_ids, f"есть только {sorted(keyword_ids)}")
    check(f"{proto}: есть заголовок", proto in title_ids, f"есть только {sorted(title_ids)}")

print("\n[2] Динамический каталог protocol_extractor форматирует разметку и кнопки")
import protocol_extractor
dummy_protos = [
    {
        "id": 101,
        "title": "Протокол ирригации корневых каналов",
        "category": "эндодонтия",
        "indication": "Пульпит и периодонтит",
        "contraindications": "Перфорации корня",
        "steps_json": json.dumps(["NaOCl 3% 20 мин", "EDTA 17% 1 мин", "Физраствор"]),
        "materials": "NaOCl, EDTA",
        "nuances": "Активация ультразвуком",
        "author_doctor": "Dr. Endo"
    }
]
cat_text, cat_btns = protocol_extractor.format_protocol_catalog(dummy_protos)
check("каталог содержит заголовок", "Клинические протоколы" in cat_text)
check("каталог генерирует кнопку просмотра", any(any("proto:view:101" in getattr(b, "data", b"").decode("utf-8", "ignore") for b in row) for row in cat_btns))
check("каталог генерирует кнопку возврата в главное меню", any(any("nav:main" in getattr(b, "data", b"").decode("utf-8", "ignore") for b in row) for row in cat_btns))

view_text, view_btns = protocol_extractor.format_protocol_view(dummy_protos[0])
check("карточка протокола содержит этапы", "NaOCl 3%" in view_text)
check("карточка протокола генерирует кнопку возврата к списку", any(any(b_data in getattr(b, "data", b"").decode("utf-8", "ignore") for b in row for b_data in ("proto:list", "proto:back")) for row in view_btns))

print("\n[3] Диспетчер assistant.py поддерживает все маршруты протоколов")
check("обработчик списка/возврата proto:list и proto:back присутствует", 'if data_str in ("proto:back", "proto:list"):' in SOURCE)
check("обработчик фильтрации по категориям proto:cat: присутствует", 'if data_str.startswith("proto:cat:"):' in SOURCE)
check("обработчик просмотра proto:view: присутствует", 'if data_str.startswith("proto:view:"):' in SOURCE)
check("обработчик поискового/ключевого вызова proto: присутствует", 'if data_str.startswith("proto:"):' in SOURCE)

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
