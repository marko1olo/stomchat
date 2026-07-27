"""
/stats: настоящий подсчёт тем вместо статичного текста.

Раньше команда отдавала жёстко зашитый список с числами вида «~5 400
упоминаний» и подписью «на основе анализа 117 000+ сообщений». Числа не
менялись никогда и к сегодняшнему дню разошлись с архивом: имплантация была
занижена в 2.3 раза, а порядок неверен — имплантация обогнала эндодонтию.
Колонки category_l1/l2/l3, на которые это могло опираться, в архиве пусты:
0 из 117 847.

Отдельно проверяется граница слова. Подстрочный поиск «кт» ловит «доктор»,
«практика», «который»: по подстроке тема «Диагностика и снимки» выходила на
первое место с 12 093 упоминаниями вместо реальных 1 322.

Считает на СИНТЕТИЧЕСКИХ базах, боевые не открываются.

Запуск: python test_chat_statistics.py
"""
import asyncio
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_stats_")
config.DB_PATH = os.path.join(_TMPDIR, "messages.db")

import assistant

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def build_db(path, table, texts):
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE {table} (msg_id INTEGER PRIMARY KEY, text TEXT)")
    db.executemany(f"INSERT INTO {table} (text) VALUES (?)", [(t,) for t in texts])
    db.commit()
    db.close()


print("\n[1] Граница слова: аббревиатура не ловится внутри обычных слов")
diagnostics = assistant._STATS_PATTERNS["📸 Диагностика и снимки"]
for word in ("доктор", "практика", "который", "актуально", "факт", "контакт"):
    check(f"«{word}» не считается за КТ", diagnostics.search(word) is None)
for phrase in ("нужно КТ", "кт-снимок приложил", "сделал снимок", "рентген показал"):
    check(f"«{phrase}» распознано", diagnostics.search(phrase) is not None)

print("\n[2] Термин ловится как начало слова, включая склонения")
ortho = assistant._STATS_PATTERNS["👑 Ортопедия и коронки"]
for phrase in ("поставил коронку", "коронки на 6 и 7", "циркониевый каркас", "винир на 11"):
    check(f"«{phrase}» распознано", ortho.search(phrase) is not None)
check("«микрокоронка» не считается — термин не в начале слова",
      ortho.search("микрокоронка") is None)

print("\n[3] Подсчёт по синтетическим базам совпадает с ожидаемым")
archive_texts = [
    "поставил коронку из циркония",
    "спорим про уступ и вертипреп",
    "какая анестезия при пульпите, артикаин?",
    "просто болтовня без терминов",
    "",
    None,
]
live_texts = [
    "имплант поставили вчера",
    "коронка сколола",
    "доктор, вы тут?",          # ловушка на подстроку «кт»
]
build_db(os.path.join(_TMPDIR, "archive.db"), "archive_messages", archive_texts)
build_db(config.DB_PATH, "messages", live_texts)

original_cwd = os.getcwd()
os.chdir(_TMPDIR)
os.rename("archive.db", "stomat_archive.db")


async def run():
    counts, scanned = await assistant.get_topic_statistics(force=True)
    check("пустые и NULL строки не считаются", scanned == 7, f"got {scanned}")
    check("ортопедия посчитана", counts["👑 Ортопедия и коронки"] == 2,
          f"got {counts['👑 Ортопедия и коронки']}")
    check("препарирование посчитано", counts["📐 Препарирование и уступ"] == 1,
          f"got {counts['📐 Препарирование и уступ']}")
    check("анестезия посчитана", counts["💉 Анестезия"] == 1, f"got {counts['💉 Анестезия']}")
    check("имплантация посчитана", counts["🔩 Имплантация"] == 1, f"got {counts['🔩 Имплантация']}")
    check("«доктор» не попал в диагностику", counts["📸 Диагностика и снимки"] == 0,
          f"got {counts['📸 Диагностика и снимки']}")

    print("\n[4] Кэш работает и не пересчитывает заново")
    first = await assistant.get_topic_statistics()
    build_db(os.path.join(_TMPDIR, "extra.db"), "messages", ["коронка", "коронка"])
    second = await assistant.get_topic_statistics()
    check("повторный вызов отдаёт то же самое", first == second)
    check("кэш заполнен", assistant._stats_cache["payload"] is not None)
    check("force=True пересчитывает",
          (await assistant.get_topic_statistics(force=True))[1] == 7)

    print("\n[5] Отрисовка")
    text = assistant.render_topic_statistics(counts, scanned)
    check("темы отсортированы по убыванию",
          text.index("Ортопедия") < text.index("Анестезия"), "порядок неверен")
    check("пустые темы не показываются", "Эндодонтия" not in text, f"got {text}")
    check("указано реальное число сообщений", "7" in text, f"got {text}")
    check("есть оговорка про метод подсчёта", "одно сообщение может попасть" in text)
    check("разметка валидна", markup_ok(text), "разметка сломана")

    print("\n[6] Пустая статистика — честный отказ, а не нули")
    check("нулевой скан не отрисовывается", assistant.render_topic_statistics({}, 0) is None)
    check("все нули не отрисовываются",
          assistant.render_topic_statistics({label: 0 for label in assistant.STATS_TOPICS}, 100) is None)

    print("\n[7] Статичного текста в команде больше нет")
    source = io.open(os.path.join(original_cwd, "assistant.py"), encoding="utf-8").read()
    stats_handler = source.split('if text.lower() == "/stats":', 1)[1].split("return", 1)[0]
    check("нет захардкоженных «~5,400+»", "5,400" not in stats_handler, "статика осталась")
    check("нет захардкоженного «117,000+»", "117,000" not in stats_handler)
    check("вызывается настоящий расчёт", "get_topic_statistics" in stats_handler)


def markup_ok(text):
    stack = []
    for closing, name in re.findall(r"<(/?)([a-zA-Z]+)", text):
        if closing:
            if name in stack:
                del stack[stack.index(name):]
            else:
                return False
        else:
            stack.append(name)
    return not stack


try:
    asyncio.run(run())
finally:
    os.chdir(original_cwd)
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
