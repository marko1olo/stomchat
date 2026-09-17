import asyncio  # Добавлено
import os
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
import user_memory
from collections import Counter
from blocking_tools import create_telegraph_page_async, generate_gemini_text_async
from datetime import datetime

logger = logging.getLogger(__name__)
TELEGRAPH_TIMEOUT_SECONDS = 60
GEMINI_GENERATION_TIMEOUT_SECONDS = 2100
TELEGRAM_SEND_TIMEOUT_SECONDS = 90
PIN_TIMEOUT_SECONDS = 30
RECENT_DELIVERY_SCAN_LIMIT = 20
# Порог безопасного объема HTML для Telegraph.
# Замер DOM-дерева показал: лимит JSON-структуры дает CONTENT_TOO_BIG при 14 000+ символов.
# Безопасный потолок с запасом под узлы DOM, картинки figure и подвал со ссылками: 11 500 символов.
TELEGRAPH_SAFE_HTML_LIMIT = 11500
WEEKLY_HTML_LIMIT = TELEGRAPH_SAFE_HTML_LIMIT

# Бюджет символов для ДНЕВНОГО отчета (обзор за 1 день переписки)
DAILY_CHAR_BUDGET = WEEKLY_HTML_LIMIT - 1000

# Бюджет символов для НЕДЕЛЬНОГО отчета (масштабный клинический лонгрид за 7 дней).
WEEKLY_CHAR_BUDGET = WEEKLY_HTML_LIMIT - 1200
MAX_USERS_CONTEXT_CHARS = 2000

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


def _get_bot_deep_link():
    try:
        import assistant
        username = getattr(assistant, "BOT_USERNAME", None) or os.getenv("STOMCHAT_BOT_USERNAME", "").lstrip("@")
    except Exception:
        username = os.getenv("STOMCHAT_BOT_USERNAME", "").lstrip("@")
    if not username:
        username = "docendobot"
    return f'<a href="https://t.me/{username}?start=consult">@{username}</a>'


def build_clinical_assistant_cta(format_type: str = "telegraph") -> str:
    """
    Генерирует продающий клинический призыв к действию (CTA) с подробным описанием
    возможностей бота в личных сообщениях:
    - Мульти-анализ снимков и фото (Vision, серии/альбомы);
    - Точный расчет дозировок анестезии и антибиотиков по весу и соматике (ASA);
    - Каталог клинических протоколов (/protocols);
    - Клиническая память доктора (/profile);
    - Симулятор клинических кейсов.
    """
    try:
        import assistant
        username = getattr(assistant, "BOT_USERNAME", None) or os.getenv("STOMCHAT_BOT_USERNAME", "").lstrip("@")
    except Exception:
        username = os.getenv("STOMCHAT_BOT_USERNAME", "").lstrip("@")
    if not username:
        username = "docendobot"

    if format_type == "telegraph":
        return (
            "<hr/>\n"
            f"<p>🤖 <b>Клинический ассистент <a href=\"https://t.me/{username}?start=consult\">@{username}</a> в Telegram:</b> "
            f"анализ снимков и КЛКТ (Vision), расчет анестезии и антибиотиков по весу и соматике (ASA), "
            f"доказательные протоколы (/protocols) и клиническая память врача (/profile).</p>\n"
            f"<p>👉 <b><a href=\"https://t.me/{username}?start=consult\">Открыть ассистента в ЛС (@{username})</a></b> "
            f"— мгновенный разбор кейса или снимка прямо на приеме.</p>"
        )
    elif format_type == "telegram":
        return (
            f"💬 <b>Клинический ассистент в ЛС:</b> <a href=\"https://t.me/{username}?start=consult\">@{username}</a>\n"
            f"<i>(Анализ снимков/КЛКТ, расчет анестезии по весу и соматике, клинические протоколы /protocols, память врача /profile)</i>"
        )
    return ""



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


