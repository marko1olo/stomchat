"""
Импорт модуля не должен ничего делать: ни править файлы, ни тратить деньги.

Что произошло 28 июля 2026. Обход всех модулей обычным `importlib.import_module`
— проверка, что проект вообще собирается, — привёл к следующему:

  * patch_assistant.py и patch_assistant_v2.py ПЕРЕПИСАЛИ assistant.py. Они
    открывают его на запись прямо на уровне модуля. Образцы для replace давно
    не совпадают с текущим кодом, поэтому замена попала не туда: инструкция
    триажа в ДВУХ местах превратилась в литерал "{ignore_instruction}" (бот
    перестал бы понимать, когда молчать на оффтоп), а блок «История диалога»
    продублировался по два лишних раза в двух промптах. Откачено git checkout;
  * benchmark.py отправил реальные платные запросы в Groq по трём моделям;
  * delist.py сходил в реальное API Google за перечнем моделей.

Один импорт — испорченный боевой файл и потраченная квота. Поэтому проверка
статическая: она РАЗБИРАЕТ исходники через ast и ничего не выполняет. Импорт
подозрительного модуля внутри теста воспроизвёл бы ровно ту аварию.

Что расширено 29 июля 2026. Проверка [1] стояла на ФОРМЕ той аварии: она ловила
`open(<литерал, кончающийся на .py>, 'w')`. Замер по живому дереву: таких вызовов
НОЛЬ, а записей на уровне модуля 22 — все через переменную. Значит 163 проверки
проходили вакуумно, и модуль, который при импорте пишет .json, .db, .log или ту
же assistant.py через переменную, шёл молча. Теперь запрещён КЛАСС:

  * боевые модули и инструменты при импорте не пишут ни одного файла и не
    открывают базу иначе как `mode=ro`;
  * тесты и черновики могут писать фикстуры во временный каталог, но не в корень
    репозитория (набор запускается из корня, литерал без разделителя каталогов
    пишет прямо сюда);
  * ни один модуль не выполняет изменяющий запрос к боевой базе при импорте.

Сами детекторы прогоняются на подсаженных опасных образцах ([1a]): ноль
замечаний одинаково выглядит у здорового дерева и у сломанного детектора.

Запуск: python test_import_safety.py
"""
import ast
import io
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCES = sorted(f for f in os.listdir(".") if f.endswith(".py"))


UNPARSED, _TREES = {}, {}


def parse(path):
    """
    Разбор с кэшем и без падения на файле, который прямо сейчас перезаписывают.

    Файл, застигнутый на середине чужой записи, давал IndentationError на весь
    набор: вместо «PASSED / FAILED» приходил traceback, и отличить чужую запись от
    настоящей поломки было нельзя. Теперь такой файл попадает в отдельную
    проверку [7], а остальные проверяются как обычно. Кэш нужен ещё и потому, что
    каждый файл разбирается по многу раз (замыкание, [1], [3], [5]).
    """
    if path not in _TREES:
        try:
            _TREES[path] = ast.parse(io.open(path, encoding="utf-8-sig").read())
        except (SyntaxError, ValueError, OSError, UnicodeDecodeError) as exc:
            UNPARSED[path] = f"{type(exc).__name__}: {exc}"
            _TREES[path] = ast.parse("")  # пустой модуль: проверки ниже не упадут на нём
    return _TREES[path]


def local_modules():
    """Имена модулей, которые лежат рядом. Импорт чего-то ещё — внешняя зависимость."""
    return {name[:-3] for name in SOURCES}


def imports_of(module, known):
    """
    Локальные модули, которые импортирует данный, включая импорты внутри функций.

    Ленивый импорт внутри функции — тоже импорт: tg_safety именно так и тянет
    telethon, а assistant так тянет часть инструментов. Для замыкания «что
    поднимает бота» важен факт зависимости, а не место строки.
    """
    found = set()
    for node in ast.walk(parse(module + ".py")):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # относительный импорт: пакетов здесь нет
            if node.module:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            # main.py достаёт media_tools через __import__("media_tools").
            # Без этой ветки динамическая зависимость выпала бы из замыкания.
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value.split(".")[0])
    return found & known


