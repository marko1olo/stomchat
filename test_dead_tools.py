"""
Реестр мёртвых инструментов в корне: они есть, их никто не зовёт, и они обезврежены.

Зачем файл нужен. В корне рядом с боевым кодом лежат разовые скрипты, которые
никто не импортирует. Пока они просто лежат, они безобидны — но ровно один такой
скрипт 28 июля 2026 переписал assistant.py от обычного `import`, а другой
отправил платные запросы в Groq. Мёртвый файл опасен не тем, что работает, а тем,
что его можно случайно подключить или запустить.

Что защищает каждая проверка:

  [1] «мёртвый» правда мёртв. Если кто-то подключит search_engine.py, врач
      получит поиск, который при отсутствии пакета ddgs не деградирует, а
      валится на импорте (строка 4, вне try) — то есть бот не поднимется вообще,
      а не «поищет хуже». Проверка падает, когда мёртвый оживает: это не запрет,
      это требование обновить реестр осознанно.
  [2] мёртвый безопасен при импорте: ни записи на диск, ни соединения с базой,
      ни сетевого вызова на уровне модуля. Иначе достаточно, чтобы новый тест
      обошёл модули importlib — и авария 28 июля повторится.
  [3] реестр полон. Без этой проверки следующий разовый скрипт просто растворится
      среди 165 файлов корня, и через месяц никто не отличит его от боевого:
      именно так patch_assistant_v2.py и дожил до дня, когда его импортировали.
  [4] детекторы проверяются на подсаженных образцах. Ноль замечаний одинаково
      выглядит у чистого дерева и у сломанного детектора.

Разбор СТАТИЧЕСКИЙ: ни один разбираемый модуль не импортируется и не
исполняется. Импорт подозрительного модуля внутри теста и есть та самая авария.

Запуск: python test_dead_tools.py
"""
import ast
import io
import os
import re
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
KNOWN = {name[:-3] for name in SOURCES}


UNPARSED, _TREES = {}, {}


def parse(path):
    """
    Разбор с кэшем и без падения на файле, который прямо сейчас перезаписывают.

    Рядом работают другие агенты; файл, застигнутый на середине записи, ронял весь
    набор traceback-ом вместо «PASSED / FAILED». Такой файл называется отдельной
    проверкой ниже, остальные проверяются как обычно.
    """
    if path not in _TREES:
        try:
            _TREES[path] = ast.parse(io.open(path, encoding="utf-8-sig").read())
        except (SyntaxError, ValueError, OSError, UnicodeDecodeError) as exc:
            UNPARSED[path] = f"{type(exc).__name__}: {exc}"
            _TREES[path] = ast.parse("")
    return _TREES[path]


def imports_of(module):
    """Локальные модули, которые импортирует данный, включая ленивые и __import__."""
    found = set()
    for node in ast.walk(parse(module + ".py")):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if not node.level and node.module:
                found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value.split(".")[0])
    return found & KNOWN


GRAPH = {name[:-3]: imports_of(name[:-3]) for name in SOURCES}


def closure(entry, graph):
    seen, queue = set(), [entry]
    while queue:
        current = queue.pop()
        if current in seen or current not in graph:
            continue
        seen.add(current)
        queue.extend(dep for dep in graph[current] if dep not in seen)
    return seen


PRODUCTION = closure("main", GRAPH)


def unreferenced_tools(graph, production):
    """
    Инструменты корня, которых никто не подключает.

    Тесты и черновики агентов (`_*`) исключены по имени: они по определению не
    импортируются, и с ними список превратился бы в перечень всего дерева. Что
    черновики действительно никем не подключаются, проверяет test_import_safety.

    Функция чистая, чтобы её можно было прогнать на подсаженном графе: реестр,
    который не ловит новый мёртвый файл, хуже отсутствующего.
    """
    used = set()
    for module, deps in graph.items():
        used |= {dep for dep in deps if dep != module}
    return sorted(m for m in graph
                  if m not in production
                  and not m.startswith(("test_", "_"))
                  and m not in used)


# Мёртвые инструменты: имя -> чем он был и чем заменён.
DEAD = {
    "search_engine": "старый поиск через ddgs напрямую; заменён search_engine_safe.py, "
                     "который ходит в подпроцесс и деградирует строкой вместо падения",
    "inspector": "разовый подсчёт сообщений/фото/видео в исходном чате",
    "id": "разовый вывод списка диалогов, чтобы найти SOURCE_CHAT_ID",
    "delme": "то же, что id.py, вторая копия того же скрипта",
    "pathcheck": "разовая выборочная проверка 50 путей к медиа в архиве",
}

