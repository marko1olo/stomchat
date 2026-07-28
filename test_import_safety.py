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


# Замыкание импортов от main.py: ровно эти модули поднимают бота.
PRODUCTION = {
    "assistant", "blocking_tools", "config", "database", "dental_vocab",
    "gemini_client", "html_safe", "main", "media_tools", "runtime_guard",
    "summarizer", "vision",
}

SOURCES = sorted(f for f in os.listdir(".") if f.endswith(".py"))


def parse(path):
    return ast.parse(io.open(path, encoding="utf-8-sig").read())


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

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
