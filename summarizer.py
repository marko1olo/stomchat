import asyncio  # Добавлено
import os
import config
import database
import dental_vocab
import html_safe
import logging
import random
# search_engine_safe сюда импортировался, но не вызывался ни разу — вместе с
# ним впустую подтягивался web_search_async. Сам модуль оставлен: он рабочий,
# просто нигде не подключён (см. заметку в отчёте).
import re
import html
import runtime_guard
from blocking_tools import create_telegraph_page_async, generate_gemini_text_async
from datetime import datetime

logger = logging.getLogger(__name__)
TELEGRAPH_TIMEOUT_SECONDS = 60
GEMINI_GENERATION_TIMEOUT_SECONDS = 2100
TELEGRAM_SEND_TIMEOUT_SECONDS = 90
PIN_TIMEOUT_SECONDS = 30
RECENT_DELIVERY_SCAN_LIMIT = 20
# Предел, на котором конвейер режет статью перед публикацией в Telegraph, — то
# же значение, что по умолчанию у html_safe.safe_truncate_html. Нужен здесь,
# чтобы зарезервировать место под подвал со счётчиком: он дописывается ПОСЛЕ
# обрезки, а не до неё.
WEEKLY_HTML_LIMIT = 9500

# Сколько символов просить у модели для ДНЕВНОГО дайджеста.
#
# Цифра выведена из порога обрезки, а не выбрана отдельно. В промпте их было ДВЕ
# и взаимоисключающих: правило оформления требовало «жёсткий лимит 4000-5000
# символов», а последняя строка задания — «в пределах 7000-9000». Модель слушается
# последней и самой конкретной.
#
# Замер по bot.log, 23 РЕАЛЬНЫЕ дневные генерации: длина ответа модели от 1822 до
# 11264, в среднем 8968. Лимит «4000-5000» нарушен в 22 случаях из 23. После
# clean_markdown_to_html разметка добавляет около 2% (11264 -> 11466), и статья
# упиралась в обрезку на 9500: превышение в 10 случаях из 23, то есть 43%
# дайджестов уходили сообществу с обрубленным хвостом и припиской о сокращении.
#
# Режется именно хвост структуры: «ЮМОР/ЦИТАТЫ», «ЭКСПЕРТ ДНЯ» и бонусные блоки
# с пометкой «(В конец)». Хуже всех доставалось «МИКРО-ЛАЙФХАКАМ ДНЯ»: они лежат
# в BONUS_ALWAYS, то есть применимы всегда и выбираются чаще прочих.
#
# Запас 1000 символов покрывает разметку и подвал со счётчиком сообщений.
DAILY_CHAR_BUDGET = WEEKLY_HTML_LIMIT - 1000

# То же для НЕДЕЛЬНОГО отчёта. Цифра там была согласована сама с собой, но зашита
# в трёх местах промпта руками — ровно та конструкция, из которой в дневном
# промпте и выросло расхождение. Выводим из того же порога: у недельного ещё
# дописывается подвал со счётчиком, поэтому запас чуть больше.
WEEKLY_CHAR_BUDGET = WEEKLY_HTML_LIMIT - 1200

_summary_generation_lock = None

# Публикация в Telegraph живёт в blocking_tools и исполняется подпроцессом с
# таймаутом. Здесь лежала вторая, синхронная реализация вместе с клиентом
# TelegraphPoster, создаваемым НА ИМПОРТЕ модуля, — и её не вызывал никто.
# Помимо дубля это был риск на старте: при пустом токене прямо на импорте
# уходил сетевой вызов create_api_token без таймаута, то есть подъём бота
# зависел от доступности Telegraph. Сегодня токен задан и вызов не срабатывает,
# но при ротации ключа сработал бы.


def _write_summary_stage(stage, **payload):
    status = {
        "active": stage != "idle",
        "stage": stage,
    }
    status.update(payload)
    runtime_guard.write_summary_status(status)


def _normalize_delivery_text(value):
    """
    Приводит текст к виду, по которому сравниваются «то же самое сообщение».

    Тег заменяется ПРОБЕЛОМ, а не пустой строкой. Иначе «<b>Дайджест</b>тело»
    превращалось в «Дайджесттело», тогда как Telegram отдаёт текст уже без
    разметки — «Дайджест тело», — и защита от дубля не узнавала собственный
    только что опубликованный отчёт. Практически она не срабатывала никогда:
    в дайджесте теги стоят вплотную к словам всегда. Смысл защиты — не
    опубликовать отчёт второй раз после таймаута отправки.
    """
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# HTML-помощники переехали в html_safe: та же логика нужна ассистенту (кнопки
# протоколов, статьи энциклопедии), а две копии этой обрезки — прямой путь к
# расхождению. Локальные имена оставлены как были.
_html_to_plain = html_safe.html_to_plain
_balance_html = html_safe.balance_html
_unclosed_tags = html_safe.unclosed_tags
_safe_cut_index = html_safe.safe_cut_index
_safe_truncate_html = html_safe.safe_truncate_html


_HTML_PARSE_MARKERS = (
    "parse entities",
    "unsupported start tag",
    "unmatched end tag",
    "unexpected end tag",
    "can't find end",
    "entity",
)
TELEGRAM_PLAIN_TEXT_LIMIT = 4000

# Длина цитаты из сообщения, на которое ответили. Подставляется только когда
# родителя в выборке нет — иначе хватает ссылки на MSG.
REPLY_QUOTE_MAX_CHARS = 80


def _reply_context(reply_id, reply_lookup, batch_ids):
    """
    Префикс с контекстом ответа. Возвращает (текст, подставлена_ли_цитата).

    В дневной сборке цитата ВЫЧИСЛЯЛАСЬ (short_p_text) и не использовалась
    нигде — модель получала только имя. В недельной ответы не учитывались
    вообще, и в промпте это компенсировалось указанием «сообщения подряд считай
    монологом».

    Замер на живом дневном окне: 403 из 695 сообщений выборки — ответы, и у 105
    из них родитель в выборку не попал. Модель видела «(Ответ Петру) А стоит
    это того? Сколько лет этим конструкциям?» — без единого указания, о чём
    речь, и достраивала контекст сама.

    Цитата подставляется ТОЛЬКО когда родителя в выборке нет; если есть —
    достаточно ссылки на MSG. Замер: адресная подстановка +5% к дневному
    промпту и +2% к недельному, тогда как цитата ко всем ответам дала бы +18%.
    """
    if not reply_id:
        return "", 0

    parent = reply_lookup.get(reply_id)
    if not parent:
        return "", 0

    parent_name, parent_text = parent
    parent_text = (parent_text or "").strip()

    if reply_id in batch_ids or not parent_text:
        return f"(Ответ {parent_name}, MSG_{reply_id}) ", 0

    quote = parent_text[:REPLY_QUOTE_MAX_CHARS]
    if len(parent_text) > REPLY_QUOTE_MAX_CHARS:
        quote += "…"
    return f"(Ответ {parent_name} на «{quote}») ", 1


def _message_matches_topic(message, topic_id):
    if not topic_id:
        return True
    reply_to = getattr(message, "reply_to", None)
    return (
        getattr(reply_to, "reply_to_msg_id", None) == topic_id
        or getattr(reply_to, "reply_to_top_id", None) == topic_id
    )


