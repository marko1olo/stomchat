"""
Единый прогон всех наборов проверок с контролем целостности боевых файлов.

Зачем отдельный запускающий скрипт. Прогон собирался руками из длинной строки
bash: цикл по test_*.py, подсчёт [OK]/[FAIL] через grep, md5 боевых файлов до и
после. Строку приходилось перенабирать каждый раз, список охраняемых файлов
дрейфовал от прогона к прогону, а забытый файл в списке означал незамеченную
порчу. Дважды за работу тесты писали в боевые файлы — assistant_state.json и
bot_summary_status.json, — и поймала это ровно такая сверка.

Что делает:
  * запускает каждый test_*.py отдельным процессом с таймаутом;
  * считает пройденные и провалённые проверки по строкам вывода;
  * до и после прогона снимает md5 боевых файлов и исходников;
  * печатает сводку и возвращает ненулевой код при любом провале или изменении.

Журнал уводится в bot_test.log автоматически: runtime_guard смотрит на имя
точки входа. Здесь имя не начинается с test_, поэтому переменную ставим сами —
иначе прогон писал бы в боевой bot.log.

Запуск:  python run_all_tests.py            все наборы
         python run_all_tests.py wiki rag   только совпадающие по имени
"""
import hashlib
import io
import os
import subprocess
import sys
import time

os.environ.setdefault("STOMCHAT_LOG_PATH", "bot_test.log")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TEST_TIMEOUT_SECONDS = 600

# Файлы, которые прогон НЕ имеет права менять. Боевые данные плюс исходники:
# после того как импорт одного модуля переписал assistant.py, контроль
# исходников стал обязательным, а не избыточным.
GUARDED = [
    "assistant_state.json",
    "bot_state.json",
    "bot_summary_status.json",
    "bot_heartbeat.json",
    "bot.log",
    "stomat_bot.db",
    "stomat_wiki.db",
    "stomat_archive.db",
    "assistant.py",
    "main.py",
    "database.py",
    "summarizer.py",
    "runtime_guard.py",
    "gemini_client.py",
    "vision.py",
    "blocking_tools.py",
    "dental_vocab.py",
    "html_safe.py",
    "media_tools.py",
]


def digest(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.md5(handle.read()).hexdigest()
    except OSError:
        return None


def snapshot():
    return {path: digest(path) for path in GUARDED if os.path.exists(path)}


def count_checks(output):
    passed = failed = 0
    failures = []
    for line in output.split("\n"):
        if line.startswith("  [OK  ]"):
            passed += 1
        elif line.startswith("  [FAIL]"):
            failed += 1
            failures.append(line.strip())
    return passed, failed, failures


def main():
    patterns = [arg.lower() for arg in sys.argv[1:]]
    tests = sorted(f for f in os.listdir(".") if f.startswith("test_") and f.endswith(".py"))
    if patterns:
        tests = [t for t in tests if any(p in t.lower() for p in patterns)]
    if not tests:
        print("наборы не найдены")
        return 1

    before = snapshot()
    total_passed = total_failed = 0
    broken = []
    crashed = []
    started = time.monotonic()

    for name in tests:
        began = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, name],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=TEST_TIMEOUT_SECONDS,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            code = proc.returncode
        except subprocess.TimeoutExpired:
            output, code = "", -1
            crashed.append(f"{name}: превышен таймаут {TEST_TIMEOUT_SECONDS} с")

        passed, failed, failures = count_checks(output)
        total_passed += passed
        total_failed += failed
        spent = time.monotonic() - began

        if failed:
            broken.append((name, failures))
            mark = "ПРОВАЛ"
        elif code != 0 and not failures:
            # Ненулевой код без единой строки [FAIL] — набор упал до проверок:
            # синтаксис, импорт, исключение на уровне модуля.
            crashed.append(f"{name}: код выхода {code}")
            mark = "УПАЛ"
        else:
            mark = "ок"

        print(f"  {mark:6} {name:34} проверок {passed + failed:4}  {spent:5.1f} с")

    after = snapshot()
    changed = [p for p in set(before) | set(after) if before.get(p) != after.get(p)]

    print("\n" + "=" * 66)
    print(f"наборов: {len(tests)}   проверок: {total_passed + total_failed}   "
          f"провалено: {total_failed}   время: {time.monotonic() - started:.0f} с")

    if broken:
        print("\nПРОВАЛЕННЫЕ ПРОВЕРКИ:")
        for name, failures in broken:
            print(f"  {name}")
            for line in failures:
                print(f"     {line}")

    if crashed:
        print("\nНАБОРЫ, УПАВШИЕ ДО ПРОВЕРОК:")
        for item in crashed:
            print(f"  {item}")

    if changed:
        print("\nПРОГОН ИЗМЕНИЛ ОХРАНЯЕМЫЕ ФАЙЛЫ:")
        for path in sorted(changed):
            print(f"  {path}: {before.get(path)} -> {after.get(path)}")
        print("  Это нарушение изоляции: тест пишет туда, куда не должен.")
    else:
        print("\nохраняемые файлы целы: " + str(len(before)) + " шт, md5 совпал")

    return 1 if (total_failed or crashed or changed) else 0


if __name__ == "__main__":
    sys.exit(main())
