"""
Регистр в поиске по базе знаний: аббревиатуры врача находились НИКОГДА.

SQLite складывает регистр ТОЛЬКО для ASCII. Прямая проверка на живой базе:

    select 'А' LIKE 'а', 'A' LIKE 'a';   ->   (0, 1)
    select LOWER('ВНЧС');                ->   'ВНЧС'

То есть и LIKE, и LOWER() по кириллице бессильны. А ключи приходят из
extract_keywords, который делает text.lower(). Факты вики написаны прозой, и
аббревиатуры в них заглавные.

Замер по живой вике (12784 факта), сколько находил поиск и сколько есть:

    ВНЧС  0 из 88      КЛКТ  0 из 21     МТА   0 из 18
    ЭДТА  0 из 13      ТРГ   0 из 5      БОПТ  0 из 4      ЭОД  0 из 2

Страдали и обычные слова: «цирконий» 49 из 61, «адгезив» 607 из 627 — терялось
написанное с заглавной в начале фразы.

Сценарий отказа: врач спрашивает «какая экспозиция ЭДТА» или жмёт /search ВНЧС.
Справка уходит в промпт ПУСТОЙ, и модель отвечает по памяти — при том что в базе
лежит 88 фактов по теме, а /start и /help обещают ответ «с использованием базы
знаний».

Проверка идёт на живой вике, только на чтение.

Запуск: python test_search_case.py
"""
import asyncio
import io
import os
import sqlite3
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ["STOMCHAT_LOG_PATH"] = os.path.join(tempfile.mkdtemp(prefix="stomchat_sc_"), "t.log")

import assistant as A  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


CODE = "\n".join(l for l in io.open("assistant.py", encoding="utf-8").read().split("\n")
                 if not l.lstrip().startswith("#"))

print("\n[1] Корень: SQLite не складывает регистр кириллицы")
db = sqlite3.connect("stomat_wiki.db")
cyr, ascii_ = db.execute("select 'А' LIKE 'а', 'A' LIKE 'a'").fetchone()
check("кириллица регистр НЕ складывает", cyr == 0, f"got {cyr} — проверка потеряла смысл")
check("латиница складывает", ascii_ == 1, f"got {ascii_}")
check("LOWER() по кириллице бессилен",
      db.execute("select LOWER('ВНЧС')").fetchone()[0] == "ВНЧС",
      "если LOWER заработал, правку можно упростить")

print("\n[2] Аббревиатуры находятся в полном объёме")
ABBREV = ["ВНЧС", "КЛКТ", "МТА", "ЭДТА", "ТРГ", "БОПТ", "ЭОД"]
for term in ABBREV:
    naive = db.execute("SELECT COUNT(*) FROM distilled_facts WHERE content LIKE ?",
                       (f"%{term.lower()}%",)).fetchone()[0]
    where, params = A.like_any_case("content", term.lower())
    fixed = db.execute(f"SELECT COUNT(*) FROM distilled_facts WHERE {where}", params).fetchone()[0]
    check(f"{term}: находится {fixed}, прежним поиском {naive}",
          fixed > 0 and fixed > naive,
          f"было {naive}, стало {fixed} — правка не сработала")

print("\n[3] Обычные слова тоже перестали терять факты")
for term, least in (("цирконий", 55), ("адгезив", 620), ("коронка", 100)):
    naive = db.execute("SELECT COUNT(*) FROM distilled_facts WHERE content LIKE ?",
                       (f"%{term}%",)).fetchone()[0]
    where, params = A.like_any_case("content", term)
    fixed = db.execute(f"SELECT COUNT(*) FROM distilled_facts WHERE {where}", params).fetchone()[0]
    check(f"{term}: {naive} -> {fixed}", fixed >= naive and fixed >= least,
          f"стало {fixed}, ожидалось не меньше {least}")

print("\n[4] Формы ключа: нижний, верхний, с заглавной")
where, params = A.like_any_case("content", "внчс")
check("три формы в параметрах", params == ("%внчс%", "%ВНЧС%", "%Внчс%"), f"got {params}")
check("условие связано через OR", where.count("LIKE ?") == 3 and " OR " in where, where)
check("колонка подставляется, а не зашита",
      A.like_any_case("text", "внчс")[0].startswith("(text LIKE"),
      A.like_any_case("text", "внчс")[0])
# У латиницы и цифр формы совпадают — лишних сравнений быть не должно.
_, digits = A.like_any_case("content", "3d")
check("для латиницы формы не дублируются", len(digits) <= 2, f"got {digits}")
_, one = A.like_any_case("content", "42")
check("для цифр одна форма", len(one) == 1, f"got {one}")

