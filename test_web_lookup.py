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

print("\n[10] Подпроцесс отдаёт СТРУКТУРУ, а не строку")
# Пока ссылка склеена с текстом, вынуть её можно только регуляркой. Замер по 556
# живым ссылкам из stomat_archive.db и stomat_wiki.db: адрес
# .../Оксид_циркония(IV) обе строковые формы обрывали на «(IV» — врач получает
# ссылку, которая не открывается, то есть утверждение без проверяемого источника.
# Сеть здесь не трогаем: провайдеры подменены фальшивыми модулями.
import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import types  # noqa: E402

import blocking_tools as B  # noqa: E402


def _entry(seq, index=0):
    """
    Результат поиска по номеру, не роняя прогон. Не структура -> пустой словарь.

    Обращение entry["url"] к СТРОКЕ даёт TypeError, и он обрывает весь файл на
    первой же неверной форме. Проверено диверсией: возврат строковой формы в
    _web_search_sync гасил 25 проверок ниже, включая всю совместимость со старой
    формой, и сводка PASSED/FAILED не печаталась вовсе — то есть поломка формы
    прятала за собой любую другую поломку разбора.
    """
    try:
        entry = seq[index]
    except Exception:
        return {}
    return entry if isinstance(entry, dict) else {}


# Адреса со скобкой — не редкость: именно так ru.wikipedia разводит
# стоматологические омонимы, и таких страниц бот ищет больше всего.
_TAVILY_URL = "https://ru.wikipedia.org/wiki/Пломба_(стоматология)"
_DDGS_URL = "https://ru.m.wikipedia.org/wiki/Оксид_циркония(IV)"

_fake_config = types.ModuleType("config")
_fake_config.SEARCH_PROVIDER = "tavily"
_fake_config.TAVILY_API_KEY = "fake-key-never-used"
_saved_modules = {name: sys.modules.get(name) for name in ("config", "tavily", "ddgs")}
sys.modules["config"] = _fake_config

_fake_tavily = types.ModuleType("tavily")


class _FakeTavilyClient:
    def __init__(self, api_key=None):
        pass

    def search(self, query=None, search_depth=None, max_results=None):
        return {"results": [{"content": "Биодентин закрывает перфорацию.", "url": _TAVILY_URL}]}


_fake_tavily.TavilyClient = _FakeTavilyClient
sys.modules["tavily"] = _fake_tavily

tav_results, tav_errors = B._web_search_sync("перфорация", 2)
check("tavily: результат — структура, а не строка",
      len(tav_results) == 1 and isinstance(tav_results[0], dict), f"got {tav_results!r}")
check("tavily: ключи text и url на месте",
      set(_entry(tav_results)) >= {"text", "url"}, f"got {sorted(_entry(tav_results))}")
check("tavily: ссылка со скобкой доехала посимвольно",
      _entry(tav_results).get("url") == _TAVILY_URL, f"got {_entry(tav_results).get('url')!r}")
# Выдержка обязана быть НЕПУСТОЙ: проверка «в тексте нет http» на пустом тексте
# проходит сама собой, и сломанная форма прошла бы как исправная.
check("tavily: ссылка не вклеена в текст выдержки",
      _entry(tav_results).get("text") and "http" not in _entry(tav_results)["text"],
      f"got {_entry(tav_results).get('text')!r}")
check("tavily: живой провайдер не считается отказавшим", tav_errors == [], f"got {tav_errors!r}")

_fake_config.SEARCH_PROVIDER = "duckduckgo"
_fake_ddgs = types.ModuleType("ddgs")


class _FakeDDGS:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text(self, *args, **kwargs):
        return [{"body": "МТА даёт лучший прогноз (мета-анализ).", "href": _DDGS_URL}]


_fake_ddgs.DDGS = _FakeDDGS
sys.modules["ddgs"] = _fake_ddgs

ddg_results, ddg_errors = B._web_search_sync("мта", 2)
check("ddgs: результат — структура, а не строка",
      len(ddg_results) == 1 and isinstance(ddg_results[0], dict), f"got {ddg_results!r}")
check("ddgs: ссылка со скобкой доехала посимвольно",
      _entry(ddg_results).get("url") == _DDGS_URL, f"got {_entry(ddg_results).get('url')!r}")