def production_closure(entry="main"):
    """
    Модули, которые реально поднимают бота — обход графа от main.py.

    Прежде этот список был перечислен РУКАМИ и разошёлся с деревом: в нём было 12
    модулей, а в замыкании 13 — не хватало tg_safety (603 строки, импортируется из
    assistant). То есть проверка [3] «боевой модуль не ходит в сеть при импорте» на
    нём не выполнялась ВООБЩЕ, и новый боевой модуль так же тихо оставался бы вне
    охраны. Теперь список вычисляется, поэтому разойтись не может.
    """
    known = local_modules()
    seen, queue = set(), [entry]
    while queue:
        current = queue.pop()
        if current in seen or current not in known:
            continue
        seen.add(current)
        queue.extend(dep for dep in imports_of(current, known) if dep not in seen)
    return seen


PRODUCTION = production_closure()


def top_level_body(tree):
    """
    Узлы, которые ВЫПОЛНЯЮТСЯ при импорте.

    Тела функций и классов сюда не входят: они только определяются. Первая
    версия этой проверки обходила дерево через ast.walk и считала действием
    любой вызов внутри любой функции — тогда «нарушителями» оказались все
    боевые модули разом. Признак неверной проверки: она срабатывает везде.
    """
    out = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.If) and "__main__" in ast.dump(child.test):
                continue
            out.append(child)
            visit(child)

    visit(tree)
    return out


def calls_in(nodes):
    for node in nodes:
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            yield name, node


def guards_import(tree):
    """Есть ли отказ выполняться при импорте: raise на уровне модуля."""
    for stmt in tree.body[:12]:
        if isinstance(stmt, ast.Raise):
            return True
        if isinstance(stmt, ast.If) and "__main__" in ast.dump(stmt.test):
            if any(isinstance(s, ast.Raise) for s in stmt.body):
                return True
    return False


def module_constants(nodes):
    """
    Строковое содержимое имён, которым на уровне модуля присвоили путь.

    Без разворачивания имени проверка видит `sqlite3.connect(WIKI)` как «цель
    неизвестна» и одинаково пропускает боевую вику и её ro-копию. Замер по
    дереву: пять черновиков держат в переменной именно `?mode=ro`-URI, а
    test_savdel_taxonomy — `REPO / "stomat_wiki.db"`. Отличить одно от другого
    можно только так.
    """
    out = {}
    for node in nodes:
        if not isinstance(node, ast.Assign):
            continue
        texts = " ".join(sub.value for sub in ast.walk(node.value)
                         if isinstance(sub, ast.Constant) and isinstance(sub.value, str))
        if not texts:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = (out.get(target.id, "") + " " + texts).strip()
    return out


def target_text(node, consts):
    """Всё, что известно про цель вызова: литералы выражения плюс развёрнутые имена."""
    parts = [sub.value for sub in ast.walk(node)
             if isinstance(sub, ast.Constant) and isinstance(sub.value, str)]
    parts += [consts[sub.id] for sub in ast.walk(node)
              if isinstance(sub, ast.Name) and sub.id in consts]
    return " ".join(parts)


def write_mode(node):
    """Режим open() — пустая строка, если файл открывают на чтение."""
    mode = ""
    for arg in node.args[1:]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            mode = arg.value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value or ""
    return mode if any(flag in mode for flag in ("w", "a", "+", "x")) else ""


def bare_repo_target(node):
    """
    Литерал без разделителя каталогов — это файл в КОРНЕ репозитория.

    Набор запускается из корня, поэтому `open("assistant.py", "w")` и
    `open("bot.log", "a")` пишут в боевое дерево, где бы в файле они ни стояли.
    Склейка с временным каталогом сюда не попадает: там есть разделитель.
    """
    if not (node.args and isinstance(node.args[0], ast.Constant)):
        return None
    value = node.args[0].value
    if not isinstance(value, str) or "/" in value or "\\" in value:
        return None
    return value


