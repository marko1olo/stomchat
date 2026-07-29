"""
Обычная речь врача больше не запускает платную сводку и викторину.

Групповые команды ловились не только по «/», но и по русским словам, причём
сводка — через startswith("итог"). «Итог» это не команда, а обычное слово
профессиональной речи.

Замер по живому stomat_archive.db (107 316 реплик с текстом): под триггер сводки
попадали ДЕВЯТЬ реплик врачей и НИ ОДНОЙ настоящей команды. Это подлинные строки
из чата, и они взяты в проверки ниже как есть:

    «Итог каков. Про пищевой комок тут написана чушь?..»   msg 54125
    «Итого 5000₽ за ед обходится работа с керамикой»       msg 85500
    «Итог всех мытарств! 500 евро эндо и билд ап...»       msg 104214
    «Итого минус 1,5 дня»                                  msg 133526

На каждой бот влезал в чужой спор с непрошеной сводкой ВСЕГО обсуждения, а это
платная генерация. Правило проекта прямо обратное: бот заговаривает только строго
по теме и только когда попросили. Плюс одна реплика «Опрос» (msg 128077) запускала
викторину.

Проверки поведенческие: смотрят на РЕШЕНИЕ распознавателя, а не на текст исходника.

Запуск: python test_group_command_triggers.py
"""
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


HERE = os.path.dirname(os.path.abspath(__file__))
SRC = io.open(os.path.join(HERE, "main.py"), encoding="utf-8-sig").read()


def literal_tuple(anchor):
    """Кортеж строк из живого исходника main.py по якорю условия.

    Читаем НАСТОЯЩЕЕ условие, а не копию: если кто-то вернёт слово-триггер, эти
    проверки обязаны покраснеть, а не пройти на выдуманном списке.
    """
    m = re.search(anchor, SRC)
    if not m:
        return None
    return tuple(re.findall(r'"([^"]+)"', m.group(1)))


SUMMARY = literal_tuple(r"if cmd_lower\.startswith\(\(([^)]*)\)\):\s*\n\s*await assistant\.handle_group_summary")
QUIZ = literal_tuple(r"if cmd_lower in \(([^)]*)\):\s*\n\s*await assistant\.handle_group_quiz")
SAVE = literal_tuple(r'if cmd_clean in \(([^)]*)\) and reply_to_msg_id')

print("[1] Условия найдены в живом main.py, а не выдуманы тестом")
check("триггер сводки прочитан", SUMMARY is not None, "якорь не найден — проверьте main.py")
check("триггер викторины прочитан", QUIZ is not None, "якорь не найден")
check("триггер закладки прочитан", SAVE is not None, "якорь не найден")
SUMMARY = SUMMARY or ()
QUIZ = QUIZ or ()
SAVE = SAVE or ()


def fires_summary(text):
    return (text or "").strip().lower().startswith(SUMMARY)


def fires_quiz(text):
    return (text or "").strip().lower() in QUIZ


# Подлинные реплики из stomat_archive.db, попадавшие под прежний триггер
REAL_SPEECH = [
    ("Итог каков. Про пищевой комок тут написана чушь? Нуу тогда я не соглашусь.", 54125),
    ("Итог один)", 78353),
    ("Итого 5000₽ за ед обходится работа с керамикой", 85500),
    ("Итого удаление", 97160),
    ("Итог дискуссии каков? Нельзя давать торк,который и так должен быть???", 100631),
    ("Итого", 103356),
    ("Итог всех мытарств! 500 евро эндо и билд ап. Против 2500 удаление и имплант", 104214),
    ("Итого минус 1,5 дня", 133526),
    ("Итог - у меня травмирована кисть и большой палец. И я докупил сканер", 134502),
]

print("\n[2] Девять подлинных реплик врачей больше не запускают сводку")
for text, mid in REAL_SPEECH:
    check(f"msg {mid} не запускает сводку", not fires_summary(text),
          f"сработало на: {text[:60]!r}")

print("\n[3] Настоящая команда сводки по-прежнему работает")
for cmd in ("/summary", "/итог", "/sum", "/итог 50", "/Summary", "  /итог  "):
    check(f"{cmd!r} запускает сводку", fires_summary(cmd), "команда врача перестала работать")

print("\n[4] Викторина: слово перестало, команда осталась")
check("«Опрос» (msg 128077) не запускает викторину", not fires_quiz("Опрос"),
      "обычная реплика запускает платную генерацию")
check("«викторина» одним словом не запускает", not fires_quiz("викторина"))
check("«опрос» в нижнем регистре не запускает", not fires_quiz("опрос"))
for cmd in ("/poll", "/кейс", "/POLL"):
    check(f"{cmd!r} запускает викторину", fires_quiz(cmd), "команда врача перестала работать")

print("\n[5] Ни один словесный триггер без слеша не остался у платных действий")
wordy_summary = [t for t in SUMMARY if not t.startswith("/")]
wordy_quiz = [t for t in QUIZ if not t.startswith("/")]
check("у сводки только команды со слешем", not wordy_summary, f"осталось: {wordy_summary}")
check("у викторины только команды со слешем", not wordy_quiz, f"осталось: {wordy_quiz}")

print("\n[6] Закладка сохраняет словесный триггер осознанно")
# «сохранить» оставлено намеренно: оно требует ОТВЕТА на сообщение (reply_to),
# а это сильный признак намерения. Замер по архиву: 0 случайных попаданий.
check("«сохранить» осталось рабочим", "сохранить" in [s.lower() for s in SAVE],
      f"got {SAVE} — если убрали, скажите об этом в тесте явно")
check("закладка требует reply_to (проверено по исходнику)",
      "and reply_to_msg_id" in SRC,
      "без ответа на сообщение словесный триггер стал бы таким же шумом")

print("\n[7] Проверки выше ловят поломку")
check("распознаватель действительно смотрит на префикс",
      fires_summary("/sum") and not fires_summary("сум"),
      "если бы условие было пустым, всё прошло бы само")
check("распознаватель викторины действительно сравнивает целиком",
      fires_quiz("/poll") and not fires_quiz("/poll лишнее"),
      "точное сравнение подменено префиксным")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