check("ddgs: ссылка не вклеена в текст выдержки",
      _entry(ddg_results).get("text") and "http" not in _entry(ddg_results)["text"]
      and "Source" not in _entry(ddg_results)["text"],
      f"got {_entry(ddg_results).get('text')!r}")
check("ddgs: живой провайдер не считается отказавшим", ddg_errors == [], f"got {ddg_errors!r}")

for name, module in _saved_modules.items():
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module

# Между ребёнком и родителем стоит JSON. Структура обязана пережить сериализацию:
# если она превратится в строку или в список, ссылка снова уедет в текст.
_wire = json.loads(json.dumps({"ok": True, "results": tav_results + ddg_results},
                              ensure_ascii=False))
check("структура переживает JSON между процессами",
      all(isinstance(item, dict) and item.get("url") for item in _wire["results"]),
      f"got {_wire['results']!r}")
check("ссылка со скобкой цела и после JSON",
      [_entry(_wire["results"], i).get("url") for i in range(len(_wire["results"]))]
      == [_TAVILY_URL, _DDGS_URL],
      f"got {[_entry(_wire['results'], i).get('url') for i in range(len(_wire['results']))]}")

# Слой качества обязан брать ссылку из ПОЛЯ. Проверяем поведением: гасим все три
# регулярки разбора строк — структурный путь не имеет права от них зависеть.
_saved_res = (W._SOURCE_MARKER_RE, W._TRAILING_URL_RE, W._ANY_URL_RE)
_never = re.compile(r"(?!x)x")
W._SOURCE_MARKER_RE, W._TRAILING_URL_RE, W._ANY_URL_RE = _never, _never, _never
try:
    structural = W.parse_result(_entry(_wire["results"]))
finally:
    W._SOURCE_MARKER_RE, W._TRAILING_URL_RE, W._ANY_URL_RE = _saved_res
check("структурный путь не зависит от регулярок разбора строк",
      structural and structural["url"] == _TAVILY_URL
      and structural["host"] == "ru.wikipedia.org", f"got {structural!r}")

# Худшее следствие подмены хоста: на странице бывает свой блок «источник», и по
# ссылке ИЗ ТЕКСТА считается уровень доверия и работает отсев рекламы. Измерено:
# обзор с pubmed при строковом разборе выбрасывался как реклама клиники, и врачу
# уходило «нашлась только реклама» — при обзоре на руках.
_poisoned = {"text": "Систематический обзор: биодентин не хуже МТА "
                     "(Источник: https://implant-msk.ru/blog)",
             "url": "https://pubmed.ncbi.nlm.nih.gov/40000000/"}
kept_poisoned, report_poisoned = W.rank_sources([_poisoned])
check("чужой маркер в тексте не подменяет хост источника",
      [e["host"] for e in kept_poisoned] == ["pubmed.ncbi.nlm.nih.gov"],
      f"got {[e['host'] for e in kept_poisoned]}, ads={report_poisoned['ads']}")
check("обзор не выброшен как реклама клиники", not report_poisoned["ads"],
      f"ads={report_poisoned['ads']}")
check("обзору присвоен уровень исследования",
      kept_poisoned and kept_poisoned[0]["tier"] == 1,
      f"got {kept_poisoned[0]['tier'] if kept_poisoned else None}")
_ready = W.prepare("Можно ли биодентин при перфорации?", [_poisoned])
check("врач получает промпт и ссылку, а не «нашлась только реклама»",
      _ready["prompt"] and "pubmed.ncbi.nlm.nih.gov/40000000/" in _ready["footer"]
      and not _ready["message"], f"got {_ready['message']!r} / {_ready['footer']!r}")

# Чужая ссылка не имеет права уехать в промпт внутри выдержки. Она не проходила
# ни ранжирование по уровню, ни отсев рекламы, а в подписи её нет — модель
# процитирует адрес клиники как часть систематического обзора, и врач получит
# ссылку, которую выдали за источник. Замер по stomat_archive.db: из 591 строки со
# ссылкой у 110 ссылка стоит В СЕРЕДИНЕ текста, то есть случай рядовой.
_ad_in_text = {"text": "Обзор: биодентин не хуже МТА. Разбор с ценами: "
                       "https://implant-msk.ru/blog/perf — там же протокол клиники.",
               "url": "https://pubmed.ncbi.nlm.nih.gov/40000000/"}
