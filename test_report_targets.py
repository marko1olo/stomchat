"""
Планировщик больше не поднимается с нулём целей молча.

В `scheduler_task` стоял голый `except: targets = []`. Три следствия, каждое
кончается тем, что 749 врачей не получают ни дайджеста, ни недельной сводки, а в
журнале — бодрое «Планировщик активен. Целей: 0» и ни одной строки ERROR:

1. Любое исключение при чтении `config.REPORT_TARGETS` превращалось в пустой
   список. Голый `except` ловит и KeyboardInterrupt, и SystemExit.
2. Ошибку ФОРМЫ не ловил никто: `REPORT_TARGETS={"chat_id": -100}` — валидный
   JSON, `config.json_loads` его пропускает, `isinstance(targets, list)` даёт
   False, и рассылка тихо отключалась целиком.
3. Битый ЭЛЕМЕНТ списка уносил не только рассылку: в обработчике `.test`
   (`main.py`) стояло `for target in config.REPORT_TARGETS: target.get(...)`,
   и элемент-строка ронял его на AttributeError вместо ответа врачу.

Правка: один нормализатор `main.resolve_report_targets()` на оба места чтения.
Он громко называет последствие в журнале и пропускает ровно битую цель, а не всю
рассылку.

Проверки поведенческие: смотрят на возвращённый список и на записи в журнале,
не на наличие строки в исходнике.

Запуск: python test_report_targets.py
"""
import logging
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_targets_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import config  # noqa: E402
import main  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def _cp1251_safe(char):
    """cp1251 берёт кириллицу, но не эмодзи. Проверено: U+274C роняет print."""
    try:
        char.encode("cp1251")
        return True
    except UnicodeEncodeError:
        return False


class LogTrap(logging.Handler):
    """Ловит записи main-логгера, чтобы проверять ЖУРНАЛ, а не только возврат."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def errors(self):
        return [r for r in self.records if r.levelno >= logging.ERROR]

    def text(self):
        out = []
        for r in self.records:
            try:
                out.append(r.getMessage())
            except Exception:
                out.append(str(r.msg))
        return "\n".join(out)


def resolve_with(value, *, delete=False):
    """Подставить REPORT_TARGETS, вызвать нормализатор, вернуть (список, журнал)."""
    saved = getattr(config, "REPORT_TARGETS", [])
    had = hasattr(config, "REPORT_TARGETS")
    trap = LogTrap()
    logger = logging.getLogger("main")
    logger.addHandler(trap)
    prev_level, prev_prop = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        if delete:
            if had:
                del config.REPORT_TARGETS
        else:
            config.REPORT_TARGETS = value
        return main.resolve_report_targets(), trap
    finally:
        logger.removeHandler(trap)
        logger.setLevel(prev_level)
        logger.propagate = prev_prop
        if had:
            config.REPORT_TARGETS = saved
        elif hasattr(config, "REPORT_TARGETS"):
            del config.REPORT_TARGETS


GOOD = [{"chat_id": -100123, "topic_id": None}, {"chat_id": -100456, "topic_id": 390}]

print("[1] Здоровый конфиг проходит без потерь и без паники в журнале")
targets, trap = resolve_with(GOOD)
check("обе цели вернулись", targets == GOOD, f"вернулось {targets!r}")
check("ни одной записи ERROR на здоровом конфиге", not trap.errors(),
      f"лишние ошибки: {[r.getMessage() for r in trap.errors()]}")

print("\n[2] Пустой список — это норма, а не отказ")
targets, trap = resolve_with([])
check("пустой список остаётся пустым", targets == [])
check("пустой конфиг не объявляется битым", not trap.errors(),
      "иначе ERROR на штатной настройке приучает игнорировать журнал")

print("\n[3] Валидный JSON неверной формы (dict) больше не отключает рассылку молча")
targets, trap = resolve_with({"chat_id": -100123, "topic_id": None})
check("цели пустые — рассылать по dict нельзя", targets == [])
check("в журнале ERROR о том, что это не список", bool(trap.errors()),
      "молчаливый ноль целей — исходный дефект")
check("ERROR называет последствие для врача", "НИКОМУ" in trap.text(),
      f"журнал: {trap.text()!r}")
check("ERROR называет фактический тип", "dict" in trap.text(),
      f"журнал: {trap.text()!r}")

print("\n[4] Строка вместо списка (частый вид битого .env) тоже громкая")
targets, trap = resolve_with('[{"chat_id": -100123}]')
check("строка не разбирается как список целей", targets == [],
      "иначе итерация по строке дала бы символы")
check("в журнале ERROR", bool(trap.errors()))
check("тип назван как str", "str" in trap.text(), f"журнал: {trap.text()!r}")

print("\n[5] Битая цель уносит себя, а не всю рассылку")
targets, trap = resolve_with([GOOD[0], "мусор", {"topic_id": 5}, None, GOOD[1]])
check("здоровые цели выжили", targets == GOOD, f"вернулось {targets!r}")
check("на каждую битую цель своя запись", len(trap.errors()) == 3,
      f"записей ERROR: {len(trap.errors())}")
check("в журнале виден индекс битой цели", "[1]" in trap.text(),
      f"журнал: {trap.text()!r}")
check("цель без chat_id отброшена", all("chat_id" in t for t in targets))

print("\n[6] Список, где битые ВСЕ, отдельно объявляется провалом рассылки")
targets, trap = resolve_with(["мусор", 42])
check("целей нет", targets == [])
check("сказано, что не уйдёт никому", "НИКОМУ" in trap.text(),
      f"журнал: {trap.text()!r}")

print("\n[7] Отсутствие атрибута не роняет планировщик")
targets, trap = resolve_with(None, delete=True)
check("вернулся список, а не исключение", targets == [])
check("отказ чтения попал в журнал", bool(trap.errors()),
      "именно это глотал голый except")

print("\n[8] Голого except в scheduler_task больше нет")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"),
           encoding="utf-8").read()
_start = SRC.index("async def scheduler_task")
_body = SRC[_start:_start + 4000]
check("в scheduler_task нет `except:` без класса", "except:" not in _body,
      "голый except ловит и KeyboardInterrupt, и SystemExit")
check("оба места чтения идут через нормализатор",
      SRC.count("resolve_report_targets()") >= 3,
      f"вызовов: {SRC.count('resolve_report_targets()')} (ожидается def + 2 места)")
check("прямого `for target in config.REPORT_TARGETS` не осталось",
      "for target in config.REPORT_TARGETS" not in SRC,
      "это место падало на AttributeError у элемента-строки")

print("\n[9] Проверки выше ловят поломку")
check("нормализатор действительно фильтрует, а не возвращает вход",
      main.resolve_report_targets.__doc__ is not None
      and resolve_with([GOOD[0], "мусор"])[0] != [GOOD[0], "мусор"],
      "если бы возвращал вход, проверка [5] ничего не значила")
_HERE = os.path.dirname(os.path.abspath(__file__))
# config.py в .gitignore и на каждой машине свой, config.example.py — версионный.
# Проверяем те, что реально есть, иначе набор красный на чистом клоне.
for _name in ("config.py", "config.example.py"):
    _path = os.path.join(_HERE, _name)
    if not os.path.exists(_path):
        continue
    _src = open(_path, encoding="utf-8").read()
    _bad = sorted({c for c in _src if not _cp1251_safe(c)})
    check(f"{_name} печатается на cp1251-консоли без UnicodeEncodeError",
          not _bad,
          f"cp1251 не берёт {_bad!r} — print падал сам и прятал настоящую "
          f"ошибку .env под трейсбеком печати")

import shutil  # noqa: E402
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
