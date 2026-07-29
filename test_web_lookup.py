"""
Слой качества над веб-поиском: разбор выдачи, отсев рекламы, заземление ответа.

Механизм поиска в проекте был построен, протестирован и НЕ ВЫЗЫВАЛСЯ НИКЕМ: grep
по всем .py давал только определения (search_engine.py, search_engine_safe.py,
blocking_tools.web_search_async) и тесты. Функции у бота не было.

Измерено по живым базам (только чтение):
  * 12 784 факта в stomat_wiki.db, ссылку содержат 4 (0.03%), DOI — ноль,
    PubMed упомянут в двух. Проверяемый источник врачу бот дать не мог.
  * Архив кончается 2026-02-19 — на сегодня 160 дней без новых знаний.
  * При этом пробелов по темам нет: из двадцати клинических терминов, о которых
    в чате спрашивали, ни один не остался без факта. То есть поиск нужен не
    вместо корпуса, а ради ссылки и свежести.

Запуск: python test_web_lookup.py
"""
import ast
import io
import logging
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import web_lookup as W  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


print("\n[1] Импорт безвреден")
# Модуль уходит в тот же промпт, что и корпус знаний, и импортируется из ядра.
# Импорт, который читает конфиг или лезет в сеть, роняет весь бот на старте —
# в этом проекте импорт уже один раз портил продакшн-файл.
SRC = io.open("web_lookup.py", encoding="utf-8").read()
# Греп по всему файлу здесь не годится: он находит слово в комментарии, который
# объясняет, почему этого импорта нет. Смотрим НА ДЕРЕВО, а не на текст.
_TREE = ast.parse(SRC)
_IMPORTED = set()
for _node in ast.walk(_TREE):
    if isinstance(_node, ast.Import):
        _IMPORTED.update(alias.name.split(".")[0] for alias in _node.names)
    elif isinstance(_node, ast.ImportFrom) and _node.module:
        _IMPORTED.add(_node.module.split(".")[0])
check("конфиг на импорте не читается", "config" not in _IMPORTED,
      f"импорты: {sorted(_IMPORTED)}")
check("сети на импорте нет",
      not ({"requests", "ddgs", "tavily", "urllib3", "httpx", "socket"} & _IMPORTED),
      f"импорты: {sorted(_IMPORTED)}")
check("тяжёлых модулей проекта не тянет",
      not ({"assistant", "main", "blocking_tools", "database"} & _IMPORTED),
      f"импорты: {sorted(_IMPORTED)}")
check("логирование не настраивается", "basicConfig" not in SRC,
      "переопределит формат журнала всего бота")

print("\n[2] Ссылка выделяется из обеих форм выдачи")
# Провайдеры отдают РАЗНЫЕ формы: tavily клеит "текст (url)", подпроцесс ddgs —
# "текст\n(Source: url)". Ранжировать по домену нельзя, пока ссылка внутри текста.
# Сценарий отказа: врач получает ответ без единой ссылки, то есть ровно то, что
# корпус и так умеет, — и не может ничего проверить.
cases = [
    ("ddgs", "Биодентин применяется при перфорации.\n(Source: https://pubmed.ncbi.nlm.nih.gov/12345/)",
     "pubmed.ncbi.nlm.nih.gov"),
    ("tavily", "МТА даёт лучший прогноз (https://www.cochranelibrary.com/cdsr/doi/10.1002/x)",
     "cochranelibrary.com"),
    ("русский маркер", "Артикаин 4% (Источник: https://rlsnet.ru/drugs/articaine)", "rlsnet.ru"),
    ("ссылка внутри текста", "Смотри https://ada.org/resources про фторлак", "ada.org"),
]
for label, raw, host in cases:
    entry = W.parse_result(raw)
    check(f"{label}: хост разобран", entry and entry["host"] == host,
          f"got {entry['host'] if entry else None!r}")
    check(f"{label}: ссылка убрана из текста", entry and "http" not in entry["text"],
          f"got {entry['text'] if entry else None!r}")

# Текст со своими скобками не должен ломать разбор — иначе половина выдержки
# уедет в ссылку или наоборот.
tricky = W.parse_result(
    "Кальций-силикатные цементы (Biodentine, MTA) закрывают перфорацию.\n"
    "(Source: https://onlinelibrary.wiley.com/doi/10.1111/iej.13123)")
check("скобки внутри текста не ломают разбор",
      tricky and tricky["host"] == "onlinelibrary.wiley.com"
      and "Biodentine, MTA" in tricky["text"],
      f"got {tricky!r}")
check("мусор без текста отбрасывается", W.parse_result("   ") is None
      and W.parse_result(None) is None and W.parse_result(42) is None)