_ad_ready = W.prepare("Чем закрыть перфорацию?", [_ad_in_text])
check("рекламная ссылка из текста не уезжает в промпт",
      _ad_ready["prompt"] and "implant-msk.ru" not in _ad_ready["prompt"],
      f"got {_ad_ready['prompt']!r}")
check("настоящий источник в подписи остался",
      "pubmed.ncbi.nlm.nih.gov/40000000/" in _ad_ready["footer"], f"got {_ad_ready['footer']!r}")
# Смысл выдержки при этом обязан выжить: вырезаем ссылку, а не клинику текста.
check("клинический смысл выдержки не вырезан вместе со ссылкой",
      "биодентин не хуже МТА" in _ad_ready["prompt"]
      and "протокол клиники" in _ad_ready["prompt"], f"got {_ad_ready['prompt']!r}")
# Обе формы обязаны дать ОДНУ выдержку: расхождение путей и есть корень дефекта.
_ad_legacy = f"{_ad_in_text['text']} (Source: {_ad_in_text['url']})"
_legacy_ready = W.prepare("Чем закрыть перфорацию?", [_ad_legacy])
check("старая форма даёт ту же выдержку, что структурная",
      W.parse_result(_ad_legacy)["text"] == W.parse_result(_ad_in_text)["text"],
      f"стар={W.parse_result(_ad_legacy)['text']!r} нов={W.parse_result(_ad_in_text)['text']!r}")
check("старая форма тоже не тащит рекламную ссылку в промпт",
      _legacy_ready["prompt"] and "implant-msk.ru" not in _legacy_ready["prompt"],
      f"got {_legacy_ready['prompt']!r}")
# Выдержка из одной ссылки: как источник с номером она пустая, и модель поставит
# [1] под утверждением, которого в источнике нет.
check("выдержка без прозы не становится источником с номером",
      W.parse_result({"text": "https://ada.org/page", "url": "https://ada.org/page"}) is None,
      f"got {W.parse_result({'text': 'https://ada.org/page', 'url': 'https://ada.org/page'})!r}")
check("число в выдержке не пострадало от вырезания ссылки",
      W.parse_result({"text": "Артикаин не более 7 мг/кг, см. https://rlsnet.ru/x подробнее.",
                      "url": "https://rlsnet.ru/x"})["text"].count("7 мг/кг") == 1,
      f"got {W.parse_result({'text': 'Артикаин не более 7 мг/кг, см. https://rlsnet.ru/x подробнее.', 'url': 'https://rlsnet.ru/x'})!r}")

print("\n[11] Родитель отдаёт одну форму, что бы ни прислал ребёнок")
# Вызывающий, который потянется за entry["url"] у строки, получит TypeError по
# всему поиску: врач вместо ответа со ссылками не получит ничего. Старая форма
# может приехать из сохранённой нагрузки прошлой версии, поэтому обе обязаны
# сводиться к одной структуре.


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_saved_tool = B._run_json_tool


def _tool_returning(results):
    async def _stub(action, payload, timeout=None):
        return {"ok": True, "results": results}, None
    return _stub


_LEGACY = [
    f"Биодентин закрывает перфорацию. ({_TAVILY_URL})",            # старая форма tavily
    f"МТА даёт лучший прогноз.\n(Source: {_DDGS_URL})",            # старая форма ddgs
    "Артикаин 4% (Источник: https://rlsnet.ru/drugs/articaine)",   # форма search_engine.py
]
B._run_json_tool = _tool_returning(list(_LEGACY))
legacy_out, legacy_err = _run(B.web_search_async("биодентин", 3, timeout=5))
check("старые строки приведены к структуре",
      len(legacy_out) == 3 and all(isinstance(e, dict) for e in legacy_out),
      f"got {legacy_out!r}")
check("старая форма tavily: ссылка со скобкой восстановлена целиком",
      _entry(legacy_out, 0).get("url") == _TAVILY_URL, f"got {_entry(legacy_out, 0).get('url')!r}")