def embed_media_into_summary_html(final_html: str, media_map: dict, media_captions: dict = None, max_images: int = 6) -> str:
    """
    Внедряет семантические узлы Telegraph figure/img/figcaption в итоговый HTML.
    
    1. Точная замена маркеров [IMG_{m_id}], расставленных моделью.
    2. Детерминированный фоллбэк: если модель описала случай, но забыла маркер,
       допустимые клинические снимки встраиваются в раздел клинических кейсов.
    3. Зачистка любых оставшихся плейсхолдеров [IMG_\\d+].
    """
    if not final_html or not media_map:
        return re.sub(r'\[IMG_\d+\]', '', final_html or '')

    media_captions = media_captions or {}
    placed_ids = set()
    llm_placed_count = 0

    # Шаг 1: Точная замена маркеров, сгенерированных LLM
    for m_id, url in media_map.items():
        if not url:
            continue
        placeholder = f"[IMG_{m_id}]"
        if placeholder in final_html:
            caption = media_captions.get(m_id) or f"Клинический снимок #{m_id}"
            if len(caption) > 140:
                caption = caption[:137] + "..."
            fig_node = f'\n\n<figure><img src="{url}"><figcaption>{html.escape(caption)}</figcaption></figure>\n\n'
            # Заменяем плейсхолдер вместе с возможной пунктуацией встык
            pattern = re.compile(rf'[.,;:—\-]?\s*\[IMG_{m_id}\]\s*[.,;:—\-]?', re.IGNORECASE)
            final_html = pattern.sub(fig_node, final_html)
            placed_ids.add(m_id)
            llm_placed_count += 1

    fallback_count = 0
    # Шаг 2: Детерминированный фоллбэк для клинических снимков, упущенных моделью
    remaining_ids = [m_id for m_id, url in media_map.items() if m_id not in placed_ids and url]
    if remaining_ids and (llm_placed_count < max_images):
        case_headers = [
            "КЛИНИЧЕСКИЕ КЕЙСЫ",
            "КЛИНИЧЕСКАЯ ПАНОРАМА",
            "ТЕМА ДНЯ",
            "Клинические кейсы",
            "Клиническая панорама"
        ]
        
        valid_fallback = []
        for m_id in remaining_ids:
            cap = media_captions.get(m_id, "")
            # Исключаем явные мемы / немедицинские заглушки
            if "немедицинск" in cap.lower() or "мем" in cap.lower():
                continue
            valid_fallback.append(m_id)

        target_header_pos = -1
        for hdr in case_headers:
            pos = final_html.find(hdr)
            if pos != -1:
                target_header_pos = pos
                break

        slots_left = max_images - llm_placed_count
        for m_id in valid_fallback[:slots_left]:
            url = media_map[m_id]
            caption = media_captions.get(m_id) or f"Клинический снимок #{m_id}"
            if len(caption) > 140:
                caption = caption[:137] + "..."
            fig_node = f'<figure><img src="{url}"><figcaption>{html.escape(caption)}</figcaption></figure>'

            msg_marker = f"MSG_{m_id}"
            if msg_marker in final_html:
                pos = final_html.find(msg_marker)
                end_p = final_html.find("\n\n", pos)
                if end_p != -1:
                    final_html = final_html[:end_p] + f"\n\n{fig_node}" + final_html[end_p:]
                else:
                    final_html += f"\n\n{fig_node}"
                placed_ids.add(m_id)
                fallback_count += 1
            elif target_header_pos != -1:
                end_header_p = final_html.find("\n\n", target_header_pos)
                if end_header_p != -1:
                    final_html = final_html[:end_header_p] + f"\n\n{fig_node}" + final_html[end_header_p:]
                    # Продвигаем позицию за вставленный снимок и следующий абзац текста,
                    # чтобы последующие снимки не склеивались встык
                    next_scan_pos = end_header_p + len(fig_node) + 4
                    next_p = final_html.find("\n\n", next_scan_pos)
                    target_header_pos = (next_p + 2) if next_p != -1 else next_scan_pos
                else:
                    final_html += f"\n\n{fig_node}"
                placed_ids.add(m_id)
                fallback_count += 1

    final_html = re.sub(r'\[IMG_\d+\]', '', final_html)
    logger.info(
        "summary media embedded: total=%d llm_placed=%d fallback=%d final_images=%d",
        len(media_map), llm_placed_count, fallback_count, len(placed_ids)
    )
    return final_html



def _message_matches_topic(message, topic_id):
    if not topic_id:
        return True
    reply_to = getattr(message, "reply_to", None)
    return (
        getattr(reply_to, "reply_to_msg_id", None) == topic_id
        or getattr(reply_to, "reply_to_top_id", None) == topic_id
    )


class _LocalMessage:
    def __init__(self, msg_id, text="", reply_to_msg_id=None):
        self.id = msg_id
        self.text = text
        self.message = text
        self.raw_text = text
        self.reply_to = type("ReplyTo", (), {
            "reply_to_msg_id": reply_to_msg_id,
            "reply_to_top_id": reply_to_msg_id,
        })()


