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


def parse(path):
    return ast.parse(io.open(path, encoding="utf-8-sig").read())


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


print("\n[1] Ни один модуль не переписывает исходник при импорте")
# Именно это испортило assistant.py. Проверяем ВСЕ файлы, включая инструменты:
# опасен сам факт, что кто-то может их импортировать.
#
# Ловим запись именно в .py. Тесты пишут временные файлы на уровне модуля
# законно (фикстуры во временном каталоге), и запрещать это здесь незачем —
# за тем, чтобы они не трогали БОЕВЫЕ файлы, следит test_isolation.py.
for path in SOURCES:
    tree = parse(path)
    if guards_import(tree):
        continue
    writers = []
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
        if not any(flag in mode for flag in ("w", "a", "+", "x")):
            continue
        target = node.args[0] if node.args else None
        is_source = isinstance(target, ast.Constant) and str(target.value).endswith(".py")
        if is_source:
            writers.append((node.lineno, target.value))
    check(f"{path} не переписывает .py при импорте", not writers,
          f"open(..., 'w') на уровне модуля: {writers}")

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

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
