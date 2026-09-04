"""
Слой качества над веб-поиском: ранжирование источников и заземление ответа.

Зачем он вообще. Механизм поиска в проекте построен и рабочий: подпроцесс
(blocking_tools.web_search_async), обёртка (search_engine_safe.perform_search),
свой набор проверок в test_fix_subprocess.py. Долго его не вызывал НИКТО: код
был, а функции у бота не было. ЭТО ЗАКРЫТО — команда /web подключена
(assistant.py:3341 разбор, :3405 вызов run_lookup, :308 меню Telegram, :3081
строка в /help), и путь сторожат test_web_lookup и test_search_command. Фразу
про «не вызывает никто» не возвращать: она читалась как живой долг и уже
отправила одного исполнителя подключать подключённое.

Зачем он нужен ВРАЧУ — измерено по stomat_wiki.db и stomat_archive.db, только
чтение. Не «закрыть пробелы корпуса»: проверка по двадцати клиническим терминам
(биодентин, MTA, артикаин, бисфосфонаты, варфарин, синус-лифтинг, бруксизм,
элайнеры) не нашла НИ ОДНОГО термина, о котором в чате спрашивали, а в корпусе не
было ни факта. Корпус тему покрывает. Дело в другом:

  * Из 12 784 фактов ссылку содержат 4 (0.03%), DOI — ноль, PubMed упомянут в
    двух. То есть бот физически не может дать врачу источник, который тот
    откроет и проверит сам. Всё, что бот знает, — это мнение коллег по чату,
    пересказанное без атрибуции.
  * Архив кончается 2026-02-19, самый свежий факт — того же дня. На сегодня это
    160 дней слепоты: ничего о новом материале, отозванном препарате или
    изменившейся рекомендации в корпусе нет и не появится.

Поэтому веб-поиск здесь — не замена корпусу, а другой инструмент: он даёт
ПРОВЕРЯЕМУЮ ссылку и свежесть там, где чат-мнение не годится.

Почему нельзя просто вызвать perform_search и отдать врачу что вернулось.
Аудитория — практикующие стоматологи, а выдача общего поиска по стоматологическим
запросам на первых местах держит рекламу клиник: цены на имплантацию, акции,
рассрочка, «запишитесь на бесплатную консультацию». Для профессионала такой ответ
хуже молчания: он не только бесполезен, он выдаёт бота за источник, которому
доверять нельзя. Плюс выдача уходит в валидатор как справочный материал, то есть
мусор попадает не только в текст ответа, но и в критерий его проверки.

Поэтому здесь три вещи, которых в поиске не было:
  1. Приведение результата к (текст, ссылка, хост). Подпроцесс теперь отдаёт
     структуру {"text", "url"} — ссылка приезжает отдельным полем и её больше не
     выковыривают регуляркой. Разбор строк оставлен как совместимость со старой
     формой (tavily клеил "текст (url)", ddgs — "текст\\n(Source: url)"): такая
     нагрузка может приехать из сохранённого ответа прошлой версии, и молча
     потерять на ней ссылку хуже, чем держать два пути.
  2. Ранжирование по уровню источника и отсев рекламы клиник.
  3. Заземлённый промпт: отвечать ТОЛЬКО по выдержкам, помечать утверждения
     номером источника, при нехватке данных говорить об этом прямо.

Модуль намеренно без внешних зависимостей и без побочных эффектов при импорте:
ни сети, ни чтения конфига, ни настройки логирования.
"""
import asyncio
import logging
import re
import time
from urllib.parse import urlsplit

# Обрезка по границе предложения — одна реализация на бот, в html_safe (импорт
# без побочных эффектов: там только html, logging и re). Здесь нужен один текст,
# счётчик отброшенного берёт fit_budget по разнице длин. Имя clip_at_sentence
# оставлено как ссылка на неё: своей логики за ним больше нет, а три копии
# расходились на 6 входах из 8 замера.
from html_safe import clip_at_sentence_text as clip_at_sentence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Разбор результата провайдера
# ---------------------------------------------------------------------------

# Маркер ссылки в конце строки. "Source" писал подпроцесс до перехода на
# структуру, "Источник" — старая обёртка search_engine.py; поддерживаем оба,
# потому что формат в проекте уже разошёлся, и молча потерять ссылку хуже, чем
# принять две формы.
#
# Скобка внутри адреса РАЗРЕШЕНА (класс исключает только пробел и `]`). Замер по
# 556 живым ссылкам из stomat_archive.db и stomat_wiki.db: прежний класс
# `[^\s\)\]]+` обрывал .../Оксид_циркония(IV) на «(IV», и такая ссылка не
# открывается — врач видит источник, а проверить утверждение не может. Именно так
# ru.wikipedia разводит стоматологические омонимы: Пломба_(стоматология),
# Мост_(стоматология), Коронка_(стоматология). Лишнюю скобку от самого текста
# снимает _trim_unbalanced_parens.
_SOURCE_MARKER_RE = re.compile(
    r"[\(\[]\s*(?:source|источник)\s*:\s*(https?://[^\s\]]+)\s*[\)\]]",
    re.IGNORECASE,
)
# tavily клеил ссылку без слова-маркера: "текст (https://...)".
_TRAILING_URL_RE = re.compile(r"[\(\[]\s*(https?://[^\s\]]+)\s*[\)\]]\s*$")
_ANY_URL_RE = re.compile(r"https?://[^\s\]]+")


