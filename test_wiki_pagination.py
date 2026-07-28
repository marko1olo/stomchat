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


print("\n[N] Карта кодов рубрик существует в одном экземпляре")
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

# Литеральных словарей с КОДАМИ рубрик быть не должно больше одного. Отличаем
# их от карты слов для запасного поиска (там у ortho_bopt значения "bopt",
# "уступ", "преп") по тому, что код начинается с цифры: "2.2.1".
_literal_maps = _re2.findall(r'\{[^{}]*"ortho_bopt"\s*:\s*\[\s*"\d[^{}]*\}', _CODE, _re2.S)
check("литеральная карта кодов ровно одна", len(_literal_maps) == 1,
      f"найдено {len(_literal_maps)} — копии разъедутся")
check("запасной путь использует общий словарь",
      "codes_map = WIKI_SUBTOPIC_CODES" in _CODE,
      "внутри query_wiki_subtopic снова своя карта")

# Подтемы не должны перекрываться: иначе одна статья живёт в двух кнопках.
_pairs = [(a, b) for a in assistant.WIKI_SUBTOPIC_CODES for b in assistant.WIKI_SUBTOPIC_CODES if a < b]
_overlap = [(a, b, sorted(set(assistant.WIKI_SUBTOPIC_CODES[a]) & set(assistant.WIKI_SUBTOPIC_CODES[b])))
            for a, b in _pairs
            if set(assistant.WIKI_SUBTOPIC_CODES[a]) & set(assistant.WIKI_SUBTOPIC_CODES[b])]
check("ни одна пара подтем не делит код", not _overlap, f"пересечения: {_overlap}")
check("подтем столько же, сколько кнопок в меню",
      len(assistant.WIKI_SUBTOPIC_CODES) == 14, f"got {len(assistant.WIKI_SUBTOPIC_CODES)}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