check("старая форма ddgs: ссылка со скобкой восстановлена целиком",
      _entry(legacy_out, 1).get("url") == _DDGS_URL, f"got {_entry(legacy_out, 1).get('url')!r}")
check("старая форма с русским маркером: ссылка восстановлена",
      _entry(legacy_out, 2).get("url") == "https://rlsnet.ru/drugs/articaine",
      f"got {_entry(legacy_out, 2).get('url')!r}")
_legacy_texts = [_entry(legacy_out, i).get("text", "") for i in range(len(_LEGACY))]
check("текст выдержки очищен от склеенной ссылки",
      all(text and "http" not in text for text in _legacy_texts), f"got {_legacy_texts!r}")

B._run_json_tool = _tool_returning([
    {"text": "Биодентин закрывает перфорацию.", "url": _TAVILY_URL},
    {"content": "Форма самого tavily.", "url": "https://ada.org/a"},
    {"body": "Форма самого ddgs.", "href": "https://who.int/b"},
])
new_out, _ = _run(B.web_search_async("биодентин", 3, timeout=5))
_new_urls = [_entry(new_out, i).get("url") for i in range(len(new_out))]
_new_texts = [_entry(new_out, i).get("text") for i in range(len(new_out))]
check("новая форма проходит без искажений",
      _new_urls == [_TAVILY_URL, "https://ada.org/a", "https://who.int/b"], f"got {_new_urls!r}")
check("имена полей самих провайдеров тоже приняты",
      _new_texts[1:] == ["Форма самого tavily.", "Форма самого ddgs."], f"got {_new_texts!r}")

B._run_json_tool = _tool_returning(["   ", {"text": "", "url": ""}, "Есть текст без ссылки"])
mixed_out, _ = _run(B.web_search_async("вопрос", 3, timeout=5))
check("пустышка не считается находкой", len(mixed_out) == 1, f"got {mixed_out!r}")
check("находка без ссылки не выброшена молча",
      _entry(mixed_out).get("text") == "Есть текст без ссылки"
      and _entry(mixed_out).get("url") == "", f"got {mixed_out!r}")


# Порченая нагрузка: не строка и не структура. Такой элемент выбрасывается, и
# выбросить его МОЛЧА — значит оставить «поиск нашёл 3, показал 1» без
# объяснения: врач не узнает, что часть выдачи потерялась, и разобраться потом
# будет нечем. Проверяем ЖУРНАЛ, а не только возврат: диверсия показала, что без
# этой проверки снятие записи не ронял ни одну из 94 проверок.


