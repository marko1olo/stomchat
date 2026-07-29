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
import re
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


CODE = "\n".join(ln for ln in io.open("assistant.py", encoding="utf-8").read().split("\n")
                 if not ln.lstrip().startswith("#"))

print("\n[1] Корень: SQLite не складывает регистр кириллицы")
# mode=ro: этот набор читает боевую вику (12 784 факта) на уровне модуля и делает
# только SELECT. Без ro импорт открывает ручку НА ЗАПИСЬ к боевым данным.
db = sqlite3.connect("file:stomat_wiki.db?mode=ro", uri=True)
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
        lines = [ln for ln in (wiki + "\n" + archive).split("\n") if ln.strip()]
        check(f"«{question[:34]}»: справка не пуста", len(lines) >= 5,
              f"строк {len(lines)} — модель ответит по памяти")
        check(f"«{term}» действительно встречается в справке",
              term in (wiki + archive).lower(),
              "справка есть, но не по теме вопроса")


asyncio.run(corpora())

print("\n[7] Цена правки измерена, а не предположена")
# Здесь сравнивались ДВА тайминга с порогом «вчетверо», и в полном прогоне свиты
# (рядом идут 76 других наборов) проверка упала на 17.7 против 77.8 мс — отношение
# 4.40 при пороге 4.0, — а в одиночных прогонах на тихой машине проходила. Замер
# 12 пар подряд на этой машине объясняет почему: минимум naive гуляет от 29.3 до
# 98.0 мс (3.3x), а ОТНОШЕНИЕ минимума к минимуму от 0.77 до 2.79 (3.6x) — ошибки
# числителя и знаменателя идут в разные стороны и умножаются, поэтому отношение
# дрожит СИЛЬНЕЕ каждого из замеров. Флакующая проверка приучает не верить
# красному набору, то есть не защищает врача ни от чего.
#
# Цена теперь считается шагами виртуальной машины SQLite (set_progress_handler) и
# от нагрузки на машину не зависит ВООБЩЕ: замер дал 0.0000% дрожания на пяти
# прогонах подряд и точную модель «шагов на строку = 1 + 3k» для k форм LIKE —
# 4.001 / 7.001 / 10.001 / 13.001 при k = 1/2/3/4. Порог 3.0 лежит между тремя
# формами (2.500) и четырьмя (3.250), поэтому проверка стала СТРОЖЕ прежней:
# лишнюю форму LIKE порог 4.0 пропускал, а этот ловит.
term = "внчс"
FACT_ROWS = db.execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0]


def _vdbe_steps(sql, args):
    """Шаги виртуальной машины SQLite: цена запроса, посчитанная без часов."""
    counted = [0]

    def _tick():
        counted[0] += 1
        return 0

    db.execute(sql, args).fetchall()  # прогрев: первый разбор схемы даёт лишние шаги
    db.set_progress_handler(_tick, 1)
    try:
        db.execute(sql, args).fetchall()
    finally:
        db.set_progress_handler(None, 0)
    return counted[0]


naive_sql = "SELECT COUNT(*) FROM distilled_facts WHERE content LIKE ?"
naive_steps = _vdbe_steps(naive_sql, (f"%{term}%",))
where, params = A.like_any_case("content", term)
fixed_sql = f"SELECT COUNT(*) FROM distilled_facts WHERE {where}"
fixed_steps = _vdbe_steps(fixed_sql, params)
print(f"      {FACT_ROWS} фактов: было {naive_steps} шагов SQLite "
      f"({naive_steps / FACT_ROWS:.3f} на строку), стало {fixed_steps} "
      f"({fixed_steps / FACT_ROWS:.3f} на строку), отношение "
      f"{fixed_steps / max(naive_steps, 1):.3f}")
# Порядок важен: сперва убеждаемся, что счётчик вообще работает. Если бы он молчал,
# оба числа были бы нулями и проверка отношения стала бы пустышкой, которая
# «проходит» при любой поломке продакшна.
check("счётчик цены детерминирован: два прогона дают одно число",
      _vdbe_steps(fixed_sql, params) == fixed_steps,
      "второй прогон дал другое число — проверка ниже опять начнёт флакать")
check("счётчик действительно считает, а не молчит", naive_steps >= FACT_ROWS,
      f"{naive_steps} шагов на {FACT_ROWS} строк — счётчик не сработал, "
      f"и отношение ниже ничего не проверяет")
check("три формы дороже одной не более чем втрое", fixed_steps <= naive_steps * 3,
      f"{naive_steps} -> {fixed_steps} шагов (x{fixed_steps / max(naive_steps, 1):.2f}) — "
      f"лишняя форма LIKE либо скан вместо обрыва: врач ждёт справку дольше")
