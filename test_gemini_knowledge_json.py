"""
Синтез знаний: ответ модели не должен приниматься на веру.

Каждый дефект здесь назван последствием для врача, а не описанием кода.

1. JSON-режим не включался НИ РАЗУ: `models_to_try` содержал одну Gemma, ветка
   `if not is_gemma` была недостижима. Замер по боевой вики: 353 факта, у которых
   ВСЕ токены `source_ids` вида `MSG_2421` (2 073 битых токена), — врач читает
   статью и не может открыть ни одного исходного сообщения, чтобы проверить, от
   кого совет и в каком контексте он дан.
2. Код категории приходил списком и склеивался в '2.3.2, 2.3.1, 2.3.3'. Таких
   записей 12 667 из 12 784. Экспорт savdel.py ищет категорию точным кодом,
   поэтому статья существует, но ни в один файл к врачу не попадает.
3. Пустой ответ модели возвращал None и НИ ОДНОЙ строки в журнале (замер:
   0 строк на 3 ключа). Пачка реплик тихо не давала фактов, и причину узнать
   было негде.
4. 404 глотался веткой `if "404" not in err`. При одной модели в списке это
   значит: синтез знаний умер целиком, вики перестала расти, и в журнале об этом
   ни строки.
5. Обрезка по max_output_tokens (8192) не проверялась: часть фактов пачки молча
   не доезжала до вики.

Сети нет: `genai.Client` подменён, боевые ключи не используются, базы не
открываются. Часы подменены — тайминговых флаков нет by design.

Запуск: python test_gemini_knowledge_json.py
"""
import json
import logging
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

# Боевые ключи в тест не попадают ни при каком исходе.
config.GOOGLE_KEYS = ["fake-key-1", "fake-key-2", "fake-key-3"]

import gemini_knowledge as gk  # noqa: E402
from google.genai import types  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# --- подмена транспорта и часов -------------------------------------------

REQUESTS = []      # (model_id, mime, schema_or_None) каждого ушедшего запроса
SLEEPS = []        # длительности сна