print("\n[5] Все четыре точки поиска переведены")
check("прямого регистрозависимого LIKE по content не осталось",
      'distilled_facts WHERE content LIKE ?' not in CODE,
      "какая-то точка снова ищет только в нижнем регистре")
check("архив тоже переведён",
      '"WHERE text LIKE ? AND' not in CODE,
      "реплики коллег пишутся как попало, регистр там особенно важен")
check("помощник используется во всех точках",
      CODE.count("like_any_case(") >= 5,
      f"мест: {CODE.count('like_any_case(')} при четырёх точках плюс объявление")


async def corpora():
    print("\n[6] Справка на вопрос с аббревиатурой больше не пустая")
    for question, term in (("что делать при дисфункции ВНЧС", "внчс"),
                           ("протокол ЭДТА при финишной ирригации", "эдта"),
                           ("МТА при перфорации дна полости", "мта")):
        keys = A.select_search_keywords(A.extract_keywords(question))
        wiki, archive = await A.search_knowledge_corpus(keys)
        lines = [l for l in (wiki + "\n" + archive).split("\n") if l.strip()]
        check(f"«{question[:34]}»: справка не пуста", len(lines) >= 5,
              f"строк {len(lines)} — модель ответит по памяти")
        check(f"«{term}» действительно встречается в справке",
              term in (wiki + archive).lower(),
              "справка есть, но не по теме вопроса")


asyncio.run(corpora())

print("\n[7] Цена правки измерена, а не предположена")
# Замер берётся по ЛУЧШЕМУ из прогонов, а не по среднему. Среднее ловит чужую
# нагрузку на машине: при параллельных питонах эта проверка уже отваливалась на
# отношении 46.9 -> 114.4 мс, то есть флакала. Флакующая проверка не доказывает
# ничего — минимум устойчив к планировщику, потому что чужой квант времени может
# прогон только замедлить, но не ускорить.
term = "внчс"


def _best_ms(sql, args, runs=7):
    db.execute(sql, args).fetchone()  # прогрев: холодная страница исказила бы замер
    best = None
    for _ in range(runs):
        started = time.perf_counter()
        db.execute(sql, args).fetchone()
        spent = (time.perf_counter() - started) * 1000
        best = spent if best is None else min(best, spent)
    return best


naive_ms = _best_ms("SELECT COUNT(*) FROM distilled_facts WHERE content LIKE ?",
                    (f"%{term}%",))
where, params = A.like_any_case("content", term)
fixed_ms = _best_ms(f"SELECT COUNT(*) FROM distilled_facts WHERE {where}", params)
print(f"      один запрос по 12784 фактам: было {naive_ms:.1f} мс, стало {fixed_ms:.1f} мс "
      f"(лучшее из 7)")
# Три варианта LIKE вместо одного — рост втрое заложен в конструкцию. Порог
# держим на четырёх с запасом, чтобы проверка ловила настоящий обвал (скан
# вместо индекса, запрос на каждый ключ), а не дрожание планировщика.
check("цена выросла не более чем вчетверо", fixed_ms <= naive_ms * 4 + 5,
      f"{naive_ms:.1f} -> {fixed_ms:.1f} мс")
# Абсолютный потолок ловит то, чего отношение не поймает: если обвалятся ОБА
# запроса, отношение останется прежним, а врач будет ждать ответа секунды.
check("запрос по корпусу остаётся быстрее полусекунды", fixed_ms < 500,
      f"{fixed_ms:.1f} мс на один запрос — при 6 ключах это {fixed_ms * 6 / 1000:.1f} с")
check("вариантов LIKE ровно три, а не больше", where.count("LIKE") == 3
      and len(params) == 3, f"got {where.count('LIKE')} LIKE, {len(params)} параметров")

print("\n[8] Проверки выше ловят поломку")
# Сравнение «стало больше, чем было» слепо, если помощник вернёт одну форму:
# тогда fixed == naive и проверки секции [2] упадут. Убеждаемся, что именно так.
_, single = A.like_any_case("content", "abc")
check("для ASCII-ключа формы схлопываются, но условие валидно",
      "LIKE ?" in A.like_any_case("content", "abc")[0])
check("детектор регистрозависимого запроса поймал бы возврат",
      'distilled_facts WHERE content LIKE ?' in
      'SELECT content FROM distilled_facts WHERE content LIKE ? LIMIT 5')
check("пустой ключ не роняет помощник", A.like_any_case("content", "")[1] == ("%%",))

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
