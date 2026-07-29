"""
Страховка: ни один тест не имеет права писать в боевые файлы.

Это не абстрактное правило. Два теста его нарушили и записали в живой
assistant_state.json фантомных пользователей 777 и 4242 — в бою по ним пошли
бы попытки личных сообщений в никуда. Обнаружилось только при сверке размера
файла, потому что обработчик ЛС пишет активность врача для проактивных пингов
через load_state/save_state, и тесту достаточно один раз его вызвать.

Проверка структурная: если тест дёргает обработчики, которые сохраняют
состояние, он обязан увести STATE_PATH во временный каталог. Отдельно
проверяется, что файл состояния сейчас содержит только реальных врачей.

Запуск: python test_isolation.py
"""
import ast
import io
import json
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


# Функции, любой вызов которых приводит к записи состояния.
STATE_WRITERS = (
    "handle_private_message",
    "check_and_send_pm_pings",
    "check_and_send_group_activity_pings",
    "commit_pm_ping",
    "drop_pm_ping",
    "set_ping_opt_out",
    "record_passive_attempt",
    "record_passive_success",
    "save_state",
)

TESTS = sorted(f for f in os.listdir(".") if f.startswith("test_") and f.endswith(".py"))


def strip_comments(source):
    """Код без строк-комментариев: упоминание функции в пояснении не в счёт."""
    return "\n".join(l for l in source.split("\n") if not l.lstrip().startswith("#"))


print(f"\n[1] Тесты, пишущие состояние, уводят STATE_PATH во временный каталог")
check("тесты вообще найдены", len(TESTS) >= 20, f"got {len(TESTS)}")

offenders = []
for name in TESTS:
    if name == os.path.basename(__file__):
        continue
    source = io.open(name, encoding="utf-8").read()
    code = strip_comments(source)
    if "import assistant" not in code:
        continue
    calls = [fn for fn in STATE_WRITERS if re.search(rf"\.{re.escape(fn)}\s*\(", code)]
    if not calls:
        continue
    isolated = "assistant.STATE_PATH" in code
    if not isolated:
        offenders.append((name, calls))
    check(f"{name} изолирует состояние", isolated,
          f"вызывает {calls}, но пишет в боевой файл")

check("нарушителей нет", not offenders, f"got {[n for n, _ in offenders]}")

print("\n[2] Тесты, работающие с базой, уводят DB_PATH")
db_offenders = []
for name in TESTS:
    if name == os.path.basename(__file__):
        continue
    code = strip_comments(io.open(name, encoding="utf-8").read())
    if "import database" not in code:
        continue
    # Достаточно чтения справочных функций — записи не делают только те,
    # кто не вызывает init_db/save_*.
    writes = re.search(r"database\.(init_db|save_\w+|set_\w+|delete_\w+|mark_\w+|update_\w+)\s*\(", code)
    if writes and "config.DB_PATH" not in code:
        db_offenders.append(name)
    if writes:
        check(f"{name} изолирует базу", "config.DB_PATH" in code,
              "пишет в боевую stomat_bot.db")
check("нарушителей по базе нет", not db_offenders, f"got {db_offenders}")

print("\n[3] Боевое состояние не содержит следов тестов")
state_path = "assistant_state.json"
if os.path.exists(state_path):
    state = json.load(io.open(state_path, encoding="utf-8"))
    pings = state.get("pm_pings", {})
    # Реальные Telegram-id девятизначные и длиннее; тестовые были короткими.
    phantom = [k for k in pings if k.isdigit() and len(k) < 8]
    check("фантомных пользователей нет", not phantom,
          f"в боевом состоянии остались тестовые записи: {phantom}")
    check("реальные записи на месте", len(pings) >= 1, f"got {len(pings)}")
    print(f"      записей в pm_pings: {len(pings)}")
else:
    check("боевого состояния рядом нет — проверка неприменима", True)

