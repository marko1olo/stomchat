# -*- coding: utf-8 -*-
"""Таксономия рубрик вики: один источник, а не пять расходящихся копий.

ЗАМЕРЕНО НА БОЕВОЙ ВИКЕ 2026-07-29 (только mode=ro): 12 784 факта, 110 разных
кодов-ТОКЕНОВ при 6 711 разных ЗНАЧЕНИЯХ category_code (99.1 % записей хранят
СПИСОК через запятую), 42 317 пометок. Копий таксономии было ПЯТЬ:
savdel.CAT_MAP (55 кодов), checker.CAT_NAMES (53), дерево distiller.py (65 кодов),
assistant.WIKI_TREE (53 кода кнопок) и дерево reclass.py (62 кода, разделов 8-10
нет вовсе).

Что каждым дефектом платит врач:

Д1. Код есть в базе, но ни одна карта его не знает. Замер: 58 живых кодов вне
    CAT_MAP, 393 пометки, 51 факт не попадает НИ В ОДИН файл ревью и не
    открывается НИ ОДНОЙ кнопкой бота. Врач такой факт не найдёт никогда, а
    раздел в интерфейсе выглядит пустым, хотя факты в базе есть.

Д2. Карты расходятся между собой. Замер: `6.1.2` (82 факта) есть в кнопках бота,
    но нет в выгрузке; `8.1.1/9.1.1/10.1.1` есть в выгрузке, но кнопки нет;
    дерево reclass.py не знало разделов 8-10 и переклеило 80 фактов детской
    стоматологии и 35 по материаловедению в разделы 1-7 — как классы они исчезли.
    Врач видит в боте один набор статей, а в методичке по тому же разделу другой.

Д3. Отбор подстрокой вместо границы токена. В живой вике 137 ПАР кодов, где один
    код — подстрока другого (`1.1` лежит и внутри `1.1.4`, и внутри `2.1.1`, и
    внутри `6.1.1`). По снимку до реклассификации подстрочный отбор разложил
    51 факт по 14 ЧУЖИМ файлам. Врач читает чужой раздел как свой — это хуже
    потери: пропажу хотя бы видно.

Д4. Выдуманное имя рубрики. Имена 49 живых кодов из 110 достоверно неизвестны
    (302 пометки). Придуманное клиническое название хуже честного «БЕЗ ИМЕНИ»:
    по придуманному врач сделает вывод о лечении.

Запуск: python test_taxonomy_single.py    (offline, боевые базы только на чтение)
"""
import ast
import asyncio
import contextlib
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
# Журнал сита — в temp: боевой distiller.log трогать нельзя.
os.environ.setdefault("STOMCHAT_DISTILLER_LOG",
                      os.path.join(tempfile.gettempdir(), "taxonomy_test_distiller.log"))

import taxonomy as T   # noqa: E402
import savdel          # noqa: E402
import checker         # noqa: E402
import distiller       # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[OK  ] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


LIVE_WIKI = REPO / "stomat_wiki.db"


