"""
Дисциплина незваных ответов: триаж, наблюдаемость и застрявший кулдаун.

Три дефекта, найденных разведкой на живом архиве (117 847 реплик).

1. Ветка «обсуждение клинического поста» ОБХОДИЛА LLM-триаж целиком. Условие
   содержало исключение `and not (reply_to_msg_id and "discussion thread" in
   trigger_reason)`, а решение заговорить принимали два условия и больше ничего:
   у родителя есть медиа И под ним три ответа. О СОДЕРЖАНИИ этих ответов условие
   не говорит ничего.

   Замер: условию удовлетворяют 4075 реплик, после подавления обработанных
   тредов остаётся 891 реальное вторжение (0.88 в сутки), и 472 из них — 53% —
   без единого стоматологического слова. Бот отвечал бы клинической лекцией на
   «Спасибо вам большое! 🔥🤩», «Смекаю)», «Техник рукастый», «Бинго) Или как
   там?! Фулхаус))». Промпт триажа целиком про «пользователи НЕ любят, когда бот
   лезет в их разговор» — и именно он пропускался.

2. Причина молчания писалась в журнал на уровне debug, а корневой уровень INFO.
   Замер по всем журналам на диске (126 340 строк): строка встречается 0 раз.
   При этом кулдаун закрыт большую часть суток — это статистически главная
   причина, по которой бот в чате молчит, и она была ненаблюдаема.

3. Метка времени из БУДУЩЕГО запирала пассивный триггер на величину скачка часов:
   проба через настоящий passive_gate_block_reason давала 525 720 минут молчания
   при метке на год вперёд и почти восемь тысяч лет при "9999-12-31". Значение с
   часовым поясом поднимало TypeError, не перехваченный в этой функции, — он
   валил ассистента на КАЖДОМ входящем сообщении до ручной правки файла.

Запуск: python test_trigger_discipline.py
"""
import io
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_trig_")
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
CODE = "\n".join(l for l in SOURCE.split("\n") if not l.lstrip().startswith("#"))
NOW = datetime.now()

print("\n[1] Триаж проходят все незваные срабатывания")
check("исключения для ветки клинического поста не осталось",
      'discussion thread" in trigger_reason' not in CODE,
      "ветка снова обходит триаж: 891 вторжение в сутки, 53% не про стоматологию")
check("условие триажа осталось на месте",
      "if triggered and not is_dialogue:" in CODE,
      "триаж потерялся целиком — бот заговорит на всё подряд")
# Диалог по-прежнему триаж не проходит: врач сам ответил боту, спрашивать
# уместность повторно незачем.
triage_block = CODE.split("if triggered and not is_dialogue:", 1)[1][:600]
check("платная попытка списывается до триажа", "record_passive_attempt()" in triage_block,
      "в активном чате бот заплатит за триаж на каждом входящем")
check("решение принимает LLM, а не регулярка", "check_llm_triage" in triage_block)

print("\n[2] Причина молчания видна в журнале")
check("причина пишется на уровне info", 'logger.info("Passive text trigger suppressed'
      in CODE or "logger.info(f\"Passive text trigger suppressed" in CODE,
      "уровень debug не эмитится: корневой INFO, строка не появится никогда")
check("debug на этой строке не остался",
      'logger.debug(f"Passive text trigger suppressed' not in CODE)
# Соседние ветки того же решения тоже должны быть видны — иначе картина неполная.
for marker in ("Bot is silenced until", "triage",
               "No matching knowledge corpus", "IGNORE"):
    check(f"ветка «{marker[:24]}» логируется", marker.lower() in CODE.lower())

print("\n[3] Кулдаун не запирается меткой из будущего")
# record_passive_success пишет datetime.now() без сверки: скачок часов вперёд
# (VM после suspend, старт до синхронизации NTP) кладёт в состояние будущую дату.
for label, value, must_open in (
    ("нормальная метка 30 минут назад", (NOW - timedelta(minutes=30)).isoformat(), False),
    ("метка на день вперёд", (NOW + timedelta(days=1)).isoformat(), True),
    ("метка на год вперёд", (NOW + timedelta(days=365)).isoformat(), True),
    ("метка 9999-12-31", "9999-12-31", True),
    ("метка с часовым поясом", (NOW - timedelta(minutes=30)).isoformat() + "+04:00", True),
    ("мусор вместо даты", "не дата вовсе", True),
):
    try:
        reason = A.passive_gate_block_reason({"last_passive_text_run": value})
        opened = reason is None
        check(f"{label}: гейт {'открыт' if must_open else 'закрыт'}",
              opened == must_open,
              f"got {reason!r}")
    except Exception as exc:
        check(f"{label}: без исключения", False, f"{type(exc).__name__}: {exc}")

print("\n[4] Разбор метки не роняет обработчик")
# Единственное чтение времени состояния БЕЗ try/except — вычитание внутри
# passive_gate_block_reason. Одно tz-aware значение поднимало TypeError, который
# пробивал check_and_trigger_assistant и гасился только общим except в main:
# ассистент падал на каждом входящем сообщении.
for key in ("last_passive_text_run", "last_passive_attempt"):
    for bad in ((NOW + timedelta(days=400)).isoformat(),
                (NOW).isoformat() + "+03:00", "", None, 12345, "9999-99-99"):
        try:
            A.passive_gate_block_reason({key: bad})
            ok = True
        except Exception as exc:
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        check(f"{key}={bad!r} не роняет", ok, detail if not ok else "")

print("\n[5] Нормальная работа кулдауна не сломана")
recent = {"last_passive_text_run": (NOW - timedelta(minutes=30)).isoformat()}
reason = A.passive_gate_block_reason(recent)
check("свежий ответ держит окно", reason is not None, f"got {reason!r}")
check("в причине указан остаток", reason and "min left" in reason, f"got {reason!r}")
old = {"last_passive_text_run": (NOW - timedelta(hours=5)).isoformat()}
check("истёкшее окно открывает гейт", A.passive_gate_block_reason(old) is None,
      f"got {A.passive_gate_block_reason(old)!r}")
check("пустое состояние открывает гейт", A.passive_gate_block_reason({}) is None)

print("\n[6] Проверки выше ловят поломку")
check("разбор метки действительно различает будущее и прошлое",
      A._parse_state_dt((NOW + timedelta(days=1)).isoformat())
      != A._parse_state_dt((NOW - timedelta(days=1)).isoformat()),
      "если бы не различал, вся секция [3] ничего не значила")
check("будущая метка приводится к «никогда»",
      A._parse_state_dt((NOW + timedelta(days=1)).isoformat()) == datetime(2000, 1, 1),
      f"got {A._parse_state_dt((NOW + timedelta(days=1)).isoformat())}")
check("детектор исключения для ветки поймал бы возврат",
      'discussion thread" in trigger_reason' in
      'if triggered and not (reply_to_msg_id and "discussion thread" in trigger_reason):')

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