check("структурированный результат поддержан",
      (W.parse_result({"text": "факт", "url": "https://who.int/a"}) or {}).get("host") == "who.int",
      "задел на структурированный ответ подпроцесса")

print("\n[3] Домен сравнивается по хосту, а не подстрокой")
# Вхождение подстрокой на домене — тот же дефект, что ловился в этом проекте
# восемь раз на русском тексте. "ada.org" подстрокой лежит внутри "canada.org",
# "who.int" — внутри "pwho.int". Сценарий отказа: подставной домен получает
# уровень авторитетного источника, и врач видит рекламу с пометкой «исследование».
for host, want_trusted in (
    ("ada.org", True), ("jada.ada.org", True),
    ("canada.org", False), ("notada.org", False), ("ada.org.implant-msk.ru", False),
    ("who.int", True), ("pwho.int", False), ("who.int.evil.com", False),
    ("pubmed.ncbi.nlm.nih.gov", True), ("pubmed.ncbi.nlm.nih.gov.phish.ru", False),
):
    trusted = W.tier_of(host) < W.UNKNOWN_TIER
    check(f"{host}: {'доверенный' if want_trusted else 'НЕ доверенный'}",
          trusted == want_trusted, f"tier={W.tier_of(host)}")

print("\n[4] Реклама клиник не попадает врачу")
# Выдача общего поиска по стоматологическим запросам на первых местах держит
# рекламу: цены, акции, «запишитесь». Для профессионала это хуже молчания, и
# отдельно опасно тем, что тот же текст уходит в справочный материал валидатора.
ads = [
    "Имплантация зубов от 19900 руб. Акция! Записаться на приём — звоните.\n"
    "(Source: https://implant-moscow.ru/prices)",
    "Наша клиника — лучшая клиника района. Бесплатная консультация и рассрочка.\n"
    "(Source: https://klinika-ulybka.ru/)",
    "Цены на лечение кариеса. Скидка 20% до конца месяца.\n(Source: https://zoon.ru/msk/dental)",
]
for raw in ads:
    entry = W.parse_result(raw)
    is_ad, why = W.is_advertising(entry)
    check(f"реклама отсеяна: {entry['host']}", is_ad, f"пропущена, why={why!r}")

# Профессиональный источник рекламой не объявляется, даже если в тексте есть
# слово из списка: «акция» бывает акцией препарата, а терять из-за одного слова
# систематический обзор нельзя.
pro = W.parse_result("Акция препарата сохраняется 6 часов; стоимость лечения в расчёт "
                     "не входила.\n(Source: https://pubmed.ncbi.nlm.nih.gov/999/)")
is_ad, why = W.is_advertising(pro)
check("исследование не объявлено рекламой", not is_ad, f"why={why!r}")
single = W.parse_result("Пациенту назначена скидка не предусмотрена протоколом.\n"
                        "(Source: https://some-unknown-blog.ru/a)")
check("один маркер рекламой не считается", not W.is_advertising(single)[0])

# Отдельно: рекламный ДОМЕН обязан отсекаться сам по себе, даже когда текст
# выдержки нейтральный. Иначе проверка выше держится только на словах в тексте,
# и страница клиники с сухим описанием методики проходит как источник.
neutral_ad = W.parse_result("Методика проведения синус-лифтинга описана поэтапно.\n"
                            "(Source: https://implant-msk.ru/metodika)")
is_ad, why = W.is_advertising(neutral_ad)
check("рекламный домен отсеян и без рекламных слов", is_ad, f"пропущен, why={why!r}")
check("причина отсева названа доменом", "домен" in why, f"got {why!r}")

print("\n[5] Источники ранжируются, дубли схлопываются, потери названы")
raw_results = [
    "Реклама: имплантация от 19900, записаться, акция.\n(Source: https://implant-msk.ru/p)",
    "Систематический обзор: биодентин не хуже МТА.\n(Source: https://pubmed.ncbi.nlm.nih.gov/1/)",
    "Определение термина из энциклопедии.\n(Source: https://ru.wikipedia.org/wiki/МТА)",
    "Инструкция производителя по применению.\n(Source: https://rlsnet.ru/drugs/mta)",
    "Систематический обзор: биодентин не хуже МТА. Дубликат выдачи.\n"
    "(Source: https://pubmed.ncbi.nlm.nih.gov/1/)",
    "Заметка в личном блоге без репутации.\n(Source: https://some-blog.example/post)",
]
ranked, report = W.rank_sources(raw_results)
check("реклама выброшена и названа", report["ads"] and len(report["ads"]) == 1,
      f"ads={report['ads']}")