class FakeClock:
    """Часы, которые двигает только sleep: дедлайн проверяется детерминированно."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        SLEEPS.append(seconds)
        self.now += seconds


CLOCK = FakeClock()
gk.time = CLOCK


class Resp:
    def __init__(self, text, finish_reason=None):
        self.text = text
        if finish_reason is None:
            self.candidates = []
        else:
            self.candidates = [type("C", (), {"finish_reason": finish_reason})()]


def install(behaviour):
    """behaviour(model_id) -> Resp | Exception. Ни одного сетевого вызова."""

    class Models:
        def generate_content(self, model=None, contents=None, config=None):
            REQUESTS.append((
                model,
                getattr(config, "response_mime_type", None),
                getattr(config, "response_schema", None),
            ))
            out = behaviour(model)
            if isinstance(out, Exception):
                raise out
            return out

    class Client:
        def __init__(self, api_key=None):
            assert str(api_key).startswith("fake-key"), "в тест утёк боевой ключ"
            self.models = Models()

    gk.genai = type("G", (), {"Client": Client})()


class Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        self.rows.append((record.levelname, record.getMessage()))

    def levels(self, name):
        return [m for lvl, m in self.rows if lvl == name]


LOG = Records()
gk.logger.addHandler(LOG)
gk.logger.setLevel(logging.DEBUG)
gk.logger.propagate = False


def run(behaviour):
    """Один вызов синтеза. Возвращает (результат, запросы, журнал)."""
    REQUESTS.clear()
    SLEEPS.clear()
    LOG.rows.clear()
    CLOCK.now = 1000.0
    install(behaviour)
    result = gk.generate_fact_json("промпт про реставрации")
    return result, list(REQUESTS), LOG


GOOD = json.dumps({"facts": [
    {"c": "2.3.2", "f": "Методика описана сухим техническим языком.", "s": [2421, 2424], "case": False},
]}, ensure_ascii=False)

# Ровно та форма, что оставила 353 факта без провенанса и 12 667 со склеенным кодом.
MSG_GARBAGE = json.dumps({"facts": [
    {"c": "2.3.2, 2.3.1, 2.3.3", "f": "Текст статьи.", "s": ["MSG_2421", "MSG_2424", "MSG_2425"]},
]}, ensure_ascii=False)


print("\n[1] ГЛАВНОЕ: JSON-режим действительно уходит в запрос (ветка была мертва)")
res, reqs, log = run(lambda m: Resp(GOOD))
check("валидный ответ возвращён как есть", res == GOOD, f"вернулось {res!r}")
check("хотя бы один запрос ушёл с response_mime_type=application/json",
      any(mime == "application/json" for _m, mime, _s in reqs),
      f"mime в запросах: {[m for _x, m, _y in reqs]}")
check("в том же запросе ушла схема ответа",
      any(mime == "application/json" and schema for _m, mime, schema in reqs))
check("на валидном ответе хватило одного запроса", len(reqs) == 1, f"запросов {len(reqs)}")
check("валидный ответ не порождает ERROR в журнале", not log.levels("ERROR"),
      f"{log.levels('ERROR')[:1]}")

print("\n[2] Схема запрещает ровно те два поля, которыми испорчена вики")
schema = types.Schema.model_validate(gk._FACTS_SCHEMA)   # проверяет сам SDK, не наш глазомер
item = schema.properties["facts"].items
check("s объявлен массивом целых (ярлык MSG_ невыразим)",
      item.properties["s"].items.type == types.Type.INTEGER,
      f"тип: {item.properties['s'].items.type}")
check("c объявлен строкой (склейка списка кодов не родится на стороне модели)",
      item.properties["c"].type == types.Type.STRING)
check("c, f, s обязательны", set(item.required or []) == {"c", "f", "s"}, f"{item.required}")

print("\n[3] ГЛАВНОЕ: мусор с MSG_ отвергнут громко, а не записан")
res, reqs, log = run(lambda m: Resp(MSG_GARBAGE))
check("мусор с MSG_ НЕ возвращён вызывающему", res is None, f"вернулось {res!r}")
errors = log.levels("ERROR")
check("в журнале есть строка отказа", any("ОТКАЗ" in m for m in errors), f"{errors[:1]}")
check("причина отказа называет провенанс",
      any("провенанс" in m for m in errors), f"{errors[:1]}")
check("причина отказа называет склейку кода категории",
      any("склейка" in m for m in errors), f"{errors[:1]}")
check("итоговая строка называет последствие для вики",
      any("в вики не попадёт ничего" in m for m in errors), f"{errors[-1:]}")
check("перед отказом перебраны все модели каскада",
      len({m for m, _mi, _s in reqs}) == len(gk._models_to_try()),
      f"модели: {sorted({m for m, _mi, _s in reqs})}")

print("\n[4] Каждый из двух дефектов вики ловится по отдельности")
only_msg = json.dumps({"facts": [{"c": "2.3.2", "f": "текст", "s": ["MSG_9"]}]}, ensure_ascii=False)
verdict, why = gk.validate_fact_payload(only_msg)
check("ярлык MSG_ при верном коде — контракт нарушен", verdict == "contract", f"{verdict} / {why}")
only_code = json.dumps({"facts": [{"c": "2.3.2, 2.3.1", "f": "текст", "s": [9]}]}, ensure_ascii=False)
verdict, why = gk.validate_fact_payload(only_code)
check("склейка кодов при верных id — контракт нарушен", verdict == "contract", f"{verdict} / {why}")
as_list = json.dumps({"facts": [{"c": ["2.3.2", "2.3.1"], "f": "текст", "s": [9]}]}, ensure_ascii=False)
verdict, why = gk.validate_fact_payload(as_list)
check("код категории списком — контракт нарушен", verdict == "contract", f"{verdict} / {why}")
no_prov = json.dumps({"facts": [{"c": "2.3.2", "f": "текст"}]}, ensure_ascii=False)
verdict, why = gk.validate_fact_payload(no_prov)
check("факт вообще без поля s — контракт нарушен", verdict == "contract", f"{verdict} / {why}")

print("\n[5] Строгость не съедает законные ответы")
verdict, _ = gk.validate_fact_payload(
    json.dumps({"facts": [{"c": "2.3.2", "f": "текст", "s": ["2421", "2424"]}]}, ensure_ascii=False))
check("id строкой из цифр принимается", verdict == "ok", verdict)
verdict, _ = gk.validate_fact_payload('{"facts": []}')
check("честное {\"facts\": []} принимается (иначе каждая пустая пачка жгла бы 3 попытки)",
      verdict == "ok", verdict)
verdict, _ = gk.validate_fact_payload("```json\n" + GOOD + "\n```")
check("ответ в маркдаун-обёртке принимается", verdict == "ok", verdict)
verdict, _ = gk.validate_fact_payload(
    json.dumps([{"c": "2.3.2", "f": "текст", "s": [9]}], ensure_ascii=False))
check("верхнеуровневый список фактов принимается", verdict == "ok", verdict)
verdict, why = gk.validate_fact_payload(
    json.dumps({"facts": [{"c": "2.3.2", "f": "текст", "s": ["²"]}]}, ensure_ascii=False))
check("надстрочная '²' отвергнута (isdigit() её пропускает, int() падает)",
      verdict == "contract", f"{verdict} / {why}")

print("\n[6] Пустой ответ модели больше не молчит")
# ВАЖНО про формулировку проверок ниже. Подстрока «пустой ответ» есть и в строке
# про КОНКРЕТНУЮ модель, и в итоговой строке «СИНТЕЗ НЕ СОСТОЯЛСЯ ... Причины:»,
# потому что причины собираются из того же текста. Диверсия «убрать строку про
# конкретную модель» такую проверку НЕ уронила: подстрока приезжала из итоговой
# строки. Поэтому здесь якорь на фразу-последствие, которая есть ТОЛЬКО в строке
# про модель, и на КОЛИЧЕСТВО таких строк — по одной на попытку.
CONSEQ = "не даст ни одного факта"


def per_model_lines(log):
    return [m for m in log.levels("ERROR") if CONSEQ in m]


res, reqs, log = run(lambda m: Resp(""))
check("пустой ответ -> None", res is None, f"{res!r}")
check("на КАЖДУЮ пустую попытку своя строка в журнале, а не одна общая",
      len(per_model_lines(log)) == len(reqs),
      f"строк {len(per_model_lines(log))} при {len(reqs)} попытках")
check("строка про пустой ответ называет модель",
      all(any(mid in m for mid, _mi, _s in reqs) for m in per_model_lines(log)),
      f"{per_model_lines(log)[:1]}")
res, reqs, log = run(lambda m: Resp(None))
check("text=None тоже даёт строку на каждую попытку",
      res is None and len(per_model_lines(log)) == len(reqs),
      f"строк {len(per_model_lines(log))} при {len(reqs)} попытках")
res, reqs, log = run(lambda m: Resp("", finish_reason="SAFETY"))
check("finish_reason стоит в строке ПРО МОДЕЛЬ, а не только в сводке причин",
      any("SAFETY" in m for m in per_model_lines(log)), f"{per_model_lines(log)[:1]}")

print("\n[7] 404 больше не глотается и не жжёт все ключи")
res, reqs, log = run(lambda m: Exception("404 model not found"))
check("404 по всем моделям -> None", res is None, f"{res!r}")
# Подстрока «404» приезжает и из общей ветки `Error %s: %s` (там текст исключения),
# поэтому якорь на фразу, которая есть только в ветке 404. Иначе проверка зелёная
# даже когда 404 обрабатывается как безымянная ошибка и жжёт все 10 ключей.
check("404 распознан именно как снятая модель, а не как безымянная ошибка",
      any("снята с обслуживания" in m for m in log.levels("ERROR")), f"{log.levels('ERROR')[:1]}")
check("404 стоит один запрос на модель, а не по одному на каждый ключ",
      len(reqs) == len(gk._models_to_try()),
      f"запросов {len(reqs)} при {len(config.GOOGLE_KEYS)} ключах и {len(gk._models_to_try())} моделях")

print("\n[8] Каскад: недоступность первых моделей не убивает синтез")
res, reqs, log = run(lambda m: Resp(GOOD) if "gemma" in m else Exception("404 not found"))
check("Gemma подхватывает работу и факты доезжают", res == GOOD, f"{res!r}")
check("Gemma опрошена без JSON-режима (она его не поддерживает)",
      any(m for m, mime, _s in reqs if "gemma" in m and mime is None),
      f"{[(m, mi) for m, mi, _s in reqs]}")
check("Gemma в каскаде последняя", gk._models_to_try()[-1] == "models/gemma-3-27b-it",
      f"{gk._models_to_try()}")

print("\n[9] Обрезанный ответ: факты не теряются молча")
res, reqs, log = run(lambda m: Resp(GOOD, finish_reason="MAX_TOKENS"))
check("пригодный, но обрезанный ответ всё равно возвращён", res == GOOD, f"{res!r}")
check("обрезка названа в журнале последствием",
      any("неполный" in m for m in log.levels("ERROR")), f"{log.levels('ERROR')[:1]}")

truncated = '{"facts": [{"c": "2.3.2", "f": "первая статья", "s": [2421]}, {"c": "2.3.3", "f": "втор'
res, reqs, log = run(lambda m: Resp(truncated, finish_reason="MAX_TOKENS"))
check("нераспарсенный ответ отдан на спасение целых фактов, а не выброшен",
      res == truncated, f"{res!r}")
check("спасение объявлено в журнале",
      any("спасение" in m for m in log.levels("ERROR")), f"{log.levels('ERROR')[:1]}")

print("\n[10] Дедлайн вызова: сито не встаёт на одной пачке")
# На боевой машине ключей ДЕСЯТЬ (замерено: len(config.GOOGLE_KEYS) == 10), и
# худший случай 3 модели x 10 ключей x 10 с сна = 300 с. Проверять дедлайн на
# трёх ключах бессмысленно: 90 с в него влезают, ветка не исполняется и проверка
# зелёная на нерабочем коде. Поэтому здесь боевое число ключей.
_saved_keys = config.GOOGLE_KEYS
config.GOOGLE_KEYS = [f"fake-key-{i}" for i in range(10)]
models = len(gk._models_to_try())
res, reqs, log = run(lambda m: Exception("429 RESOURCE_EXHAUSTED"))
check("429 по всем ключам -> None", res is None, f"{res!r}")
check("суммарный сон не превысил дедлайн вызова",
      sum(SLEEPS) <= gk._CALL_DEADLINE_SECONDS,
      f"сон {sum(SLEEPS)} с при дедлайне {gk._CALL_DEADLINE_SECONDS} с")
check("дедлайн или отмена сна названы в журнале",
      any("дедлайн" in m.lower() for m in log.levels("ERROR")), f"{log.levels('ERROR')[:2]}")
check("дедлайн ОБОРВАЛ каскад, а не дал пройти все 30 попыток",
      len(reqs) < models * 10,
      f"запросов {len(reqs)} при потолке {models * 10}; сон {sum(SLEEPS)} с")
check("причина 429 не вытеснена из журнала (иначе оператор не отличит квоту от поломки)",
      any("429" in m for m in log.levels("ERROR") + log.levels("WARNING")))
config.GOOGLE_KEYS = _saved_keys

# На трёх ключах (90 с сна) дедлайн НЕ должен срабатывать — иначе он рубил бы
# здоровый перебор ключей и пачка уходила бы в отказ на живой квоте.
res, reqs, log = run(lambda m: Exception("429 RESOURCE_EXHAUSTED"))
check("на трёх ключах дедлайн не срабатывает досрочно",
      not any("дедлайн" in m.lower() for m in log.levels("ERROR")),
      f"{[m for m in log.levels('ERROR') if 'дедлайн' in m.lower()][:1]}")
check("на трёх ключах перебраны все 3 модели x 3 ключа",
      len(reqs) == len(gk._models_to_try()) * 3, f"запросов {len(reqs)}")

print("\n[12] Рабочая модель не голодает из-за сна на чужой исчерпанной квоте")
# Измерено до правки: новые JSON-модели отдают 429 на всех 10 боевых ключах, сон
# 10 с съедает все 120 с дедлайна, и до Gemma — единственной проверенно рабочей
# модели — очередь НЕ доходит. При латентности запроса 2 с пачка уходила в отказ,
# хотя Gemma ответила бы: врач не получал статью не из-за отсутствия модели, а
# из-за того, что её место в очереди выел сон на чужой квоте.
_saved_keys = config.GOOGLE_KEYS
config.GOOGLE_KEYS = [f"fake-key-{i}" for i in range(10)]


def quota_dead_except_gemma(latency):
    """429 на всех моделях кроме Gemma; каждый запрос стоит времени, как в бою."""

    def behaviour(model):
        CLOCK.now += latency
        if "gemma" in model:
            return Resp(GOOD)
        return Exception("429 RESOURCE_EXHAUSTED")

    return behaviour


for _latency in (0.0, 2.0, 5.0):
    res, reqs, log = run(quota_dead_except_gemma(_latency))
    check(f"при латентности {_latency} с очередь доходит до Gemma",
          any("gemma" in m for m, _mi, _s in reqs),
          f"опрошены только {sorted({m for m, _mi, _s in reqs})}, потрачено {CLOCK.now - 1000.0} с")
    check(f"при латентности {_latency} с факты доезжают до вики", res == GOOD,
          f"вернулось {res!r}; потрачено {CLOCK.now - 1000.0} с")
    check(f"при латентности {_latency} с общий дедлайн не превышен",
          CLOCK.now - 1000.0 <= gk._CALL_DEADLINE_SECONDS,
          f"потрачено {CLOCK.now - 1000.0} с при дедлайне {gk._CALL_DEADLINE_SECONDS} с")

check("резерв под последнюю модель строго меньше общего дедлайна (бюджет вложен)",
      0 < gk._LAST_RESORT_RESERVE_SECONDS < gk._CALL_DEADLINE_SECONDS,
      f"резерв {gk._LAST_RESORT_RESERVE_SECONDS} при дедлайне {gk._CALL_DEADLINE_SECONDS}")
check("резерва хватает минимум на одну попытку со сном 10 с",
      gk._LAST_RESORT_RESERVE_SECONDS > 10, f"{gk._LAST_RESORT_RESERVE_SECONDS}")
config.GOOGLE_KEYS = _saved_keys

print("\n[11] Без ключей — громкий отказ, а не тихий None")
_saved = config.GOOGLE_KEYS
config.GOOGLE_KEYS = []
res, reqs, log = run(lambda m: Resp(GOOD))
check("нет ключей -> None и строка в журнале",
      res is None and bool(log.levels("ERROR")) and not reqs, f"{log.levels('ERROR')}")
config.GOOGLE_KEYS = _saved

print("\n[13] Дырки, найденные диверсиями скептика (без них тест оставался зелёным)")
# Диверсия «пустой ответ обрывает модель (break вместо continue)» НЕ роняла ни
# одной проверки: сравнение «строк в журнале == попыток» самореферентно и
# остаётся верным, когда попыток стало меньше. Здесь пришпилено само ЧИСЛО
# попыток: пустой кандидат на одном ключе не отменяет остальные девять, иначе
# пачка уходит в отказ на живой квоте и врач не получает статью, которую
# следующий ключ отдал бы.
_saved_keys = config.GOOGLE_KEYS
config.GOOGLE_KEYS = [f"fake-key-{i}" for i in range(10)]
res, reqs, log = run(lambda m: Resp(""))
check("пустой ответ не отменяет ротацию ключей: опрошены все 10 на каждой модели",
      len(reqs) == len(gk._models_to_try()) * 10,
      f"запросов {len(reqs)} при {len(gk._models_to_try())} моделях x 10 ключей")
check("на каждую из этих попыток своя строка в журнале",
      len(per_model_lines(log)) == len(reqs),
      f"строк {len(per_model_lines(log))} при {len(reqs)} попытках")
config.GOOGLE_KEYS = _saved_keys

# Диверсия «снять проверку пустого текста статьи» тоже не роняла ничего: правило
# в validate_fact_payload было, а проверки на него не было. Пустая статья в вики —
# это строка в выгрузке savdel.py, под которой врач не найдёт ни одного слова.
blank = json.dumps({"facts": [{"c": "2.3.2", "f": "   ", "s": [9]}]}, ensure_ascii=False)
verdict, why = gk.validate_fact_payload(blank)
check("факт с пустым текстом статьи — контракт нарушен", verdict == "contract", f"{verdict} / {why}")
res, reqs, log = run(lambda m: Resp(blank))
check("пустая статья не возвращается вызывающему", res is None, f"вернулось {res!r}")
check("причина отказа называет пустой текст статьи",
      any("пустой текст" in m for m in log.levels("ERROR")), f"{log.levels('ERROR')[:1]}")

print(f"\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалились:")
    for name in FAIL:
        print(f"  - {name}")
sys.exit(1 if FAIL else 0)
