"""
Личные сообщения: кулдаун, бюджет истории и полнота ленты после кейса.

Тест дописан оркестратором: агент, вносивший правки в assistant.py, упал на
лимите кредитов, успев изменить код (63 строки) но не оставив ни одной проверки.
Правки были не проверены никем.

Что закрывается:

  1. check_user_cooldown округлял остаток через int(). При elapsed=4.5 остаток
     0.5 превращался в 0, а все вызывающие читают 0 как «кулдауна нет»
     (`if cooldown > 0`, `if not check_user_cooldown(...)`). Отметка времени при
     этом НЕ обновлялась — она переписывается только на выходе после `if`.
     Итог: в последнюю секунду каждого окна запрос проходил, и подряд идущие
     сообщения могли проскакивать бесконечно, сдвигаясь на эту секунду. Для
     /quiz (60 с) и уведомления о частоте (30 с) дыра та же, только шире.

  2. История ЛС уходила в промпт без ограничения по символам, обесценивая
     бюджет справки (_CORPUS_MAX_CHARS = 6000 на корпус): 35 реплик могли
     занять больше, чем вся клиническая справка. При этом САМУЮ СВЕЖУЮ реплику
     подрезать нельзя: в текстовой ветке ЛС отдельного поля «Вопрос
     пользователя» в промпте нет, и текущее сообщение врача попадает модели
     ТОЛЬКО как последняя строка блока истории.

  3. Ответы экзаменатора в /case не писались в историю ЛС нигде, а ходы врача
     писались безусловно. Для кейса из четырёх шагов в pm_messages оставались
     четыре реплики врача подряд без единого ответа между ними, и следующий
     обычный вопрос собирал промпт из этой односторонней ленты.

Боевые файлы не открываются: состояние и журнал уведены во временный каталог.

Запуск: python test_fix_pm.py
"""
import io
import os
import re
import shutil
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_pm_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")

import assistant as A  # noqa: E402

# Состояние кулдаунов живёт в файле состояния ассистента — уводим его.
A.STATE_PATH = os.path.join(_TMPDIR, "state.json")
A.STATE_TMP_PATH = A.STATE_PATH + ".tmp"
A.STATE_BAK_PATH = A.STATE_PATH + ".bak"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCE = io.open("assistant.py", encoding="utf-8").read()
CODE = "\n".join(l for l in SOURCE.split("\n") if not l.lstrip().startswith("#"))

print("\n[1] Кулдаун не отдаёт ноль, пока окно не закрылось")
# Ноль означает «можно работать»: если он возвращается раньше времени, окно
# просто не работает.
#
# Время НЕ подменяем: функция берёт datetime.now() и держит отметки в словаре
# USER_COOLDOWNS в памяти. Подставляем метку нужной давности прямо туда — так
# проверяется настоящая арифметика без заглушек. Первая версия этой секции
# подменяла A.time.time и не влияла ни на что: функция этот источник времени
# не использует, и проверки «прошли» на реальных часах.
from datetime import datetime as _dt, timedelta as _td  # noqa: E402

KEY = (1, 42, "pm_chat")


def at(ago_seconds, seconds=5):
    """Остаток кулдауна, если последний вызов был ago_seconds назад."""
    A.USER_COOLDOWNS[KEY] = _dt.now() - _td(seconds=ago_seconds)
    return A.check_user_cooldown(*KEY, seconds=seconds)


A.USER_COOLDOWNS.pop(KEY, None)
check("первый вызов проходит", A.check_user_cooldown(*KEY, seconds=5) == 0)

check("на 0.5 секунде окно закрыто, остаток пять", at(0.5) == 5, f"got {at(0.5)}")
check("на 4.5 секунде окно ещё закрыто", at(4.5) > 0,
      f"got {at(4.5)} — вызывающий прочитает это как «можно»")
check("остаток округлён вверх до целой секунды", at(4.5) == 1, f"got {at(4.5)}")
check("на 4.99 секунде окно всё ещё закрыто", at(4.99) > 0, f"got {at(4.99)}")
check("на 5.01 секунде окно открылось", at(5.01) == 0, f"got {at(5.01)}")

# Ключевая проверка: подряд идущие вызовы в последнюю секунду НЕ должны
# проскакивать. Раньше каждый такой возвращал int(0.x) = 0, а отметка времени
# не сдвигалась, потому что переписывается только после `if`.
slipped = sum(1 for i in range(20) if at(4.5 + i * 0.02) == 0)
check("в последнюю секунду окна не проскакивает ни один вызов", slipped == 0,
      f"проскочило {slipped} из 20")