check("дубль схлопнут", report["duplicates"] >= 1, f"got {report['duplicates']}")
check("исследование стоит первым", ranked and ranked[0]["host"] == "pubmed.ncbi.nlm.nih.gov",
      f"got {[e['host'] for e in ranked]}")
check("энциклопедия ниже регулятора",
      [e["host"] for e in ranked].index("rlsnet.ru")
      < [e["host"] for e in ranked].index("ru.wikipedia.org"),
      f"got {[e['host'] for e in ranked]}")
check("блог без репутации последний", ranked[-1]["host"] == "some-blog.example",
      f"got {[e['host'] for e in ranked]}")
check("отчёт считает всё, что пришло", report["total"] == len(raw_results),
      f"got {report['total']}")

# Урезание сверх лимита обязано быть названо: показать два источника из семи и
# выдать это за «всё, что есть в открытых источниках» — это ложь врачу.
many = [f"Обзор номер {i}.\n(Source: https://pubmed.ncbi.nlm.nih.gov/{i}/)" for i in range(9)]
_, report_many = W.rank_sources(many, max_sources=3)
check("сверхлимитные источники посчитаны", report_many["over_limit"] == 6,
      f"got {report_many['over_limit']}")

# Схлопывать похожее нельзя, схлопывать вложенное — можно. Разница клиническая:
# два разных числа в одинаковых по форме фразах — это две разные дозы, и потеря
# одной из них меняет назначение. Порог «80% общего начала», который тут стоял
# сначала, склеивал именно такие пары (и заодно девять разных обзоров в один).
doses = [
    "Максимальная доза для взрослого пациента при массе тела более 70 кг "
    "составляет 7 мг/кг.\n(Source: https://rlsnet.ru/drugs/a)",
    "Максимальная доза для взрослого пациента при массе тела более 70 кг "
    "составляет 5 мг/кг.\n(Source: https://rlsnet.ru/drugs/a)",
]
kept_doses, report_doses = W.rank_sources(doses)
check("разные дозы не схлопнуты в одну", len(kept_doses) == 2
      and report_doses["duplicates"] == 0,
      f"осталось {len(kept_doses)}: {[e['text'] for e in kept_doses]}")
nested = [
    "Биодентин не хуже МТА.\n(Source: https://pubmed.ncbi.nlm.nih.gov/7/)",
    "Биодентин не хуже МТА. Выборка 120 зубов.\n(Source: https://pubmed.ncbi.nlm.nih.gov/7/)",
]
kept_nested, report_nested = W.rank_sources(nested)
check("вложенный дубль схлопнут в пользу полного",
      len(kept_nested) == 1 and "120 зубов" in kept_nested[0]["text"],
      f"осталось {[e['text'] for e in kept_nested]}")

print("\n[6] Бюджет режет по предложению и не молчит")
# Выдержки уходят в тот же промпт, что корпус знаний. Провайдер иногда отдаёт
# полстраницы, и одна такая выдержка съела бы блок целиком.
long_entry = {"host": "pubmed.ncbi.nlm.nih.gov", "url": "https://pubmed.ncbi.nlm.nih.gov/1/",
              "tier": 1, "text": ("Первое предложение обзора. " * 40) + "Обрывок без точки"}
fitted, dropped = W.fit_budget([long_entry])
check("длинная выдержка обрезана", len(fitted[0]["text"]) <= W.WEB_ENTRY_MAX_CHARS,
      f"got {len(fitted[0]['text'])}")
check("обрез по границе предложения, не по полуслову",
      fitted[0]["text"].rstrip().endswith((".", "!", "?", ";", "…")),
      f"кончается на {fitted[0]['text'][-30:]!r}")
# Клинический текст на полуслове опаснее короткого: «не более 3 мг/кг в су…»
# читается как другая доза. Требовать точку в конце неверно — обрез посреди фразы
# законен, если он ПОМЕЧЕН и не разрывает токен. Это и есть инвариант, и он
# проверяется на всех длинах, а не на одной удобной.
_DOSE = ("Доза не более 7 мг/кг. Далее следует продолжение фразы, "
         "которое обрывается по счётчику символов ровно посередине слова")
for _limit in range(12, len(_DOSE) + 4):
    _clipped = W.clip_at_sentence(_DOSE, _limit)
    _marked = _clipped.rstrip().endswith((".", "!", "?", ";", "…")) or len(_DOSE) <= _limit
    _torn = (_clipped.endswith("…")
             and len(_clipped) > 1
             and _clipped[-2].isalnum()
             and len(_clipped) - 1 < len(_DOSE)
             and _DOSE[len(_clipped) - 1].isalnum())
    if not _marked or _torn:
        check(f"обрез на {_limit} символах помечен и не рвёт токен", False,
              f"got {_clipped!r}")
        break