print("\n[4] Ни один тест не хардкодит путь к боевым файлам для записи")
for name in TESTS:
    if name == os.path.basename(__file__):
        continue
    code = strip_comments(io.open(name, encoding="utf-8").read())
    bad_write = re.search(r'open\(\s*["\'](assistant_state\.json|stomat_\w+\.db)["\']\s*,\s*["\'][wa]', code)
    check(f"{name} не открывает боевой файл на запись", bad_write is None,
          f"найдено: {bad_write.group(0) if bad_write else ''}")
    # sqlite-соединение к боевой базе БЕЗ mode=ro — это ручка НА ЗАПИСЬ, и
    # проверка выше её не видела: она искала только open(..., "w"). Замер разведки
    # нашёл ТРИ таких места (test_fix_pm2:121, test_message_splitting:197,
    # test_search_case:60), плюс этот сторож сразу нашёл четвёртое и пятое в
    # test_rag_quality. Все они делали лишь SELECT, поэтому md5 боевой вики не
    # менялся — вреда не было, но и защиты не было: первый же UPDATE по такой
    # ручке уходит в 12 784 боевых факта. Отдельно замерено, что на базе в режиме
    # WAL даже чтение через rw-соединение создаёт рядом файлы -wal и -shm.
    #
    # Разбор через ast, а НЕ регуляркой: первая версия проверки была регуляркой и
    # дала три ложные тревоги на test_import_safety и test_dead_tools, где строки
    # вида 'sqlite3.connect("stomat_archive.db")' лежат внутри python-исходника,
    # поданного детектору как ОБРАЗЕЦ. ast видит настоящий вызов, а не текст.
    rw_targets = []
    try:
        for node in ast.walk(ast.parse(code)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if fname != "connect":
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            target = first.value
            if re.search(r"stomat_\w+\.db", target) and not target.startswith("file:"):
                rw_targets.append(target)
    except SyntaxError:
        rw_targets.append("файл не разобран — проверка не выполнена")
    check(f"{name} не открывает боевую базу на запись через connect",
          not rw_targets,
          f'найдено: {rw_targets} — нужен connect("file:...?mode=ro", uri=True)')

print("\n[5] Тесты, доходящие до генерации, уводят файл статуса")
# generate_gemini_text_async снимает флаг «идёт генерация» в finally, то есть
# пишет в bot_summary_status.json даже когда сам вызов LLM заглушен. На этом
# попались test_gemini_pacing и test_snapshot_document: боевой файл менялся.
STATUS_WRITERS = (
    r"generate_gemini_text_async\s*\(",
    r"\.generate_text\s*\(",
    r"\.write_summary_status\s*\(",
    r"\.clear_summary_status\s*\(",
    r"\.release_generation_status\s*\(",
)
status_offenders = []
for name in TESTS:
    if name == os.path.basename(__file__):
        continue
    code = strip_comments(io.open(name, encoding="utf-8").read())
    hits = [p for p in STATUS_WRITERS if re.search(p, code)]
    if not hits:
        continue
    isolated = "SUMMARY_STATUS_PATH" in code
    if not isolated:
        status_offenders.append(name)
    check(f"{name} изолирует файл статуса", isolated,
          "доходит до записи флага, но пишет в боевой bot_summary_status.json")
check("нарушителей по файлу статуса нет", not status_offenders, f"got {status_offenders}")

print("\n[6] Сброс флага идёт через общую охрану, а не мимо неё")
# Правило «обычная генерация не гасит флаг идущей сводки» должно жить в одном
# месте. Прямой write_summary_status({"active": False}) в рабочем коде обходит
# его — и зависший дайджест перестаёт быть виден сторожу.
for module in ("blocking_tools.py", "gemini_client.py", "assistant.py", "summarizer.py"):
    if not os.path.exists(module):
        continue
    body = strip_comments(io.open(module, encoding="utf-8").read())
    raw = re.search(r'write_summary_status\(\s*\{\s*["\']active["\']\s*:\s*False', body)
    check(f"{module} не гасит флаг напрямую", raw is None,
          "безусловный сброс в обход release_generation_status")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
