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
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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


print("\n[8] Клиническая валидация эталонных EBM-протоколов сообщества")
all_seeded = protocol_extractor._DEFAULT_COMMUNITY_PROTOCOLS
titles = [p["title"] for p in all_seeded]

# 1. Протокол антибиотикопрофилактики эндокардита AHA 2021
check("протокол антибиотикопрофилактики AHA присутствует в базе", any("Антибиотикопрофилактика инфекционного эндокардита" in t for t in titles))
proph_p = next((p for p in all_seeded if "Антибиотикопрофилактика инфекционного эндокардита" in p["title"]), None)
if proph_p:
    p_text = json.dumps(proph_p, ensure_ascii=False)
    check("AHA: Амоксициллин 2.0 г за 30-60 минут", "2.0 г" in p_text and "30–60 минут" in p_text)
    check("AHA: Дети 50 мг/кг", "50 мг/кг" in p_text)
    check("AHA: Строгий запрет клиндамицина (C. difficile)", "КЛИНДАМИЦИН ПОЛНОСТЬЮ ИСКЛЮЧЕН" in p_text and "Clostridioides difficile" in p_text)

# 2. Протокол дезинфекции каналов и нейтрализации ирригантов ESE/AAE
check("протокол дезинфекции каналов ESE/AAE присутствует в базе", any("Хемомеханическая дезинфекция каналов" in t for t in titles))
irr_p = next((p for p in all_seeded if "Хемомеханическая дезинфекция каналов" in p["title"]), None)
if irr_p:
    i_text = json.dumps(irr_p, ensure_ascii=False)
    check("дезинфекция каналов: NaOCl 3-5.25% + PUI", "NaOCl" in i_text and "PUI" in i_text)
    check("дезинфекция каналов: 17% ЭДТА", "17% ЭДТА" in i_text)
    check("дезинфекция каналов: обязательный промежуточный смыв физраствором", "0.9% физраствора" in i_text and "ПРОМЕЖУТОЧНЫЙ СМЫВ" in i_text)
    check("дезинфекция каналов: запрет парахлоранилина PCA", "парахлоранилина (PCA)" in i_text and "СТРОЖАЙШИЙ ЗАПРЕТ" in i_text)

print("\n[9] Защита от ReDoS и устойчивость к большим клиническим выпискам")
import time
large_clinical_text = ("Клинический случай эндодонтического лечения зуба 46. Этап 1: изоляция коффердам. " * 300) + "\n"
t0 = time.perf_counter()
cand_res = protocol_extractor.is_protocol_candidate(large_clinical_text)
t_candidate = time.perf_counter() - t0
check("is_protocol_candidate быстро обрабатывает большие тексты (< 50 мс)", t_candidate < 0.05, f"{t_candidate*1000:.2f} ms")
check("is_protocol_candidate детектирует наличие протокола", cand_res is True)

# Тест безопасного экранирования и парсинга markdown
safe_md = protocol_extractor._safe_html_text("Пациент принимал ***Амоксициллин*** и **Ибупрофен** 2 * 2 мл.")
check("_safe_html_text корректно форматирует теги без ReDoS", "<b><i>Амоксициллин</i></b>" in safe_md and "<b>Ибупрофен</b>" in safe_md)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