else:
    check("обрез помечен и не рвёт токен на всех длинах", True)
# Число с единицей должно уцелеть целиком, а не превратиться в другую дозу.
for _limit in range(24, 60):
    _clipped = W.clip_at_sentence(_DOSE, _limit)
    if "7 мг" in _clipped and "7 мг/кг" not in _clipped:
        check("единица измерения не отрезана от числа", False, f"got {_clipped!r}")
        break
else:
    check("единица измерения не отрезана от числа", True)

bulk = [{"host": f"h{i}.example", "url": f"https://h{i}.example/", "tier": 5,
         "text": "Т" * 800} for i in range(10)]
fitted, dropped = W.fit_budget(bulk)
total = sum(len(e["text"]) for e in fitted)
check("блок влезает в потолок", total <= W.WEB_MAX_CHARS, f"got {total}")
check("невлезшие выдержки посчитаны", dropped == 10 - len(fitted), f"got {dropped}")

records = []


class _Collect(logging.Handler):
    def emit(self, record):
        records.append(record)


_handler = _Collect()
W.logger.addHandler(_handler)
W.logger.setLevel(logging.INFO)
try:
    W.fit_budget(bulk)
    W.rank_sources(raw_results)
finally:
    W.logger.removeHandler(_handler)
check("потери попали в журнал, а не в тишину",
      any("не влезло" in r.getMessage() for r in records)
      and any("filtered" in r.getMessage() for r in records),
      f"записей: {[r.getMessage()[:60] for r in records]}")
check("уровень не debug — иначе строки не будет видно",
      all(r.levelno >= logging.INFO for r in records),
      "корневой логгер на INFO: debug не эмитится")

print("\n[7] Промпт заземлён на выдержки")
# Модель, дополнившая веб-ответ собственной памятью, делает ссылки декорацией:
# врач считает утверждение подтверждённым источником, которого под ним нет.
prompt = W.build_lookup_prompt("Можно ли ставить биодентин при перфорации?", ranked)
check("вопрос попал в промпт", "биодентин при перфорации" in prompt)
check("источники пронумерованы", "[1]" in prompt and "[2]" in prompt)
check("запрет добавлять по памяти есть", "ТОЛЬКО на выдержки" in prompt
      and "по памяти" in prompt)
check("разрешено ответить «этого нет»", "этого нет" in prompt)
check("числа переносятся дословно", "дословно" in prompt)
check("противоречие источников не скрывается", "противоречат" in prompt)
check("уровень источника подписан", "исследование" in prompt)
check("пустой список источников не даёт промпта", W.build_lookup_prompt("вопрос", []) is None)

footer = W.format_sources_footer(ranked)
check("подпись со ссылками нумерована", footer.startswith("Источники:") and "1." in footer)
check("в подписи настоящие ссылки", "https://" in footer)

print("\n[8] Отказ поиска отличается от «ничего не нашлось»")
# Врач должен различать сломанный инструмент и честное отсутствие материала:
# в первом случае вопрос стоит повторить, во втором — нет.
failed = W.prepare("вопрос", [], error="providers_failed")
check("отказ поиска назван отказом", failed["prompt"] is None
      and failed["message"] == W.SEARCH_FAILED)
only_ads = W.prepare("вопрос", [
    "Имплантация от 19900. Акция, записаться!\n(Source: https://implant-msk.ru/p)"])
check("осталась только реклама — сказано прямо", only_ads["prompt"] is None
      and only_ads["message"] == W.NOTHING_USABLE)
check("две ситуации не совпадают по тексту", W.SEARCH_FAILED != W.NOTHING_USABLE)
good = W.prepare("Можно ли биодентин?", raw_results)
check("нормальный проход даёт промпт и подпись",
      good["prompt"] and good["footer"] and not good["message"])
check("отчёт доезжает до вызывающего", "total" in good["report"]
      and "budget_dropped" in good["report"])

print("\n[9] Проверки выше ловят поломку")
# Если бы сравнение доменов шло вхождением, секция [3] прошла бы на подставном
# домене. Здесь это показано прямо на функции сравнения.
check("суффиксное сравнение действительно строже вхождения",
      ("ada.org" in "canada.org") and not W._host_matches("canada.org", "ada.org"),
      "вхождение находит ada.org в canada.org — сравнение обязано это отличать")
check("уровни упорядочены по убыванию доверия",
      W.tier_of("pubmed.ncbi.nlm.nih.gov") < W.tier_of("rlsnet.ru") < W.tier_of("wikipedia.org")
      < W.tier_of("unknown.example"),
      "иначе сортировка ставит энциклопедию выше обзора")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