def _trim_unbalanced_parens(url):
    """
    Снять с хвоста ссылки закрывающие скобки, которых она сама не открывала.

    Скобка внутри адреса нужна (см. Пломба_(стоматология)), скобка от обёртки
    текста — нет: и то и другое кончается ссылкой, которая не открывается, а
    ответ без работающей ссылки для врача равен утверждению без источника.
    """
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def _strip_urls(text):
    """
    Выкинуть из ВЫДЕРЖКИ все ссылки: источник задаёт поле url, а не текст страницы.

    Замер по stomat_archive.db: из 591 сообщения со ссылкой у 110 ссылка стоит в
    СЕРЕДИНЕ текста, а не в конце, — то есть ссылка внутри прозы на этом домене
    обычное дело, и текст страницы провайдера ведёт себя так же. Такая ссылка не
    проходила ни ранжирование по уровню, ни отсев рекламы: измерено, что адрес
    клиники внутри выдержки из pubmed уезжал в промпт целиком, а в подписи для
    врача его не было. Модель цитирует его как часть источника — врач получает
    ссылку на рекламу под видом систематического обзора и проверить её не может.
    """
    text = _SOURCE_MARKER_RE.sub(" ", text)
    text = _ANY_URL_RE.sub(" ", text)
    # Скобки, осиротевшие после снятой ссылки: «подробнее ( )» в клиническом
    # тексте читается как пропущенное слово, а не как убранный адрес.
    text = re.sub(r"[\(\[]\s*[\)\]]", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _host_of(url):
    """Хост в нижнем регистре без www. Пустая строка, если ссылка не разобралась."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def parse_result(raw):
    """
    Результат провайдера -> {"text", "url", "host"}. None, если разбирать нечего.

    Основной путь — структурный: подпроцесс отдаёт {"text", "url"}, ссылка берётся
    из своего поля и никакому разбору не подвергается. Разбор строки — путь
    совместимости для старой формы: ссылку ищем в три приёма, от самого надёжного
    к самому общему — маркер "(Source: ...)", затем ссылка в скобках на конце
    строки, затем любая ссылка в тексте. Найденный хвост из текста убираем — иначе
    он уедет в промпт как часть выдержки и модель начнёт цитировать url'ы
    вперемешку с фактами.
    """
    if isinstance(raw, dict):
        # Структурная форма подпроцесса. Ключи content/body/href — имена самих
        # провайдеров (tavily и ddgs), принимаем и их.
        text = (raw.get("text") or raw.get("content") or raw.get("body") or "").strip()
        url = _trim_unbalanced_parens((raw.get("url") or raw.get("href") or "").strip())
        if not text:
            return None
        if url:
            # Поле со ссылкой — истина. Искать ссылку в тексте при заполненном
            # поле нельзя: на странице бывает свой блок «источник», и хост из него
            # уводит и уровень доверия, и отсев рекламы на чужой домен. Измерено:
            # обзор с pubmed при таком разборе выбрасывался как реклама клиники, и
            # врачу уходило «нашлась только реклама».
            #
            # Из самой выдержки ссылки убираем: они не источник и в подпись не
            # попадают, а в промпте модель цитирует их наравне с фактами.
            excerpt = _strip_urls(text)
            if not excerpt:
                # От выдержки осталась одна ссылка. Показать её как источник с
                # номером — значит дать модели пустое место, на которое можно
                # сослаться: [1] будет стоять под утверждением, которого в
                # источнике нет.
                return None
            return {"text": excerpt, "url": url, "host": _host_of(url)}
        # Поля со ссылкой нет — полуструктурная нагрузка старой версии, ссылка
        # может лежать внутри текста. Не поискать её значит показать врачу
        # утверждение без источника, то есть ровно тот отказ, ради которого этот
        # слой и существует.
        raw = text

    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    url = ""
    match = _SOURCE_MARKER_RE.search(text)
    if match:
        url = _trim_unbalanced_parens(match.group(1))
        text = (text[:match.start()] + text[match.end():]).strip()
    else:
        match = _TRAILING_URL_RE.search(text)
        if match:
            url = _trim_unbalanced_parens(match.group(1))
            text = text[:match.start()].strip()
        else:
            found = _ANY_URL_RE.search(text)
            if found:
                url = _trim_unbalanced_parens(found.group(0))
                # Ссылку из середины текста тоже вырезаем: иначе она уедет в
                # промпт внутри выдержки, и модель начнёт цитировать url'ы
                # вперемешку с фактами. Вырезаем ровно ссылку: снятая скобка
                # принадлежит тексту, и забрать её значит испортить выдержку.
                text = (text[:found.start()] + text[found.start() + len(url):]).strip()

    # Извлечённую ссылку убрали выше, но в тексте могут остаться ДРУГИЕ: страница
    # ссылается на партнёров и на себя. Обе формы обязаны давать одну выдержку,
    # иначе расхождение путей вернётся тем же классом отказа.
    text = _strip_urls(text)
    text = text.strip().strip("•▪\U0001f539-– \n\t")
    if not text:
        return None
    return {"text": text, "url": url, "host": _host_of(url)}


# ---------------------------------------------------------------------------
# Уровень источника
# ---------------------------------------------------------------------------

# Уровни по убыванию доверия. Сравнение идёт по ХОСТУ и по суффиксу, а не
# подстрокой по всей ссылке: "ada.org" подстрокой находится в "canada.org", а
# "who.int" — в "pwho.int". Совпадение подстрокой на доменах и на русском тексте
# в этом проекте ловилось уже восемь раз, так что здесь только суффиксное
# сравнение с точкой.
SOURCE_TIERS = (
    # 1. Исследования и систематические обзоры.
    (1, (
        "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "cochrane.org",
        "cochranelibrary.com", "jada.ada.org", "nature.com", "thelancet.com",
        "sciencedirect.com", "link.springer.com", "onlinelibrary.wiley.com",
        "bmj.com", "jclinmed.com", "frontiersin.org", "mdpi.com",
    )),
    # 2. Профессиональные организации и регуляторы: клинические рекомендации,
    # инструкции к препаратам, официальные позиции.
    (2, (
        "ada.org", "efp.org", "aae.org", "aaoms.org", "who.int", "fdiworlddental.org",
        "grls.rosminzdrav.ru", "rlsnet.ru", "drugs.com", "dailymed.nlm.nih.gov",
        "e-lactancia.org", "medscape.com", "minzdrav.gov.ru", "cr.minzdrav.gov.ru",
        "e-stomatology.ru", "straumann.com", "dentsplysirona.com",
    )),
    # 3. Русскоязычная профессиональная периодика и научные архивы.
    (3, (
        "cyberleninka.ru", "elibrary.ru", "mediasphera.ru", "dentalmagazine.ru",
        "stomatologiya.info", "dental-press.com", "rusdent.com", "dentalclub.ru",
        "endoexperience.com",
    )),
    # 4. Энциклопедии: годятся как определение термина, не как основание решения.
    (4, ("wikipedia.org", "who.int.wikipedia.org", "britannica.com")),
)

# Уровень для всего остального. Не «мусор» — просто без известной репутации:
# такой источник берём, но последним и с пометкой.
UNKNOWN_TIER = 5

# Признаки рекламы клиники в ХОСТЕ. Здесь допустима проверка вхождением: это
# именно поиск слова внутри имени домена ("implant-moscow.ru"), а не сравнение
# доменов между собой.
#
# ДОЛГ P1 врач: маркер ищется подстрокой ВНУТРИ метки домена, и единственная
#   защита авторитетного источника — попасть в SOURCE_TIERS руками -> замер
#   29 июля 2026 на 15 настоящих источниках: 7 выброшено как реклама клиники —
#   clinicaltrials.gov, clinicaltrialsregister.eu, mayoclinic.org,
#   my.clevelandclinic.org, clinicalkey.com (все по подстроке "clinic") и
#   aaid-implant.org (по "implant"); на выдаче из clinicaltrials.gov плюс
#   mayoclinic.org врач получает НЕ ссылки, а текст «нашлась только реклама
#   клиник — профессиональных материалов нет», то есть регистр клинических
#   исследований выдаётся за рекламу. Отсев самой рекламы при этом не страдает:
#   4 рекламных домена из 4 отсеяны и без этих маркеров по тексту. Тот же класс
#   «совпадение подстрокой», что в проекте ловился восемь раз; правило рядом
#   (_host_matches) уже сравнивает по границе метки — маркеры хоста на него не
#   перевели.
JUNK_HOST_MARKERS = (
    "stomatolog-", "-stomatolog", "implant", "implanty", "zubi", "zuby", "clinic",
    "klinika", "dental-clinic", "prices", "price", "zapis", "vsevrachi", "doctor-",
    "zoon", "prodoctorov", "napopravku", "docdoc", "sberhealth", "yandex",
    "avito", "otzovik", "irecommend", "stom-", "-stom",
)

# Признаки рекламы в ТЕКСТЕ выдержки. Профессиональный материал не зовёт
# записаться и не называет цену со скидкой.
AD_TEXT_MARKERS = (
    "запишитесь", "записаться на приём", "записаться на прием", "бесплатная консультация",
    "акция", "скидка", "рассрочка", "от 1990", "цены на", "стоимость лечения",
    "наши специалисты", "наша клиника", "лучшая клиника", "звоните", "оставьте заявку",
    "лицензия №", "работаем без выходных",
)


def _host_matches(host, domain):
    """Точное совпадение хоста или его поддомена. 'ada.org' не матчит 'canada.org'."""
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def tier_of(host):
    """Уровень доверия к хосту: 1 — лучший, UNKNOWN_TIER — неизвестный."""
    for tier, domains in SOURCE_TIERS:
        for domain in domains:
            if _host_matches(host, domain):
                return tier
    return UNKNOWN_TIER


def is_advertising(entry):
    """
    True, если выдержка похожа на рекламу клиники. Причина — вторым значением.

    Известный профессиональный источник рекламой не объявляется никогда: у
    журнала может быть в тексте слово «акция» (акция препарата, акционный потенциал),
    и терять из-за этого систематический обзор нельзя.
    """
    host = entry.get("host") or ""
    if tier_of(host) < UNKNOWN_TIER:
        return False, ""
    for marker in JUNK_HOST_MARKERS:
        if marker in host:
            return True, f"домен рекламный ({marker})"
    low = (entry.get("text") or "").lower()
    hits = [marker for marker in AD_TEXT_MARKERS if marker in low]
    if len(hits) >= 2:
        # Один маркер — совпадение; два и больше в одной выдержке это уже реклама.
        return True, "текст рекламный (" + ", ".join(hits[:3]) + ")"
    return False, ""


# ---------------------------------------------------------------------------
# Бюджет текста
# ---------------------------------------------------------------------------

# Потолок на весь блок выдержек. Тот же порядок, что у корпуса знаний в
# assistant.py (_CORPUS_MAX_CHARS = 6000): выдержки идут в тот же промпт и в тот
# же справочный материал валидатора, а окно у них общее.
WEB_MAX_CHARS = 4000
# Потолок на одну выдержку. Провайдер иногда отдаёт полстраницы, и одна такая
# выдержка съедала бы весь блок, оставив остальные источники за бортом.
WEB_ENTRY_MAX_CHARS = 900
# Сколько источников максимум показываем врачу. Больше пяти ссылок в сообщении
# Telegram — это стена, которую никто не открывает.
WEB_MAX_SOURCES = 5


# ---------------------------------------------------------------------------
# Пайплайн
# ---------------------------------------------------------------------------

def _normalize(text):
    """Текст для сравнения выдержек: регистр и пробелы не должны мешать."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def rank_sources(raw_results, max_sources=WEB_MAX_SOURCES):
    """
    Разобрать, отсеять рекламу, схлопнуть дубли, отсортировать по уровню.

    Возвращает (kept, report). report — что и почему выброшено; он обязателен:
    молча урезать выдачу значит показать врачу два источника из семи и выдать это
    за всё, что есть в открытых источниках.
    """
    parsed = []
    report = {"total": 0, "unparsed": 0, "ads": [], "duplicates": 0, "over_limit": 0}

    for raw in raw_results or []:
        report["total"] += 1
        entry = parse_result(raw)
        if not entry:
            report["unparsed"] += 1
            continue
        is_ad, why = is_advertising(entry)
        if is_ad:
            report["ads"].append(f"{entry['host'] or '?'}: {why}")
            continue
        entry["tier"] = tier_of(entry["host"])
        parsed.append(entry)

    # Дубли: один и тот же хост часто отдаёт несколько почти одинаковых выдержек,
    # и они РАЗНОЙ длины — вторая обычно та же фраза плюс хвост. Сравнение по
    # фиксированному префиксу такие пары не ловит (первые 80 символов уже
    # расходятся), поэтому сравниваем нормализованные тексты на вложенность.
    unique = []
    for entry in sorted(parsed, key=lambda e: (e["tier"], -len(e["text"]))):
        norm = _normalize(entry["text"])
        twin = None
        for kept in unique:
            if kept["host"] != entry["host"]:
                continue
            other = _normalize(kept["text"])
            shorter, longer = sorted((norm, other), key=len)
            if not shorter:
                continue
            # ТОЛЬКО вложенность. Похожесть по общему префиксу проверять нельзя:
            # «доза для взрослых 7 мг/кг» и «доза для взрослых 5 мг/кг» совпадают
            # на 88% начала, и порог 80% молча выбросил бы одну из двух ДОЗ.
            # Проверено: на девяти разных обзорах с общим началом «Обзор номер»
            # префиксный порог схлопывал все девять в один.
            if shorter in longer:
                twin = kept
                break
        if twin is None:
            unique.append(entry)
        else:
            report["duplicates"] += 1

    ordered = sorted(unique, key=lambda e: (e["tier"], -len(e["text"])))
    if len(ordered) > max_sources:
        report["over_limit"] = len(ordered) - max_sources
        ordered = ordered[:max_sources]

    if report["ads"] or report["unparsed"] or report["over_limit"]:
        logger.info(
            "web lookup filtered: всего=%s принято=%s реклама=%s без_ссылки=%s "
            "дубли=%s сверх_лимита=%s (%s)",
            report["total"], len(ordered), len(report["ads"]), report["unparsed"],
            report["duplicates"], report["over_limit"], "; ".join(report["ads"][:3]),
        )
    return ordered, report


def fit_budget(sources, max_chars=WEB_MAX_CHARS, entry_max=WEB_ENTRY_MAX_CHARS):
    """Уложить выдержки в бюджет, обрезая по предложению, и сказать, что выпало."""
    out = []
    used = 0
    dropped = 0
    for entry in sources:
        text = clip_at_sentence(entry["text"], entry_max)
        cost = len(text) + 2
        if used + cost > max_chars:
            dropped += 1
            continue
        used += cost
        trimmed = dict(entry)
        trimmed["text"] = text
        out.append(trimmed)
    if dropped:
        logger.info("web lookup budget: %s выдержек не влезло в %s символов",
                    dropped, max_chars)
    return out, dropped


_TIER_LABEL = {
    1: "исследование",
    2: "профессиональная организация или регулятор",
    3: "профессиональная периодика",
    4: "энциклопедия",
    UNKNOWN_TIER: "источник без известной репутации",
}


def build_lookup_prompt(question, sources):
    """
    Промпт, заземлённый на выдержки. Пустой список источников -> None.

    Никакой свободы «дополнить по памяти»: у бота есть отдельный путь для ответов
    из корпуса знаний, а этот путь существует ровно затем, чтобы ответ опирался на
    показанные врачу ссылки. Модель, дополнившая веб-ответ собственной памятью,
    делает ссылки декорацией.
    """
    if not sources:
        return None
    blocks = []
    for number, entry in enumerate(sources, 1):
        label = _TIER_LABEL.get(entry.get("tier", UNKNOWN_TIER), _TIER_LABEL[UNKNOWN_TIER])
        blocks.append(f"[{number}] {entry['host'] or 'без домена'} — {label}\n{entry['text']}")
    excerpts = "\n\n".join(blocks)
    return (
        "Ты отвечаешь практикующему стоматологу. Ниже выдержки из открытых "
        "источников, найденные по его вопросу.\n\n"
        f"ВОПРОС: {question}\n\n"
        f"ВЫДЕРЖКИ:\n{excerpts}\n\n"
        "ПРАВИЛА ОТВЕТА:\n"
        "1. Опирайся ТОЛЬКО на выдержки выше. Ничего не добавляй по памяти.\n"
        "2. После каждого утверждения ставь номер источника в квадратных скобках.\n"
        "3. Если выдержки на вопрос не отвечают — скажи это прямо и не выдумывай. "
        "Ответ «в найденных источниках этого нет» — правильный ответ.\n"
        "4. Дозировки, концентрации, сроки и противопоказания переноси дословно. "
        "Если в выдержке числа нет — не называй его.\n"
        "5. Если источники противоречат друг другу, покажи расхождение, а не "
        "выбирай одну сторону молча.\n"
        "6. Пиши как коллега коллеге: по делу, без вводных и без рекламы."
    )


def format_sources_footer(sources):
    """Нумерованный список ссылок под ответом. Пусто -> пустая строка."""
    if not sources:
        return ""
    lines = ["Источники:"]
    for number, entry in enumerate(sources, 1):
        url = entry.get("url") or ""
        host = entry.get("host") or "источник"
        title = (entry.get("title") or "").strip()
        # Очистка от сырых внутренних редиректов Google Vertex AI Search
        if "vertexaisearch.cloud.google.com" in url or "vertexaisearch.cloud.google.com" in host:
            label = title or "Научная публикация / Clinical Evidence"
            lines.append(f"{number}. 📖 {label}")
        elif url:
            lines.append(f"{number}. {host} — {url}")
        else:
            lines.append(f"{number}. {host}")
    return "\n".join(lines)


# Что сказать врачу, когда поиск сработал, но профессиональных источников не
# осталось. Это НЕ то же самое, что отказ поиска: врач должен различать «в
# открытых источниках нет» и «поиск не сработал».
NOTHING_USABLE = (
    "В открытых источниках по этому вопросу нашлась только реклама клиник — "
    "профессиональных материалов нет. Спрошу иначе или ответь по своему опыту."
)
SEARCH_FAILED = (
    "Поиск не отработал (внешний сервис недоступен). Это сбой инструмента, а не "
    "отсутствие информации."
)


def prepare(question, raw_results, error=None):
    """
    Полный проход: разбор -> отсев -> бюджет -> промпт и подпись.

    Возвращает словарь с ключами prompt, footer, sources, report, message.
    message заполнен только когда отвечать не из чего, и тогда prompt равен None:
    вызывающему остаётся отправить message как есть.
    """
    if error and not raw_results:
        logger.warning("web lookup failed: %s", str(error)[:200])
        return {"prompt": None, "footer": "", "sources": [], "report": {},
                "message": SEARCH_FAILED}

    ranked, report = rank_sources(raw_results)
    sources, dropped = fit_budget(ranked)
    report["budget_dropped"] = dropped
    if not sources:
        return {"prompt": None, "footer": "", "sources": [], "report": report,
                "message": NOTHING_USABLE}
    return {
        "prompt": build_lookup_prompt(question, sources),
        "footer": format_sources_footer(sources),
        "sources": sources,
        "report": report,
        "message": "",
    }


# ---------------------------------------------------------------------------
# Живой проход команды врача: поиск -> отсев -> заземлённый ответ
# ---------------------------------------------------------------------------
#
# Всё выше — чистые функции, и до этой правки на них НИКТО не звонил: слой
# качества, транспорт (blocking_tools.web_search_async) и обёртка
# (search_engine_safe.perform_search) существовали, а команды у врача не было.
# Замер по assistant.py, main.py и summarizer.py: слов web_search_async,
# perform_search, search_engine_safe, web_lookup, DDGS, tavily — ноль во всех
# трёх файлах. То есть на вопрос про материал, появившийся после 2026-02-19
# (последняя дата в архиве, на сегодня 160 дней назад), бот отвечал по памяти
# чата, а ссылку в корпусе содержат 4 факта из 12 784.
#
# Проход здесь, а не в assistant.py, по двум причинам. Первая: сеть и генерация
# приходят ПАРАМЕТРАМИ, поэтому модуль по-прежнему не тянет ни blocking_tools,
# ни config — это сторожит test_web_lookup.py разбором дерева импортов, и
# нарушение уронило бы бота на старте. Вторая: проверить поведение (ссылка в
# ответе, честный отказ, WARNING в журнале) можно подставным провайдером, без
# единого живого запроса в сеть.

# --- Бюджеты. Один набор, каждое следующее число считается из предыдущих ------
#
# Вложенность бюджетов в этом проекте ловилась ЧЕТЫРЕ раза, поэтому ни одного
# независимого литерала: всё, что можно вывести, выведено.
#
# Ключевой замер, из-за которого числа не такие, как кажется: родитель
# подпроцесса ждёт НЕ свой бюджет, а бюджет ребёнка плюс запас на его подъём
# (blocking_tools._run_json_tool: `deadline = timeout + _SUBPROCESS_STARTUP_SLACK_SECONDS`,
# запас 10 с). Значит search_engine_safe.perform_search со своими «45 с и до двух
# попыток» стоит вызывающему не 90 с, а 110 с. Прежний вызывающий, посчитавший
# 90, вылетел бы по своему таймауту на 20 с раньше, чем поиск успел бы честно
# отказаться, — и врач не получил бы даже причины.
SUBPROCESS_SLACK_SECONDS = 10.0

# Бюджет РЕБЁНКА на одну попытку поиска. Число из search_engine_safe.perform_search:
# второе число рядом разъедется, а поведение провайдера от места вызова не зависит.
SEARCH_ATTEMPT_TIMEOUT_SECONDS = 45.0

# Две попытки: полный запрос, затем укороченный. Так делает perform_search, и это
# не украшение — общий поиск по длинной клинической фразе часто отдаёт пусто, а по
# четырём словам находит.
SEARCH_ATTEMPTS = 2
SEARCH_SHORT_WORDS = 4

# Сколько находок просим у провайдера. Показываем максимум WEB_MAX_SOURCES = 5, но
# просить ровно пять нельзя: отсев рекламы клиник съедает часть выдачи, и тогда
# врач получит «ничего пригодного» при живых источниках на второй странице.
SEARCH_MAX_RESULTS = 8

# Что одна попытка и весь поиск стоят ВЫЗЫВАЮЩЕМУ.
SEARCH_ATTEMPT_COST_SECONDS = SEARCH_ATTEMPT_TIMEOUT_SECONDS + SUBPROCESS_SLACK_SECONDS
SEARCH_TOTAL_COST_SECONDS = SEARCH_ATTEMPT_COST_SECONDS * SEARCH_ATTEMPTS

# Бюджет РЕБЁНКА на генерацию ответа. 90 с — то же, что на всех остальных путях
# ассистента (generate_gemini_text_async(..., timeout=90)).
ANSWER_TIMEOUT_SECONDS = 90.0
ANSWER_COST_SECONDS = ANSWER_TIMEOUT_SECONDS + SUBPROCESS_SLACK_SECONDS

# Меньше этого генерацию не начинаем. Запрос, которому не хватит времени даже
# соединиться, только сожжёт остаток бюджета: лучше отдать врачу найденные ссылки.
ANSWER_MIN_TIMEOUT_SECONDS = 20.0

# Троттлинг LLM-шлюза разносит СТАРТЫ запросов на 3 с
# (blocking_tools._GEMINI_MIN_INTERVAL_SECONDS). Ждать под этой блокировкой —
# время из нашего бюджета, и не учтённое оно съедало бы конец генерации.
LLM_PACE_SLACK_SECONDS = 5.0

# Полная цена прохода для вызывающего: 110 + 100 + 5 = 215 с.
LOOKUP_TOTAL_COST_SECONDS = (
    SEARCH_TOTAL_COST_SECONDS + ANSWER_COST_SECONDS + LLM_PACE_SLACK_SECONDS
)

# Потолок готового сообщения ДО экранирования. Жёсткий предел Telegram 4096, и
# битая или переросшая разметка отклоняет сообщение ЦЕЛИКОМ — врач не увидит ни
# ответа, ни ссылок. Запас нужен под clean_html_formatting: он превращает «&» в
# «&amp;» (+4 символа на каждый, а «&» в адресах обычное дело) и при длине больше
# 4000 срывается в аварийный режим — снимает ВСЮ разметку и дописывает служебную
# приписку. 3400 = 4000 минус 600 на экранирование и приписку.
MESSAGE_MAX_CHARS = 3400

OUTCOME_OK = "ok"
OUTCOME_SEARCH_FAILED = "search_failed"
OUTCOME_NOTHING_FOUND = "nothing_found"
OUTCOME_NOTHING_USABLE = "nothing_usable"
OUTCOME_ANSWER_FAILED = "answer_failed"
OUTCOME_NO_BUDGET = "no_budget"
OUTCOME_EMPTY_QUERY = "empty_query"

# Пустая выдача БЕЗ отказа провайдера — это честное «не нашлось», и путать её с
# отсевом рекламы нельзя: prepare() на пустом списке отдаёт NOTHING_USABLE, то
# есть говорит врачу про рекламу клиник там, где провайдер вернул ноль строк.
# Врач по такому тексту решит, что тема утонула в рекламе, и переспросит иначе —
# вместо того чтобы понять, что искать надо другими словами.
NOTHING_FOUND = (
    "В открытых источниках по этому запросу ничего не нашлось. Попробуйте другие "
    "слова: поиск идёт по всему интернету, а не по нашей базе."
)
# Источники есть, а ответа по ним нет. Ссылки всё равно отдаём: для клинического
# вопроса проверяемый источник без пересказа полезнее пересказа без источника.
ANSWER_FAILED_PREFIX = (
    "Источники нашлись, но связный ответ по ним собрать не удалось (сбой "
    "генерации). Вот сами источники — они по вашему запросу:"
)
NO_BUDGET_PREFIX = (
    "Источники нашлись, но времени на разбор не осталось: поиск занял почти весь "
    "срок команды. Вот сами источники:"
)
EMPTY_QUERY = (
    "После очистки от знаков в запросе не осталось слов для поиска. Напишите "
    "запрос словами — например «биодентин перфорация дна полости»."
)

INTENT_WEB_SEARCH = "INTENT_WEB_SEARCH"

# Из запроса убираем только пунктуацию. Точка, дробь, процент и дефис остаются:
# «0.5%», «мг/кг» и «синус-лифтинг» без них превращаются в другие слова, а доза,
# потерявшая дробь, — это уже другая доза.
_QUERY_JUNK_RE = re.compile(r"[^\w\s./%+-]", re.UNICODE)

# Префиксы и фразы прямого поискового триггера
_SEARCH_TRIGGER_PREFIXES = [
    # "найди..."
    r"^найди(?:\s+(?:мне|пожалуйста|инфу|информацию|данные|публикации|статьи|исследования|материалы))?\s+(?:по|про|о|об|в\s+сети|в\s+интернете|в\s+pubmed|в\s+пабмеде|в\s+гугле)?\s*",
    r"^найти(?:\s+(?:инфу|информацию|данные|публикации|статьи|исследования))?\s+(?:по|про|о|об)?\s*",
    r"^отыщи(?:\s+(?:статьи|информацию))?\s+(?:по|про|о|об)?\s*",
    # "погугли / загугли / поищи..."
    r"^(?:погугли|загугли|прогугли|погуглить|загуглить)(?:\s+(?:мне|пожалуйста))?\s*(?:что\s+пишут\s+в\s+pubmed\s+про|что\s+пишут\s+про|что\s+известно\s+про|про|по|о|об)?\s*",
    r"^поищи(?:\s+(?:мне|пожалуйста|инфу|информацию|статьи|исследования|данные))?\s+(?:в\s+интернете|в\s+сети|в\s+гугле|в\s+яндексе|в\s+pubmed|в\s+пабмеде|по|про|о|об)?\s*",
    r"^поиск(?:\s+(?:в\s+сети|в\s+интернете|в\s+pubmed|статей|информации))?\s+(?:по|про|о|об)?\s*:\s*",
    # "что пишут / говорит pubmed / пабмед..."
    r"^что\s+(?:пишут|пишет|говорит|известно|найдено|есть)\s+(?:в\s+)?(?:pubmed|пабмед|пабмеде|cochrane|кокрейн|кохране|научной\s+литературе|исследованиях|науке)\s+(?:про|по|о|об|насчет|относительно)\s*",
    r"^что\s+(?:пишут|пишет|говорит)\s+наука\s+(?:про|по|о|об)\s*",
    # "какие исследования / статьи есть..."
    r"^какие\s+(?:есть\s+)?(?:свежие\s+|новые\s+|актуальные\s+|последние\s+)?(?:исследования|статьи|публикации|данные|мета-?анализы|обзоры)\s+(?:есть\s+)?(?:по|про|о|об)\s*",
    r"^какие\s+исследования\s+(?:по|про|о|об)\s*",
    # "покажи / дай статьи / пруфы..."
    r"^(?:покажи|дай|приведи)\s+(?:мне\s+)?(?:статьи|исследования|ссылки|пруфы|источники|публикации)\s+(?:по|про|о|об)\s*",
    # English patterns
    r"^(?:search\s+for|google|look\s+up(?:\s+in\s+pubmed)?|what\s+does\s+pubmed\s+say\s+about|find\s+articles\s+(?:on|about))\s*",
]

_SEARCH_PREFIX_COMBINED_RE = re.compile(
    "|".join(f"(?:{p})" for p in _SEARCH_TRIGGER_PREFIXES),
    re.IGNORECASE | re.UNICODE
)

_FRESH_YEAR_RE = re.compile(r"\b(?:2025|2026|2027|2028)\b")
_FRESH_CONTEXT_RE = re.compile(
    r"\b(?:препарат\w*|материал\w*|протокол\w*|рекомендаци\w*|исследован\w*|стать\w*|"
    r"одобрен\w*|отзыв\w*|fda|минздрав\w*|клинрек\w*|нов\w+|свеж\w+|актуальн\w+|"
    r"мета-?анализ\w*|обзор\w*|запрет\w*|разрешен\w*|зарегистрирован\w*)\b",
    re.IGNORECASE | re.UNICODE
)

_EXPLICIT_FRESH_PHRASES = (
    "новый препарат", "новые препараты", "нового препарата", "новых препаратов",
    "одобрение нового", "одобрен в 20", "одобрена в 20", "одобрены в 20",
    "отзыв препарата", "отозван", "отозвали", "снят с производства",
    "новые клинические рекомендации", "свежие клинические рекомендации",
    "обновление рекомендаций", "обновленный протокол", "обновленные протоколы",
    "свежие исследования", "свежие статьи", "последние исследования",
    "новые данные 20", "исследования 2025", "исследования 2026",
    "fda 2025", "fda 2026", "минздрав 2025", "минздрав 2026",
)

_DIRECT_SEARCH_KEYWORDS = (
    "погугли", "загугли", "прогугли", "поищи в сети", "поищи в интернете",
    "найди в сети", "найди в интернете", "найди статьи", "найди исследования",
    "что пишут в pubmed", "что пишет pubmed", "что говорит pubmed",
    "что пишут в пабмед", "что говорит пабмед", "что в pubmed",
    "pubmed", "пабмед", "cochrane", "кокрейн", "кохран",
)


def strip_search_prefixes(text):
    """Удаляет поисковые вводные префиксы, оставляя чистый предмет поиска."""
    if not text:
        return ""
    cleaned = text.strip()
    if (cleaned.startswith(("«", '"', "'")) and cleaned.endswith(("»", '"', "'"))) and len(cleaned) > 2:
        cleaned = cleaned[1:-1].strip()
    for _ in range(3):
        prev = cleaned
        cleaned = _SEARCH_PREFIX_COMBINED_RE.sub("", cleaned).strip()
        cleaned = re.sub(r"^[\s:,\-–—]+", "", cleaned).strip()
        if cleaned == prev:
            break
    cleaned = re.sub(r"[\s?!.,;]+$", "", cleaned).strip()
    return cleaned


def is_fresh_scientific_data_query(text):
    """True, если запрос явно требует свежих научных данных (2025-2026 гг, новые одобрения, отзывы)."""
    if not text:
        return False
    low = text.lower()
    if any(phrase in low for phrase in _EXPLICIT_FRESH_PHRASES):
        return True
    has_year = bool(_FRESH_YEAR_RE.search(low))
    has_context = bool(_FRESH_CONTEXT_RE.search(low))
    if has_year and has_context:
        return True
    return False


def detect_web_search_intent(text):
    """
    Классификация интента INTENT_WEB_SEARCH.
    Возвращает (is_web_search, cleaned_query, is_fresh_data).
    """
    if not text or len(text.strip()) < 3:
        return False, "", False

    raw_text = text.strip()
    low = raw_text.lower()

    is_direct_search = bool(_SEARCH_PREFIX_COMBINED_RE.search(raw_text)) or any(kw in low for kw in _DIRECT_SEARCH_KEYWORDS)
    is_fresh = is_fresh_scientific_data_query(raw_text)

    if is_direct_search or is_fresh:
        cleaned = strip_search_prefixes(raw_text)
        if not cleaned:
            cleaned = clean_query(raw_text)
        if len(cleaned) >= 2:
            return True, cleaned, is_fresh

    return False, "", False


def clean_query(question):
    """Запрос провайдеру: без пунктуации, без сдвоенных пробелов."""
    return re.sub(r"\s+", " ", _QUERY_JUNK_RE.sub(" ", question or "")).strip()


def query_variants(query):
    """Полный запрос, затем укороченный. Дубль не возвращается."""
    variants = []
    for candidate in (query, " ".join(query.split()[:SEARCH_SHORT_WORDS])):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants[:SEARCH_ATTEMPTS]


def compose_answer(answer, footer, max_chars=MESSAGE_MAX_CHARS):
    """
    Ответ плюс подпись со ссылками, уложенные в предел одного сообщения.

    Режется ОТВЕТ, а не подпись. Для медицинского содержания утверждение без
    источника хуже отсутствия ответа, поэтому ссылки в сообщении остаются всегда,
    а обрезку видит текст пересказа — и обрезается он по границе предложения, а
    не по счётчику символов: «не более 3 мг/кг в су…» читается как другая доза.
    """
    answer = (answer or "").strip()
    footer = (footer or "").strip()
    if not footer:
        return clip_at_sentence(answer, max_chars)
    room = max_chars - len(footer) - 2
    if room < 200:
        # Подпись такая длинная, что на пересказ места нет. Отдаём ссылки: они и
        # есть то, чего у бота не было.
        return footer
    return clip_at_sentence(answer, room) + "\n\n" + footer


def _refusal(text, outcome, report=None, sources=None, attempts=0, elapsed=0.0):
    return {"text": text, "outcome": outcome, "sources": sources or [],
            "report": report or {}, "attempts": attempts, "elapsed": elapsed}


async def run_lookup(question, search_call, generate_call,
                     budget=LOOKUP_TOTAL_COST_SECONDS, log=None,
                     grounding_call=None):
    """
    Полный живой проход. Возвращает словарь; ключ text НИКОГДА не пустой.

    `search_call(query, timeout)` -> (results, error), `generate_call(prompt,
    timeout)` -> (text, error). Обе передаются параметром, поэтому модуль не
    знает ни про подпроцесс, ни про конфиг, а проверка гоняет проход подставным
    провайдером без единого запроса в сеть.

    Если передан `grounding_call(query, timeout)` -> (grounding_dict, error),
    сначала пробуется заземленная генерация Google Search Grounding (gemini-2.5-flash
    с поиском в сети). При сбое или отсутствии ключей автоматически выполняется
    быстрый откат на связку search_call (Tavily/DDGS) + generate_call.

    Молчания здесь нет НИ НА ОДНОЙ ветке. Тишина в этом файле уже стоила
    двухчасовых кулдаунов за вопросы, которые бот просто не разобрал: врач ждёт
    ответа, которого не будет, и в журнале об этом ни строки. Поэтому каждый
    отказ — это и текст врачу, и WARNING с причиной.

    Бюджет `budget` — ПОЛНЫЙ срок на поиск и генерацию. Из него вычитается всё:
    каждая попытка поиска считается по своей цене для вызывающего (бюджет ребёнка
    плюс запас на подъём), и попытка, которой не хватает места, не начинается.
    Генерация получает остаток, а не своё желаемое число.
    """
    log = log or logger
    started = time.monotonic()

    def left():
        return budget - (time.monotonic() - started)

    query = clean_query(question)
    if not query:
        log.warning("web lookup: пустой запрос после очистки (было %d символов)",
                    len(question or ""))
        return _refusal(EMPTY_QUERY, OUTCOME_EMPTY_QUERY)

    # 1. Попытка Google Search Grounding (если передан callable)
    if grounding_call is not None:
        try:
            # Оставляем гарантированный запас на поиск и генерацию при отказе заземления
            need_fallback = SEARCH_ATTEMPT_COST_SECONDS + ANSWER_MIN_TIMEOUT_SECONDS + SUBPROCESS_SLACK_SECONDS
            ground_budget = min(15.0, left() - need_fallback)
            if ground_budget >= 4.0:
                ground_res, ground_err = await asyncio.wait_for(
                    grounding_call(query, ground_budget),
                    timeout=ground_budget + SUBPROCESS_SLACK_SECONDS,
                )
                if ground_res and ground_res.get("text"):
                    raw_src = ground_res.get("sources") or []
                    ranked, report = rank_sources(raw_src)
                    sources, dropped = fit_budget(ranked)
                    report["budget_dropped"] = dropped
                    footer = format_sources_footer(sources)
                    answer_text = compose_answer(ground_res["text"], footer)
                    log.info(
                        "web lookup: Google Search Grounding успешно выполнен (источников=%d)",
                        len(sources),
                    )
                    return {
                        "text": answer_text,
                        "outcome": OUTCOME_OK,
                        "sources": sources,
                        "report": report,
                        "grounding_provider": "google_search",
                        "attempts": 1,
                        "elapsed": time.monotonic() - started,
                    }
                elif ground_err:
                    log.info(
                        "web lookup: Google Search Grounding недоступен (%s) — переключаюсь на Tavily/DDGS",
                        str(ground_err)[:100],
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info(
                "web lookup: ошибка Google Search Grounding (%s) — переключаюсь на Tavily/DDGS",
                str(exc)[:100],
            )

    results, error, attempts = [], None, 0
    for variant in query_variants(query):
        # Попытка, которой не хватает времени, НЕ начинается: иначе она отберёт
        # бюджет у генерации и врач получит ссылки без разбора вместо ответа.
        need = SEARCH_ATTEMPT_COST_SECONDS + ANSWER_MIN_TIMEOUT_SECONDS + SUBPROCESS_SLACK_SECONDS
        if left() < need:
            log.warning(
                "web lookup: попытка поиска пропущена, осталось %.1f с из %.0f "
                "(нужно %.0f) — врач получит ответ по уже найденному",
                left(), budget, need,
            )
            break
        attempts += 1
        try:
            # Жёсткий потолок поверх бюджета ребёнка. Он срабатывает только если
            # транспорт нарушил собственный срок; без него один зависший вызов
            # съедал бы весь бюджет команды, и врач не получил бы даже отказа.
            results, error = await asyncio.wait_for(
                search_call(variant, SEARCH_ATTEMPT_TIMEOUT_SECONDS),
                timeout=SEARCH_ATTEMPT_COST_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Отказ провайдера не имеет права уронить обработчик: обработчик
            # ЛС держит замок на пользователя, и все следующие вопросы этого
            # врача встали бы в очередь за упавшим.
            results, error = [], "%s: %s" % (type(exc).__name__, str(exc)[:200])
            log.warning("web lookup: поиск отказал (попытка %d, запрос %d символов): %s",
                        attempts, len(variant), error)
        if results:
            break

    if not results and error:
        # prepare() напишет свой WARNING с причиной и вернёт SEARCH_FAILED.
        ready = prepare(question, [], error=error)
        return _refusal(ready["message"], OUTCOME_SEARCH_FAILED,
                        attempts=attempts, elapsed=time.monotonic() - started)
    if not results:
        log.warning(
            "web lookup: выдача пуста без отказа провайдера, попыток %d, запрос "
            "%d символов — врачу уходит «ничего не нашлось»", attempts, len(query),
        )
        return _refusal(NOTHING_FOUND, OUTCOME_NOTHING_FOUND,
                        attempts=attempts, elapsed=time.monotonic() - started)

    ready = prepare(question, results)
    if not ready["sources"]:
        # Вся выдача отсеяна как реклама клиник. Это честный отказ, и он обязан
        # звучать: молча отдать пустоту значит выдать бота за источник, который
        # «ничего не знает», хотя он просто не пустил рекламу к врачу.
        report = ready.get("report") or {}
        log.warning(
            "web lookup: пригодных источников нет — всего %s, реклама %s, без "
            "ссылки %s (%s); врачу уходит честный отказ",
            report.get("total"), len(report.get("ads") or []),
            report.get("unparsed"), "; ".join((report.get("ads") or [])[:3]),
        )
        return _refusal(ready["message"], OUTCOME_NOTHING_USABLE, report=report,
                        attempts=attempts, elapsed=time.monotonic() - started)

    footer = ready["footer"]
    # Генерация получает ОСТАТОК, а не своё желаемое число.
    answer_budget = min(ANSWER_TIMEOUT_SECONDS,
                        left() - LLM_PACE_SLACK_SECONDS - SUBPROCESS_SLACK_SECONDS)
    if answer_budget < ANSWER_MIN_TIMEOUT_SECONDS:
        log.warning(
            "web lookup: на генерацию осталось %.1f с при минимуме %.0f — врач "
            "получит %d ссылок без разбора вместо ничего",
            answer_budget, ANSWER_MIN_TIMEOUT_SECONDS, len(ready["sources"]),
        )
        return _refusal(NO_BUDGET_PREFIX + "\n\n" + footer, OUTCOME_NO_BUDGET,
                        report=ready.get("report"), sources=ready["sources"],
                        attempts=attempts, elapsed=time.monotonic() - started)

    try:
        answer, gen_error = await asyncio.wait_for(
            generate_call(ready["prompt"], answer_budget),
            timeout=answer_budget + SUBPROCESS_SLACK_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        answer, gen_error = None, "%s: %s" % (type(exc).__name__, str(exc)[:200])

    if not answer:
        log.warning(
            "web lookup: ответ по %d источникам не собран (%s) — врачу уходят "
            "ссылки без разбора", len(ready["sources"]), str(gen_error)[:200],
        )
        return _refusal(ANSWER_FAILED_PREFIX + "\n\n" + footer, OUTCOME_ANSWER_FAILED,
                        report=ready.get("report"), sources=ready["sources"],
                        attempts=attempts, elapsed=time.monotonic() - started)

    return {
        "text": compose_answer(answer, footer),
        "outcome": OUTCOME_OK,
        "sources": ready["sources"],
        "report": ready.get("report") or {},
        "attempts": attempts,
        "elapsed": time.monotonic() - started,
    }
