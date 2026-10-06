"""
Просьба замолчать соблюдается ВСЕМИ путями, а не тремя из четырёх.

Проверка silenced_until была скопирована в трёх местах, и четвёртый путь —
триггер упоминания «бот» — её потерял. Хуже всего то, что этот путь вызывается
РОВНО ТОГДА, когда основной ассистент промолчал, а при активной тишине тот
молчит именно из-за неё: флаг тишины сам передавал управление пути, который его
не смотрел.

Замер по живому архиву, последовательность 2025-06-05:

    09:37:06  «Бот очень назойливый мне не нравится»
              -> бот извинился, silenced_until = +4 часа
    09:41:44  «Какой бот советуете использовать?» (про ЧУЖОГО бота)
              -> проходит регулярку упоминания, тишина не проверяется

В четырёхчасовом окне тишины лежит 138 сообщений, 14 из них задевают регулярку
упоминания — тринадцать попыток нарушить только что данное обещание. Для чата
практикующих врачей это прямое нарушение договорённости: попросили замолчать,
бот замолчал на четыре минуты.

Проверка статическая плюс прямая проверка помощника: ничего не отправляется.

Запуск: python test_silence_all_paths.py
"""
import ast
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_sil_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import assistant as A  # noqa: E402

A.STATE_PATH = os.path.join(_TMPDIR, "state.json")
A.STATE_TMP_PATH = A.STATE_PATH + ".tmp"
A.STATE_BAK_PATH = A.STATE_PATH + ".bak"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCE = io.open("assistant.py", encoding="utf-8").read()
LINES = SOURCE.split("\n")
TREE = ast.parse(SOURCE)
FUNCS = {fn.name: fn for fn in ast.walk(TREE)
         if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef))}


def body_of(name):
    fn = FUNCS.get(name)
    if not fn:
        return ""
    return "\n".join(LINES[fn.lineno - 1:getattr(fn, "end_lineno", fn.lineno)])


print("\n[1] Помощник тишины работает по существу")
now = datetime.now()
check("активная тишина распознана",
      A.is_silenced({"silenced_until": (now + timedelta(hours=1)).isoformat()}) is True)
check("истёкшая тишина не держит",
      A.is_silenced({"silenced_until": (now - timedelta(minutes=1)).isoformat()}) is False)
check("отсутствие метки — не тишина", A.is_silenced({}) is False)
# Битая метка не должна глушить бота НАВСЕГДА: это было бы хуже дефекта.
check("битая метка не глушит бота навсегда",
      A.is_silenced({"silenced_until": "не дата вовсе"}) is False)
check("пустая метка не глушит", A.is_silenced({"silenced_until": ""}) is False)
check("None в метке не роняет", A.is_silenced({"silenced_until": None}) is False)

print("\n[2] Тишину проверяют ВСЕ пути, которые могут заговорить")
SPEAKING = [
    ("check_and_trigger_assistant", "пассивный ответ в чате"),
    ("check_and_trigger_assistant_media", "разбор снимка в чате"),
    ("check_and_trigger_referee", "рефери"),
    ("check_bot_mention_trigger", "упоминание «бот» в чате"),
]
for name, human in SPEAKING:
    check(f"{human} проверяет тишину", "is_silenced(" in body_of(name),
          f"{name}: бот заговорит, хотя его просили молчать")

print("\n[3] Проверка живёт в одном месте, а не копиями")
CODE = "\n".join(l for l in LINES if not l.lstrip().startswith("#"))
check("встроенных копий разбора метки не осталось",
      CODE.count('silenced_until_str = state.get') <= 1,
      "копия неизбежно снова разъедется — так и потерялся четвёртый путь")
check("помощник объявлен один раз", CODE.count("def is_silenced(") == 1)
check("все четыре пути зовут помощника",
      CODE.count("is_silenced(") >= 5,
      f"вызовов {CODE.count('is_silenced(')} при четырёх путях плюс объявление")

print("\n[4] Путь упоминания перекрыт именно там, где обходил")
mention = body_of("check_bot_mention_trigger")
check("проверка стоит до генерации ответа",
      mention.index("is_silenced(") < mention.index("generate_gemini_text_async"),
      "тишина проверяется после платного вызова модели")
check("проверка стоит до триажа",
      "is_silenced(" in mention.split("BOT_MENTION_SHADOW_MODE", 1)[0],
      "триаж успеет отработать и потратить вызов")
check("состояние читается свежим, а не переданным",
      "is_silenced(load_state()" in mention,
      "переданное состояние может быть протухшим к моменту проверки")

print("\n[5] Извинение и выставление тишины на месте")
silence_fn = body_of("check_and_apply_silence")
check("тишина выставляется на четыре часа", "hours=4" in silence_fn)
check("бот извиняется, а не молчит внезапно",
      "умолкаю" in silence_fn, "врач не поймёт, услышали ли его")

print("\n[6] Проверки выше ловят поломку")
check("детектор пропажи проверки сработал бы",
      "is_silenced(" not in "async def foo():\n    return False")
check("помощник действительно различает состояния",
      A.is_silenced({"silenced_until": (now + timedelta(hours=1)).isoformat()})
      != A.is_silenced({}),
      "если бы не различал, вся секция [1] ничего не значила")
# Ровно тот случай из архива: тишина активна, прошло 4 минуты 38 секунд.
started = now - timedelta(minutes=4, seconds=38)
check("случай из архива: через 4 минуты бот всё ещё молчит",
      A.is_silenced({"silenced_until": (started + timedelta(hours=4)).isoformat()}) is True,
      "именно так он и заговорил снова")

import shutil  # noqa: E402

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