# Длинные окна: /quiz 60 с и уведомление о частоте 30 с — дыра та же, шире.
QUIZ = (1, 42, "quiz")
A.USER_COOLDOWNS[QUIZ] = _dt.now() - _td(seconds=59.5)
check("длинное окно не открывается за полсекунды до срока",
      A.check_user_cooldown(*QUIZ, seconds=60) > 0)
A.USER_COOLDOWNS[QUIZ] = _dt.now() - _td(seconds=60.5)
check("длинное окно открывается по сроку",
      A.check_user_cooldown(*QUIZ, seconds=60) == 0)

check("округление вверх, а не отбрасывание дроби",
      "math.ceil(seconds - elapsed)" in CODE, "вернулся int() и последняя секунда снова теряется")

print("\n[2] Бюджет истории ЛС: режется старое, свежее цело")
budget = A._PM_HISTORY_MAX_CHARS
entry_cap = A._PM_HISTORY_ENTRY_MAX_CHARS
check("бюджет истории объявлен", isinstance(budget, int) and budget > 0, f"got {budget}")
check("бюджет истории не больше бюджета справки", budget <= A._CORPUS_MAX_CHARS,
      f"история {budget} против справки {A._CORPUS_MAX_CHARS}")
check("предел записи меньше общего бюджета", entry_cap < budget,
      f"{entry_cap} против {budget}")

old_lines = [f"Врач: старая реплика номер {i} " + "х" * 400 for i in range(40)]
question = "Врач: " + "подробное описание случая. " * 120
fitted = A._fit_pm_history(old_lines + [question])
joined = "\n".join(fitted)
check("блок истории уложен в бюджет", len(joined) <= budget, f"got {len(joined)}")
check("самая свежая реплика на месте целиком", fitted[-1] == question,
      f"свежая реплика изменена: {len(fitted[-1])} против {len(question)}")
check("старые реплики отброшены, а не свежие", len(fitted) < len(old_lines) + 1,
      f"осталось {len(fitted)} из {len(old_lines) + 1}")
check("порядок хронологический", fitted == sorted(fitted, key=lambda l: (old_lines + [question]).index(l)))

# Длинная СТАРАЯ реплика подрезается, но по границе предложения.
long_old = "Бот: " + "Гипохлорит применяют в концентрации от 3 до 5 процентов. " * 60
fitted2 = A._fit_pm_history([long_old, "Врач: короткий вопрос"])
check("длинная старая реплика подрезана", len(fitted2[0]) <= entry_cap + 2,
      f"got {len(fitted2[0])}")
check("подрезка не рвёт слово",
      fitted2[0].rstrip().endswith((".", "!", "?", ";", "…")), repr(fitted2[0][-40:]))
check("пустая история не роняет", A._fit_pm_history([]) == [])
check("одна реплика возвращается как есть",
      A._fit_pm_history(["Врач: вопрос"]) == ["Врач: вопрос"])

check("обе ветки промпта ЛС идут через бюджет",
      CODE.count("_fit_pm_history(context_msgs)") == 2,
      f"мест: {CODE.count('_fit_pm_history(context_msgs)')}, а веток две")

print("\n[3] История ЛС после кейса двусторонняя")
# Ходы врача пишутся в pm_messages до маршрутизации в симулятор. Если ответы
# экзаменатора не писать, лента становится односторонней, и следующий обычный
# вопрос собирает промпт из ходов кейса как из реплик, адресованных модели.
case_block = CODE.split("async def handle_interactive_case_step", 1)[1].split("\nasync def ", 1)[0]
check("ответ экзаменатора пишется в историю ЛС",
      "save_pm_message" in case_block, "лента останется односторонней")
check("реплика кейса помечена в истории",
      "[Клинический кейс]" in case_block,
      "разбор кейса неотличим от обычного ответа бота")
check("условие кейса тоже попадает в историю",
      CODE.count("[Клинический кейс]") >= 2,
      "первый ход врача лежит в истории как реплика ни на что")
check("запись истории не роняет кейс при отказе базы",
      "Failed to persist case examiner reply" in SOURCE or
      "except Exception as save_err" in case_block,
      "падение записи истории утащит весь кейс")

print("\n[4] Проверки выше действительно ловят поломку")
# Секции [1]-[3] опираются на строки исходника, а совпадение строки одинаково
# выглядит у исправленного кода и у сломанной проверки. Убеждаемся, что
# детекторы срабатывают на заведомо неверном образце.
check("детектор int() поймал бы возврат",
      "math.ceil(seconds - elapsed)" not in "return int(seconds - elapsed)")
check("детектор односторонней ленты поймал бы пустой блок",
      "save_pm_message" not in "async def handle_interactive_case_step(): pass")
_probe = A._fit_pm_history(["Врач: " + "я" * (budget * 2)])
check("бюджет не выбрасывает единственную реплику", len(_probe) == 1,
      "иначе вопрос врача исчезнет целиком")

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