# Остальные никем не импортируемые файлы корня: живые, но запускаются руками.
# Реестр обязан объяснять КАЖДЫЙ такой файл, иначе проверка [3] бессмысленна.
STANDALONE = {
    "run_all_tests": "запускает весь набор, вызывается человеком",
    "config.example": "версионный шаблон config.py, полноту сторожит test_config_contract.py",
    "benchmark": "обезврежен ImportError, замер моделей Groq, запуск руками",
    "delist": "обезврежен ImportError, перечень моделей Google, запуск руками",
    "patch_assistant": "обезврежен ImportError, разовая правка assistant.py",
    "patch_assistant_v2": "обезврежен ImportError, разовая правка assistant.py",
    "import_videos": "снят с вооружения, обезврежен, замена — videosi.py",
    "prompter": "заказ монографий по выгрузке savdel.py, запуск руками",
    "debugdist": "отладочный прогон дистилляции, запуск руками",
    # deppd убран из реестра НАМЕРЕННО: у него появился тест
    # (test_archive_dump.py), который его импортирует, поэтому «никем не
    # импортируемым» он больше не числится, и проверка [3] это ловит.
    # Заодно снята неверная запись: она гласила «разбор зависимостей», а deppd —
    # это ДАМПЕР архива, единственный путь, которым 117 847 реплик врачей попали
    # в stomat_archive.db. Имя было угадано по звучанию, и такая догадка в
    # реестре опаснее пустой строки: следующий поверит и не станет искать.
    "filemake": "сборка файлов выгрузки, запуск руками",
    "visionproc": "пакетная обработка изображений, запуск руками",
    "search_engine_safe": "готовая замена search_engine.py, к живому пути врача ещё не подключена",
    "apply_content_hash_migration": "разовая миграция таблицы distilled_facts на UNIQUE(content_hash)",
    "dump_recent_bot_disasters": "диагностический дампер последних аварийных сообщений бота",
    "fix_corrupted_source_ids": "разовая вычистка MSG_ префиксов из source_ids в stomat_wiki.db",
}


def top_level_body(tree):
    """Узлы, которые ВЫПОЛНЯЮТСЯ при импорте: тела функций и классов не в счёт."""
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


def module_level_writes(tree):
    """open(...) на запись на уровне модуля — цель любая."""
    out = []
    for name, node in calls_in(top_level_body(tree)):
        if name != "open":
            continue
        mode = ""
        for arg in node.args[1:]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                mode = arg.value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value or ""
        if any(flag in mode for flag in ("w", "a", "+", "x")):
            out.append((node.lineno, mode))
    return out


def module_level_db(tree):
    """Соединение с базой на уровне модуля. Для мёртвого инструмента их быть не должно вовсе."""
    return [(node.lineno, ast.unparse(node)[:70])
            for name, node in calls_in(top_level_body(tree)) if name == "connect"]


# Имена, вызов которых на уровне модуля означает уход в сеть или запуск процесса.
# `connect` проверяется отдельно, чтобы sqlite не путался с сетью.
OUTBOUND = {"post", "get", "request", "urlopen", "search", "text", "generate_content",
            "send_message", "start", "run", "Popen", "check_output", "system",
            "invoke", "chat", "create", "list", "iter_messages", "iter_dialogs"}


def module_level_outbound(tree):
    return sorted({(node.lineno, name) for name, node in calls_in(top_level_body(tree))
                   if name in OUTBOUND})


def public_names(tree):
    return [node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not node.name.startswith("_")]


def qualified_users(module, names):
    """
    Кто обращается к module.name — точным разбором, а не поиском подстроки.

    Подстрока здесь врёт дважды: имя `main` встречается в 80 файлах из 165, а
    `search_engine` целиком лежит внутри `search_engine_safe`. Поэтому ищется
    именно узел `Attribute(value=Name(module), attr=name)`.
    """
    hits = []
    for path in SOURCES:
        if path[:-3] == module:
            continue
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == module and node.attr in names:
                hits.append(f"{path}:{node.lineno} {module}.{node.attr}")
            elif isinstance(node, ast.ImportFrom) and node.module == module:
                hits.append(f"{path}:{node.lineno} from {module} import ...")
    return hits


def strip_comments(source):
    return "\n".join(l for l in source.split("\n") if not l.lstrip().startswith("#"))


print(f"\nв корне {len(SOURCES)} файлов .py, боевое замыкание main.py: {len(PRODUCTION)} модулей")
print(f"инструментов, которых никто не импортирует: {len(unreferenced_tools(GRAPH, PRODUCTION))}")