# Счётчик шагов считает КОЛИЧЕСТВО инструкций, а не их цену, и на этом он слеп:
# замер показал, что вариант с питоновской функцией складывания регистра стоит
# 1.25 шага от одиночного LIKE, но 108 мс против 60 — то есть 1.8x реального
# времени при «подешевевшем» счётчике. Поэтому отдельно смотрим скомпилированную
# программу запроса: вызывать на каждую строку она должна только встроенный like,
# без обратного вызова в питон. Помощник и выбран ровно поэтому — три формы в
# одном запросе дешевле create_function.
_fns = sorted({row[5] for row in db.execute("EXPLAIN " + fixed_sql, params).fetchall()
               if row[1].startswith("Function")})
check("на каждую строку зовётся только встроенный like, без питона",
      _fns == ["like(2)"], f"в программе запроса функции {_fns}")
# Абсолютный потолок по часам оставлен: он ловит то, чего счётчик шагов не видит —
# если сама база станет медленной (диск, WAL, чужой писатель), шаги не изменятся, а
# врач будет ждать ответа секунды. Потолок абсолютный, не относительный, и замер
# 12 прогонов дал максимум 84 мс при пороге 500 — запас шестикратный.


def _best_ms(sql, args, runs=7):
    db.execute(sql, args).fetchone()  # прогрев: холодная страница исказила бы замер
    best = None
    for _ in range(runs):
        started = time.perf_counter()
        db.execute(sql, args).fetchone()
        spent = (time.perf_counter() - started) * 1000
        best = spent if best is None else min(best, spent)
    return best


fixed_ms = _best_ms(fixed_sql, params)
print(f"      он же по часам: {fixed_ms:.1f} мс (лучшее из 7)")
check("запрос по корпусу остаётся быстрее полусекунды", fixed_ms < 500,
      f"{fixed_ms:.1f} мс на один запрос — при 6 ключах это {fixed_ms * 6 / 1000:.1f} с")
check("вариантов LIKE ровно три, а не больше", where.count("LIKE") == 3
      and len(params) == 3, f"got {where.count('LIKE')} LIKE, {len(params)} параметров")

print("\n[7a] Каждый ключ врача доходит до базы, и выборка ограничена")
# Считаем не время, а СКОЛЬКО РАЗ бот сходил в базу за один поиск: от нагрузки на
# машину это не зависит вообще. Проверка ловит то, чего тайминг не видел ни в одной
# формулировке. Замер: если снять LIMIT у выборки, первый же ключ набирает
# _CORPUS_CANDIDATE_CAP=60 кандидатов, цикл по ключам обрывается через break, и
# запросов становится 2 вместо 6 — врач спросил про три вещи, а искали только
# первую, причём молча. Строк в справке при этом стало даже МЕНЬШЕ (16 против 20).
_real_connect = sqlite3.connect
_traced = []


def _tracing_connect(target, *a, **kw):
    # Продакшн открывает боевые базы НА ЗАПИСЬ (sqlite3.connect без mode=ro), а тут
    # идёт только чтение — на время замера уводим соединение в ro и вешаем трассу.
    if isinstance(target, str) and target.endswith(".db") and not kw.get("uri"):
        kw = dict(kw)
        kw["uri"] = True
        target = f"file:{target}?mode=ro"
    conn = _real_connect(target, *a, **kw)
    conn.set_trace_callback(_traced.append)
    return conn


async def traced_corpus(keys):
    del _traced[:]
    sqlite3.connect = _tracing_connect
    try:
        return await A.search_knowledge_corpus(keys)
    finally:
        sqlite3.connect = _real_connect


CORPUS_KEYS = ["внчс", "адгезив", "цирконий"]
traced_wiki, traced_archive = asyncio.run(traced_corpus(list(CORPUS_KEYS)))
selects = [" ".join(s.split()) for s in _traced if s.lstrip().upper().startswith("SELECT")]
limits = [int(m.group(1)) for m in (re.search(r"LIMIT\s+(\d+)", s, re.I) for s in selects) if m]
print(f"      поиск по {len(CORPUS_KEYS)} ключам: операторов {len(_traced)}, "
      f"из них SELECT {len(selects)}, LIMIT-ы {limits}")
check("справка по трём ключам не пуста", traced_wiki.strip() != "",
      "трасса сняла соединение, а не только посчитала его")
check("на каждый ключ по одной выборке в вике и в архиве",
      len(selects) == 2 * len(CORPUS_KEYS),
      f"SELECT-ов {len(selects)}, ожидалось {2 * len(CORPUS_KEYS)} — либо часть ключей "
      f"врача до базы не дошла, либо запрос размножился на каждую форму")
check("каждая выборка ограничена LIMIT", len(limits) == len(selects) and len(selects) > 0,
      f"без LIMIT {len(selects) - len(limits)} из {len(selects)} — лишние строки читаются "
      f"впустую и вытесняют другие ключи")
check(f"строк на ключ не больше запаса кандидатов ({A._CORPUS_CANDIDATE_CAP})",
      bool(limits) and max(limits) <= A._CORPUS_CANDIDATE_CAP,
      f"LIMIT {limits} против запаса {A._CORPUS_CANDIDATE_CAP}: один ключ съедает весь "
      f"запас и следующие ключи молча не ищутся")

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
