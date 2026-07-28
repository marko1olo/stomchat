"""
Энциклопедия /wiki: доступность базы знаний и листание статей.

Подтема грузилась целиком запросом с LIMIT 15 на код категории. При 12 784
фактах в базе энциклопедия показывала 284 статьи — 2.2%. В разделе «Коронки и
мосты» доступно 4149 статей, врач видел 29. Остальные 97.8% базы, ради которой
и работал весь пайплайн дистилляции, через интерфейс не открывались никак.

Плюс «Сплинты и шины» были подмножеством «Окклюзии и сустава»: коды
["2.3.1","2.3.2"] против ["2.3.2"], из 505 статей по сплинтам 461 показывалась
и в соседней кнопке.

Проверка идёт на СИНТЕТИЧЕСКОЙ базе знаний, боевая не открывается.

Запуск: python test_wiki_pagination.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_wiki_")
_ORIGINAL_CWD = os.getcwd()

import assistant

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def build_wiki(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE distilled_facts (id INTEGER PRIMARY KEY, category_code TEXT, content TEXT)")
    rows = []
    # 40 статей по коронкам — заведомо больше прежнего предела в 15.
    for i in range(40):
        rows.append(("2.1.2, 2.2.1", f"Статья о коронках номер {i}"))
    for i in range(5):
        rows.append(("1.1.3", f"Ирригация: протокол {i}"))
    # Дубли по содержанию не должны раздувать счётчик.
    rows.append(("1.1.3", "Ирригация: протокол 0"))
    # Пустые строки не должны попадать в выдачу.
    rows.append(("1.1.3", "   "))
    rows.append(("1.1.3", None))
    # Отдельные коды окклюзии и сплинтов.
    for i in range(7):
        rows.append(("2.3.1", f"Окклюзия: факт {i}"))
    for i in range(4):
        rows.append(("2.3.2", f"Сплинт: факт {i}"))
    db.executemany("INSERT INTO distilled_facts (category_code, content) VALUES (?, ?)", rows)
    db.commit()
    db.close()


async def run():
    print("\n[1] Раздел отдаёт ВСЕ статьи, а не первые пятнадцать")
    fact, total = await assistant.query_wiki_fact_page("ortho_crown", 0)
    check("видно все 40 статей по коронкам", total == 40, f"got {total}")
    check("первая статья получена", fact is not None and "коронк" in fact.lower(), f"got {fact!r}")

    print("\n[2] Каждая страница отдаёт свою статью")
    seen = set()
    for page in range(total):
        text, _ = await assistant.query_wiki_fact_page("ortho_crown", page)
        seen.add(text)
    check("все страницы различны", len(seen) == 40, f"уникальных {len(seen)}")

    print("\n[3] Порядок статей устойчив между запросами")
    first_pass = [(await assistant.query_wiki_fact_page("ortho_crown", p))[0] for p in range(5)]
    second_pass = [(await assistant.query_wiki_fact_page("ortho_crown", p))[0] for p in range(5)]
    check("одна и та же страница даёт ту же статью", first_pass == second_pass,
          "порядок плавает — закладка сохранит не то, что на экране")

    print("\n[4] Листание закольцовано в обе стороны")
    first, count = await assistant.query_wiki_fact_page("endo_irr", 0)
    last, _ = await assistant.query_wiki_fact_page("endo_irr", count - 1)
    before_first, _ = await assistant.query_wiki_fact_page("endo_irr", -1)
    after_last, _ = await assistant.query_wiki_fact_page("endo_irr", count)
    check("«Пред» с первой ведёт на последнюю", before_first == last)
    check("«След» с последней ведёт на первую", after_last == first)
    check("далёкий индекс не выходит за границы",
          (await assistant.query_wiki_fact_page("endo_irr", 10 ** 6))[0] is not None)

    print("\n[5] Мусор в базе не попадает в выдачу")
    check("дубли по содержанию схлопнуты, пустые отброшены", count == 5, f"got {count}")
    texts = {(await assistant.query_wiki_fact_page("endo_irr", p))[0] for p in range(count)}
    check("пустых статей нет", all(t and t.strip() for t in texts), f"got {texts}")

    print("\n[6] Сплинты и окклюзия больше не дублируют друг друга")
    check("окклюзия закреплена за своим кодом",
          assistant.WIKI_SUBTOPIC_CODES["gnat_joint"] == ["2.3.1"],
          f"got {assistant.WIKI_SUBTOPIC_CODES['gnat_joint']}")
    check("сплинты закреплены за своим кодом",
          assistant.WIKI_SUBTOPIC_CODES["gnat_splint"] == ["2.3.2"])
    _, joint_total = await assistant.query_wiki_fact_page("gnat_joint", 0)
    _, splint_total = await assistant.query_wiki_fact_page("gnat_splint", 0)
    check("разделы независимы", (joint_total, splint_total) == (7, 4),
          f"got {joint_total} и {splint_total}")
    joint = {(await assistant.query_wiki_fact_page("gnat_joint", p))[0] for p in range(joint_total)}
    splint = {(await assistant.query_wiki_fact_page("gnat_splint", p))[0] for p in range(splint_total)}
    check("содержание не пересекается", not (joint & splint), f"пересечение {joint & splint}")

    print("\n[7] Неизвестная подтема и отсутствующая база не роняют обработчик")
    check("неизвестный id -> пусто", await assistant.query_wiki_fact_page("нет_такой", 0) == (None, 0))
    os.rename("stomat_wiki.db", "hidden.db")
    try:
        check("без файла базы -> пусто",
              await assistant.query_wiki_fact_page("ortho_crown", 0) == (None, 0))
    finally:
        os.rename("hidden.db", "stomat_wiki.db")

    print("\n[8] Закладка сохраняет ровно ту статью, что на экране")
    # Обе ветки обязаны использовать один и тот же запрос: иначе номер
    # страницы означает у них разные статьи.
    source = open(os.path.join(_ORIGINAL_CWD, "assistant.py"), encoding="utf-8").read()
    save_block = source.split('if data_str.startswith("wiki_save:")', 1)[1].split("return", 1)[0]
    check("обработчик закладки ходит тем же запросом",
          "query_wiki_fact_page" in save_block, "используется другая выборка")
    check("старая загрузка всего раздела в закладке не осталась",
          "query_wiki_subtopic(" not in save_block)

    page_block = source.split('if data_str.startswith("wiki_page:")', 1)[1].split("return", 1)[0]
    check("страница статьи тоже ходит постранично", "query_wiki_fact_page" in page_block)


os.chdir(_TMPDIR)
build_wiki("stomat_wiki.db")
try:
    asyncio.run(run())
finally:
    os.chdir(_ORIGINAL_CWD)
    shutil.rmtree(_TMPDIR, ignore_errors=True)


print("\n[9] Дерево рубрик — единственный источник истины")
# Копий было ДВЕ: модульный WIKI_SUBTOPIC_CODES и локальный codes_map внутри
# query_wiki_subtopic. Они уже разъехались — в локальной осталось
# "gnat_joint": ["2.3.1", "2.3.2"], то есть значение, которое модульный словарь
# описывает как исправленный дефект: «Окклюзия» была надмножеством «Сплинтов»,
# из 505 статей по сплинтам 461 показывалась в соседней кнопке. Правку внесли в
# одну копию. Проявиться не успело только потому, что запасной путь достижим
# лишь при пустой подтеме.
import io as _io2
import re as _re2

_SRC = _io2.open(os.path.join(_ORIGINAL_CWD, "assistant.py"), encoding="utf-8").read()
_CODE = "\n".join(l for l in _SRC.split("\n") if not l.lstrip().startswith("#"))

# Коды, названия подтем и кнопки — всё выводится из WIKI_TREE. Литеральных
# карт кодов в файле быть не должно вообще: их было четыре штуки (две карты
# кодов, две карты названий) плюс цепочка elif со списками кнопок.
_literal_maps = _re2.findall(r'\{[^{}]*"ortho_bopt"\s*:\s*\[\s*"\d[^{}]*\}', _CODE, _re2.S)
check("литеральных карт кодов не осталось", len(_literal_maps) == 0,
      f"найдено {len(_literal_maps)} — копии разъедутся")
check("запасной путь использует общий словарь",
      "codes_map = WIKI_SUBTOPIC_CODES" in _CODE,
      "внутри query_wiki_subtopic снова своя карта")
check("коды выводятся из дерева", "for sub_id, _sub_title, codes in subs" in _CODE,
      "WIKI_SUBTOPIC_CODES снова заполняется руками")
check("названия подтем выводятся из дерева",
      _CODE.count("subtopic_names = WIKI_SUBTOPIC_NAMES") == 2,
      "где-то осталась своя карта названий")

# Подтемы не должны перекрываться: иначе одна статья живёт в двух кнопках.
_codes = assistant.WIKI_SUBTOPIC_CODES
_pairs = [(a, b) for a in _codes for b in _codes if a < b]
_overlap = [(a, b, sorted(set(_codes[a]) & set(_codes[b]))) for a, b in _pairs
            if set(_codes[a]) & set(_codes[b])]
check("ни одна пара подтем не делит код", not _overlap, f"пересечения: {_overlap}")

# Каждая подтема обязана быть достижима: имя, раздел и кнопка.
check("у каждой подтемы есть название",
      all(s in assistant.WIKI_SUBTOPIC_NAMES for s in _codes),
      f"без названия: {[s for s in _codes if s not in assistant.WIKI_SUBTOPIC_NAMES]}")
check("префикс подтемы совпадает с разделом — иначе «Назад» ведёт в пустоту",
      all(s.split("_")[0] in assistant.WIKI_TREE for s in _codes),
      f"осиротевшие: {[s for s in _codes if s.split('_')[0] not in assistant.WIKI_TREE]}")
_menu_ids = {b.data.decode().split(":")[1]
             for cid in assistant.WIKI_TREE
             for row in assistant.wiki_category_buttons(cid) for b in row
             if b.data and b.data.decode().startswith("wiki_page:")}
check("каждая подтема имеет кнопку в своём разделе", _menu_ids == set(_codes),
      f"без кнопки: {sorted(set(_codes) - _menu_ids)}")
_topic_ids = {b.data.decode().split(":")[1]
              for row in assistant.wiki_topic_buttons() for b in row
              if b.data and b.data.decode().startswith("wiki_cat:")}
check("каждый раздел есть в рубрикаторе",
      set(assistant.WIKI_TREE) <= _topic_ids,
      f"нет кнопки у разделов: {sorted(set(assistant.WIKI_TREE) - _topic_ids)}")
check("неизвестный раздел не роняет сборку кнопок",
      len(assistant.wiki_category_buttons("нет_такого")) == 1)

print("\n[10] Что видно на кнопке и что попадает в длинную статью")
# Разброс по объёму огромный: «Отбеливание» 18 статей, «Коронки» 3734. Без числа
# на кнопке врач не понимает, куда попадает — в подборку из двух десятков
# заметок или в раздел, который за вечер не пролистать.
_counts = {"ortho_vin": 1426, "ortho_crown": 3734}
_labels = [b.text for row in assistant.wiki_category_buttons("ortho", _counts) for b in row]
check("число статей вынесено на кнопку",
      any("· 3734" in l for l in _labels), f"got {_labels[:3]}")
check("подтема без счётчика остаётся без числа, а не с нулём",
      not any("· 0" in l or l.endswith("· ") for l in _labels), f"got {_labels}")
_plain = [b.text for row in assistant.wiki_category_buttons("ortho") for b in row]
check("без счётчиков кнопки собираются как раньше",
      all("·" not in l for l in _plain), f"got {_plain[:3]}")

# Обрезка длинной статьи: рвала ровно на 3900-м символе, посреди слова. На живой
# вике это 30 статей из 12 784, и врач читал «...переход от корня к кон».
_long = ("Гипохлорит натрия применяют в концентрации от 3 до 5 процентов. "
         "Экспозиция составляет не менее тридцати минут на канал. ") * 60
_out = assistant.clean_html_formatting(_long)
_body = _out.split("\n\n[Показано")[0]
check("длинная статья обрезана", len(_out) < len(_long))
check("влезает в лимит Telegram", len(_out) <= 4096, f"got {len(_out)}")
check("обрыв на границе предложения, а не посреди слова",
      _body.rstrip().endswith((".", "!", "?", ";", "…")), repr(_body[-40:]))
check("сказано сколько показано и сколько осталось",
      "Показано" in _out and "не поместились" in _out, _out[-90:])
check("число потерянного посчитано верно",
      f"из {len(_long)}" in _out, _out[-90:])
_short = "Короткая статья про ирригацию канала."
check("короткая статья не трогается",
      "Показано" not in assistant.clean_html_formatting(_short))

# Висящий номер следующего пункта списка не должен оставаться на конце.
_numbered = ("Первый абзац с длинным текстом про препарирование зуба. " * 70) + "\n\n5. Следующий пункт"
_body2 = assistant.clean_html_formatting(_numbered).split("\n\n[Показано")[0]
check("номер следующего пункта не остаётся сиротой",
      not _re2.search(r"\d+\.\s*$", _body2), repr(_body2[-30:]))

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