print("\n[1] Каждый мёртвый инструмент действительно мёртв")
for module, what in sorted(DEAD.items()):
    path = module + ".py"
    if not os.path.exists(path):
        check(f"{path} удалён — учёт больше не нужен", True)
        continue
    tree = parse(path)
    lines = len(io.open(path, encoding="utf-8-sig").read().split("\n"))
    print(f"      {path}: {lines} строк — {what}")

    importers = sorted(m for m, deps in GRAPH.items() if module in deps and m != module)
    check(f"{path} никем не импортируется", not importers,
          f"импортируют {importers} — модуль ожил, реестр надо обновить")

    # Второй, независимый метод: текст с вырезанными строками-комментариями.
    # Разбор через ast и поиск по тексту ломаются по-разному, и если ослабнет
    # обход графа (как уже было с ленивыми импортами), текстовая проверка
    # удержит факт.
    pattern = re.compile(rf"^\s*(?:import\s+{re.escape(module)}\b|from\s+{re.escape(module)}\s+import)", re.M)
    textual = sorted(p for p in SOURCES if p[:-3] != module
                     and pattern.search(strip_comments(io.open(p, encoding="utf-8-sig").read())))
    check(f"{path} не импортируется и по тексту", not textual, f"нашлось в {textual}")

    names = public_names(tree)
    users = qualified_users(module, names)
    check(f"{path}: ни одна его функция не вызывается ({names})", not users, f"обращения: {users}")

    check(f"{path} вне боевого замыкания main.py", module not in PRODUCTION,
          "мёртвый инструмент оказался в боевом пути")

print("\n[2] Каждый мёртвый инструмент безопасен при импорте")
for module in sorted(DEAD):
    path = module + ".py"
    if not os.path.exists(path):
        continue
    tree = parse(path)
    writes, dbs, out = module_level_writes(tree), module_level_db(tree), module_level_outbound(tree)
    check(f"{path} не пишет на диск при импорте", not writes, f"open на запись: {writes}")
    check(f"{path} не открывает базу при импорте", not dbs, f"connect на уровне модуля: {dbs}")
    check(f"{path} не уходит в сеть при импорте", not out, f"вызовы: {out}")

# Что именно выполняется при импорте — в вывод, чтобы «безопасен» не был
# голословным. Замер 29.07.2026: у четырёх инструментов на уровне модуля пусто,
# у search_engine.py два вызова — logging.getLogger и TavilyClient. Конструктор
# TavilyClient в сеть НЕ ходит (прочитан исходник установленного пакета: он
# собирает requests.Session и заголовки), поэтому проверка выше зелёная честно.
for module in sorted(DEAD):
    path = module + ".py"
    if os.path.exists(path):
        names = sorted({name for name, _ in calls_in(top_level_body(parse(path))) if name})
        print(f"      {path} на уровне модуля вызывает: {names or 'ничего'}")

# search_engine.py — единственный без точки входа: запускать его нечем, а
# импортировать нельзя. Это и есть определение мёртвого груза, и если у него
# однажды появится __main__, файл перестанет быть мёртвым молча.
if os.path.exists("search_engine.py"):
    tree = parse("search_engine.py")
    has_main = any(isinstance(s, ast.If) and "__main__" in ast.dump(s.test) for s in tree.body)
    check("search_engine.py по-прежнему без точки входа __main__", not has_main,
          "появился запуск — файл больше не мёртвый груз, реестр надо обновить")

print("\n[3] Реестр полон: новый мёртвый инструмент не растворится в корне")
unreferenced = unreferenced_tools(GRAPH, PRODUCTION)
registered = set(DEAD) | set(STANDALONE)
unknown = [m for m in unreferenced if m not in registered]
check("в корне нет незарегистрированного никем не подключённого инструмента", not unknown,
      f"{unknown} — либо это мёртвый груз (в DEAD), либо самостоятельный скрипт (в STANDALONE с причиной)")

stale = sorted(m for m in registered if m in KNOWN and m not in unreferenced and os.path.exists(m + ".py"))
check("в реестре нет модуля, который на самом деле подключён", not stale,
      f"{stale} — кто-то их импортирует, запись в реестре устарела")

check("мёртвые и самостоятельные не пересекаются", not (set(DEAD) & set(STANDALONE)),
      f"в двух разделах сразу: {sorted(set(DEAD) & set(STANDALONE))}")
