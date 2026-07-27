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

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