async def _find_recent_matching_message(client, chat_id, topic_id, text):
    wanted = _normalize_delivery_text(text)
    if not wanted:
        return None

    # 1. Локальная проверка по SQLite (bot_sent_messages + messages) перед сетевым вызовом,
    # устраняющая ошибку MTProto 'GetHistoryRequest restricted for bot users'
    try:
        if hasattr(database, "get_recent_messages_for_dedup"):
            db_messages = await database.get_recent_messages_for_dedup(
                chat_id=chat_id, limit=RECENT_DELIVERY_SCAN_LIMIT
            )
            for msg_id, msg_text, reply_to_id in db_messages or []:
                local_msg = _LocalMessage(msg_id, msg_text or "", reply_to_id)
                if not _message_matches_topic(local_msg, topic_id):
                    continue
                if _normalize_delivery_text(msg_text or "") == wanted:
                    return local_msg
    except Exception as exc:
        logger.debug("local db recent message check failed chat=%s topic=%s: %s", chat_id, topic_id, exc)

    # 2. Сетевой fallback через client.get_messages (для тестов с FakeClient)
    try:
        recent_messages = await asyncio.wait_for(
            client.get_messages(chat_id, limit=RECENT_DELIVERY_SCAN_LIMIT),
            timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        err_msg = str(exc).lower()
        if "gethistoryrequest" in err_msg or "bot users" in err_msg or "restricted" in err_msg:
            logger.debug("recent delivery scan skipped (bot restricted) chat=%s topic=%s: %s", chat_id, topic_id, exc)
        else:
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

    # ХАРД-ГАРД: Защита от случайной отправки неструктурированных простыней текста (>1800 символов)
    # Ни при каких сбоях бот не должен спамить в чат сырыми кусками HTML-статьи.
    if len(text) > 1800 and not text.startswith(("<pre>", "```")):
        logger.error(
            "CRITICAL SAFEGUARD TRIGGERED: Attempted to send raw long text (%d chars) to chat=%s label=%s! "
            "Truncating to emergency teaser format to protect chat from text dump.",
            len(text), chat_id, label
        )
        text = _safe_truncate_html(text, max_len=1200) + "\n\n⚠️ <i>(Сообщение сокращено системой безопасности от переполнения чата)</i>"

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


async def _create_telegraph_page_resilient(title: str, telegraph_html: str, full_html: str, footer: str, timeout: float = TELEGRAPH_TIMEOUT_SECONDS):
    """
    Публикует страницу в Telegraph с каскадным авто-даунсайзингом при ошибке CONTENT_TOO_BIG.
    Гарантирует, что Telegraph не упадет из-за превышения размера узлов DOM.
    Возвращает (page_url, error_message).
    """
    res = await create_telegraph_page_async(title, telegraph_html, timeout=timeout)
    url, err = res if isinstance(res, tuple) else (res, None)
    if url:
        return url, None

    # Каскад 1: если ошибка CONTENT_TOO_BIG, сначала пробуем РАЗДЕЛИТЬ НА ЧАСТИ (multipart)
    # без потери клинического контента
    if err and "CONTENT_TOO_BIG" in str(err):
        logger.warning(
            "Telegraph CONTENT_TOO_BIG on attempt 1 (chars=%d). Splitting into multipart pages...",
            len(telegraph_html)
        )
        try:
            from html_safe import split_html_for_telegraph
            chunks = split_html_for_telegraph(full_html, max_len=18000)
            if len(chunks) <= 1:
                chunks = split_html_for_telegraph(full_html, max_len=9500)

            if len(chunks) >= 2:
                logger.info("Splitting article into %d linked Telegraph pages", len(chunks))
                part_urls = [None] * len(chunks)
                last_idx = len(chunks) - 1
                p_last_content = chunks[last_idx] + footer
                p_last_title = f"{title} (Часть {last_idx + 1})"
                res_last = await create_telegraph_page_async(p_last_title, p_last_content, timeout=timeout)
                url_last, err_last = res_last if isinstance(res_last, tuple) else (res_last, None)
                if url_last:
                    part_urls[last_idx] = url_last
                    all_parts_ok = True
                    for idx in range(last_idx - 1, -1, -1):
                        next_url = part_urls[idx + 1]
                        nav_next = f'\n\n<hr><p>👉 <b><a href="{next_url}">Читать продолжение (Часть {idx + 2}) ➡️</a></b></p>'
                        p_content = chunks[idx] + nav_next
                        p_title = f"{title} (Часть {idx + 1})" if idx > 0 else title
                        res_p = await create_telegraph_page_async(p_title, p_content, timeout=timeout)
                        url_p, err_p = res_p if isinstance(res_p, tuple) else (res_p, None)
                        if url_p:
                            part_urls[idx] = url_p
                        else:
                            all_parts_ok = False
                            break
                    if all_parts_ok and part_urls[0]:
                        logger.info("Multipart Telegraph published successfully: %s", part_urls)
                        return part_urls[0], None
        except Exception as split_err:
            logger.error("Multipart Telegraph splitting failed: %s", split_err)

        # Каскад 2: аварийный даунсайзинг до 9000 символов, если мультипарт не сработал
        downsized_1 = _safe_truncate_html(full_html, max_len=9000 - len(footer)) + footer
        res2 = await create_telegraph_page_async(title, downsized_1, timeout=timeout)
        url2, err2 = res2 if isinstance(res2, tuple) else (res2, None)
        if url2:
            logger.info("Telegraph recovered on attempt 2 after downsizing to 9000 chars")
            return url2, None

        # Каскад 3: если всё еще CONTENT_TOO_BIG, вырезаем <figure> и ужимаем до 7500
        if err2 and "CONTENT_TOO_BIG" in str(err2):
            logger.warning(
                "Telegraph CONTENT_TOO_BIG on attempt 2. Stripping <figure> tags and downsizing to 7500..."
            )
            no_figures = re.sub(r'<figure>.*?</figure>', '', full_html, flags=re.DOTALL)
            downsized_2 = _safe_truncate_html(no_figures, max_len=7500 - len(footer)) + footer
            res3 = await create_telegraph_page_async(title, downsized_2, timeout=timeout)
            url3, err3 = res3 if isinstance(res3, tuple) else (res3, None)
            if url3:
                logger.info("Telegraph recovered on attempt 3 without figures at 7500 chars")
                return url3, None
            return None, err3

    return None, err


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
    


clean_markdown_to_html = html_safe.clean_markdown_to_html


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
    if topic_id:
        send_params['reply_to'] = topic_id

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
    media_captions = {} # Описания снимков для подписей figcaption

    logger.info(f"summary build start chat={chat_id} messages={len(filtered_messages)}")
    reply_ids = [msg[6] for msg in filtered_messages if msg[6]]
    reply_lookup = await asyncio.wait_for(
        database.get_texts_by_ids(reply_ids),
        timeout=30,
    )

    batch_ids = {msg[0] for msg in filtered_messages}
    quoted_context = 0
    author_counts = Counter()

    for msg in filtered_messages:
        # Распаковка полей из БД (обратная совместимость с 8- и 9-элементными кортежами)
        m_id, name, username, text, m_desc, date, reply_id, m_url = msg[:8]
        sender_id = msg[8] if len(msg) > 8 else None

        if sender_id is not None:
            try:
                uid = int(sender_id)
                if uid > 0:
                    txt = (text or "").strip()
                    if txt and not user_memory.is_trivial_message(txt):
                        author_counts[uid] += 2
                    elif txt:
                        author_counts[uid] += 1
            except (ValueError, TypeError):
                pass

        quote_text, was_quoted = _reply_context(reply_id, reply_lookup, batch_ids)
        quoted_context += was_quoted

        full_text_parts.append(f"MSG_{m_id} | {name}: {quote_text}{text or ''}\n")

        if m_desc:
            full_text_parts.append(f"(На фото в MSG_{m_id}: {m_desc})\n")

        if m_url:
            media_map[m_id] = m_url
            if m_desc:
                media_captions[m_id] = m_desc

    full_text = "".join(full_text_parts)
    logger.info(
        f"summary build done chat={chat_id} chars={len(full_text)} "
        f"replies={len(reply_lookup)} quoted={quoted_context} media={len(media_map)}"
    )

    # Формирование блока клинических профилей активных авторов (лимит до 2000 символов)
    active_user_ids = [uid for uid, _ in author_counts.most_common(20)]
    users_chunk_context = ""
    if active_user_ids:
        try:
            users_chunk_context = await user_memory.format_users_chunk_context(
                active_user_ids, max_chars=MAX_USERS_CONTEXT_CHARS
            )
        except Exception as e:
            logger.error(f"Error fetching clinical profiles for daily summary: {e}")
            users_chunk_context = ""

    profiles_block = f"\n{users_chunk_context}\n" if users_chunk_context else ""

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
    5. Каждый новый раздел ОБЯЗАН начинаться с новой строки со знака ## и отделяться от текста ПУСТОЙ СТРОКОЙ (\n\n). Запрещено сливать заголовок раздела и текст в один абзац!
    === СТРУКТУРА СТАТЬИ (СТРОГО СОБЛЮДАТЬ ЗАГОЛОВКИ И ПЕРЕНОСЫ) ===
    ## 0. 📚 ТЕОРЕТИЧЕСКИЙ СПРАВОЧНИК: [Тема]
    (Краткий ввод в теорию по самой сложной теме дня. Дай определение технологии, перечисли показания и противопоказания согласно мировым стандартам стоматологии. Это фундамент для дальнейшего разбора сообщений).

    ## 1. 🔥 ТЕМА ДНЯ (ГЛУБОКИЙ РАЗБОР): [Тема]
    (Выбери самую сложную ИЛИ обсуждаемую тему. Распиши её как мини-лекцию: в чем суть, какие инструменты нужны, какие основные ошибки. Минимум 3-4 абзаца).

    ## 2. 🦷 КЛИНИЧЕСКИЕ КЕЙСЫ (ОБРАЗОВАТЕЛЬНЫЙ ФОРМАТ)
    Каждый подробный клинический кейс начинай с подзаголовка:
    ### Кейс 1: [Имя врача / диагноз / суть случая]
    **▶️ СИТУАЦИЯ:** ...
    [IMG_XXXXX]
    **ЧТО СДЕЛАЛИ:** ...
    **ЛОГИКА ЛЕЧЕНИЯ:** ...
    **ТЕХНИЧЕСКИЕ ДЕТАЛИ:** ...
    **ПОЧЕМУ ТАК:** ...
    **ВЫВОД:** ...

    Сколько кейсов разбирать так подробно: не больше четырёх, самых содержательных.
    Остальные перечисли одной строкой каждый — суть и вывод, без шести полей.
    Шесть полей по 3-6 предложений на десять кейсов физически не укладываются в лимит: это 18-36 тысяч символов только на этом разделе.

    ## 3. 💰 РЫНОК И ДЕНЬГИ (ИНСАЙДЫ)
    (Здесь пиши ВСЕ цифры, которые найдешь. Зарплаты, выручки, стоимость аренды, цены на материалы. Сравнивай мнения. Это самый важный блок для владельцев и врачей).

    ## 4. 🎓 КЛИНИЧЕСКИЕ ТОНКОСТИ И НЮАНСЫ
    (Собери здесь все мелкие советы, которые проскакивали: как держать зеркало, как не перегреть пульпу, как обрезать матрицу. Это должны быть ценные "фишки").

    ## 5. 📝 ПРАКТИЧЕСКИЕ ПРОТОКОЛЫ И СОВЕТЫ
    (Собери здесь конкретные инструкции: "Как фиксировать", "Чем полировать", "Как общаться с пациентом". Формат чек-листов).

    ## 6. ⚔️ АНАЛИЗ ГЛАВНОГО СПОРА
    (Если был спор. Оформи как:
    🛑 Лагерь А (Аргументы): ...
    ✅ Лагерь Б (Аргументы): ...)
    Найди самый жесткий спор дня. Распиши позиции сторон максимально подробно, с аргументами и контраргументами. Распиши конфликт подробно. Вынеси вердикт на основе EBM.

    ## 6.5. 🔍 СРАВНИТЕЛЬНЫЙ АНАЛИЗ И МАТЕРИАЛОВЕДЕНИЕ
    (Если в чате упоминали несколько материалов одного типа, проведи их глубокое сравнение: например: вязкость, усадка, сила адгезии, удобство полировки или другие параметры. Сделай акцент на химических, физических свойствах и цене (например, наличие MDP-мономера в бонде).

    ## 7. 🛠 ОБЗОР МАТЕРИАЛОВ
    (Честные отзывы из чата. Что говно, а что топ. Подробные отзывы о материалах. Корректные названия).
    
    ## 8. 😂 ЮМОР / ЦИТАТЫ
    (1-3 лучшие шутки или цитаты для атмосферы (с контекстом).

    ## 9. 🌟 ЭКСПЕРТ ДНЯ
    Выбери врача из чата, который продемонстрировал наивысшую клиническую экспертизу и помог коллегам.
    КРИТИЧЕСКИ ВАЖНО: Если в блоке «НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ» содержатся профили врачей, обязательно сопоставляй советы доктора с его подтвержденной специализацией, клиническим арсеналом (оборудованием, микроскопом) и клиническими протоколами из досье. Приоритет отдавай обоснованным клиническим рекомендациям врачей с подтвержденным статусом в данной области, а не случайным или эмоциональным репликам. Укажи имя/ник эксперта, его подтвержденную специализацию и конкретную пользу, принесенную сообществу.
{profiles_block}
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
        cleaned_html = clean_markdown_to_html(raw_summary)
        
        # Вставка фото (семантический узел Telegraph figure/figcaption + детерминированный fallback)
        full_html = embed_media_into_summary_html(cleaned_html, media_map, media_captions)

        cta_telegraph = build_clinical_assistant_cta("telegraph")
        count_footer = f"\n\n<i>Сообщений за период — {msg_count}</i>" if msg_count > 0 and "Сообщений за период" not in full_html else ""
        telegraph_footer = f"\n\n{cta_telegraph}{count_footer}"
        
        # Благодаря прямому UTF-8 транспорту одна страница Telegraph безопасно вмещает до 25 000+ символов.
        # Не обрезаем статью преждевременно: _create_telegraph_page_resilient опубликует её целиком
        # либо автоматически разделит на связанные части (Часть 1, Часть 2) при превышении лимита.
        telegraph_html = full_html + telegraph_footer
        
        TELEGRAPH_THRESHOLD = 1500 
        sent_msg = None

        # --- НАСТРОЙКА ОТПРАВКИ В ТОПИК ---
        send_params = {
            'parse_mode': 'HTML',
            'link_preview': True
        }
        if topic_id:
            send_params['reply_to'] = topic_id

        msg_to_send = ""

        if len(full_html) < TELEGRAPH_THRESHOLD:
            msg_to_send = full_html
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
            # Создание Telegraph с каскадным даунсайзингом
            logger.info("📜 Создаем Telegraph страницу (Daily)...")
            date_str = get_russian_date(datetime.now())
            title = f"Дайджест 'Учимся Вместе' - {date_str}"
            logger.info(f"summary telegraph start chat={chat_id} chars={len(telegraph_html)}")
            _write_summary_stage(
                "telegraph_create",
                kind="daily",
                chat_id=chat_id,
                topic_id=topic_id,
                message_count=len(messages),
                html_chars=len(telegraph_html),
            )
            page_url, telegraph_error = await _create_telegraph_page_resilient(
                title,
                telegraph_html,
                full_html,
                telegraph_footer,
                timeout=TELEGRAPH_TIMEOUT_SECONDS,
            )
            if telegraph_error:
                logger.error("Telegraph daily subprocess failed: %s", telegraph_error)
            logger.info(f"summary telegraph done chat={chat_id} ok={bool(page_url)}")

            # ХУКИ И БУЛЛЕТЫ ДЛЯ ТИЗЕРА
            intros = [
                "⚡️ Коллеги, ключевые клинические обсуждения и опыт чата за последние 24 часа.",
                "🔬 Главные клинические случаи, разборы протоколов и аргументы в спорах за сутки.",
                "📚 Практическая выжимка дня: тактика лечения, ошибки и разбор материалов.",
                "☕️ Сэкономьте время на чтении архива чата. Вот сухой остаток дня.",
                "🧠 Концентрат практического опыта коллег в одном разборе.",
                "💎 Выжимка клинических инсайтов и практических нюансов дня в одной статье.",
                "📉 Рынок, кейсы и технологии. Выжимка для тех, кто ценит время.",
                "🚀 Готовая выжимка для вас по итогам сегодняшних обсуждений.",
                "👀 О чем спорили, над чем рассуждали и чему научились сегодня.",
                "🧬 Только доказательная медицина и реальная практика. Никакой воды.",
                "🛡 Ваша профессиональная опора: клинические нюансы и опыт коллег.",
                "🔥 Самые горячие дискуссии и полезные находки за 24 часа.",
                "📝 Методичка дня: протоколы, настройки, материалы.",
                "🎯 Бьем точно в цель: только то, что пригодится на завтрашнем приеме.",
                "🧱 Фундаментальные знания и практические лайфхаки сегодняшнего дня.",
                "⚖️ Взвешенный взгляд на споры и новинки стоматологии."
            ]

            bullet_sets = [
                "🔥 <b>Глубокий разбор тем дня</b>\n🦷 <b>Протоколы лечения (Step-by-step)</b>\n🛠 <b>Честные отзывы о материалах</b>",
                "🛑 <b>Разбор клинических ошибок</b>\n💉 <b>Нюансы анестезии и хирургии</b>\n⚔️ <b>Аргументы из горячих споров</b>",
                "💰 <b>Инсайды по рынку и ценам</b>\n📸 <b>Разбор фотопротоколов</b>\n🔩 <b>Технические настройки оборудования</b>",
                "🔹 <b>Только проверенные факты</b>\n🔹 <b>Ссылки на исследования</b>\n🔹 <b>Опыт коллег без цензуры</b>",
                "🎓 <b>Мини-лекции по сложным темам</b>\n🔬 <b>Макро-фото клинических случаев</b>\n📝 <b>Готовые алгоритмы действий</b>",
                "🛠 <b>Чем работать: обзор инструментов</b>\n📉 <b>Как сэкономить не теряя качество</b>\n🧠 <b>Коллективный разум в действии</b>",
                "⚠️ <b>Предупреждения и грабли</b>\n✅ <b>Золотые стандарты лечения</b>\n💬 <b>Лучшие цитаты и разборы</b>",
                "🌶 <b>Сложные кейсы и их решения</b>\n🧪 <b>Химия материалов простыми словами</b>\n🤝 <b>Врачебная этика и общение</b>",
                "▶️ <b>Кейсы дня</b>\n💡 <b>Лайфхаки, упрощающие жизнь</b>\n💊 <b>Фармакология на практике</b>",
                "🌟 <b>Эксперты дня и их советы</b>\n📦 <b>Распаковка новых методик</b>\n🏁 <b>Итоги и выводы</b>"
            ]

            ctas = [
                "👉 <b><a href='{url}'>Читать дайджест (Instant View)</a></b>",
                "📖 <b><a href='{url}'>Открыть статью (5 минут чтения)</a></b>",
                "⚡️ <b><a href='{url}'>Изучить подробности</a></b>",
                "📲 <b><a href='{url}'>Смотреть</a></b>",
                "🧐 <b><a href='{url}'>Перейти к чтению</a></b>"
            ]

            def _extract_topics(html_doc: str, max_t: int = 3) -> list:
                candidates = []
                patterns = [
                    r'(?:ТЕМА ДНЯ|ТЕМА ДНЯ \(ГЛУБОКИЙ РАЗБОР\))[:\s\-–—]+([^\n<]+)',
                    r'(?:▶️\s*СИТУАЦИЯ|КЕЙС|СЛУЧАЙ)[:\s\-–—]+([^\n<]+)',
                    r'<h4>([^<]+)</h4>',
                    r'<h3>([^<]+)</h3>',
                    r'<b>([А-ЯЁ\s]{6,40})</b>'
                ]
                for pat in patterns:
                    for m in re.findall(pat, html_doc, re.IGNORECASE):
                        cl = re.sub(r'<[^>]+>', '', m).strip(' :–—-.')
                        if 8 <= len(cl) <= 60:
                            if not any(st in cl.upper() for st in (
                                "СТРУКТУРА", "ПРАВИЛА", "ВЫВОД", "ЛОГИКА", "ТЕХНИЧЕСКИЕ", "ПОЧЕМУ", 
                                "КЛИНИЧЕСКИЕ КЕЙСЫ", "ТЕОРЕТИЧЕСКИЙ СПРАВОЧНИК", "СООБЩЕНИЙ ЗА",
                                "ДАЙДЖЕСТ", "УЧИМСЯ ВМЕСТЕ"
                            )):
                                if cl not in candidates:
                                    candidates.append(cl)
                        if len(candidates) >= max_t:
                            break
                    if len(candidates) >= max_t:
                        break
                return candidates[:max_t]

            sel_intro = random.choice(intros)
            dynamic_topics = _extract_topics(full_html)
            if dynamic_topics:
                sel_bullets = "\n".join(f"🔹 <b>{t}</b>" for t in dynamic_topics)
            else:
                sel_bullets = random.choice(bullet_sets)

            if page_url:
                sel_cta = random.choice(ctas).format(url=page_url)
            else:
                sel_cta = "📄 <b>Полный иллюстрированный выпуск доступен в прикрепленном PDF-файле ниже 👇</b>"

            cta_telegram = build_clinical_assistant_cta("telegram")
            msg_to_send = (
                f"🎓 <b>Дайджест из чата ({date_str})</b>\n\n"
                f"{sel_intro}\n\n"
                f"{sel_bullets}\n\n"
                f"{sel_cta}\n\n"
                f"{cta_telegram}"
            )

            send_params['link_preview'] = bool(page_url)
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

        # 9. ЗАКРЕП И ГЕНЕРАЦИЯ PDF
        if sent_msg:
            await _notify_delivery(delivery_hook, sent_msg)
            await _pin_message_safely(client, chat_id, sent_msg.id)

            # Генерация и отправка верстанной журнальной PDF-версии вестника
            try:
                from digest_pdf import generate_digest_pdf
                pdf_title = f"Клинический Дайджест StomChat ({date_str})"
                pdf_path = await generate_digest_pdf(
                    html_content=full_html,
                    title=pdf_title,
                    subtitle="Ежедневный клинический вестник профессионального сообщества",
                    msg_count=msg_count,
                    date_str=date_str,
                )
                if pdf_path and os.path.exists(pdf_path) and hasattr(client, "send_file"):
                    pdf_caption = (
                        f"📄 <b>{pdf_title}</b>\n\n"
                        f"Полная иллюстрированная версия со всеми снимками, протоколами и кейсами дня."
                    )
                    pdf_params = {'caption': pdf_caption, 'parse_mode': 'HTML'}
                    if topic_id:
                        pdf_params['reply_to'] = topic_id
                    await client.send_file(chat_id, pdf_path, **pdf_params)
                    logger.info("PDF daily digest document delivered to chat=%s", chat_id)
            except Exception as pdf_err:
                logger.warning("PDF daily digest delivery failed: %s", pdf_err)

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
    if topic_id:
        send_params['reply_to'] = topic_id

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
    media_captions = {}

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
    author_counts = Counter()

    for msg in filtered_messages:
        # Распаковка полей из БД (обратная совместимость с 8- и 9-элементными кортежами)
        m_id, name, username, text, m_desc, date, reply_id, m_url = msg[:8]
        sender_id = msg[8] if len(msg) > 8 else None

        if sender_id is not None:
            try:
                uid = int(sender_id)
                if uid > 0:
                    txt = (text or "").strip()
                    if txt and not user_memory.is_trivial_message(txt):
                        author_counts[uid] += 2
                    elif txt:
                        author_counts[uid] += 1
            except (ValueError, TypeError):
                pass

        dt_str = date.strftime('%d.%m') if isinstance(date, datetime) else str(date)[:10]

        quote_text, was_quoted = _reply_context(reply_id, reply_lookup, batch_ids)
        quoted_context += was_quoted

        # Маркеры для нейронки, чтобы она видела структуру диалогов
        full_text_parts.append(f"MSG_{m_id} | {dt_str} | {name}: {quote_text}{text or ''}\n")

        if m_desc:
            full_text_parts.append(f"[ВАЖНО: К этому сообщению прикреплено ФОТО/ВИДЕО: {m_desc}]\n")

        if m_url:
            media_map[m_id] = m_url
            if m_desc:
                media_captions[m_id] = m_desc

    full_text = "".join(full_text_parts)
    logger.info(
        f"weekly build done chat={chat_id} chars={len(full_text)} "
        f"replies={len(reply_lookup)} quoted={quoted_context} media={len(media_map)}"
    )

    # Формирование блока клинических профилей авторов недели (лимит до 2000 символов)
    active_user_ids = [uid for uid, _ in author_counts.most_common(20)]
    users_chunk_context = ""
    if active_user_ids:
        try:
            users_chunk_context = await user_memory.format_users_chunk_context(
                active_user_ids, max_chars=MAX_USERS_CONTEXT_CHARS
            )
        except Exception as e:
            logger.error(f"Error fetching clinical profiles for weekly summary: {e}")
            users_chunk_context = ""

    profiles_block = f"\n{users_chunk_context}\n" if users_chunk_context else ""

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
    Ты — главный редактор клинического стоматологического издания.
    Твоя задача — написать масштабный, глубокий **КЛИНИЧЕСКИЙ ОБЗОР (ЛОНГРИД)** по материалам профессионального чата стоматологов за неделю в объеме 11 000 – {WEEKLY_CHAR_BUDGET} символов.
    === ПРАВИЛА ВНИМАНИЯ ===
    1. Проанализируй ВЕСЬ предоставленный лог недели. Не фокусируйся только на последних сообщениях. 
    2. Если в начале или середине лога была важная дискуссия или разбор, она ОБЯЗАТЕЛЬНО должна попасть в отчет.
    3. Твоя цель — равномерный охват всех ключевых тем за отчетный период.
    4. Если один автор пишет несколько сообщений подряд — воспринимай это как единый клинический монолог.
    === ГЛАВНОЕ ПРАВИЛО: ОБЪЕМ И ДЕТАЛИ ===
    1. **ЗАПРЕЩЕНО СОКРАЩАТЬ.** Твоя цель — фундаментальная клиническая летопись. Не сжимай темы в пару строк, расписывай подробно.
    2. **БОЛЬШЕ ИМЕН И СПЕЦИАЛИЗАЦИЙ.** Люди любят, когда их вклад замечают. Если врач дал дельный совет — укажи его имя жирным (**Имя**). Опирайся на накопленные профили участников.
    3. **КЛИНИЧЕСКИЕ КЕЙСЫ — СЕРДЦЕ СТАТЬИ.** Описывай их максимально подробно: зуб, диагноз, жалобы, снимки, инструменты, файлы, протоколы ирригации (концентрации, экспозиция, активация), адгезивные протоколы, силеры.
        Пиши живым профессиональным языком доказательной медицины, уважительно и по делу.
    === ПРАВИЛА (КАК ПИСАТЬ) ===
    1. НИКАКОЙ ВОДЫ: Запрещены пустые фразы типа "развернулась жаркая дискуссия", "наше сообщество единодушно". Пиши сразу суть: "Главная тема недели — ..." или "Врач X показал сложный случай...".
    2. БОЛЬШЕ МЯСА И ПАРАМЕТРОВ: Нужны цифры, бренды, протоколы. Ищи конкретные названия брендов, настройки эндомоторов (торк, скорость), время экспозиции, цифры зарплат и цен (до рубля), проценты. Общие фразы ("обсудили цены") ЗАПРЕЩЕНЫ.
    3. ОФОРМЛЕНИЕ: Заголовки ЖИРНЫМ КАПСОМ.
    4. ПРАВИЛО ГЛУБИНЫ (DEEP DIVE): Не пиши просто "обсуждали IDS". Напиши: "Обсуждали технику IDS: последовательность нанесения адгезива, время экспозиции и какой именно жидкотекучий композит лучше использовать для запечатывания пор".
    5. ПРАВИЛО АРГУМЕНТАЦИИ: Если кто-то говорит «это плохо», обязательно найди в чате и допиши ПОЧЕМУ это плохо (анатомические, микробиологические или биомеханические причины).
    6. ОПИСАНИЕ ТЕХНОЛОГИЙ: Если упоминается методика (например, «вертипреп» или «латеральная компакция»), кратко опиши её суть для тех, кто не в теме, чтобы статья была самодостаточной.
    7. АКАДЕМИЧЕСКАЯ ТОЧНОСТЬ И БАЗА: Используй терминологию доказательной медицины (EBM). Прежде чем переходить к лайфхакам из чата, кратко опиши суть (деградация коллагена, гибридный слой, анатомия системы корневых каналов).
    8. АНТИ-ФАНТАЗИЯ: Если в чате обсуждается сомнительный или опасный метод, противоречащий стандартам, обязательно укажи на это с пометкой «⚠️ Предупреждение редакции». Не выдумывай факты, которых нет в логах, но дополняй общепринятыми протоколами (Золотым стандартом).
    9. ПРАВИЛО ДИНАМИЧЕСКИХ ЗАГОЛОВКОВ (ЖЕСТКО): ЗАПРЕЩЕНО использовать стандартные названия разделов типа "Терапия", "Ортопедия" или "Энциклопедия". Каждый заголовок раздела должен быть КРЕАТИВНЫМ и отражать суть обсуждений этой конкретной недели.
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
    === ПРАВИЛО РАБОТЫ С КЛИНИЧЕСКИМИ СНИМКАМИ (КРИТИЧНО) ===
    Если к разбираемому случаю в логе прикреплен снимок (помечено как "ФОТО/ВИДЕО" у MSG_XXXXX):
    Ты ОБЯЗАН вставить маркер [IMG_XXXXX] отдельной строкой прямо в разбор кейса (после Названия кейса или Сути), чтобы снимок появился в статье.
    Пример:
    **Кейс №1: [Название проблемы] от врача [Имя]**
    [IMG_12345]
    *   **Суть:** ...
    
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
    (ЗАПРЕЩЕНО писать просто список имен. Перечисли врачей, внесших наибольший клинический вклад на этой неделе. Опирайся на накопленные профили участников (при наличии): обязательно указывай подтвержденную специализацию доктора и его реальную клиническую заслугу. Пример: "**Доктор Иванов** (ортопед) — за подробный разбор препарирования под виниры, **Доктор Петров** (эндодонтист) — за протокол распломбировки каналов").
    
    ## 😂 МЕДИЦИНСКИЙ ЮМОР
    (Шутки, мемы, забавные диалоги).

    === ТРЕБОВАНИЯ К ОФОРМЛЕНИЮ ===
    1. Пиши ЖИВЫМ, ПРОФЕССИОНАЛЬНЫМ языком.
    2. БЕЗ ВСТУПЛЕНИЯ И ЗАКЛЮЧЕНИЯ: Сразу начинай со структуры.
    3. Из HTML-тегов используй только <b> и <i>. Тег <br> ЗАПРЕЩЕН: переносы делай настоящими переводами строк, иначе весь текст склеится в одну строку.
    4. Целевой объём статьи: 11 000 – {WEEKLY_CHAR_BUDGET} символов. Распределяй объём гармонично по всем разделам, чтобы финальные рубрики (Доска почета, Юмор) оставались полными и завершенными.
{profiles_block}
    ЛОГ НЕДЕЛИ:
    {full_text}
    Инструкция для самопроверки: Напиши масштабную, глубокую и развернутую статью в объеме 11 000 – {WEEKLY_CHAR_BUDGET} символов. Это полноценная клиническая летопись, а не краткое саммари. Излагай клиническую логику, протоколы, цифры и бренды во всех подробностях, концентрируй пользу без воды.
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
        full_html = clean_markdown_to_html(raw_text)
        
        # Вставка изображений (семантический узел Telegraph figure/figcaption + детерминированный fallback)
        full_html = embed_media_into_summary_html(full_html, media_map, media_captions)
        
        # Клинический CTA-подвал для Telegraph с описанием возможностей бота в ЛС
        cta_telegraph = build_clinical_assistant_cta("telegraph")
        count_footer = f"\n\n<i>Сообщений за неделю — {msg_count}</i>" if msg_count > 0 and "Сообщений за неделю" not in full_html else ""
        telegraph_footer = f"\n\n{cta_telegraph}{count_footer}"

        # Благодаря прямому UTF-8 транспорту одна страница Telegraph безопасно вмещает до 28 000+ символов.
        # Не обрезаем статью преждевременно: _create_telegraph_page_resilient опубликует её целиком
        # либо автоматически разделит на связанные части (Часть 1, Часть 2) при превышении лимита.
        telegraph_html = full_html + telegraph_footer

        # Публикация в Telegraph с каскадным даунсайзингом
        date_str = get_russian_date(datetime.now())
        title = f"WEEKLY: Большая Стоматологическая Газета ({date_str})"
        
        logger.info(f"📤 Отправка в Telegraph (Title: {title})...")
        _write_summary_stage(
            "telegraph_create",
            kind="weekly",
            chat_id=chat_id,
            topic_id=topic_id,
            message_count=len(messages),
            html_chars=len(telegraph_html),
        )
        page_url, telegraph_error = await _create_telegraph_page_resilient(
            title,
            telegraph_html,
            full_html,
            telegraph_footer,
            timeout=TELEGRAPH_TIMEOUT_SECONDS,
        )
        if telegraph_error:
            logger.error("Telegraph weekly subprocess failed: %s", telegraph_error)
        logger.info(f"summary weekly telegraph done chat={chat_id} ok={bool(page_url)}")

        logger.info(f"Готовим тизер для Weekly (Telegraph: {page_url or 'недоступен'})...")
        
        weekly_teasers = [
            f"🗞 <b>ВЫШЕЛ НОВЫЙ НОМЕР WEEKLY ({date_str})</b>\n\n"
            f"Коллеги, это не просто саммари. Это летопись нашей недели.\n"
            f"Внутри огромная статья с разбором всех полетов.\n\n"
            f"💉 <b>Клиническая панорама:</b> Подробный разбор кейсов (эндо, реставрации, хирургия).\n"
            f"🗣 <b>Личности:</b> Кого цитировали, с кем спорили, кому ставили лайки.\n"
            f"⚙️ <b>Материаловедение:</b> Честные отзывы о брендах без рекламы.\n\n"
            f"Чтиво на 15 минут. Заваривайте кофе.",

            f"🔥 <b>ИТОГИ НЕДЕЛИ: Большой разбор ({date_str})</b>\n\n"
            f"Собрали в одну статью всё, чем жил чат последние 7 дней.\n\n"
            f"👨‍⚕️ <b>Доска почета:</b> Ищите свои фамилии в тексте!\n"
            f"🦷 <b>Кейс-марафон:</b> Фотопротоколы и тактика лечения.\n"
            f"⚔️ <b>Баттлы:</b> Аргументы сторон в вечных спорах.\n\n"
            f"Энциклопедия коллективного опыта готова."
        ]
        
        sel_teaser = random.choice(weekly_teasers)
        if page_url:
            sel_cta = f"👉 <b><a href='{page_url}'>ЧИТАТЬ ПОЛНЫЙ ВЫПУСК</a></b>"
        else:
            sel_cta = "📄 <b>Полный иллюстрированный выпуск доступен в прикрепленном PDF-файле ниже 👇</b>"

        cta_telegram = build_clinical_assistant_cta("telegram")
        msg_to_send = (
            f"{sel_teaser}\n\n"
            f"{sel_cta}\n\n"
            f"{cta_telegram}"
        )
        
        send_params = {
            'parse_mode': 'HTML',
            'link_preview': bool(page_url)
        }
        if topic_id:
            send_params['reply_to'] = topic_id
        
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

            # Фоновая генерация и отправка журнальной PDF-версии вестника
            try:
                from digest_pdf import generate_digest_pdf
                pdf_title = f"Клинический Вестник StomChat ({date_str})"
                # КРИТИЧНО: В PDF передаем ПОЛНУЮ версию статьи (full_html),
                # а не усеченную под лимиты сервиса Telegraph!
                pdf_path = await generate_digest_pdf(
                    html_content=full_html,
                    title=pdf_title,
                    subtitle="Большая стоматологическая газета • Недельный обзор",
                    msg_count=msg_count,
                    date_str=date_str,
                )
                if pdf_path and os.path.exists(pdf_path) and hasattr(client, "send_file"):
                    pdf_caption = (
                        f"📄 <b>{pdf_title}</b>\n\n"
                        f"Полная журнальная PDF-версия вестника со всеми клиническими снимками, протоколами и таблицами."
                    )
                    pdf_params = {'caption': pdf_caption, 'parse_mode': 'HTML'}
                    if topic_id:
                        pdf_params['reply_to'] = topic_id
                    await client.send_file(chat_id, pdf_path, **pdf_params)
                    logger.info("PDF weekly digest document delivered to chat=%s", chat_id)
            except Exception as pdf_err:
                logger.warning("PDF digest delivery failed (non-critical): %s", pdf_err)

        logger.info(f"✅ MASSIVE Weekly Digest отправлен в {chat_id}")
        runtime_guard.clear_summary_status("weekly_summary_done")
        return msg_to_send

    except Exception:
        logger.exception("weekly summary failed")
        runtime_guard.clear_summary_status("weekly_summary_failed")
        return None