def live_tokens():
    """Коды-токены боевой вики с числом пометок. Строго на чтение."""
    if not LIVE_WIKI.exists():
        return None, None
    con = sqlite3.connect(LIVE_WIKI.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = [r[0] for r in con.execute("SELECT category_code FROM distilled_facts")]
    finally:
        con.close()
    counts = {}
    for value in rows:
        for token in T.parse_codes(value):
            counts[token] = counts.get(token, 0) + 1
    return rows, counts


ROWS, TOKENS = live_tokens()

# ==============================================================================
# [1] Источник ОДИН: не «одинаковые копии», а один объект (Д2)
# ==============================================================================
print("\n[1] У сита, выгрузки и проверки классификации один источник")
check("savdel.CAT_MAP — тот же объект, что taxonomy.EXPORT_SLUGS",
      savdel.CAT_MAP is T.EXPORT_SLUGS, "снова копия: разойдётся, как разошлись пять прежних")
check("checker.CAT_NAMES — тот же объект, что taxonomy.DISPLAY_NAMES",
      checker.CAT_NAMES is T.DISPLAY_NAMES)
check("distiller.KNOWLEDGE_TREE — тот же объект, что taxonomy.KNOWLEDGE_TREE",
      distiller.KNOWLEDGE_TREE is T.KNOWLEDGE_TREE,
      "дерево промпта снова отдельное — модель разметит базу кодами, которых сито не знает")
for _name in ("LEGAL_CODES", "LEGAL_SECTIONS", "LEAF_CODES", "LEAF_CHILDREN",
              "NON_EXPORTABLE_LEAVES"):
    check(f"distiller.{_name} — тот же объект, что в taxonomy",
          getattr(distiller, _name) is getattr(T, _name))
check("distiller.FALLBACK_CODE совпадает с taxonomy.FALLBACK_CODE",
      distiller.FALLBACK_CODE == T.FALLBACK_CODE == "10.1.1")

# Поведенческое доказательство единственности: правка в источнике ВИДНА во всех
# трёх потребителях сразу. Если кто-то вернёт копию, правка до него не дойдёт —
# ровно так и появлялись рубрики, которые выгрузка знает, а навигация нет.
_slug_before = T.EXPORT_SLUGS["2.1.1"]
_name_before = T.DISPLAY_NAMES["2.1.1"]
try:
    T.EXPORT_SLUGS["2.1.1"] = "ПРОБА_ЕДИНСТВЕННОГО_ИСТОЧНИКА"
    T.DISPLAY_NAMES["2.1.1"] = "ПРОБА_ИМЕНИ"
    check("правка рубрики в источнике сразу видна выгрузке",
          savdel.CAT_MAP["2.1.1"] == "ПРОБА_ЕДИНСТВЕННОГО_ИСТОЧНИКА")
    check("правка имени в источнике сразу видна проверке классификации",
          checker.CAT_NAMES["2.1.1"] == "ПРОБА_ИМЕНИ"
          and T.display_name("2.1.1") == "ПРОБА_ИМЕНИ")
finally:
    T.EXPORT_SLUGS["2.1.1"] = _slug_before
    T.DISPLAY_NAMES["2.1.1"] = _name_before
check("проба откачена: имя рубрики 2.1.1 восстановлено",
      savdel.CAT_MAP["2.1.1"] == "Орто_Виниры" and T.display_name("2.1.1").endswith("Виниры (Керамика/Композит)"),
      f"осталось {savdel.CAT_MAP['2.1.1']!r} / {T.display_name('2.1.1')!r}")

# Страховка от возврата шестой копии: ни в одном потребителе нет своего словаря
# «код -> имя». Проверяется по ast, а не по тексту: комментарий с кодами не должен
# считаться нарушением.
for _module in ("savdel.py", "checker.py", "distiller.py"):
    _tree = ast.parse(io.open(REPO / _module, encoding="utf-8-sig").read())
    _own = []
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.Dict):
            _keys = [k.value for k in _node.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if sum(1 for k in _keys if re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){1,3}", k)) >= 5:
                _own.append(_node.lineno)
    check(f"{_module} не держит своей карты кодов",
          not _own, f"словарь кодов на строках {_own} — это шестая копия")

# ==============================================================================
# [2] Каждый живой код базы либо назван, либо честно помечен безымянным (Д1, Д4)
# ==============================================================================
print("\n[2] Каждый живой код назван или честно помечен как безымянный")
if TOKENS is None:
    check("боевая вика доступна для замера", False, "stomat_wiki.db не найден — замер невозможен")
else:
    named = sorted(t for t in TOKENS if T.has_name(t))
    unnamed = sorted(t for t in TOKENS if not T.has_name(t))
    marks_named = sum(TOKENS[t] for t in named)
    marks_unnamed = sum(TOKENS[t] for t in unnamed)
    print(f"       ЗАМЕР: живых токенов {len(TOKENS)} — с именем {len(named)} "
          f"({marks_named} пометок), без имени {len(unnamed)} ({marks_unnamed} пометок, "
          f"{100.0 * marks_unnamed / (marks_named + marks_unnamed):.2f} %)")
    print(f"       БЕЗ ИМЕНИ: {', '.join(f'{t}({TOKENS[t]})' for t in sorted(unnamed, key=lambda t: -TOKENS[t]))}")
    check("живых токенов ровно 110, как замерено", len(TOKENS) == 110, f"стало {len(TOKENS)}")
    check("названы и помечены вместе покрывают ВСЕ живые коды",
          len(named) + len(unnamed) == len(TOKENS))
    check("у каждого живого кода непустое имя или пометка",
          all(T.display_name(t).strip() for t in TOKENS),
          "пустое имя читается как «раздел без названия», а не как «имени нет»")
    check("безымянный код помечен ЯВНО и код виден в пометке",
          all(T.display_name(t).startswith(T.UNNAMED_PREFIX) and t in T.display_name(t)
              for t in unnamed),
          "врач не поймёт, какой именно раздел потерян")
    check("названный код НЕ помечен как безымянный",
          not any(T.display_name(t).startswith(T.UNNAMED_PREFIX) for t in named))
    # Прямо против выдумывания: безымянному коду нельзя подсунуть НИ ОДНО настоящее
    # клиническое имя — ни раздела, ни группы, ни соседнего листа. Иначе врач
    # прочитает раздел с чужим названием и сделает по нему вывод о лечении, не имея
    # ни одного признака, что название приписано.
    _real = set(T.DISPLAY_NAMES.values()) | set(T.SECTION_NAMES.values()) \
        | set(T.GROUP_NAMES.values()) | set(T.LEAF_NAMES.values())
    _invented = sorted(t for t in unnamed if T.display_name(t) in _real
                       or T.describe(t) in _real)
    check("безымянному коду не приписано настоящее клиническое имя",
          not _invented,
          f"выдумано имя для {_invented} — врач не отличит приписанное от настоящего")
    _bare = sorted(t for t in unnamed if T.display_name(t) == t or T.describe(t) == t)
    check("безымянный код не выдаётся за имя самим собой",
          not _bare, f"имя = сам код у {_bare}: в интерфейсе это выглядит как сбой")
    check("у безымянного кода назван его раздел — есть где искать",
          all(T.section_of(t) and f"раздел {T.section_of(t)}" in T.describe(t) for t in unnamed),
          f"без раздела: {[t for t in unnamed if not T.section_of(t)]}")
    check("доля пометок на безымянных кодах не выросла (замер 0.71 %)",
          marks_unnamed <= 0.05 * (marks_named + marks_unnamed),
          f"{marks_unnamed} из {marks_named + marks_unnamed} — врач не найдёт эти факты")
    check("все 55 рубрик выгрузки имеют человеческое имя",
          all(T.has_name(c) for c in T.EXPORT_SLUGS))
    # Имена НЕ выдуманы: каждое взято из текста дерева, который уходит модели.
    check("ни одно имя листа не выдумано — все есть в тексте дерева",
          all(n in T.KNOWLEDGE_TREE for n in T.LEAF_NAMES.values()),
          "имя рубрики появилось из воздуха: врач сделает вывод о лечении по выдумке")
    check("ни одно имя раздела/группы не выдумано",
          all(n in T.KNOWLEDGE_TREE for n in list(T.SECTION_NAMES.values()) + list(T.GROUP_NAMES.values())))

# ==============================================================================
# [3] Отбор идёт по ГРАНИЦЕ ТОКЕНА, а не подстрокой (Д3)
# ==============================================================================
print("\n[3] Отбор по границе токена, а не подстрокой")
if TOKENS is not None:
    live = sorted(TOKENS)
    pairs = [(a, b) for a in live for b in live if a != b and a in b]
    print(f"       ЗАМЕР: пар «токен — подстрока другого токена» в живой вике: {len(pairs)}")
    sel_substring = sorted({a for a in T.EXPORT_SLUGS for b in live if a != b and a in b})
    check("коды отбора не являются подстрокой другого живого токена",
          not sel_substring, f"подстроки: {sel_substring} — тогда отбор ОБЯЗАН идти по границе")
    check("в вике есть коды, для которых подстрочный отбор развалился бы",
          len(pairs) >= 100, f"пар {len(pairs)}: проверка ниже теряет смысл")

# Стенд: коды, которые сталкиваются подстрокой. Отбор гоняется НАСТОЯЩИМ SQL.
STAND = [
    (1, "1.1.4", "СВОЙ-1.1.4"),
    (2, "2.1.1", "ЧУЖОЙ-2.1.1-содержит-1.1"),
    (3, "6.1.1", "ЧУЖОЙ-6.1.1-содержит-1.1"),
    (4, "1.1", "РОДИТЕЛЬ-L2-1.1"),
    (5, "1.1.0", "ХВОСТ-1.1.0"),
    (6, "2.1.10", "ХВОСТ-2.1.10"),
    (7, "12.1.1", "ГОЛОВА-12.1.1"),
    (8, "2.2.3.1", "ПОДКОД-2.2.3.1"),
    (9, "2.2.6, 2.1.2", "МУЛЬТИТЕГ-два-кода"),
    (10, " 2.1.1 ", "С-ПРОБЕЛАМИ-2.1.1"),
]
mem = sqlite3.connect(":memory:")
mem.execute("CREATE TABLE distilled_facts (id INTEGER PRIMARY KEY, category_code TEXT, content TEXT)")
mem.executemany("INSERT INTO distilled_facts VALUES (?,?,?)", STAND)


def by_token(code):
    return {r[0] for r in mem.execute(
        f"SELECT id FROM distilled_facts WHERE {T.token_sql()}", T.token_patterns(code))}


def by_substring(code):
    return {r[0] for r in mem.execute(
        "SELECT id FROM distilled_facts WHERE category_code LIKE ?", (f"%{code}%",))}


_tok, _sub = by_token("1.1"), by_substring("1.1")
print(f"       ЗАМЕР на стенде: код 1.1 по границе токена -> {sorted(_tok)}, "
      f"подстрокой -> {sorted(_sub)}")
check("подстрочный отбор действительно тянет чужие разделы (проверка имеет зубы)",
      {2, 3} <= _sub, f"подстрока дала {sorted(_sub)}")
check("отбор по границе не тянет 2.1.1 и 6.1.1 в раздел 1.1", not ({2, 3} & _tok))
check("отбор по границе берёт сам код 1.1 и его подкоды",
      {1, 4, 5} <= _tok, f"взято {sorted(_tok)}")
_tok211 = by_token("2.1.1")
check("2.1.10 (хвост) не попадает в 2.1.1", 6 not in _tok211)
check("12.1.1 (голова) не попадает в 2.1.1", 7 not in _tok211)
check("код с пробелами по краям находится", 10 in _tok211)
check("подкод 2.2.3.1 попадает в файл родителя 2.2.3", 8 in by_token("2.2.3"))
check("подкод 2.2.3.1 НЕ попадает в 2.3.1", 8 not in by_token("2.3.1"))
check("мультитег виден в ОБОИХ своих рубриках",
      9 in by_token("2.2.6") and 9 in by_token("2.1.2"),
      "второй тег фактически терялся: раздел для врача пуст")
# Тот же отбор на питоне обязан давать тот же ответ: два независимых пути.
_disagree = [(code, row_id) for code in ("1.1", "2.1.1", "2.2.3", "2.2.6", "2.1.2", "2.3.1")
             for row_id, value, _txt in STAND
             if (row_id in by_token(code)) != T.matches_token(value, code)]
check("SQL-отбор и питоновский matches_token дают один ответ",
      not _disagree, f"расхождения: {_disagree}")
_bad_ok = []
for _bad in ("2.1.1'; DROP TABLE distilled_facts--", "2.1._", "2.1.%", "", "abc", "1"):
    try:
        T.token_patterns(_bad)
        _bad_ok.append(_bad)
    except ValueError:
        pass
check("недопустимый код категории отвергается до похода в SQL",
      not _bad_ok, f"пропущены: {_bad_ok} — '_' и '%' в LIKE наберут в файл чужие факты")
mem.close()

# Живая вика: тот же вопрос на реальных данных, два независимых способа счёта.
if ROWS is not None:
    con = sqlite3.connect(LIVE_WIKI.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        sql_n = con.execute(
            f"SELECT COUNT(*) FROM distilled_facts WHERE {T.token_sql()}",
            T.token_patterns("1.1")).fetchone()[0]
        sub_n = con.execute(
            "SELECT COUNT(*) FROM distilled_facts WHERE category_code LIKE ?",
            ("%1.1%",)).fetchone()[0]
    finally:
        con.close()
    py_n = sum(1 for value in ROWS if T.matches_token(value, "1.1"))
    print(f"       ЗАМЕР на боевой вике: код 1.1 по границе -> {sql_n} фактов, "
          f"подстрокой -> {sub_n} (разница {sub_n - sql_n} чужих)")
    check("на боевой вике SQL и питон согласны по коду 1.1",
          sql_n == py_n, f"SQL {sql_n}, питон {py_n}")
    # Замер 2026-07-29: 5428 против 1118, то есть 4310 ЧУЖИХ фактов уехали бы в
    # раздел эндодонтии из ортопедии, хирургии и оборудования (2.1.1, 3.1.1, 6.1.1
    # все содержат '1.1' подстрокой). Порог взят от замера, а не на глаз.
    check("подстрочный отбор на боевой вике набрал бы 4310 чужих фактов",
          sub_n - sql_n >= 4000, f"подстрока {sub_n}, граница {sql_n}, "
          f"чужих {sub_n - sql_n} — если разница схлопнулась, проверка потеряла зубы")

# ==============================================================================
# [4] Разбор списка кодов через запятую (99.1 % записей вики)
# ==============================================================================
print("\n[4] Разбор списка кодов через запятую")
check("'2.2.6, 2.1.2' даёт РОВНО два кода",
      T.parse_codes("2.2.6, 2.1.2") == ("2.2.6", "2.1.2"),
      f"got {T.parse_codes('2.2.6, 2.1.2')!r}")
check("порядок кодов сохранён", T.parse_codes("2.1.2, 2.2.6") == ("2.1.2", "2.2.6"))
check("пробелы по краям срезаны", T.parse_codes(" 2.1.1 ,2.1.2 ") == ("2.1.1", "2.1.2"))
check("пустые звенья и хвостовая запятая не дают пустых кодов",
      T.parse_codes("2.1.1,,2.1.2,") == ("2.1.1", "2.1.2"))
check("повтор кода не удваивает раздел", T.parse_codes("1.1,1.1") == ("1.1",))
check("None и пустая строка дают пустой кортеж, а не падение",
      T.parse_codes(None) == () and T.parse_codes("") == ())
check("одиночный код без запятой разбирается", T.parse_codes("2.1.1") == ("2.1.1",))
if ROWS is not None:
    _marks = sum(len(T.parse_codes(v)) for v in ROWS)
    _multi = sum(1 for v in ROWS if len(T.parse_codes(v)) > 1)
    print(f"       ЗАМЕР: пометок {_marks}, записей с несколькими кодами {_multi} "
          f"из {len(ROWS)}")
    check("разбор даёт замеренные 42 317 пометок", _marks == 42317, f"стало {_marks}")
    check("записей с несколькими кодами — замеренные 12 667",
          _multi == 12667, f"стало {_multi}")

# ==============================================================================
# [5] Целостность единственного источника
# ==============================================================================
print("\n[5] Целостность единственного источника")
check("taxonomy.consistency_errors() пуст", not T.consistency_errors(),
      f"{T.consistency_errors()}")
check("листьев дерева 55 и столько же рубрик выгрузки",
      len(T.LEAF_NAMES) == len(T.EXPORT_SLUGS) == 55,
      f"листьев {len(T.LEAF_NAMES)}, рубрик {len(T.EXPORT_SLUGS)}")
check("разделов 10 и групп 10 — разделы 8-10 не потеряны",
      len(T.SECTION_NAMES) == 10 and len(T.GROUP_NAMES) == 10
      and {"8", "9", "10"} <= set(T.SECTION_NAMES),
      f"разделы: {sorted(T.SECTION_NAMES)}")
check("легальных кодов 65, как было до сведения копий", len(T.LEGAL_CODES) == 65)
check("корзина нечитаемых кодов выгружается",
      T.is_exportable(T.FALLBACK_CODE) and T.FALLBACK_CODE in T.LEAF_CODES,
      "всё, что модель не смогла классифицировать, не попадёт ни в один файл")
check("каждый лист дерева доезжает до файла выгрузки",
      not T.NON_EXPORTABLE_LEAVES, f"без рубрики: {sorted(T.NON_EXPORTABLE_LEAVES)}")
_illegal = {c: s for c, s in T.EXPORT_SLUGS.items()
            if set(s) & set(':/\\*?"<>|') or " " in s}
check("имя файла выгрузки не содержит запрещённых Windows символов",
      not _illegal, f"{_illegal} — экспорт упадёт на open() и ни одного файла ревью не будет")
check("имена рубрик выгрузки не совпадают с человеческими (это разные формы)",
      T.EXPORT_SLUGS["1.1.1"] == "Эндо_Доступ_МБ2"
      and T.DISPLAY_NAMES["1.1.1"].startswith("Эндодонтия: "),
      f"{T.EXPORT_SLUGS['1.1.1']!r} / {T.DISPLAY_NAMES['1.1.1']!r}")
check("normalize_code срезает пробелы, а не ломает код",
      T.normalize_code(" 2.1.1 ") == "2.1.1" and T.normalize_code(None) == "")
check("is_exportable различает рубрику и безымянный код",
      T.is_exportable("2.1.1") and not T.is_exportable("6.1.2"))
check("export_slug для кода без рубрики возвращает None, а не выдумку",
      T.export_slug("6.1.2") is None and T.export_slug("2.1.1") == "Орто_Виниры")

# ==============================================================================
# [6] Импорт taxonomy НИЧЕГО не делает
# ==============================================================================
print("\n[6] Импорт taxonomy ничего не делает")
_tree = ast.parse(io.open(REPO / "taxonomy.py", encoding="utf-8-sig").read())
_calls = []
for _node in _tree.body:
    if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        continue
    for _sub in ast.walk(_node):
        if isinstance(_sub, ast.Call):
            _fn = _sub.func.attr if isinstance(_sub.func, ast.Attribute) else getattr(_sub.func, "id", None)
            if _fn in ("open", "connect", "execute", "get", "post", "run", "system",
                       "makedirs", "remove", "write"):
                _calls.append((_node.lineno, _fn))
check("на уровне модуля нет ни файлов, ни баз, ни сети",
      not _calls, f"вызовы {_calls} — импорт модуля обязан быть безвредным")
_tmp = Path(tempfile.mkdtemp(prefix="taxonomy_import_"))
try:
    _env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING="cp1251")
    _proc = subprocess.run([sys.executable, "-c",
                            "import taxonomy; print(taxonomy.display_name('1.1.1'))"],
                           cwd=str(_tmp), env=_env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    check("импорт при cp1251 на stdout не падает (кириллица в именах рубрик)",
          _proc.returncode == 0,
          f"rc={_proc.returncode} err={_proc.stderr.decode('utf-8', 'replace')[-200:]!r}")
    check("импорт не создал ни одного файла в рабочем каталоге",
          not sorted(p.name for p in _tmp.iterdir()),
          f"создано: {sorted(p.name for p in _tmp.iterdir())}")
finally:
    for _p in _tmp.iterdir():
        _p.unlink()
    _tmp.rmdir()

# ==============================================================================
# [7] Инструмент осмотра классификации: боевая вика только на чтение, безымянный
#     код назван честно. Проверяется НАБЛЮДЕНИЕМ за настоящим вызовом, а не
#     поиском строки в исходнике.
# ==============================================================================
print("\n[7] Осмотр классификации: вика на чтение, безымянный код назван честно")
import aiosqlite  # noqa: E402

_probe_dir = Path(tempfile.mkdtemp(prefix="checker_ro_"))
try:
    _probe_db = _probe_dir / "probe_wiki.db"
    _con = sqlite3.connect(_probe_db)
    _con.execute("CREATE TABLE distilled_facts (id INTEGER PRIMARY KEY, content TEXT, "
                 "category_code TEXT, is_reclassified INTEGER)")
    _con.execute("INSERT INTO distilled_facts VALUES (1, 'ПРОБА-ФАКТА', "
                 "'2.1.1, 6.1.2', 1)")
    _con.commit()
    _con.close()

    _seen = []
    _orig_connect = aiosqlite.connect

    def _spy(*args, **kwargs):
        _seen.append((args, kwargs))
        return _orig_connect(*args, **kwargs)

    aiosqlite.connect = _spy
    _saved_path = checker.DB_PATH
    checker.DB_PATH = str(_probe_db)
    _buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(_buf):
            asyncio.run(checker.inspect())
    finally:
        aiosqlite.connect = _orig_connect
        checker.DB_PATH = _saved_path
    _out = _buf.getvalue()

    check("осмотр действительно сходил в базу", bool(_seen), "connect не вызывался")
    _target = str(_seen[0][0][0]) if _seen else ""
    check("вика открыта с mode=ro и uri=True",
          "mode=ro" in _target and _seen and _seen[0][1].get("uri") is True,
          f"открыто как {_target!r} — инструмент осмотра может затереть 12 784 факта")
    _wrote, _err = True, ""
    if _target:
        _ro = sqlite3.connect(_target, uri=True)
        try:
            _ro.execute("UPDATE distilled_facts SET content='ЗАТЁРТО'")
        except sqlite3.OperationalError as _e:
            _wrote, _err = False, str(_e)
        finally:
            _ro.close()
    check("через тот же URI запись физически невозможна",
          not _wrote and "readonly" in _err,
          f"запись прошла ({_err}) — резервной копии вики в этом лане нет")
    check("названная рубрика показана врачу человеческим именем",
          "Виниры (Керамика/Композит)" in _out, f"вывод: {_out[:200]!r}")
    check("безымянный код показан как «БЕЗ ИМЕНИ», а не «неизвестный код»",
          "БЕЗ ИМЕНИ: 6.1.2" in _out and "раздел 6" in _out,
          f"вывод: {_out[:300]!r} — «неизвестный код» читается как сбой инструмента")
finally:
    shutil.rmtree(_probe_dir, ignore_errors=True)

print(f"\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