check("ни один мёртвый инструмент не входит в боевое замыкание",
      not (set(DEAD) & PRODUCTION), f"пересечение: {sorted(set(DEAD) & PRODUCTION)}")
check("у каждой записи реестра есть причина",
      all(len(v) > 15 for v in list(DEAD.values()) + list(STANDALONE.values())),
      "пустая причина превращает реестр в список имён")

print(f"      незарегистрированных: {unknown or 'нет'}; мёртвых {len(DEAD)}, "
      f"самостоятельных {len(STANDALONE)}, всего без ссылок {len(unreferenced)}")

print("\n[4] Проверки выше ловят подсаженную поломку")
# Реестр, который не замечает нового мёртвого файла, бесполезен. Проверяем это на
# подсаженном графе, а не создавая файл в корне: рядом работают другие агенты.
_fake = {"main": {"assistant"}, "assistant": set(), "zombie": set(),
         "prompter": set(), "test_x": {"assistant"}, "_probe_y": set()}
_fake_production = closure("main", _fake)
_found = unreferenced_tools(_fake, _fake_production)
check("детектор видит новый мёртвый инструмент в корне", "zombie" in _found, f"вернулось {_found}")
check("детектор не считает мёртвым боевой модуль", "assistant" not in _found, f"вернулось {_found}")
check("детектор не считает мёртвым тест", "test_x" not in _found, f"вернулось {_found}")
check("детектор не считает мёртвым черновик агента", "_probe_y" not in _found, f"вернулось {_found}")

_wired = dict(_fake)
_wired["main"] = {"assistant", "zombie"}
check("подключённый инструмент перестаёт считаться мёртвым",
      "zombie" not in unreferenced_tools(_wired, closure("main", _wired)),
      "реестр не заметит оживления модуля")

_lazy = dict(_fake)
_lazy["prompter"] = {"zombie"}
check("подключение из другого инструмента тоже снимает признак мёртвого",
      "zombie" not in unreferenced_tools(_lazy, closure("main", _lazy)),
      "учитывается только импорт из боевого пути — этого мало")

# Детекторы опасности при импорте: образцы разбираются как текст, не исполняются.
_DANGER = {
    "запись файла": ('open("out.json", "w")\n', module_level_writes),
    "запись дописыванием": ('open("bot.log", "a")\n', module_level_writes),
    "запись через mode=": ('open("x", mode="w")\n', module_level_writes),
    "соединение с базой": ('import sqlite3\nsqlite3.connect("stomat_archive.db")\n', module_level_db),
    "соединение через aiosqlite": ('import aiosqlite\naiosqlite.connect("stomat_archive.db")\n', module_level_db),
    "сетевой запрос": ('import requests\nrequests.get("https://example.org")\n', module_level_outbound),
    "запуск процесса": ('import subprocess\nsubprocess.Popen(["x"])\n', module_level_outbound),
}
for _why, (_src, _detector) in _DANGER.items():
    check(f"детектор видит: {_why}", bool(_detector(ast.parse(_src))), "образец прошёл незамеченным")

_SAFE = {
    "чтение файла": ('open("x")\n', module_level_writes),
    "то же, но внутри функции": ('def go():\n    open("out.json", "w")\n', module_level_writes),
    "база внутри функции": ('import sqlite3\ndef go():\n    sqlite3.connect("stomat_archive.db")\n', module_level_db),
    "сеть под __main__": ('import requests\nif __name__ == "__main__":\n    requests.get("https://example.org")\n',
                          module_level_outbound),
}
for _why, (_src, _detector) in _SAFE.items():
    check(f"детектор молчит на: {_why}", not _detector(ast.parse(_src)),
          "ложная тревога — набор станет красным на безопасном коде")

# Проверка на подстроку соврала бы: search_engine целиком лежит внутри
# search_engine_safe, а `main` есть почти в каждом файле. Разбор через ast обязан
# эти два случая различать, иначе реестр объявит мёртвое живым.
check("квалифицированный поиск не путает search_engine и search_engine_safe",
      not [h for h in qualified_users("search_engine", ["perform_search"])
           if "search_engine_safe" in h],
      "совпадение подстрокой принято за вызов")

print("\n[5] Все файлы корня действительно разобраны")
# Не разобранный файл выглядит как пустой модуль: он и «никого не импортирует», и
# «ничего не делает при импорте». То есть битый .py в корне тихо выводит себя
# из-под реестра.
check("ни один файл не выпал из разбора", not UNPARSED,
      f"не разобраны (значит и не учтены): {UNPARSED}")
print(f"      разобрано файлов: {len(_TREES)} из {len(SOURCES)}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
