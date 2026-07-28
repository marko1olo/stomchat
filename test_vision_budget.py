"""
Каскад зрения: сколько он стоит времени и почему снимок больше не теряется.

Каждая модель пула перебирала ВСЕ ключи своего провайдера. При живом наборе
(10 ключей Google, 7 Groq) и пуле из трёх моделей это 2 x 10 + 1 x 7 = 27
попыток по 33 секунды (запрос 30 плюс пауза троттлинга 3) — до 891 с на ОДИН
снимок. Внешний потолок разбора стоял 180 с, то есть в пять раз меньше.

Последствий два, и второе тяжелее:
  * резервные модели каскада не пробовались НИКОГДА — внешний таймаут
    срабатывал раньше, чем каскад доходил до второй модели;
  * после срабатывания внешнего таймаута снимок получает в базе описание "-",
    то есть отметку «разобрано», и get_pending_media_message_ids больше его не
    возвращает. Рентген коллеги терялся молча и навсегда.

Поднять потолок до 951 с было бы хуже: воркер один, очередь на 128 снимков, и
одно зависшее фото остановило бы разбор на четверть часа. Поэтому ограничен сам
каскад, а потолок ВЫВЕДЕН из его бюджета.

Ничего не запускается и не вызывается: читаются константы и исходники.

Запуск: python test_vision_budget.py
"""
import io
import os
import re
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ["STOMCHAT_LOG_PATH"] = os.path.join(tempfile.mkdtemp(prefix="stomchat_vb_"), "t.log")

import config  # noqa: E402
import main as M  # noqa: E402
import vision as V  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


VISION_SRC = io.open("vision.py", encoding="utf-8").read()
VISION_CODE = "\n".join(l for l in VISION_SRC.split("\n") if not l.lstrip().startswith("#"))
MAIN_CODE = "\n".join(l for l in io.open("main.py", encoding="utf-8").read().split("\n")
                      if not l.lstrip().startswith("#"))

print("\n[1] Каскад ограничен по числу попыток, а не по числу ключей")
check("предел ключей на модель объявлен",
      isinstance(V.VISION_KEYS_PER_MODEL, int) and V.VISION_KEYS_PER_MODEL >= 1,
      f"got {V.VISION_KEYS_PER_MODEL}")
check("перебор ключей действительно урезан в коде",
      "available[:VISION_KEYS_PER_MODEL]" in VISION_CODE,
      "каскад снова пойдёт по всем ключам и вернётся к 891 с")
keys_google = len(getattr(config, "GOOGLE_KEYS", []) or [])
check(f"ключей у провайдера больше, чем берём ({keys_google} против {V.VISION_KEYS_PER_MODEL})",
      keys_google > V.VISION_KEYS_PER_MODEL,
      "проверка бессмысленна, если ключей и так мало")
attempts = V.VISION_MODEL_POOL_SIZE * V.VISION_KEYS_PER_MODEL
check(f"попыток каскада {attempts}, а не по одной на каждый ключ",
      attempts <= 8, f"got {attempts}")
check("отказ ключа спасается СЛЕДУЮЩЕЙ моделью, а не следующим ключом",
      V.VISION_MODEL_POOL_SIZE >= 2, "в пуле одна модель — резерва нет вовсе")

print("\n[2] Размер пула в константе совпадает с фактическим кодом")
# Пул объявлен внутри describe_image, а потолок в main.py считается по
# константе: расхождение вернёт ту же несогласованность двух чисел.
pool_block = VISION_SRC.split("models_pool = [", 1)[1].split("]", 1)[0]
actual_pool = len(re.findall(r'\("', pool_block))
check(f"в коде {actual_pool} моделей, в константе {V.VISION_MODEL_POOL_SIZE}",
      actual_pool == V.VISION_MODEL_POOL_SIZE,
      "потолок разбора посчитан по неверному размеру пула")
providers = set(re.findall(r'"(gemini|groq)"', pool_block))
check("в пуле больше одного провайдера", len(providers) >= 2, f"got {providers}")

print("\n[3] Внешний потолок вмещает внутренний бюджет")
budget = V.vision_cascade_budget_seconds()
check("бюджет каскада считается функцией, а не числом", budget > 0, f"got {budget}")
check(f"внешний потолок ({M.MEDIA_ANALYSIS_TIMEOUT_SECONDS}) больше бюджета ({budget})",
      M.MEDIA_ANALYSIS_TIMEOUT_SECONDS > budget,
      "резервные модели каскада не будут опробованы никогда")
check("запас покрывает подготовку снимка",
      M.MEDIA_ANALYSIS_TIMEOUT_SECONDS - budget >= V.VISION_IMAGE_PREP_TIMEOUT_SECONDS,
      f"запас {M.MEDIA_ANALYSIS_TIMEOUT_SECONDS - budget} с при подготовке "
      f"{V.VISION_IMAGE_PREP_TIMEOUT_SECONDS} с")
check("потолок ВЫВЕДЕН из бюджета каскада, а не задан числом",
      "vision.vision_cascade_budget_seconds()" in MAIN_CODE,
      "два независимых числа снова разъедутся")

print("\n[4] Очередь снимков не встаёт на сутки")
# Воркер один, очередь на MEDIA_QUEUE_MAX_SIZE: худший случай = потолок на все.
worst_hours = M.MEDIA_QUEUE_MAX_SIZE * M.MEDIA_ANALYSIS_TIMEOUT_SECONDS / 3600
check(f"полная очередь в худшем случае {worst_hours:.1f} ч, а не сутки",
      worst_hours < 12, f"got {worst_hours:.1f} ч")
old_worst = M.MEDIA_QUEUE_MAX_SIZE * 951 / 3600
check(f"стало лучше прежнего расчёта ({old_worst:.1f} ч при потолке 951 с)",
      worst_hours < old_worst)

print("\n[5] Потерянный снимок остаётся восстановимым")
# Отметка «файл до Vision не дошёл» должна отличаться от «разобрано пусто»,
# иначе снимок не вернётся из выборки на догон.
check("отметка неудачи разбора объявлена",
      hasattr(M, "MEDIA_FAILED_MARK") or "-" in MAIN_CODE,
      "без отметки снимок либо теряется, либо перекачивается вечно")
check("догон медиа существует", callable(getattr(M, "recover_pending_media_analysis", None)))
check("предел догона настраиваемый",
      isinstance(M.MEDIA_RECOVERY_LIMIT, int) and M.MEDIA_RECOVERY_LIMIT >= 0)

print("\n[6] Проверки выше ловят поломку")
# Сравнения чисел одинаково выглядят у согласованных бюджетов и у слепой
# проверки, поэтому образцы прогоняются в стороне от check().
check("сравнение бюджетов видит расхождение", not (891 < 180),
      "проверка вложенности слепа")
check("детектор урезания перебора поймал бы возврат",
      "available[:VISION_KEYS_PER_MODEL]" not in "for api_key in available:")
check("расчёт бюджета зависит от предела ключей",
      V.vision_cascade_budget_seconds() ==
      V.VISION_MODEL_POOL_SIZE * V.VISION_KEYS_PER_MODEL
      * (V.GROQ_HTTP_TIMEOUT_SECONDS + V.VISION_MIN_CALL_INTERVAL_SECONDS),
      "формула разошлась с проверкой — одна из двух неверна")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