class LogTrap(logging.Handler):
    """Ловит записи blocking_tools, чтобы проверять ЖУРНАЛ, а не только возврат."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        out = []
        for record in self.records:
            try:
                out.append(record.getMessage())
            except Exception:
                out.append(str(record.msg))
        return "\n".join(out)


_trap = LogTrap()
_bt_logger = logging.getLogger("blocking_tools")
_bt_logger.addHandler(_trap)
_prev_level, _prev_prop = _bt_logger.level, _bt_logger.propagate
_bt_logger.setLevel(logging.DEBUG)
_bt_logger.propagate = False
try:
    B._run_json_tool = _tool_returning([None, 42, {"text": "Живая выдержка", "url": "https://ada.org/x"}])
    junk_out, _ = _run(B.web_search_async("вопрос", 3, timeout=5))
finally:
    _bt_logger.removeHandler(_trap)
    _bt_logger.setLevel(_prev_level)
    _bt_logger.propagate = _prev_prop

check("порченая нагрузка не доезжает до врача как выдержка",
      [_entry(junk_out, i).get("text") for i in range(len(junk_out))] == ["Живая выдержка"],
      f"got {junk_out!r}")
check("живая находка рядом с порченой не потеряна",
      _entry(junk_out).get("url") == "https://ada.org/x", f"got {junk_out!r}")
_junk_log = _trap.text()
check("порченая нагрузка попала в журнал, а не выброшена молча",
      "неизвестного вида" in _junk_log, f"журнал: {_junk_log!r}")
check("в журнале назван тип, иначе разбирать нечем",
      "NoneType" in _junk_log or "int" in _junk_log, f"журнал: {_junk_log!r}")
check("запись о порченой нагрузке — предупреждение, а не отладка",
      any(r.levelno >= logging.WARNING for r in _trap.records),
      f"уровни: {[r.levelno for r in _trap.records]}")


async def _tool_failing(action, payload, timeout=None):
    return None, "web-search failed: ddgs: RuntimeError: сеть недоступна"


B._run_json_tool = _tool_failing
fail_out, fail_err = _run(B.web_search_async("вопрос", 2, timeout=5))
check("отказ поиска остался отказом, а не пустой находкой",
      fail_out == [] and fail_err and "сеть недоступна" in fail_err, f"got {fail_out!r} {fail_err!r}")
B._run_json_tool = _saved_tool

# Сквозной путь: структура из подпроцесса -> промпт со ссылкой под ответом.
end_to_end = W.prepare("Чем закрыть перфорацию?", [
    {"text": "Биодентин закрывает перфорацию у взрослых.", "url": _TAVILY_URL},
    {"text": "Оксид циркония как каркас.", "url": _DDGS_URL},
])
check("сквозной путь даёт врачу рабочие ссылки",
      _TAVILY_URL in end_to_end["footer"] and _DDGS_URL in end_to_end["footer"],
      f"got {end_to_end['footer']!r}")
check("сквозной путь не теряет скобку в подписи",
      end_to_end["footer"].count("(стоматология)") == 1
      and end_to_end["footer"].count("(IV)") == 1, f"got {end_to_end['footer']!r}")

print("\n[12] Ветки, которые диверсия проходила молча")
# Три проверки ниже добавлены после того, как диверсии в этих ветках НЕ уронили
# ни одной из 106 проверок: ветка исполнялась, но её результат никто не смотрел.

# Полуструктурная нагрузка: словарь с текстом, но БЕЗ поля url. Такую отдаёт
# сохранённый ответ прошлой версии. Отбросить её — значит показать врачу
# утверждение без источника, то есть ровно тот отказ, ради которого слой и
# написан. Диверсия «return None вместо поиска ссылки в тексте» проходила молча.
_semi = W.parse_result(
    {"text": "Биодентин закрывает перфорацию, см. https://pubmed.ncbi.nlm.nih.gov/7/"})
check("словарь без поля url не выброшен: ссылка взята из текста",
      _semi and _semi["url"] == "https://pubmed.ncbi.nlm.nih.gov/7/"
      and _semi["host"] == "pubmed.ncbi.nlm.nih.gov", f"got {_semi!r}")
check("у полуструктурной нагрузки выдержка непустая и без ссылки",
      _semi and _semi["text"] and "http" not in _semi["text"], f"got {_semi!r}")

# Чужой маркер «(Источник: …)» обязан уйти из выдержки ЦЕЛИКОМ, а не оставить
# обрубок «(Источник: )». Обрубок уезжает в промпт, и модель дописывает под него
# источник, которого в выдержке нет: врач читает ссылку, которой не было.
# Диверсия «снять со _strip_urls снятие маркера» проходила молча: проверка «в
# тексте нет http» на обрубке выполняется.
_marker_left = W.parse_result(
    {"text": "Систематический обзор: биодентин не хуже МТА "
             "(Источник: https://implant-msk.ru/blog)",
     "url": "https://pubmed.ncbi.nlm.nih.gov/40000000/"})
check("от чужого маркера в выдержке не осталось обрубка",
      _marker_left and "Источник" not in _marker_left["text"]
      and "Source" not in _marker_left["text"], f"got {_marker_left['text']!r}")

# Словарь из одних пробелов — не находка. Диверсия «снять .strip() в
# _as_search_entry» проходила молча: пробел непустой, фильтр пустышек его
# пропускает, и поиск отчитывается «нашлось 2» при одной показанной выдержке.
_blank_tool = B._run_json_tool
try:
    B._run_json_tool = _tool_returning([
        {"text": "   ", "url": "  "},
        {"text": "Живая выдержка", "url": "https://ada.org/x"},
    ])
    _blank_out, _ = _run(B.web_search_async("вопрос", 3, timeout=5))
finally:
    B._run_json_tool = _blank_tool
check("словарь из пробелов не считается находкой",
      len(_blank_out) == 1 and _entry(_blank_out).get("text") == "Живая выдержка",
      f"got {_blank_out!r}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