# Базы, порча которых видна врачу: 12 784 факта вики, 117 847 реплик архива и
# состояние бота. Копии и временные базы под это имя не попадают.
LIVE_DATABASES = ("stomat_wiki.db", "stomat_archive.db", "stomat_bot.db")
MUTATING_SQL = ("insert", "update", "delete", "drop", "alter", "create",
                "replace", "vacuum", "attach", "truncate", "reindex")


# Запись мимо open(): shutil.copy2 и os.remove тоже пишут, а прежняя проверка
# смотрела ровно на open. Найдено на своей же оснастке — черновик диверсий делал
# `shutil.copy2(t, t + ".bak")` на уровне модуля, и детектор его не видел.
# Имена ниже однозначны, их не спутать с методом списка или строки.
UNAMBIGUOUS_MUTATORS = {"rmtree", "copy2", "copyfile", "copytree", "move",
                        "write_text", "write_bytes"}
# А эти опасны только у известного владельца: os.remove против list.remove и
# os.replace против str.replace.
OWNED_MUTATORS = {("os", "remove"), ("os", "unlink"), ("os", "rename"), ("os", "replace"),
                  ("os", "truncate"), ("os", "system"), ("shutil", "copy"),
                  ("json", "dump"), ("subprocess", "run"), ("subprocess", "Popen"),
                  ("subprocess", "call"), ("subprocess", "check_output")}
# mkdir/makedirs/mkdtemp сюда СОЗНАТЕЛЬНО не входят: созданный каталог ничего не
# уничтожает и виден глазами. Замер: в строгом классе таких вызовов на уровне
# модуля ровно два (visionproc.py:27 и :29), и они названы в выводе ниже, а не
# спрятаны за молчаливым исключением.
CREATORS = {"mkdir", "makedirs", "mkdtemp", "mkstemp"}