async def _find_recent_matching_message(client, chat_id, topic_id, text):
    wanted = _normalize_delivery_text(text)
    if not wanted:
        return None

    try:
        recent_messages = await asyncio.wait_for(
            client.get_messages(chat_id, limit=RECENT_DELIVERY_SCAN_LIMIT),
            timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("recent delivery scan failed chat=%s topic=%s: %s", chat_id, topic_id, exc)
        return None

    for message in recent_messages or []:
        if not _message_matches_topic(message, topic_id):
            continue
        message_text = getattr(message, "message", None) or getattr(message, "raw_text", "") or ""
        if _normalize_delivery_text(message_text) == wanted:
            return message

    return None


async def _send_message_once(client, chat_id, topic_id, text, send_params, label):
    existing = await _find_recent_matching_message(client, chat_id, topic_id, text)
    if existing:
        logger.warning(
            "%s duplicate guard found existing message chat=%s topic=%s msg_id=%s",
            label,
            chat_id,
            topic_id,
            getattr(existing, "id", None),
        )
        return existing

    try:
        return await asyncio.wait_for(
            client.send_message(chat_id, text, **send_params),
            timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("%s send timeout chat=%s topic=%s; scanning recent messages", label, chat_id, topic_id)
        existing = await _find_recent_matching_message(client, chat_id, topic_id, text)
        if existing:
            logger.warning(
                "%s send recovered after timeout chat=%s topic=%s msg_id=%s",
                label,
                chat_id,
                topic_id,
                getattr(existing, "id", None),
            )
            return existing
        raise
    except Exception as exc:
        # Запасного пути не было вообще: любая ошибка разбора разметки роняла
        # отправку, дайджест за день терялся, а планировщик заново генерировал
        # его каждые 10 минут с тем же битым HTML. Лучше доставить отчёт
        # плоским текстом, чем не доставить никак.
        if not any(marker in str(exc).lower() for marker in _HTML_PARSE_MARKERS):
            raise

        logger.error(
            "%s HTML rejected by Telegram chat=%s topic=%s (%s); resending as plain text",
            label, chat_id, topic_id, exc,
        )
        plain_params = {k: v for k, v in send_params.items() if k != "parse_mode"}
        plain_text = _html_to_plain(text)[:TELEGRAM_PLAIN_TEXT_LIMIT]
        if not plain_text:
            raise
        return await asyncio.wait_for(
            client.send_message(chat_id, plain_text, **plain_params),
            timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
        )


async def _pin_message_safely(client, chat_id, message_id):
    try:
        await asyncio.sleep(1)
        await asyncio.wait_for(
            client.pin_message(chat_id, message_id, notify=True),
            timeout=PIN_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("pin failed chat=%s msg_id=%s: %s", chat_id, message_id, exc)


async def _notify_delivery(delivery_hook, message):
    if delivery_hook is None or message is None:
        return
    result = delivery_hook(message)
    if asyncio.iscoroutine(result):
        await result


async def _generate_text_singleflight(prompt, kind, chat_id, topic_id, message_count, prompt_chars):
    global _summary_generation_lock
    if _summary_generation_lock is None:
        _summary_generation_lock = asyncio.Lock()

    context = {
        "kind": kind,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "message_count": message_count,
        "prompt_chars": prompt_chars,
        "thinking_level": "HIGH",
    }
    _write_summary_stage("waiting_for_generation_slot", **context)
    async with _summary_generation_lock:
        _write_summary_stage("gemini_generation_start", **context)
        response, error = await generate_gemini_text_async(
            prompt,
            context,
            timeout=GEMINI_GENERATION_TIMEOUT_SECONDS,
        )
        if error:
            logger.error("Gemini subprocess failed: %s", error)
        return response


def get_russian_date(date_input):
    """Превращает дату в формат '2 февраля 2026'."""
    months = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря"
    ]
    if isinstance(date_input, str):
        # Если пришла строка, парсим её
        dt = datetime.strptime(date_input[:19], '%Y-%m-%d %H:%M:%S')
    else:
        dt = date_input
    
    return f"{dt.day} {months[dt.month - 1]} {dt.year}"
    


def clean_markdown_to_html(text):
    if not text: return ""

    # 1. Сначала превращаем Markdown-жирный в HTML-жирный
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    
    # 2. Превращаем заголовки ### в жирный
    text = re.sub(r'^#{1,6}\s+(.*)', r'<b>\1</b>', text, flags=re.MULTILINE)

    # 3. Маркеры списков убираем, но ЗАПОМИНАЕМ, что строка была пунктом.
    # Два пункта склеивать нельзя: «Совет: убрать под микроскопом» и
    # «Альтернатива: резекция» превращались в одну фразу и читались как
    # единое предложение.
    marker_re = re.compile(r'^[ \t]*[\-•*+—▶️🛑✅🌟]+\s*')
    lines = []
    list_flags = []
    for raw_line in text.split('\n'):
        without_marker = marker_re.sub('', raw_line)
        lines.append(without_marker)
        list_flags.append(without_marker != raw_line)

    # 4. АЛГОРИТМ СЕМАНТИЧЕСКОЙ СКЛЕЙКИ
    final_lines = []
    final_is_list = []
    # Жесткие терминаторы (конец мысли)
    hard_stops = ".!?:;" 
    # Союзы и знаки продолжения
    conjunctions = r'\b(и|а|но|или|к|в|с|от|до|за|для|на|по)\s*$'

    def looks_like_heading(rendered, plain):
        """
        Строка целиком выделена жирным и коротка — это заголовок раздела.

        Проверять надо ИМЕННО текущую строку. Прежний код смотрел только на
        предыдущую, поэтому заголовок всегда затягивался в конец абзаца перед
        ним, если тот не оканчивался точкой: врач видел «...резекция
        <b>Ортопедия</b> Спор о границах уступа...» — название раздела посреди
        предложения.
        """
        stripped = rendered.strip()
        return (
            stripped.startswith("<b>")
            and stripped.endswith("</b>")
            and "</b>" not in stripped[:-4]
            and len(plain) < 60
            and plain[-1:] not in (",", ":", ";")
        )

    def append(rendered, is_list_item):
        final_lines.append(rendered)
        final_is_list.append(is_list_item)

    for line, is_list_item in zip(lines, list_flags):
        clean_line = line.strip()

        if not clean_line:
            if final_lines and final_lines[-1] != "":
                last_txt = re.sub(r'<[^>]+>', '', final_lines[-1]).strip()
                # Разрыв только если точка/двоеточие в конце
                if last_txt and last_txt[-1] in hard_stops:
                    append("", False)
            continue

        stripped_curr = re.sub(r'<[^>]+>', '', clean_line).strip()
        current_is_heading = looks_like_heading(clean_line, stripped_curr)

        # Перед заголовком раздела — всегда пустая строка. Без этого разделы
        # слипались в один абзац, когда предыдущий не оканчивался точкой.
        if current_is_heading and final_lines and final_lines[-1] != "":
            append("", False)

        if not final_lines or final_lines[-1] == "":
            append(clean_line, is_list_item)
            continue

        prev_line = final_lines[-1]
        stripped_prev = re.sub(r'<[^>]+>', '', prev_line).strip()

        if not stripped_prev or not stripped_curr:
            append(clean_line, is_list_item)
            continue

        # ПРОВЕРКА НА СКЛЕЙКУ
        should_join = False

        # Условие 1: Предыдущая строка не закончена жестким знаком
        if stripped_prev[-1] not in hard_stops:
            should_join = True

        # Условие 2: Предыдущая строка заканчивается на запятую или союз
        if stripped_prev[-1] == "," or re.search(conjunctions, stripped_prev, re.I):
            should_join = True

        # Условие 3: Текущая строка начинается с маленькой буквы или союза "и"
        if stripped_curr[0].islower() or stripped_curr.lower().startswith("и "):
            should_join = True

        # КОРРЕКЦИЯ: Защита заголовков не должна срабатывать, если есть запятая или союз
        is_header_like = prev_line.startswith('<b>') and len(stripped_prev) < 50
        if is_header_like:
            # Если это "заголовок", но он заканчивается на запятую или "и" — это НЕ заголовок, а часть списка/предложения
            if stripped_prev[-1] == "," or re.search(conjunctions, stripped_prev, re.I):
                should_join = True
            else:
                # Если знаков продолжения нет и следующая строка с Большой буквы — считаем заголовком (разрываем)
                if not stripped_curr[0].islower():
                    should_join = False

        # Заголовок раздела не приклеивается к предыдущему тексту никогда.
        if current_is_heading:
            should_join = False

        # Два пункта списка — две строки. Маркеры уже сняты, поэтому склейка
        # превращала перечисление в неразборчивую строку.
        if is_list_item and final_is_list and final_is_list[-1]:
            should_join = False

        if should_join:
            final_lines[-1] = f"{prev_line} {clean_line}"
            final_is_list[-1] = final_is_list[-1] or is_list_item
        else:
            append(clean_line, is_list_item)

    # 5. Финальная чистка
    text = "\n".join(final_lines)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


# Околоклиническая лексика, которой в dental_vocab нет и быть не должно:
# деньги, оборудование, организация работы. Для дайджеста это осмысленное
# содержание, для медицинского триажа ассистента — нет.
BUSINESS_KEYWORDS = frozenset({
    "рубл", "тысяч", "руб", "клиник", "пациент", "стоимост", "цена", "прайс",
    "зарплат", "процент", "аренд", "протокол", "сканер", "экзокад", "exocad",
    "печь", "печать", "принтер", "мотор", "оптик", "бинокуляр", "3d",
    "gbt", "srp", "ids", "вертипреп", "матриц", "панорам", "кллт",
})

# Эти ключи ищутся ТОЛЬКО с начала слова: внутри слова они цепляют бытовую
# речь. Замер на живой базе: «руб» сидит в «грубо» (15), «грубости», «зарубеж»,
# «вырубаю», «срубить»; «цена» — в «сценарии»; «3d» и «ids» — в ссылках и
# латинских словах («acids», «avoids»). 31 реплика чистого трёпа попадала в
# дайджест всего сообщества только из-за них: «Ой что-то грубо вышло»,
# «Я уже даже не вырубаю впн)».
#
# Остальные ключи ищутся где угодно намеренно: внутри слова у них истинные
# попадания — «фотопротокол» (31), «поликлиника» (10), «микромотор»,
# «эндомотор», «себестоимость», «суперсканер», «оверпрайс».
BUSINESS_PREFIX_ONLY = frozenset({"руб", "цена", "3d", "ids"})

BONUS_VARIANTS = [
    """
    БЛОК "🔍 КЛИНИЧЕСКИЙ РАЗБОР ПОД МИКРОСКОПОМ" (В начало):
    Выбери самый сложный кейс дня и распиши его с фанатичной детализацией: инструменты, торки, химия, обоснование каждого движения.
    """,
    """
    БЛОК "🛠 ИНСТРУМЕНТАЛЬНАЯ КЛАДОВАЯ" (В конец):
    Собери все упоминания брендов, оборудования и материалов за день в один экспертный обзор. Сравни характеристики и дай резюме коллег.
    """,
    """
    БЛОК "🛑 АНАТОМИЯ ОШИБКИ" (В любое место):
    Найди описание клинической неудачи. Проведи детективное расследование: почему это произошло и какой четкий протокол нужен, чтобы избежать этого.
    """,
    """
    БЛОК "🗣 ПСИХОЛОГИЯ И КОММУНИКАЦИЯ" (В любое место):
    Если обсуждали общение с пациентами или продажи — выдели это в глубокий разбор. Дай скрипты и разбор этических дилемм.
    """,
    """
    БЛОК "🧬 ДОКАЗАТЕЛЬНАЯ БАЗА (EBM)" (После теории):
    Возьми ключевую тему дня и подкрепи её данными из мировых исследований (PubMed, Cochrane).
    """,
    """
    БЛОК "📐 ЭРГОНОМИКА И ТЕХНИКА" (В любое место):
    Сфокусируйся на физике работы: постановка рук, работа с зеркалом, изоляция рабочего поля.
    """,
    """
    БЛОК "⚙️ НАСТРОЙКИ И ПАРАМЕТРЫ" (В блок тонкостей):
    Выпиши все "цифры" оборудования: торки, программы лазеров, параметры сканирования или печей. Техническая шпаргалка.
    """,
    """
    БЛОК "💊 ФАРМАКОЛОГИЧЕСКИЙ НАДЗОР" (В любое место):
    Если речь шла об анестезии или антибиотиках — сделай глубокий разбор. Дозировки, комбинации и реальные отзывы о побочках.
    """,
    """
    БЛОК "💡 МИКРО-ЛАЙФХАКИ ДНЯ" (В конец):
    Собери список из 5-7 гениальных "фишек", экономящих время или ресурсы без потери качества.
    """,
    """
    БЛОК "📸 РЕНТГЕНОЛОГИЧЕСКИЙ КОНСИЛИУМ" (В любое место):
    Если обсуждали КЛКТ, ОПТГ или прицельные снимки — сделай акцент на диагностических маркерах. На что смотреть, что легко пропустить, как интерпретировать тени.
    """,
    """
    БЛОК "🖥 ЦИФРОВОЙ ПРОТОКОЛ (DIGITAL)" (В любое место):
    Собери всё по интраоральному сканированию, моделированию в Exocad или 3D-печати. Нюансы софта, калибровки и "дружбы" цифры с клиникой.
    """,
    """
    БЛОК "💎 ЭСТЕТИЧЕСКИЙ ЦЕНЗ" (В любое место):
    Если обсуждали реставрации или виниры — разбери морфологию, макро- и микрорельеф, работу с цветом и прозрачностью. Обоснуй эстетику анатомией.
    """,
    """
    БЛОК "🦷 ПАРОДОНТОЛОГИЧЕСКИЙ СТАТУС" (В любое место):
    Сфокусируйся на мягких тканях. Пластика десны, ССТ, ФДМ, протоколы чистки и работа в карманах.
    """,
    """
    БЛОК "⚖️ ЮРИДИЧЕСКИЙ ЩИТ" (В любое место):
    Если обсуждали жалобы, ИДС, карты или законы — выжми максимум правовой информации для защиты врача.
    """,
    """
    БЛОК "🔄 ПЕРЕОЦЕНКА ЦЕННОСТЕЙ (EVOLUTION)" (В любое место):
    Сравни старые подходы (как учили раньше) с тем, что обсуждали сегодня. Почему старые методы умирают и что приходит на замену.
    """,
    """
    БЛОК "📊 БИТВА БРЕНДОВ (HEAD-TO-HEAD)" (В блок материалов):
    Выбери два популярных материала из обсуждения (например, два бонда или два композита) и проведи их жесткое сравнение по всем параметрам.
    """,
    """
    БЛОК "🏥 МЕНЕДЖМЕНТ КЛИНИКИ" (В любое место):
    Если обсуждали управление, найм, проценты или организацию процессов — выдели ключевые управленческие решения.
    """,
    """
    БЛОК "🧪 ХИМИЯ МАТЕРИАЛОВ" (В блок материалов):
    Максимально глубоко в составы. Мономеры, наполнители, реакция полимеризации. Объясни поведение материала его химической формулой.
    """
]

# Слова, по которым видно, что тема бонусного блока в этот день вообще
# поднималась. Ключ — кусок заголовка блока, по нему блок и опознаётся.
#
# Зачем: блоки выбирались случайной выборкой из всех восемнадцати, а промпт
# требовал «ты ОБЯЗАН внедрить». Замер на 140 реальных днях чата: 81 из 333
# выбранных блоков (24%) не имели в переписке НИКАКОГО материала. У
# «Фармакологического надзора» материал есть лишь в 34% дней, у «Менеджмента
# клиники» — в 43%, у «Рентгенологического консилиума» — в 54%. Модель,
# получив приказ, писала раздел с дозировками по дню, где об анестезии не было
# ни слова. Статью читают практикующие врачи.
BONUS_TRIGGERS = {
    "ИНСТРУМЕНТАЛЬНАЯ КЛАДОВАЯ": ("бренд", "фирм", "купил", "заказал", "производител",
                                  "аппарат", "наконечник", "инструмент"),
    "АНАТОМИЯ ОШИБКИ": ("ошибк", "неудач", "перелом", "скол", "осложнен", "переделыв",
                        "не получилось", "провал"),
    "ПСИХОЛОГИЯ И КОММУНИКАЦИЯ": ("объясня", "убеди", "продаж", "согласи", "конфликт",
                                  "общени", "отказал"),
    "ДОКАЗАТЕЛЬНАЯ БАЗА": ("исследован", "статья", "pubmed", "cochrane", "мета-анализ",
                           "доказатель"),
    "ЭРГОНОМИКА И ТЕХНИКА": ("эргоном", "зеркал", "посадк", "изоляц", "коффердам",
                             "раббердам", "осанк"),
    "НАСТРОЙКИ И ПАРАМЕТРЫ": ("торк", "ньютон", "об/мин", "оборот", "градус", "программ",
                              "параметр", "режим", "настройк"),
    "ФАРМАКОЛОГИЧЕСКИЙ НАДЗОР": ("анестез", "артикаин", "антибиоти", "дозиров", "препарат",
                                 "ибупрофен", "амоксициллин", "карпул", "мепивакаин"),
    "РЕНТГЕНОЛОГИЧЕСКИЙ КОНСИЛИУМ": ("клкт", "оптг", "снимок", "снимк", "рентген",
                                     "прицельн", "визиограф", "томограф"),
    "ЦИФРОВОЙ ПРОТОКОЛ": ("скан", "exocad", "экзокад", "3d", "печат", "принтер",
                          "цифров", "cad"),
    "ЭСТЕТИЧЕСКИЙ ЦЕНЗ": ("винир", "эстетик", "цвет", "оттенок", "прозрачн", "реставрац",
                          "морфолог"),
    "ПАРОДОНТОЛОГИЧЕСКИЙ СТАТУС": ("десн", "пародонт", "сст", "рецесс", "карман",
                                   "лоскут", "гингив", "чистк"),
    "ЮРИДИЧЕСКИЙ ЩИТ": ("жалоб", "идс", "суд", "закон", "юрист", "претензи",
                        "медкарт", "информированн"),
    "ПЕРЕОЦЕНКА ЦЕННОСТЕЙ": ("раньше", "устарел", "классическ", "современн", "по-новому"),
    "БИТВА БРЕНДОВ": ("сравн", "лучше чем", " vs ", "какой лучше", "выбрать между",
                      "против"),
    "МЕНЕДЖМЕНТ КЛИНИКИ": ("зарплат", "процент", "найм", "админ", "управлен", "организац",
                           "аренд", "персонал"),
    "ХИМИЯ МАТЕРИАЛОВ": ("мономер", "наполнител", "полимеризац", "состав", "химическ",
                         "адгезив", "бонд", "композит"),
}

# Эти блоки применимы всегда: они строятся на любом клиническом материале дня,
# а не на конкретной теме. Список закрытый — новый блок без слов-признаков
# сюда не попадёт молча, это ловит проверка в тесте.
BONUS_ALWAYS = ("КЛИНИЧЕСКИЙ РАЗБОР ПОД МИКРОСКОПОМ", "МИКРО-ЛАЙФХАКИ ДНЯ")

BONUS_MIN_BLOCKS = 1
BONUS_MAX_BLOCKS = 3


def bonus_block_triggers(block_text):
    """Слова-признаки блока: None — блок применим всегда, () — признаков нет."""
    for title in BONUS_ALWAYS:
        if title in block_text:
            return None
    for title, triggers in BONUS_TRIGGERS.items():
        if title in block_text:
            return triggers
    return ()


def select_bonus_blocks(blocks, day_text, rng=random):
    """
    Отбирает бонусные блоки, для которых в переписке дня есть материал.

    Раньше выбор был слепым: random.sample по всем блокам. Комментарий обещал
    «от 1 до 3», а randint(1, 4) давал до четырёх — расхождение тоже устранено.

    Если не подошёл ни один блок (день без клинического содержания), берём
    только безусловные: пустой список бонусов лучше выдуманного раздела, но
    совсем без разбора статья вырождается в перечень реплик.
    """
    low = (day_text or "").lower()
    applicable = []
    for block in blocks:
        triggers = bonus_block_triggers(block)
        if triggers is None or any(t in low for t in triggers):
            applicable.append(block)
    if not applicable:
        applicable = [b for b in blocks if bonus_block_triggers(b) is None]
    if not applicable:
        return []
    count = min(len(applicable), rng.randint(BONUS_MIN_BLOCKS, BONUS_MAX_BLOCKS))
    return rng.sample(applicable, k=count)
_BUSINESS_ANYWHERE = BUSINESS_KEYWORDS - BUSINESS_PREFIX_ONLY

_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)

def _is_useful_text(text_lower):
    """
    Есть ли в реплике профессиональное содержание.

    Клиническую часть берём из dental_vocab — того же словаря, которым живёт
    триаж ассистента. Здесь лежала СВОЯ копия на 80 корней, и реплика, чьего
    термина в ней не было, выпадала из дайджеста молча. Замер на 4000 живых
    сообщений: 166 из них — осмысленное клиническое содержание, потерянное
    фильтром («Периодонтит тоже не всегда заканчивается заживлением», «Бонд хим
    отверждения», «Ретрит файлы и протейперы»). Из 227 терминов ассистента 169
    не имели в том списке ни одного соответствия.
    """
    if any(kw in text_lower for kw in _BUSINESS_ANYWHERE):
        return True
    if any(word.startswith(kw)
           for word in _WORD_RE.findall(text_lower)
           for kw in BUSINESS_PREFIX_ONLY):
        return True
    return dental_vocab.has_dental_term(text_lower)


def filter_useful_messages(messages):
    """Фильтрует список сообщений, удаляя короткий флуд, смайлики и неинформативные реплики."""
    if not messages:
        return []

    useful = []

    for msg in messages:
        # Распаковка полей
        # m_id, name, username, text, m_desc, date, reply_id, m_url = msg
        m_desc = msg[4] if len(msg) > 4 else None
        m_url = msg[7] if len(msg) > 7 else None
        text = msg[3] if len(msg) > 3 else None
        
        # Оставляем, если есть описание медиа (описание мема/снимка) или прямая ссылка
        if m_desc or m_url:
            useful.append(msg)
            continue
            
        if not text:
            continue
            
        text_strip = text.strip()
        text_lower = text_strip.lower()

        # Оставляем, если есть знак вопроса и длина сообщения больше 7 символов
        if "?" in text_strip and len(text_strip) > 7:
            useful.append(msg)
            continue

        # Оставляем, если есть профессиональное содержание
        if _is_useful_text(text_lower):
            useful.append(msg)
            continue

    return useful

async def process_summary_batch(messages, client, chat_id, topic_id=None, msg_count=0, cached_message=None, delivery_hook=None):
    if not messages:
        return None

    # --- 1. БЫСТРЫЙ ПУТЬ (КЭШ) ---
    send_params = {'parse_mode': 'HTML', 'link_preview': True}
    if topic_id: send_params['reply_to'] = topic_id

    if cached_message:
        logger.info(f"🚀 Отправка кэша (тизера) в {chat_id}")
        logger.info(f"summary cached send start chat={chat_id}")
        _write_summary_stage(
            "telegram_cached_send",
            kind="daily",
            chat_id=chat_id,
            topic_id=topic_id,
            message_count=len(messages),
        )
        sent_msg = await _send_message_once(
            client,
            chat_id,
            topic_id,
            cached_message,
            send_params,
            "daily_cached",
        )
        await _notify_delivery(delivery_hook, sent_msg)
        await _pin_message_safely(client, chat_id, sent_msg.id)
        runtime_guard.clear_summary_status("daily_cached_done")
        return cached_message
    
    # --- 2. ОБЫЧНЫЙ ПУТЬ (ГЕНЕРАЦИЯ) ---
    filtered_messages = filter_useful_messages(messages)
    if not filtered_messages:
        logger.warning(f"No useful messages left for summary in chat={chat_id}")
        return None

    # Сборка лога переписки для нейросети
    full_text_parts = ["ЛОГ ПЕРЕПИСКИ СТОМАТОЛОГОВ:\n\n"]
    media_map = {} # Карта для вставки фото

    logger.info(f"summary build start chat={chat_id} messages={len(filtered_messages)}")
    reply_ids = [msg[6] for msg in filtered_messages if msg[6]]
    reply_lookup = await asyncio.wait_for(
        database.get_texts_by_ids(reply_ids),
        timeout=30,
    )

    batch_ids = {msg[0] for msg in filtered_messages}
    quoted_context = 0

    for msg in filtered_messages:
        # Распаковка всех полей из БД
        m_id, name, username, text, m_desc, date, reply_id, m_url = msg

        quote_text, was_quoted = _reply_context(reply_id, reply_lookup, batch_ids)
        quoted_context += was_quoted

        full_text_parts.append(f"MSG_{m_id} | {name}: {quote_text}{text or ''}\n")

        if m_desc:
            full_text_parts.append(f"(На фото в MSG_{m_id}: {m_desc})\n")

        if m_url:
            media_map[m_id] = m_url

    full_text = "".join(full_text_parts)
    logger.info(
        f"summary build done chat={chat_id} chars={len(full_text)} "
        f"replies={len(reply_lookup)} quoted={quoted_context} media={len(media_map)}"
    )

    bonus_variants = BONUS_VARIANTS
    
    # Блоки берём только те, для которых в логе дня есть материал.
    selected_bonuses = select_bonus_blocks(bonus_variants, full_text)
    bonus_instruction = "\n\n".join(selected_bonuses)
    logger.info(f"summary bonus blocks chat={chat_id} selected={len(selected_bonuses)}")

    # Добавляем общую инструкцию по внедрению бонусов
    full_bonus_instruction = f"""
    === ВАЖНО: ДОПОЛНИТЕЛЬНЫЕ ЭКСПЕРТНЫЕ БЛОКИ ===
    В дополнение к стандартной структуре "ПАНОРАМА", разверни следующие глубокие разборы:
    {bonus_instruction}
    Блоки должны органично вписываться в текст, дополняя "Панораму".
    Пиши их СТРОГО по тому, что реально обсуждали в логе выше. Если материала
    для блока в переписке не оказалось — пропусти этот блок молча. Ни одной
    цифры, дозировки, методики или ссылки на исследование, которых нет в логе
    или в твоих проверенных знаниях: статью читают практикующие врачи, и
    выдуманная конкретика опаснее, чем короткая статья.
    """

    prompt = f"""
    Ты — опытный стоматолог-практик. Твоя задача — выжать из чата конкретную пользу для коллег. 
    Пиши просто, профессионально, но без пафоса. Представь, что пересказываешь суть другу-врачу.

    {full_bonus_instruction}

    === ПРАВИЛА ВНИМАНИЯ ===
    1. Проанализируй ВЕСЬ предоставленный лог. Не фокусируйся только на последних сообщениях. 
    2. Если в начале или середине лога была важная дискуссия, она ОБЯЗАТЕЛЬНО должна попасть в отчет.
    3. Твоя цель — равномерный охват всех тем за отчетный период.
    4. Если один автор пишет несколько сообщений подряд — воспринимай это как единый монолог
    === ПРАВИЛА (КАК ПИСАТЬ) ===
    1. НИКАКОЙ ВОДЫ: Запрещены фразы типа "развернулась жаркая дискуссия", "тонкая грань", "наше профессиональное сообщество". Пиши сразу: "Сегодня спорили о..." или "Главная проблема дня — ...".
    2. БОЛЬШЕ МЯСА: Нужны цифры, бренды, протоколы. Ищи конкретные названия брендов, настройки эндомоторов, время протравки, цифры зарплат (до рубля), проценты. Общие фразы ("обсудили цены") ЗАПРЕЩЕНЫ.
    3. ОФОРМЛЕНИЕ: Заголовки ЖИРНЫМ КАПСОМ.
    4. ПРАВИЛО ГЛУБИНЫ (DEEP DIVE):Пример - Не пиши просто "обсуждали IDS". Напиши: "Обсуждали технику IDS: последовательность нанесения адгезива, время экспозиции и какой именно жидкотекучий композит лучше использовать для запечатывания пор".
    5. ПРАВИЛО АРГУМЕНТАЦИИ: Если кто-то говорит «это плохо», обязательно найди в чате и допиши ПОЧЕМУ это плохо. Приводи анатомические, физические и химические обоснования, которые звучали в чате.
    6. ОПИСАНИЕ ТЕХНОЛОГИЙ: Если упоминается методика (например, «вертипреп»), кратко опиши её суть для тех, кто не в теме, чтобы статья была самодостаточной.
    7. АКАДЕМИЧЕСКАЯ ТОЧНОСТЬ И БАЗА: Используй терминологию доказательной медицины. Прежде чем переходить к лайфхакам из чата, кратко опиши суть (например, если речь об адгезии — упомяни гибридный слой и деградацию коллагена, если об эндодонтии — анатомию системы корневых каналов, и так далее). Статья должна выглядеть как сочетание учебника и практического руководства. В то же время дружественный тон приятного общения должен быть сохранён - статью читать должно быть интересно!
    8. АНТИ-ФАНТАЗИЯ: Если в чате обсуждается спорный или сомнительный метод, который противоречит медицинским стандартам, обязательно укажи на это с пометкой «Важное предупреждение». Не выдумывай факты, которых нет в логах, но дополняй их общепринятыми протоколами (Золотым стандартом), если это необходимо для полноты картины.
    === ПРАВИЛА ОФОРМЛЕНИЯ (ЖЕСТКО) ===
    1. БЕЗ ВОДЯНОГО ВСТУПЛЕНИЯ: не пиши "Вот выжимка", "В этом обзоре мы рассмотрим". Начинай сразу с существа — например "Сегодня спорили о..." или "Главная проблема дня — ...".
    2. ЛИМИТ ДЛИНЫ: не больше {DAILY_CHAR_BUDGET} символов. Это единственная цифра длины в задании — уложись в неё, отбирая главное, а не дописывая всё подряд.
    3. Разрешено использовать маркеры списков (- или •) для перечисления пунктов.
    4. ВЫДЕЛЯЙ важные термины, бренды и выводы **жирным шрифтом**.
    5. Каждый новый раздел начинай с новой строки, выделяя ключевое слово **ЖИРНЫМ**.
    === СТРУКТУРА СТАТЬИ ===
    0. 📚 ТЕОРЕТИЧЕСКИЙ СПРАВОЧНИК  (Краткий ввод в теорию по самой сложной теме дня. Дай определение технологии, перечисли показания и противопоказания согласно мировым стандартам стоматологии. Это фундамент для дальнейшего разбора сообщений)
    1. 🔥 ТЕМА ДНЯ (ГЛУБОКИЙ РАЗБОР)
    (Выбери самую сложную ИЛИ обсуждаемую тему. Распиши её как мини-лекцию: в чем суть, какие инструменты нужны, какие основные ошибки. Минимум 3-4 абзаца).
    2. 🦷 КЛИНИЧЕСКИЕ КЕЙСЫ (ОБРАЗОВАТЕЛЬНЫЙ ФОРМАТ)
    (Не просто "проблема-решение". Пиши так:
    **▶️ СИТУАЦИЯ:** Описание (что болело, какой зуб, что на снимке).
    **ЧТО СДЕЛАЛИ:** Подробно шаги лечения.
    **ЛОГИКА ЛЕЧЕНИЯ:** Почему выбрали именно этот протокол, а не другой.
    **ТЕХНИЧЕСКИЕ ДЕТАЛИ:** Какие боры, какие торки на моторе, какая последовательность инструментов.
    **ПОЧЕМУ ТАК:** Обоснование коллег, почему выбран этот метод.
    **ВЫВОД:** Чему учит этот кейс.
    Каждый пункт - несколько предложений (3-6).
    Сколько кейсов разбирать так подробно: не больше четырёх, самых содержательных.
    Остальные перечисли одной строкой каждый — суть и вывод, без шести полей.
    Шесть полей по 3-6 предложений на десять кейсов физически не укладываются в
    лимит: это 18-36 тысяч символов только на этом разделе.
    3. 💰 РЫНОК И ДЕНЬГИ (ИНСАЙДЫ) (ЭКОНОМИЧЕСКАЯ АНАЛИТИКА)
    (Здесь пиши ВСЕ цифры, которые найдешь. Зарплаты, выручки, стоимость аренды, цены на материалы. Сравнивай мнения. Это самый важный блок для владельцев и врачей).
    4. 🎓 КЛИНИЧЕСКИЕ ТОНКОСТИ И НЮАНСЫ (НОВОЕ!)
    (Собери здесь все мелкие советы, которые проскакивали: как держать зеркало, как не перегреть пульпу, как обрезать матрицу. Это должны быть ценные "фишки").
    5. 📝 ПРАКТИЧЕСКИЕ ПРОТОКОЛЫ И СОВЕТЫ
    (Собери здесь конкретные инструкции: "Как фиксировать", "Чем полировать", "Как общаться с пациентом". Формат чек-листов).

    6. ⚔️ АНАЛИЗ ГЛАВНОГО СПОРА (Если он был) ИЛИ РАЗБОР СЛОЖНОГО КЕЙСА
    (Если был спор. Оформи как:
    🛑 Лагерь А (Аргументы): ...
    ✅ Лагерь Б (Аргументы): ...)
    Найди самый жесткий спор дня. Распиши позиции сторон максимально подробно, с аргументами и контраргументами. Распиши конфликт подробно. Вынеси вердикт на основе EBM.
    6.5. 🔍 СРАВНИТЕЛЬНЫЙ АНАЛИЗ И МАТЕРИАЛОВЕДЕНИЕ
    (Если в чате упоминали несколько материалов одного типа, проведи их глубокое сравнение: например: вязкость, усадка, сила адгезии, удобство полировки или другие параметры. Сделай акцент на химических, физических свойствах и цене (например, наличие MDP-мономера в бонде).
    7. 🛠 ОБЗОР МАТЕРИАЛОВ
    (Честные отзывы из чата. Что говно, а что топ. Подробные отзывы о материалах. Корректные названия).
    
    8. 😂 ЮМОР / ЦИТАТЫЫ
    (1-3 лучшие шутки или цитаты для атмосферы (с контекстом).

    9.🌟 ЭКСПЕРТ ДНЯ 
    Выбери врача из чата, который давал самый полезные советы или протокол. Напиши его имя и за что награжден. При выборе эксперта отдавай приоритет тем, чьи сообщения вызвали одобрение коллег или на чьи советы другие врачи отвечали благодарностью. Эксперт — это тот, чьи слова можно вставить в учебник, или тот, кому коллеги сказали "спасибо" за совет

    ТЕКСТ ПЕРЕПИСКИ:
    {full_text}

    Инструкция для самопроверки: перед выдачей прикинь длину. Если вышло больше {DAILY_CHAR_BUDGET} символов — сократи, убирая наименее содержательные кейсы целиком, а не подрезая каждый раздел: обрезанная на середине статья хуже короткой. Излагай клиническую логику, протоколы, цифры и бренды подробно, но без воды.
    """

    try:
        final_html = ""

        # Генерация в отдельном потоке (не блокирует бота)
        logger.info(f"summary gemini start chat={chat_id} prompt_chars={len(prompt)}")
        response = await _generate_text_singleflight(
            prompt,
            "daily",
            chat_id,
            topic_id,
            len(messages),
            len(prompt),
        )
        
        if not response: 
            runtime_guard.clear_summary_status("daily_gemini_no_response")
            return None
        
        raw_summary = response.text
        logger.info(f"summary gemini done chat={chat_id} chars={len(raw_summary) if raw_summary else 0}")
        final_html = clean_markdown_to_html(raw_summary)
        
        # Вставка фото (если они есть локально или URL)
        for m_id, url in media_map.items():
            placeholder = f"[IMG_{m_id}]"
            if placeholder in final_html:
                # Если URL, то вставляем img src, если нет - можно вставлять заглушку или локальный путь
                final_html = final_html.replace(placeholder, f'<img src="{url}">')

        if msg_count > 0 and "Сообщений за период" not in final_html:
            final_html += f"\n\n<i>Сообщений за период — {msg_count}</i>"
        
        final_html = _safe_truncate_html(final_html)
        
        TELEGRAPH_THRESHOLD = 1500 
        sent_msg = None

        # --- НАСТРОЙКА ОТПРАВКИ В ТОПИК ---
        # Чтобы отправить в топик, нужно сделать reply_to на ID топика
        send_params = {
            'parse_mode': 'HTML',
            'link_preview': True
        }
        if topic_id:
            send_params['reply_to'] = topic_id

        msg_to_send = ""

        if len(final_html) < TELEGRAPH_THRESHOLD:
            msg_to_send = final_html
            direct_send_params = dict(send_params)
            direct_send_params['link_preview'] = False
            logger.info(f"summary telegram send start chat={chat_id} chars={len(msg_to_send)}")
            _write_summary_stage(
                "telegram_send",
                kind="daily",
                chat_id=chat_id,
                topic_id=topic_id,
                message_count=len(messages),
                send_chars=len(msg_to_send),
            )
            sent_msg = await _send_message_once(
                client,
                chat_id,
                topic_id,
                msg_to_send,
                direct_send_params,
                "daily_direct",
            )
        else:
            # Создание Telegraph
            logger.info("📜 Создаем Telegraph страницу...")
            
            # ПРАВКА 1: СДВИГ ДАТЫ (Берем текущее время сервера, а не время первого сообщения)
            date_str = get_russian_date(datetime.now())
            
            title = f"Дайджест 'Учимся Вместе' - {date_str}"
            logger.info(f"summary telegraph start chat={chat_id} chars={len(final_html)}")
            _write_summary_stage(
                "telegraph_create",
                kind="daily",
                chat_id=chat_id,
                topic_id=topic_id,
                message_count=len(messages),
                html_chars=len(final_html),
            )
            page_url, telegraph_error = await create_telegraph_page_async(
                title,
                final_html,
                timeout=TELEGRAPH_TIMEOUT_SECONDS,
            )
            if telegraph_error:
                logger.error("Telegraph subprocess failed: %s", telegraph_error)
            logger.info(f"summary telegraph done chat={chat_id} ok={bool(page_url)}")
            
            if page_url:
                # ПРАВКА 2: ВАРИАТИВНОСТЬ И КОНСТРУКТОР ТИЗЕРА
                
                # А. ХУКИ (Вступление) - 17 вариантов
                intros = [
                    "⚡️ Коллеги, пока вы работали, чат кипел. Собрал главное за сутки.",
                    "☕️ Сэкономьте 3 часа чтения чата. Вот сухой остаток дня.",
                    "🧠 Концентрат опыта. Осторожно, высокое содержание пользы.",
                    "💎 Отделил зерна от плевел. Все инсайды дня в одной статье.",
                    "📉 Рынок, кейсы и технологии. Выжимка для тех, кто ценит время.",
                    "🦷 Не успели прочитать 500 сообщений? Я сделал это за вас.",
                    "🚀 Готовая выжимка для вас по итогам сегодняшних обсуждений.",
                    "👀 О чем спорили, над чем смеялись и чему научились сегодня.",
                    "🧬 Только доказательная медицина и реальная практика. Никакой воды.",
                    "🛡 Ваша страховка от профессионального выгорания — быть в курсе.",
                    "🔥 Самые горячие дискуссии и полезные находки за 24 часа.",
                    "🎩 Достаем кролика из шляпы: все секреты дня в одном файле.",
                    "📝 Методичка дня: протоколы, настройки, материалы.",
                    "🎯 Бьем точно в цель: только то, что пригодится на завтрашнем приеме.",
                    "🕵️‍♂️ Агент 007 в мире стоматологии докладывает обстановку.",
                    "🧱 Фундаментальные знания и мелкие лайфхаки сегодняшнего дня.",
                    "⚖️ Взвешенный взгляд на споры и новинки стоматологии."
                ]

                # Б. НАЧИНКА (Буллеты) - 10 комбинаций (Сеты)
                # Выбираем сеты, которые всегда актуальны, но звучат по-разному
                bullet_sets = [
                    # Сет 1 (Классика)
                    "🔥 <b>Глубокий разбор тем дня</b>\n"
                    "🦷 <b>Протоколы лечения (Step-by-step)</b>\n"
                    "🛠 <b>Честные отзывы о материалах</b>",

                    # Сет 2 (Проблемный)
                    "🛑 <b>Разбор клинических ошибок</b>\n"
                    "💉 <b>Нюансы анестезии и хирургии</b>\n"
                    "⚔️ <b>Аргументы из горячих споров</b>",

                    # Сет 3 (Финансово-технический)
                    "💰 <b>Инсайды по рынку и ценам</b>\n"
                    "📸 <b>Разбор фотопротоколов</b>\n"
                    "🔩 <b>Технические настройки оборудования</b>",

                    # Сет 4 (Лаконичный)
                    "🔹 <b>Только проверенные факты</b>\n"
                    "🔹 <b>Ссылки на исследования</b>\n"
                    "🔹 <b>Опыт коллег без цензуры</b>",

                    # Сет 5 (Образовательный)
                    "🎓 <b>Мини-лекции по сложным темам</b>\n"
                    "🔬 <b>Макро-фото клинических случаев</b>\n"
                    "📝 <b>Готовые алгоритмы действий</b>",

                    # Сет 6 (Инструментальный)
                    "🛠 <b>Чем работать: обзор инструментов</b>\n"
                    "📉 <b>Как сэкономить не теряя качество</b>\n"
                    "🧠 <b>Коллективный разум в действии</b>",

                    # Сет 7 (Осторожный)
                    "⚠️ <b>Предупреждения и грабли</b>\n"
                    "✅ <b>Золотые стандарты лечения</b>\n"
                    "💬 <b>Лучшие цитаты и юмор</b>",
                    
                    # Сет 8 (Для профи)
                    "🌶 <b>Сложные кейсы и их решения</b>\n"
                    "🧪 <b>Химия материалов простыми словами</b>\n"
                    "🤝 <b>Врачебная этика и общение</b>",

                    # Сет 9 (Микс)
                    "▶️ <b>Кейсы дня</b>\n"
                    "💡 <b>Лайфхаки, упрощающие жизнь</b>\n"
                    "💊 <b>Фармакология на практике</b>",

                    # Сет 10 (Итоговый)
                    "🌟 <b>Эксперты дня и их советы</b>\n"
                    "📦 <b>Распаковка новых методик</b>\n"
                    "🏁 <b>Итоги и выводы</b>"
                ]

                # В. ПРИЗЫВ (Кнопка) - 5 вариантов
                ctas = [
                    "👉 <b><a href='{url}'>Читать дайджест (Instant View)</a></b>",
                    "📖 <b><a href='{url}'>Открыть статью (5 минут чтения)</a></b>",
                    "⚡️ <b><a href='{url}'>Изучить подробности</a></b>",
                    "📲 <b><a href='{url}'>Смотреть</a></b>",
                    "🧐 <b><a href='{url}'>Перейти к чтению</a></b>"
                ]

                # Сборка конструктора
                sel_intro = random.choice(intros)
                sel_bullets = random.choice(bullet_sets)
                sel_cta = random.choice(ctas).format(url=page_url)

                msg_to_send = (
                    f"🎓 <b>Дайджест из чата ({date_str})</b>\n\n"
                    f"{sel_intro}\n\n"
                    f"{sel_bullets}\n\n"
                    f"{sel_cta}"
                )
            else:
                msg_to_send = _safe_truncate_html(final_html, max_len=3900)

            logger.info(f"summary telegram send start chat={chat_id} chars={len(msg_to_send)}")
            _write_summary_stage(
                "telegram_send",
                kind="daily",
                chat_id=chat_id,
                topic_id=topic_id,
                message_count=len(messages),
                send_chars=len(msg_to_send),
            )
            sent_msg = await _send_message_once(
                client,
                chat_id,
                topic_id,
                msg_to_send,
                send_params,
                "daily_teaser",
            )

        # 9. ЗАКРЕП
        if sent_msg:
            await _notify_delivery(delivery_hook, sent_msg)
            await _pin_message_safely(client, chat_id, sent_msg.id)

        logger.info(f"✅ Саммари отправлено в {chat_id}")
        runtime_guard.clear_summary_status("daily_summary_done")
        return msg_to_send
        
    except Exception:
        logger.exception("summary failed")
        runtime_guard.clear_summary_status("daily_summary_failed")
        return None
        
        

async def process_weekly_batch(messages, client, chat_id, topic_id=None, delivery_hook=None, cached_message=None):
    """
    Генерация МАСШТАБНОГО еженедельного альманаха.
    """
    if not messages:
        return None
    # Объявляем переменную msg_count, которой не хватало
    msg_count = len(messages)

    send_params = {'parse_mode': 'HTML', 'link_preview': True}
    if topic_id: send_params['reply_to'] = topic_id

    # --- БЫСТРЫЙ ПУТЬ (КЭШ) ---
    # Дневная ветка принимает cached_message с первого дня, недельная не
    # принимала: планировщик обходит цели по очереди, и КАЖДАЯ получала свою
    # генерацию и свою страницу Telegraph. Итог на двух целях — два разных
    # выпуска про одну и ту же неделю (оба объявлены «летописью недели») плюс
    # двойная плата за самую дорогую генерацию в проекте: недельный промпт
    # уходит с thinking HIGH и запасом таймаута 2100 с против 9500 символов
    # результата. Тизер, который возвращает первая цель, содержит ссылку на уже
    # созданную страницу — второй цели остаётся только отправка.
    if cached_message:
        logger.info(f"🚀 Отправка кэша недельного выпуска в {chat_id}")
        _write_summary_stage(
            "telegram_cached_send",
            kind="weekly",
            chat_id=chat_id,
            topic_id=topic_id,
            message_count=msg_count,
        )
        sent_msg = await _send_message_once(
            client,
            chat_id,
            topic_id,
            cached_message,
            send_params,
            "weekly_cached",
        )
        if sent_msg:
            await _notify_delivery(delivery_hook, sent_msg)
            await _pin_message_safely(client, chat_id, sent_msg.id)
        runtime_guard.clear_summary_status("weekly_cached_done")
        return cached_message

    # Фильтруем сообщения от мусора
    filtered_messages = filter_useful_messages(messages)
    if not filtered_messages:
        logger.warning(f"No useful messages left for weekly summary in chat={chat_id}")
        return None

    # 1. СБОРКА ПОЛНОГО ЛОГА
    # Собираем абсолютно всё, чтобы у нейронки была вся фактура
    full_text_parts = ["ПОЛНЫЙ ЛОГ НЕДЕЛИ (Raw Data):\n\n"]
    media_map = {}

    # Ветвление диалогов недельная сборка не учитывала вообще — модель получала
    # плоский лог, и это компенсировалось в промпте указанием «сообщения подряд
    # считай монологом». Замер на живой неделе: 98 ответов из 205 сообщений
    # выборки, у 16 родитель в выборку не попал. Стоимость контекста +2%.
    reply_ids = [msg[6] for msg in filtered_messages if msg[6]]
    reply_lookup = await asyncio.wait_for(
        database.get_texts_by_ids(reply_ids),
        timeout=30,
    )
    batch_ids = {msg[0] for msg in filtered_messages}
    quoted_context = 0

    for msg in filtered_messages:
        m_id, name, username, text, m_desc, date, reply_id, m_url = msg
        dt_str = date.strftime('%d.%m') if isinstance(date, datetime) else str(date)[:10]

        quote_text, was_quoted = _reply_context(reply_id, reply_lookup, batch_ids)
        quoted_context += was_quoted

        # Маркеры для нейронки, чтобы она видела структуру диалогов
        full_text_parts.append(f"MSG_{m_id} | {dt_str} | {name}: {quote_text}{text or ''}\n")

        if m_desc:
            full_text_parts.append(f"[ВАЖНО: К этому сообщению прикреплено ФОТО/ВИДЕО: {m_desc}]\n")

        if m_url:
            media_map[m_id] = m_url

    full_text = "".join(full_text_parts)
    logger.info(
        f"weekly build done chat={chat_id} chars={len(full_text)} "
        f"replies={len(reply_lookup)} quoted={quoted_context} media={len(media_map)}"
    )

    # 2. ПРОМПТ "MEDICAL JOURNALIST" (MAXIMUM DETAILS)
    #
    # ОБЪЁМ. Просили 8000–10000 символов при конвейере, который режет на 9500
    # (max_len по умолчанию в html_safe.safe_truncate_html), причём после
    # clean_markdown_to_html текст только растёт — каждое **X** становится
    # <b>X</b>, плюс подвал со счётчиком. Верхняя половина заказанного диапазона
    # гарантированно уезжала в «[Отчет сокращен из-за лимитов Telegraph]» вместе
    # с последними разделами (доска почёта, юмор). Дневную ветку под бюджет уже
    # подтянули под порог обрезки, и теперь обе ветки берут цифру из одной
    # константы: DAILY_CHAR_BUDGET и WEEKLY_CHAR_BUDGET выведены из
    # WEEKLY_HTML_LIMIT. Зашитая руками цифра в трёх местах промпта — ровно та
    # конструкция, из которой в дневной ветке и выросло расхождение.
    #
    # <br>. Telethon вырезает незнакомые ему теги молча. Замер на telethon
    # 1.42.0: parse('LineOne<br>LineTwo<br><br>End') даёт 'LineOneLineTwoEnd',
    # то есть весь отчёт в одну строку. Этим путём уходит аварийная отправка
    # статьи в чат, когда Telegraph недоступен. Структуру держат ПЕРЕВОДЫ СТРОК:
    # по ним clean_markdown_to_html разделяет абзацы, а blocking_tools собирает
    # <p>/<br> для Telegraph. Дневной промпт про <br> не знает вовсе.
    #
    # МАРКЕРЫ И ИМЕНА. Запрет «никаких маркеров, тире, звёздочек и эмодзи в
    # начале строк» стоял рядом с собственным шаблоном структуры, где каждый
    # заголовок начинается с эмодзи (## ⚡️, ## 🦷), кейсы расписаны через
    # «*   **Суть:**», а десятью строками ниже сказано «Разрешено использовать
    # маркеры списков». Так же спорили «укажи его имя жирным (**Имя**)» с
    # примером «**Доктор Иванов** — за разбор КЛКТ» и запрет выделять имена
    # жирным. Оставлена сторона, которую поддерживает конвейер:
    # clean_markdown_to_html снимает маркеры сам и помнит, что строка была
    # пунктом, а жирное имя внутри фразы заголовком не считается.
    #
    # UNKNOWN. Правило «считай, что это пишет Сергей Елисеев» приписывало
    # реальному врачу всё, что пришло без имени. Замер по stomat_archive.db:
    # 18965 реплик из 117847 (16.1%) идут под именем "Unknown", и за ними стоят
    # 63 РАЗНЫХ sender_id, тогда как имя самого Елисеева встречается в архиве 65
    # раз. То есть модели велели подписать его именем каждую шестую реплику чата
    # — включая чужие ошибки, споры и цены, — и вынести это в «Доску почёта»
    # статьи, которую читает всё сообщество.
    prompt = f"""
    Ты — главный редактор крупного медицинского портала.
    Твоя задача — написать **ДЕТАЛЬНЫЙ ОБЗОР** по материалам чата стоматологов за неделю в пределах {WEEKLY_CHAR_BUDGET} символов.
    === ПРАВИЛА ВНИМАНИЯ ===
    1. Проанализируй ВЕСЬ предоставленный лог. Не фокусируйся только на последних сообщениях. 
    2. Если в начале или середине лога была важная дискуссия, она ОБЯЗАТЕЛЬНО должна попасть в отчет.
    3. Твоя цель — равномерный охват всех тем за отчетный период.
    4. Если один автор пишет несколько сообщений подряд — воспринимай это как единый монолог
    === ГЛАВНОЕ ПРАВИЛО: ОБЪЕМ И ДЕТАЛИ ===
    1. **ЗАПРЕЩЕНО СОКРАЩАТЬ.** Твоя цель — не "саммари", а "летопись". Если обсуждали 15 разных тем — распиши все 15.
    2. **БОЛЬШЕ ИМЕН.** Люди любят, когда их упоминают. Если врач дал дельный совет — укажи его имя жирным (**Имя**). Постарайся упомянуть как можно больше активных участников.
    3. **КЛИНИЧЕСКИЕ КЕЙСЫ.** Это сердце статьи. Описывай их максимально подробно: какой зуб, какой диагноз, какие инструменты, какие файлы, какая ирригация.
        Пиши живым текстом, профессионально, но без пафоса. Представь, что пересказываешь суть другу-врачу. Коллеги обсуждают кейсы.
    === ПРАВИЛА (КАК ПИСАТЬ) ===
    1. НИКАКОЙ ВОДЫ: Запрещены фразы типа "развернулась жаркая дискуссия", "тонкая грань", "наше профессиональное сообщество". Пиши сразу: "Сегодня спорили о..." или "Главная проблема дня — ...".
    2. БОЛЬШЕ МЯСА: Нужны цифры, бренды, протоколы. Ищи конкретные названия брендов, настройки эндомоторов, время протравки, цифры зарплат (до рубля), проценты. Общие фразы ("обсудили цены") ЗАПРЕЩЕНЫ.
    3. ОФОРМЛЕНИЕ: Заголовки ЖИРНЫМ КАПСОМ.
    4. ПРАВИЛО ГЛУБИНЫ (DEEP DIVE):Пример - Не пиши просто "обсуждали IDS". Напиши: "Обсуждали технику IDS: последовательность нанесения адгезива, время экспозиции и какой именно жидкотекучий композит лучше использовать для запечатывания пор".
    5. ПРАВИЛО АРГУМЕНТАЦИИ: Если кто-то говорит «это плохо», обязательно найди в чате и допиши ПОЧЕМУ это плохо. Приводи анатомические, физические и химические обоснования, которые звучали в чате.
    6. ОПИСАНИЕ ТЕХНОЛОГИЙ: Если упоминается методика (например, «вертипреп»), кратко опиши её суть для тех, кто не в теме, чтобы статья была самодостаточной.
    7. АКАДЕМИЧЕСКАЯ ТОЧНОСТЬ И БАЗА: Используй терминологию доказательной медицины. Прежде чем переходить к лайфхакам из чата, кратко опиши суть (например, если речь об адгезии — упомяни гибридный слой и деградацию коллагена, если об эндодонтии — анатомию системы корневых каналов, и так далее). Статья должна выглядеть как сочетание учебника и практического руководства. В то же время дружественный тон приятного общения должен быть сохранён - статью читать должно быть интересно!
    8. АНТИ-ФАНТАЗИЯ: Если в чате обсуждается спорный или сомнительный метод, который противоречит медицинским стандартам, обязательно укажи на это с пометкой «Важное предупреждение». Не выдумывай факты, которых нет в логах, но дополняй их общепринятыми протоколами (Золотым стандартом), если это необходимо для полноты картины.
    9. ПРАВИЛО ДИНАМИЧЕСКИХ ЗАГОЛОВКОВ (ЖЕСТКО): ЗАПРЕЩЕНО использовать стандартные названия разделов типа "Терапия", "Ортопедия" или "Энциклопедия". Каждый заголовок раздела должен быть КРЕАТИВНЫМ и отражать суть обсуждений этой конкретной недели. Примеры: Вместо "Терапия" — "ЭНДОДОНТИЧЕСКИЙ МАРАФОН: БИТВА ЗА МВ2 И СТУПЕНЬКИ", вместо "Главная тема" — "ХОЛИВАР НЕДЕЛИ: ГРЕТЬ ИЛИ НЕ ГРЕТЬ ГУТТАПЕРЧУ?".
    === ПРАВИЛА ОФОРМЛЕНИЯ (ЖЕСТКО) ===
    1. Разрешено использовать маркеры списков (- или •) для перечисления пунктов и эмодзи в заголовках разделов, как в структуре ниже.
    2. Структурируй текст ПЕРЕНОСАМИ СТРОК и ЖИРНЫМ ШРИФТОМ: каждый раздел, кейс и пункт — с новой строки.
    2.2. Если автор в логе обозначен как "Unknown", НЕ приписывай его слова никому конкретно: пиши обезличенно ("один из коллег", "участник чата"). Придумывать ему имя ЗАПРЕЩЕНО.
    3. Каждый новый пункт или мысль начинай с новой строки, выделяя ключевое слово **ЖИРНЫМ**.
    4. ПРИМЕР:
       **СИТУАЦИЯ:** Описание случая...
       **РЕШЕНИЕ:** Описание решения...
    === СТРУКТУРА СТАТЬИ ===
    === СТРУКТУРА СТАТЬИ (СТРОГО СОБЛЮДАТЬ) ===
    
    # 📰 [КРЕАТИВНЫЙ ЗАГОЛОВОК ВЫПУСКА] (Отражающий главную суть недели)
    
    ## ⚡️ [ДИНАМИЧЕСКИЙ ЗАГОЛОВОК ГЛАВНОЙ ТЕМЫ]
    (Самое масштабное обсуждение. Глубокий анализ проблемы, полярные мнения, итоги. Минимум 3-4 абзаца).

    ## 🦷 [ДИНАМИЧЕСКИЙ ЗАГОЛОВОК КЛИНИЧЕСКОЙ ПАНОРАМЫ] (Самый большой раздел!)
    (Здесь собери ВСЕ кейсы, которые были. Не фильтруй. Оформляй каждый кейс отдельно):
    
    **Кейс №1: [Название проблемы] от врача [Имя]**
    *   **Суть:** ...
    *   **Протокол:** (какими инструментами работали, нюансы)
    *   **Обсуждение:** (что сказали коллеги, критиковали или хвалили)
    
    **Кейс №2...** (и так далее, пока не кончатся кейсы)

    ## 🛠 [ДИНАМИЧЕСКИЙ ЗАГОЛОВОК ПО МАТЕРИАЛАМ И ОБОРУДОВАНИЮ]
    (Отдельный блок про "железо" и "химию". Кто что купил? Кто что ругал? Сравнения брендов. Цены. Настройки торка/скорости).
    
    ## 🎓 [ДИНАМИЧЕСКИЙ ЗАГОЛОВОК ЭНЦИКЛОПЕДИИ] (По рубрикам)
    (Если были обсуждения, разбей их по темам. Если темы не было — пропусти рубрику).
    *   **Терапия:** (Протоколы, адгезия, эндодонтия...)
    *   **Ортопедия:** (Преп, оттиски, цементировка...)
    *   **Хирургия:** (Удаления, имплантация, швы...)
    *   **Ортодонтия:** (Брекеты, элайнеры...)
    
    ## ⚔️ ПОЛЕ БИТВЫ (Споры)
    (Самый жаркий конфликт. Подробно аргументы сторон. Кто победил логикой?)
    
    ## 💰 БИЗНЕС И ПРАВО
    (Пациенты, деньги, законы, проверки, зарплаты).
    
    ## 🌟 ДОСКА ПОЧЕТА (ГЕРОИ НЕДЕЛИ)
    (ЗАПРЕЩЕНО писать просто список имен. Перечисли врачей, которые на этой неделе генерировали контент. Для каждого участника ОБЯЗАТЕЛЬНО укажи его конкретную заслугу или ценный вклад. Пример: "**Доктор Иванов** — за филигранный разбор КЛКТ в сложном кейсе, **Доктор Петров** — за подробный протокол адгезивной фиксации").
    
    ## 😂 МЕДИЦИНСКИЙ ЮМОР
    (Шутки, мемы, забавные диалоги).

    === ТРЕБОВАНИЯ К ОФОРМЛЕНИЮ ===
    1. Пиши ЖИВЫМ, ПРОФЕССИОНАЛЬНЫМ языком.
    2. БЕЗ ВСТУПЛЕНИЯ И ЗАКЛЮЧЕНИЯ: Сразу начинай со структуры.
    3. Из HTML-тегов используй только <b> и <i>. Тег <br> ЗАПРЕЩЕН: переносы делай настоящими переводами строк, иначе весь текст склеится в одну строку.
    4. Объём статьи: не больше {WEEKLY_CHAR_BUDGET} символов. Всё, что выше, будет обрезано вместе с последними разделами.

    ЛОГ НЕДЕЛИ:
    {full_text}
    Инструкция для самопроверки: Напиши максимально подробную, профессиональную, глубокую и развернутую статью в пределах {WEEKLY_CHAR_BUDGET} символов. Это летопись, а не краткое саммари. Излагай клиническую логику, протоколы, цифры и бренды во всех подробностях, концентрируй пользу без воды.
    """

    try:
        logger.info("⏳ Генерируем МАСШТАБНЫЙ Weekly Digest (Longread)...")
        
        # Используем executor для асинхронности, так как генерация длинная
        response = await _generate_text_singleflight(
            prompt,
            "weekly",
            chat_id,
            topic_id,
            len(messages),
            len(prompt),
        )
        
        if not response: 
            runtime_guard.clear_summary_status("weekly_gemini_no_response")
            return None
        
        raw_text = response.text
        if not raw_text:
            logger.error("❌ Gemini вернул пустой текст для Weekly")
            runtime_guard.clear_summary_status("weekly_gemini_empty_text")
            return None
            
        logger.info(f"📝 Текст от Gemini получен ({len(raw_text)} симв.). Чистим HTML...")
        final_html = clean_markdown_to_html(raw_text)
        
        # Вставка изображений
        for m_id, url in media_map.items():
            placeholder = f"[IMG_{m_id}]"
            if placeholder in final_html:
                final_html = final_html.replace(placeholder, f'<img src="{url}">')
        
        # Подвал со счётчиком дописывается ПОСЛЕ обрезки и с запасом под свою
        # длину. Раньше он шёл до неё, а обрезка режет с конца: на статье длиннее
        # 9500 символов подвал уходил в отрез первым, вместе с «[Отчет сокращен
        # из-за лимитов Telegraph]» на его месте. То есть счётчик пропадал ровно
        # в тех выпусках, где неделя вышла содержательной.
        footer = ""
        if msg_count > 0 and "Сообщений за неделю" not in final_html:
            footer = f"\n\n<i>Сообщений за неделю — {msg_count}</i>"

        final_html = _safe_truncate_html(final_html, max_len=WEEKLY_HTML_LIMIT - len(footer)) + footer
        # Публикация в Telegraph
        date_str = get_russian_date(datetime.now())
        title = f"WEEKLY: Большая Стоматологическая Газета ({date_str})"
        
        logger.info(f"📤 Отправка в Telegraph (Title: {title})...")
        _write_summary_stage(
            "telegraph_create",
            kind="weekly",
            chat_id=chat_id,
            topic_id=topic_id,
            message_count=len(messages),
            html_chars=len(final_html),
        )
        page_url, telegraph_error = await create_telegraph_page_async(
            title,
            final_html,
            timeout=TELEGRAPH_TIMEOUT_SECONDS,
        )
        if telegraph_error:
            logger.error("Telegraph subprocess failed: %s", telegraph_error)
        
        if not page_url:
            logger.error("❌ Не удалось создать Telegraph страницу (Weekly). Проверь валидность HTML. Пробуем отправить напрямую...")
            # Попробуем отправить напрямую в телеграм, обрезав до 3900 символов
            msg_to_send = _safe_truncate_html(final_html, max_len=3900)
            send_params = {'parse_mode': 'HTML', 'link_preview': False}
            if topic_id: send_params['reply_to'] = topic_id
            # Стадию обязана обновлять и аварийная ветка. Без этой записи в
            # bot_summary_status.json до самого конца висел «telegraph_create»,
            # хотя Telegraph уже отказал: и оператор, и сторож видели зависание
            # на публикации там, где идёт отправка в чат. В дневной ветке отметка
            # перед отправкой стоит на обоих путях — тизера и прямой отправки.
            _write_summary_stage(
                "telegram_send",
                kind="weekly",
                chat_id=chat_id,
                topic_id=topic_id,
                message_count=len(messages),
                send_chars=len(msg_to_send),
            )
            sent_msg = await _send_message_once(
                client,
                chat_id,
                topic_id,
                msg_to_send,
                send_params,
                "weekly_fallback",
            )
            if sent_msg:
                await _notify_delivery(delivery_hook, sent_msg)
                await _pin_message_safely(client, chat_id, sent_msg.id)
                logger.info(f"✅ Временный Weekly Digest отправлен напрямую в {chat_id}")
                runtime_guard.clear_summary_status("weekly_summary_done")
                return msg_to_send
            
            runtime_guard.clear_summary_status("weekly_telegraph_failed")
            return None

        logger.info(f"🔗 Страница создана: {page_url}. Готовим тизер...")
        
        weekly_teasers = [
            f"🗞 <b>ВЫШЕЛ НОВЫЙ НОМЕР WEEKLY ({date_str})</b>\n\n"
            f"Коллеги, это не просто саммари. Это летопись нашей недели.\n"
            f"Внутри огромная статья с разбором всех полетов.\n\n"
            f"💉 <b>Клиническая панорама:</b> Подробный разбор кейсов (эндо, реставрации, хирургия).\n"
            f"🗣 <b>Личности:</b> Кого цитировали, с кем спорили, кому ставили лайки.\n"
            f"⚙️ <b>Материаловедение:</b> Честные отзывы о брендах без рекламы.\n\n"
            f"Чтиво на 15 минут. Заваривайте кофе.\n\n"
            f"👉 <b><a href='{page_url}'>ЧИТАТЬ ПОЛНЫЙ ВЫПУСК</a></b>",

            f"🔥 <b>ИТОГИ НЕДЕЛИ: Большой разбор ({date_str})</b>\n\n"
            f"Собрали в одну статью всё, чем жил чат последние 7 дней.\n\n"
            f"👨‍⚕️ <b>Доска почета:</b> Ищите свои фамилии в тексте!\n"
            f"🦷 <b>Кейс-марафон:</b> Фотопротоколы и тактика лечения.\n"
            f"⚔️ <b>Баттлы:</b> Аргументы сторон в вечных спорах.\n\n"
            f"Энциклопедия коллективного опыта готова.\n\n"
            f"👉 <b><a href='{page_url}'>ОТКРЫТЬ ЛОНГРИД</a></b>"
        ]
        
        msg_to_send = random.choice(weekly_teasers)
        
        send_params = {'parse_mode': 'HTML', 'link_preview': True}
        if topic_id: send_params['reply_to'] = topic_id
        
        _write_summary_stage(
            "telegram_send",
            kind="weekly",
            chat_id=chat_id,
            topic_id=topic_id,
            message_count=len(messages),
            send_chars=len(msg_to_send),
        )
        sent_msg = await _send_message_once(
            client,
            chat_id,
            topic_id,
            msg_to_send,
            send_params,
            "weekly_teaser",
        )
        
        # Закреп. Проверка на sent_msg тут была пропущена, хотя в дневной ветке
        # она есть: обращение к sent_msg.id у None бросало AttributeError уже
        # ПОСЛЕ доставки тизера, внешний except объявлял выпуск неудачей
        # (weekly_summary_failed, возврат None), планировщик не помечал цель
        # доставленной — и на следующем круге через 10 минут публиковал неделю
        # второй раз. Закреп сам по себе неудачу не создаёт: _pin_message_safely
        # глотает свои ошибки, падало именно обращение к .id.
        if sent_msg:
            await _notify_delivery(delivery_hook, sent_msg)
            await _pin_message_safely(client, chat_id, sent_msg.id)

        logger.info(f"✅ MASSIVE Weekly Digest отправлен в {chat_id}")
        runtime_guard.clear_summary_status("weekly_summary_done")
        return msg_to_send

    except Exception:
        logger.exception("weekly summary failed")
        runtime_guard.clear_summary_status("weekly_summary_failed")
        return None