def module_level_mutations(tree, names):
    """Разрушающие вызовы на уровне модуля: удаление, перезапись, копирование, запуск процесса."""
    out = []
    for node in top_level_body(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
        if attr in names or (owner, attr) in OWNED_MUTATORS:
            out.append((node.lineno, f"{owner}.{attr}" if owner else attr))
    return out


def live_db_writes(tree):
    """
    Изменяющие запросы, идущие на уровне модуля в БОЕВУЮ базу.

    Ручка опознаётся в двух формах: привязанная к имени (`db = connect(...)`) и
    цепочкой сразу (`connect("stomat_wiki.db").execute(...)`) — так написаны два
    теста. `commit` и `executescript` считаются изменением всегда: скрипт может
    содержать что угодно, а коммит на уровне модуля защищать нечем.
    """
    body = top_level_body(tree)
    consts = module_constants(body)

    def is_live_rw(call):
        if not (isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "connect"):
            return False
        where = target_text(call, consts)
        return any(db in where for db in LIVE_DATABASES) and "mode=ro" not in where

    handles = set()
    for node in body:
        if isinstance(node, ast.Assign) and is_live_rw(node.value):
            handles.update(t.id for t in node.targets if isinstance(t, ast.Name))

    hits = []
    for name, node in calls_in(body):
        if name not in ("execute", "executemany", "executescript", "commit"):
            continue
        owner = node.func.value if isinstance(node.func, ast.Attribute) else None
        on_live = is_live_rw(owner) or (isinstance(owner, ast.Name) and owner.id in handles)
        if not on_live:
            continue
        if name in ("commit", "executescript"):
            hits.append((node.lineno, name))
            continue
        sql = ""
        if node.args:
            sql = " ".join(sub.value for sub in ast.walk(node.args[0])
                           if isinstance(sub, ast.Constant) and isinstance(sub.value, str))
        words = sql.strip().lower().split()
        head = words[0] if words else ""
        if head in MUTATING_SQL or (head == "pragma" and "=" in sql):
            hits.append((node.lineno, sql.strip()[:50]))
    return hits


def live_rw_handles(tree):
    """Соединения с боевой базой НЕ в режиме ro, открытые на уровне модуля."""
    body = top_level_body(tree)
    consts = module_constants(body)
    out = []
    for name, node in calls_in(body):
        if name != "connect":
            continue
        where = target_text(node, consts)
        if any(db in where for db in LIVE_DATABASES) and "mode=ro" not in where:
            out.append(node.lineno)
    return out


print("\n[1] Импорт модуля не пишет на диск и не открывает боевую базу на запись")
# Прежняя версия требовала, чтобы первым аргументом open стоял ЛИТЕРАЛ,
# кончающийся на .py. Замер по живому дереву 29 июля 2026: таких вызовов НОЛЬ, а
# записей на уровне модуля 22 — все через переменную (CHILD, planted,
# assistant.STATE_PATH, main.SCHEDULER_STATE_PATH, gc.BANNED_MODELS_FILE). То есть
# 163 проверки проходили вакуумно: сторож стоял на ФОРМЕ аварии 28 июля, а не на
# её классе, и модуль, который при импорте пишет .json, .db, .log или ту же
# assistant.py через переменную, шёл молча. Последствие для врача: боевой файл
# или 12 784 факта вики портятся от одного import, а сторож при этом зелёный —
# значит о порче узнают не из проверки, а из жалобы врача.
#
# Строгость разная, потому что риск разный:
#   боевые модули и инструменты — при импорте не пишут ВООБЩЕ и не открывают базу
#     иначе как mode=ro: их импортирует бот, и любая запись случается в бою;
#   тесты и черновики — им можно писать фикстуры во временный каталог (за
#     боевыми файлами следит test_isolation.py), но не в корень репозитория:
#     набор запускается из корня, литерал без разделителя каталогов пишет сюда.
TOOLS = {p[:-3] for p in SOURCES
         if not p.startswith(("test_", "_")) and p[:-3] not in PRODUCTION}
STRICT = PRODUCTION | TOOLS

# Сначала — что строгий класс вообще не пуст. Диверсия «STRICT = set()» уронила
# число проверок с 352 до 318 и НЕ УРОНИЛА НИ ОДНОЙ: 34 файла тихо переехали под
# слабое правило, а набор остался зелёным. Это ровно тот снятый потолок, который
# проект уже проходил, поэтому состав класса проверяется отдельно.
check("строгий класс непустой", len(STRICT) >= 30,
      f"вычислено {len(STRICT)}: боевых {len(PRODUCTION)}, инструментов {len(TOOLS)}")
check("боевое замыкание целиком под строгим правилом", PRODUCTION <= STRICT,
      f"вне строгого класса: {sorted(PRODUCTION - STRICT)}")
for _anchor in ("main", "assistant", "database", "reclass", "savdel", "run_all_tests"):
    if _anchor in local_modules():
        check(f"в строгом классе есть {_anchor}", _anchor in STRICT,
              f"строгий класс: {len(STRICT)} модулей")
check("в строгий класс не попали тесты",
      not {m for m in STRICT if m.startswith("test_")},
      f"лишнее: {sorted(m for m in STRICT if m.startswith('test_'))}")
check("класс тестов и черновиков тоже не пуст",
      len({p[:-3] for p in SOURCES} - STRICT) >= 50,
      "правило про корень репозитория проверять стало не на чем")

for path in SOURCES:
    tree = parse(path)
    if guards_import(tree):
        continue  # отказывается работать при импорте — за этим следит [2]
    body = top_level_body(tree)
    consts = module_constants(body)
    writes, repo_writes, db_opens = [], [], []
    for name, node in calls_in(body):
        if name == "open":
            mode = write_mode(node)
            if not mode:
                continue
            where = target_text(node, consts)[:60] or "<переменная>"
            writes.append((node.lineno, mode, where))
            bare = bare_repo_target(node)
            if bare:
                repo_writes.append((node.lineno, mode, bare))
        elif name == "connect":
            if "mode=ro" not in target_text(node, consts):
                db_opens.append((node.lineno, target_text(node, consts)[:60] or "<переменная>"))
    if path[:-3] in STRICT:
        mutations = module_level_mutations(tree, UNAMBIGUOUS_MUTATORS)
        creations = module_level_mutations(tree, CREATORS)
        if creations:
            print(f"      {path} создаёт при импорте: {creations}")
        check(f"{path} ничего не пишет при импорте", not writes,
              f"open на запись на уровне модуля: {writes}")
        check(f"{path} не открывает базу на запись при импорте", not db_opens,
              f"connect без mode=ro на уровне модуля: {db_opens}")
        check(f"{path} не удаляет и не перезаписывает файлы при импорте", not mutations,
              f"разрушающие вызовы на уровне модуля: {mutations}")
    else:
        check(f"{path} не пишет в корень репозитория при импорте", not repo_writes,
              f"open на запись на уровне модуля: {repo_writes}")

# Соединение с боевой базой на уровне модуля: читать можно, менять нельзя.
# Замер 29 июля на КОПИИ (живая база открывалась только mode=ro): rw-соединение,
# через которое идёт лишь SELECT, не меняет ни один байт — md5 2207af76..., размер
# 9 158 656 и mtime совпали до и после. Но mode=ro отклоняет запись физически
# (OperationalError: attempt to write a readonly database), а rw — нет: первый же
# UPDATE по такой ручке уйдёт в 12 784 факта, которые врач читает через /wiki, и
# откатывать будет нечего.
mutators, rw_inventory = [], []
for path in SOURCES:
    if path.startswith("_"):
        continue  # черновики агентов: их никто не импортирует, это проверено ниже
    tree = parse(path)
    if guards_import(tree):
        continue
    mutators += [f"{path}:{line} {what}" for line, what in live_db_writes(tree)]
    rw_inventory += [f"{path}:{line}" for line in live_rw_handles(tree)]
check("ни один модуль не меняет боевую базу при импорте", not mutators,
      f"изменяющие запросы на уровне модуля: {mutators}")
print(f"      ручек к боевой базе НЕ в режиме ro на уровне модуля: "
      f"{len(rw_inventory)} -> {rw_inventory or 'нет'}")

# Черновики агентов из проверки выше исключены — значит надо доказать, что их
# действительно никто не подключает. Иначе исключение станет дырой само.
_scratch = {p[:-3] for p in SOURCES if p.startswith("_")}
_scratch_users = sorted(f"{p} -> {d}" for p in SOURCES
                        for d in imports_of(p[:-3], local_modules()) if d in _scratch)
check("черновики агентов (_*) никем не импортируются", not _scratch_users,
      f"импорт черновика: {_scratch_users}")

print("\n[1a] Проверки выше ловят подсаженную поломку")
# Ноль замечаний одинаково выглядит у здорового дерева и у сломанного детектора —
# именно так прежняя проверка [1] и «проходила». Поэтому детекторы прогоняются на
# заведомо опасных образцах прямо здесь. Образцы разбираются как текст, ни один
# из них не исполняется.
_SAMPLES = {
    "запись в .py через переменную": 'DST = "assistant.py"\nopen(DST, "w").write("")\n',
    "запись в журнал дописыванием": 'open("bot.log", "a").write("x")\n',
    "запись в .json на уровне модуля": 'import json\njson.dump({}, open("state.json", "w"))\n',
    "запись через mode=": 'open("assistant.py", mode="w+")\n',
    "запись внутри with": 'with open("assistant.py", "w") as fh:\n    fh.write("")\n',
    "запись внутри try": 'try:\n    open("assistant.py", "x")\nexcept OSError:\n    pass\n',
}
for _why, _src in _SAMPLES.items():
    _body = top_level_body(ast.parse(_src))
    _hits = [n for name, n in calls_in(_body) if name == "open" and write_mode(n)]
    check(f"детектор записи видит: {_why}", bool(_hits), "образец прошёл незамеченным")


# Отдельно — детектор «файл в корне репозитория». Без этих образцов его можно
# было ослепить (вернуть None всегда) и не уронить ни одной проверки: сегодня в
# корень при импорте никто не пишет, и молчание выглядело бы как чистое дерево.
for _why, _src, _want in (
    ("литерал в корне", 'open("bot.log", "a")\n', "bot.log"),
    ("исходник в корне", 'open("assistant.py", "w")\n', "assistant.py"),
    ("склейка с временным каталогом", 'import os, tempfile\nopen(os.path.join(tempfile.mkdtemp(), "x.json"), "w")\n', None),
    ("путь с разделителем", 'open("sub/dir.json", "w")\n', None),
):
    _got = [bare_repo_target(n) for name, n in calls_in(top_level_body(ast.parse(_src)))
            if name == "open"]
    check(f"детектор корня репозитория: {_why}", _got == [_want], f"вернулось {_got}, ждали {[_want]}")

_ro = 'import sqlite3\nc = sqlite3.connect("file:stomat_wiki.db?mode=ro", uri=True)\nc.execute("SELECT 1")\n'
check("детектор записи не срабатывает на чтении",
      not [n for name, n in calls_in(top_level_body(ast.parse('open("assistant.py")\nopen("x.txt", "r")\n')))
           if name == "open" and write_mode(n)],
      "любое чтение считается записью — проверка потеряла смысл")

_MUTATION_SAMPLES = {
    "снос каталога": 'import shutil\nshutil.rmtree("uploaded_media")\n',
    "копирование поверх файла": 'import shutil\nshutil.copy2("a.py", "assistant.py")\n',
    "удаление файла": 'import os\nos.remove("assistant_state.json")\n',
    "переименование поверх": 'import os\nos.replace("tmp.db", "stomat_wiki.db")\n',
    "выгрузка json": 'import json\njson.dump({}, None)\n',
    "запуск процесса": 'import subprocess\nsubprocess.run(["python", "reclass.py"])\n',
    "Path.write_text": 'from pathlib import Path\nPath("assistant.py").write_text("")\n',
}
for _why, _src in _MUTATION_SAMPLES.items():
    check(f"детектор разрушающих вызовов видит: {_why}",
          bool(module_level_mutations(ast.parse(_src), UNAMBIGUOUS_MUTATORS)),
          "образец прошёл незамеченным")

_MUTATION_SAFE = {
    "list.remove — не файл": 'items = [1]\nitems.remove(1)\n',
    "str.replace — не файл": 'name = "a".replace("a", "b")\n',
    "то же, но внутри функции": 'import shutil\ndef go():\n    shutil.rmtree("uploaded_media")\n',
    "под __main__": 'import shutil\nif __name__ == "__main__":\n    shutil.rmtree("uploaded_media")\n',
    "создание каталога — не разрушение": 'import os\nos.makedirs("out", exist_ok=True)\n',
}
for _why, _src in _MUTATION_SAFE.items():
    check(f"детектор разрушающих вызовов молчит на: {_why}",
          not module_level_mutations(ast.parse(_src), UNAMBIGUOUS_MUTATORS),
          "ложная тревога — набор станет красным на безопасном коде")

_DB_SAMPLES = {
    "UPDATE по имени ручки": 'import sqlite3\ndb = sqlite3.connect("stomat_wiki.db")\ndb.execute("UPDATE distilled_facts SET confidence=1")\n',
    "DELETE цепочкой сразу": 'import sqlite3\nsqlite3.connect("stomat_wiki.db").execute("DELETE FROM distilled_facts")\n',
    "VACUUM боевого архива": 'import sqlite3\na = sqlite3.connect("stomat_archive.db")\na.execute("VACUUM")\n',
    "executescript": 'import sqlite3\nd = sqlite3.connect("stomat_bot.db")\nd.executescript("select 1")\n',
    "commit по боевой ручке": 'import sqlite3\nd = sqlite3.connect("stomat_wiki.db")\nd.commit()\n',
    "путь через переменную": 'import sqlite3\nP = "stomat_wiki.db"\nd = sqlite3.connect(P)\nd.execute("INSERT INTO distilled_facts VALUES (1)")\n',
    "aiosqlite вместо sqlite3": 'import aiosqlite\nd = aiosqlite.connect("stomat_wiki.db")\nd.execute("DROP TABLE distilled_facts")\n',
}
for _why, _src in _DB_SAMPLES.items():
    check(f"детектор базы видит: {_why}", bool(live_db_writes(ast.parse(_src))),
          "образец прошёл незамеченным")

_SAFE_DB = {
    "SELECT по rw-ручке": 'import sqlite3\nd = sqlite3.connect("stomat_wiki.db")\nd.execute("SELECT COUNT(*) FROM distilled_facts")\n',
    "UPDATE в ro-режиме отклонит сам sqlite": _ro,
    "UPDATE во ВРЕМЕННОЙ базе": 'import os, sqlite3, tempfile\np = os.path.join(tempfile.mkdtemp(), "w.db")\nd = sqlite3.connect(p)\nd.execute("UPDATE t SET x=1")\nd.commit()\n',
    "UPDATE внутри функции, а не при импорте": 'import sqlite3\ndef go():\n    d = sqlite3.connect("stomat_wiki.db")\n    d.execute("UPDATE t SET x=1")\n    d.commit()\n',
}
for _why, _src in _SAFE_DB.items():
    check(f"детектор базы не срабатывает на: {_why}", not live_db_writes(ast.parse(_src)),
          "ложная тревога — набор станет красным на безопасном коде")

print("\n[2] Опасные одноразовые инструменты обезврежены")
# Их нельзя просто импортировать в проверку: импорт и есть авария.
DANGEROUS = {
    "patch_assistant.py": "переписывает assistant.py",
    "patch_assistant_v2.py": "переписывает assistant.py",
    "benchmark.py": "платные запросы в Groq",
    "delist.py": "запрос в API Google",
}
for path, why in DANGEROUS.items():
    if not os.path.exists(path):
        check(f"{path} удалён — проверка неприменима", True)
        continue
    check(f"{path} отказывается работать при импорте ({why})", guards_import(parse(path)),
          "модуль выполнит свою работу от простого import")

print("\n[3] Боевые модули импортируются без внешних вызовов")
# Сначала — что охраняемый список вообще не пуст. Если обход графа сломается и
# вернёт пустое множество, все проверки ниже станут пустыми, а набор — зелёным:
# ровно тот вид молчаливой лжи, против которого этот файл и написан.
check("замыкание импортов main.py непустое", len(PRODUCTION) >= 12,
      f"вычислено {len(PRODUCTION)}: {sorted(PRODUCTION)}")
for _anchor in ("main", "assistant", "database", "config", "tg_safety"):
    check(f"в охраняемом замыкании есть {_anchor}", _anchor in PRODUCTION,
          f"замыкание: {sorted(PRODUCTION)}")
check("в замыкание не попали тестовые модули",
      not {m for m in PRODUCTION if m.startswith("test_")},
      f"лишнее: {sorted(m for m in PRODUCTION if m.startswith('test_'))}")
check("в замыкание не попали обезвреженные инструменты",
      not (PRODUCTION & {"patch_assistant", "patch_assistant_v2", "benchmark", "delist"}),
      "боевой путь тянет одноразовый инструмент")

# Перекрёстная проверка ДРУГИМ методом: замыкание считается через ast, а здесь
# импорты вылавливаются регуляркой по тексту. Смысл именно в независимости: если
# обход графа ослабят (например перестанут учитывать ленивые импорты внутри
# функций — а blocking_tools тянет web_lookup именно так), ast-версия молча
# потеряет модуль, регулярка его увидит, и проверка упадёт.
_IMPORT_RE = __import__("re").compile(
    r"^\s*(?:import\s+([A-Za-z_][\w]*)|from\s+([A-Za-z_][\w]*)\s+import)", __import__("re").M)
_local = local_modules()
_missed = set()
for _module in sorted(PRODUCTION):
    _text = io.open(_module + ".py", encoding="utf-8-sig").read()
    for _m in _IMPORT_RE.finditer(_text):
        _dep = _m.group(1) or _m.group(2)
        if _dep in _local and _dep not in PRODUCTION:
            _missed.add(f"{_module} -> {_dep}")
check("ни один импорт боевого модуля не выпал из охраняемого замыкания",
      not _missed,
      f"вне охраны: {sorted(_missed)} — на этих модулях проверки ниже не выполняются")

# Подъём бота не должен зависеть от сети до того, как start_bot возьмёт
# управление: иначе падение при импорте не поймает ни один обработчик.
NETWORK = {"post", "get", "request", "urlopen", "create", "generate_content",
           "send_message", "connect", "list", "invoke", "chat"}
for path in SOURCES:
    module = path[:-3]
    if module not in PRODUCTION:
        continue
    names = {n for n, _ in calls_in(top_level_body(parse(path))) if n}
    hits = sorted(names & NETWORK)
    check(f"{path} не ходит в сеть при импорте", not hits, f"вызовы: {hits}")

print("\n[4] Точка входа защищена __main__")
main_tree = parse("main.py")
has_guard = any(isinstance(s, ast.If) and "__main__" in ast.dump(s.test) for s in main_tree.body)
check("main.py запускает бота только при прямом запуске", has_guard,
      "импорт main.py поднимет бота")
starts = [n for n, _ in calls_in(top_level_body(main_tree)) if n in ("run", "run_until_complete")]
check("asyncio.run не вызывается на уровне модуля", not starts, f"got {starts}")

print("\n[5] Тесты не импортируют опасные инструменты")
for path in SOURCES:
    if not path.startswith("test_"):
        continue
    tree = parse(path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    bad = sorted(imported & {d[:-3] for d in DANGEROUS})
    check(f"{path} не тянет опасный инструмент", not bad, f"импортирует {bad}")

print("\n[6] Прогон теста не пишет в боевой журнал")
# configure_logging() вызывается на уровне модуля в main.py, поэтому любой
# тест, импортирующий main, писал в боевой bot.log. Замер на 28 июля 2026:
# 1629 строк работы бота против 1005 строк тестовой выдумки — несуществующие
# чаты, выдуманные врачи, строки ERROR, которых в работе не было. Журнал
# ротируется по 5 МБ, один прогон набора добавляет ~0.2 МБ: полтора десятка
# прогонов вытесняют историю работы целиком.
import runtime_guard  # noqa: E402

check("этот прогон пишет не в боевой журнал", runtime_guard.LOG_PATH != "bot.log",
      f"журнал теста уходит в {runtime_guard.LOG_PATH}")
check("путь журнала определяется по точке входа",
      "_default_log_path" in io.open("runtime_guard.py", encoding="utf-8").read(),
      "правило потеряно, тесты снова затрут боевой журнал")
check("проверка действительно запущена как тест",
      os.path.basename(sys.argv[0]).startswith("test_"),
      "иначе правило проверить нечем")

# Правило проверяем на самой функции: подменяем точку входа и переменную,
# а не полагаемся на то, как запущен этот файл.
_argv, _env = sys.argv[0], os.environ.pop("STOMCHAT_LOG_PATH", None)
try:
    sys.argv[0] = "main.py"
    check("боту достаётся именно bot.log", runtime_guard._default_log_path() == "bot.log",
          runtime_guard._default_log_path())
    sys.argv[0] = "test_something.py"
    check("тесту достаётся отдельный журнал",
          runtime_guard._default_log_path() != "bot.log",
          runtime_guard._default_log_path())
    os.environ["STOMCHAT_LOG_PATH"] = "явно_заданный.log"
    sys.argv[0] = "main.py"
    check("переменная окружения перекрывает оба случая",
          runtime_guard._default_log_path() == "явно_заданный.log",
          runtime_guard._default_log_path())
finally:
    sys.argv[0] = _argv
    os.environ.pop("STOMCHAT_LOG_PATH", None)
    if _env is not None:
        os.environ["STOMCHAT_LOG_PATH"] = _env

# Журнал ограничен по размеру: бот работает месяцами.
guard_source = io.open("runtime_guard.py", encoding="utf-8").read()
check("журнал ротируется, а не растёт бесконечно", "RotatingFileHandler" in guard_source)
check("у ротации задан предел размера", "maxBytes" in guard_source)
check("хранится несколько поколений", "backupCount" in guard_source)

print("\n[7] Все файлы корня действительно разобраны")
# Файл, который не разобрался, проходит ВСЕ проверки выше вакуумно: у пустого
# модуля нет ни записи, ни соединения с базой. Без этой строки достаточно
# оставить в корне битый .py, чтобы вывести его из-под охраны и не увидеть ни
# одного замечания.
check("ни один файл не выпал из разбора", not UNPARSED,
      f"не разобраны (значит и не проверены): {UNPARSED}")
print(f"      разобрано файлов: {len(_TREES)} из {len(SOURCES)}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
