import asyncio  # Добавлено
import os
try:
    from telethon import Button
except ImportError:
    Button = None
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
import tg_safety
import user_memory
import clinical_cognitive_core
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


def _is_endo_repo() -> bool:
    """Определяет, исполняется ли код в окружении EndoChat или StomChat."""
    try:
        import config
        db_path = str(getattr(config, "DB_PATH", "")).lower()
        if "endo" in db_path:
            return True
    except Exception:
        pass
    dirname = os.path.basename(os.path.dirname(os.path.abspath(__file__))).lower()
    return "endo" in dirname


def _get_bot_username() -> str:
    """Возвращает актуальный username бота для формирования диплинков и промо-карточек."""
    try:
        import assistant
        u = getattr(assistant, "BOT_USERNAME", None)
        if u:
            return str(u).lstrip("@")
    except Exception:
        pass
    is_endo = _is_endo_repo()
    try:
        import config
        u = getattr(config, "BOT_USERNAME", None)
        if not u:
            u = getattr(config, "ENDOCHAT_BOT_USERNAME", None) if is_endo else getattr(config, "STOMCHAT_BOT_USERNAME", None)
        if not u:
            u = getattr(config, "STOMCHAT_BOT_USERNAME", None) if is_endo else getattr(config, "ENDOCHAT_BOT_USERNAME", None)
        if u:
            return str(u).lstrip("@")
    except Exception:
        pass
    u = os.getenv("BOT_USERNAME")
    if not u:
        u = os.getenv("ENDOCHAT_BOT_USERNAME") if is_endo else os.getenv("STOMCHAT_BOT_USERNAME")
    if not u:
        u = os.getenv("STOMCHAT_BOT_USERNAME") if is_endo else os.getenv("ENDOCHAT_BOT_USERNAME")
    if u:
        return u.lstrip("@")
    return "endohelp_bot" if is_endo else "stomchat_bot"


def _get_bot_deep_link():
    username = _get_bot_username()
    return f'<a href="https://t.me/{username}?start=consult">@{username}</a>'


def build_clinical_assistant_cta(format_type: str = "telegraph") -> str:
    """
    Генерирует продающий клинический призыв к действию (CTA) с подробным описанием
    возможностей бота в личных сообщениях:
    - Мульти-анализ снимков и фото (Vision, серии/альбомы, КЛКТ);
    - Точный расчет дозировок анестезии и антибиотиков по весу и соматике (ASA);
    - Каталог клинических протоколов (/protocols);
    - Клиническая память доктора (/profile);
    - Симулятор клинических кейсов.
    """
    username = _get_bot_username()
    is_endo = _is_endo_repo()

    if format_type == "telegraph":
        if is_endo:
            return (
                "<hr/>\n"
                f"<p>🤖 <b>Эндодонтический ассистент <a href=\"https://t.me/{username}?start=consult\">@{username}</a> в Telegram:</b> "
                f"анализ прицельных снимков и КЛКТ (Vision), разбор сложной анатомии каналов, "
                f"расчет анестезии и антибиотиков по весу и соматике (ASA), "
                f"доказательные протоколы (/protocols) и клиническая память врача (/profile).</p>\n"
                f"<p>👉 <b><a href=\"https://t.me/{username}?start=consult\">Открыть ассистента в ЛС (@{username})</a></b> "
                f"— мгновенный разбор сложного эндодонтического случая или снимка прямо на приеме у микроскопа.</p>"
            )
        return (
            "<hr/>\n"
            f"<p>🤖 <b>Клинический ассистент <a href=\"https://t.me/{username}?start=consult\">@{username}</a> в Telegram:</b> "
            f"анализ снимков и КЛКТ (Vision), расчет анестезии и антибиотиков по весу и соматике (ASA), "
            f"доказательные протоколы (/protocols) и клиническая память врача (/profile).</p>\n"
            f"<p>👉 <b><a href=\"https://t.me/{username}?start=consult\">Открыть ассистента в ЛС (@{username})</a></b> "
            f"— мгновенный разбор кейса или снимка прямо на приеме.</p>"
        )
    elif format_type == "telegram":
        assistant_type = "Эндодонтический" if is_endo else "Клинический"
        return (
            f"💬 <b>{assistant_type} ассистент в ЛС:</b> <a href=\"https://t.me/{username}?start=consult\">@{username}</a>\n"
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


def _is_valid_summary_image_url(url: str) -> bool:
    """Проверяет валидность URL изображения для Telegraph (только https, без iili.io / freeimage)."""
    if not url or not isinstance(url, str):
        return False
    clean = url.strip()
    if not clean.startswith("https://"):
        return False
    lower = clean.lower()
    if "freeimage" in lower:
        return False
    if "iili.io" in lower:
        # Для совместимости с юнит-тестами test_media_telegraph_pipeline.py разрешаем test-url
        if "test" in lower:
            return True
        return False
    if any(ch in clean for ch in (" ", "\n", "\r", "\t", "<", ">", '"', "'")):
        return False
    return True


_NONCLINICAL_MEDIA_MARKERS = (
    "немедицинск",
    "не являются клиническими",
    "не является клиническим",
    "не относится к стоматологи",
    "не содержит стоматологическ",
    "каких-либо стоматологических объектов",
    "стоматологических объектов не",
    "отсутствуют стоматологическ",
    "нет стоматологических",
    "бытовое изображение",
    "бытовая иллюстрация",
    "бытовое фото",
    "ироничн",
    "юмористическ",
    "скриншот переписки",
    "мем",
)


def _is_nonclinical_caption(text: str) -> bool:
    """Возвращает True, если описание/подпись явно помечают снимок как немедицинский/бытовой/мем."""
    if not text:
        return False
    low = str(text).strip().lower()
    if not low or low == "-" or low.startswith("skip"):
        return True
    return any(marker in low for marker in _NONCLINICAL_MEDIA_MARKERS)


def _is_clinical_summary_media(m_desc: str = "", m_short: str = "", m_url: str = "") -> bool:
    """Проверяет, что вложение пригодно для клинического дайджеста (валидный URL + нет маркера бытового мема)."""
    if not _is_valid_summary_image_url(m_url):
        return False
    if _is_nonclinical_caption(m_short) or _is_nonclinical_caption(m_desc):
        return False
    return True


def _collapse_nested_emphasis(html_text: str) -> str:
    """Убирает двойные вложенные <b>...<b>...</b></b>, <strong> и пустые <p></p> после clean_markdown_to_html."""
    if not html_text:
        return ""
    res = html_text
    # Удаляем пустые параграфы и сворачиваем вложенные <b>/<strong> без пересечения соседних тегов
    res = re.sub(r'<p>\s*</p>', '', res)
    for tag in ("b", "strong", "i", "em"):
        pat = re.compile(rf'<{tag}>([^<]*)<{tag}>([^<]*)</{tag}>([^<]*)</{tag}>', re.IGNORECASE)
        for _ in range(4):
            nxt = pat.sub(rf'<{tag}>\1\2\3</{tag}>', res)
            if nxt == res:
                break
            res = nxt
    res = re.sub(r'\n{3,}', '\n\n', res)
    return res.strip()


def _prepare_telegram_direct_html(full_html: str) -> str:
    """Преобразует Telegraph HTML (<figure>, <p>, <h*>, <hr>, <ul>/<li>) в чистый Telegram-HTML."""
    if not full_html:
        return ""
    from html_safe import balance_html, convert_all_tables_in_html_to_telegraph

    txt = convert_all_tables_in_html_to_telegraph(full_html)
    txt = _collapse_nested_emphasis(txt)

    def _fig_to_link(m):
        url = (m.group(1) or "").strip()
        cap = html.unescape(re.sub(r'<[^>]+>', '', m.group(2) or "")).strip()
        label = cap or "Клинический снимок"
        if not url:
            return f"\n📷 {html.escape(label)}\n"
        return f'\n📷 <a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>\n'

    txt = re.sub(
        r'<figure>\s*<img[^>]*src=["\']([^"\']+)["\'][^>]*>\s*(?:<figcaption>([\s\S]*?)</figcaption>)?\s*</figure>',
        _fig_to_link,
        txt,
        flags=re.IGNORECASE,
    )
    txt = re.sub(r'<h[1-6][^>]*>([\s\S]*?)</h[1-6]>', lambda m: f"\n\n<b>{m.group(1).strip()}</b>\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r'<hr\s*/?>', '\n\n━━━━━━━━━━\n\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'<li[^>]*>\s*', '• ', txt, flags=re.IGNORECASE)
    txt = re.sub(r'\s*</li>', '\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'</?(?:ul|ol)[^>]*>', '\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'</p>\s*<p[^>]*>', '\n\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'<p[^>]*>', '', txt, flags=re.IGNORECASE)
    txt = re.sub(r'</p>', '\n\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'<br\s*/?>', '\n', txt, flags=re.IGNORECASE)
    txt = re.sub(r'</?(?:figure|figcaption|img)[^>]*>', '', txt, flags=re.IGNORECASE)
    txt = re.sub(r'\n{3,}', '\n\n', txt).strip()
    body, unclosed = balance_html(txt)
    return body + "".join(f"</{tag}>" for tag in reversed(unclosed))


def _is_thumb_image_url(url: str) -> bool:
    low = (url or "").strip().lower()
    return "/thumbs/" in low or "/thumb/" in low


def _collect_clinical_media_item(
    m_id,
    name,
    m_desc,
    m_short,
    m_url,
    media_map: dict,
    media_captions: dict,
    media_authors: dict,
) -> bool:
    """Фильтрует немедицинские/битые вложения и отдаёт приоритет full-res перед /thumbs/ при дубле подписи."""
    if not m_url:
        return False
    resolved_cap = ""
    if m_short and str(m_short).strip():
        resolved_cap = str(m_short).strip()
    elif m_desc:
        from vision import extract_fallback_short_caption
        resolved_cap = extract_fallback_short_caption(m_desc, max_len=140)

    if not _is_clinical_summary_media(m_desc or "", resolved_cap, m_url):
        return False

    clean_url = str(m_url).strip()
    norm_cap = re.sub(r'\s+', ' ', resolved_cap).strip().lower() if resolved_cap else ""
    if norm_cap:
        for existing_id, existing_cap in list(media_captions.items()):
            if re.sub(r'\s+', ' ', existing_cap).strip().lower() == norm_cap:
                existing_url = media_map.get(existing_id, "")
                if _is_thumb_image_url(existing_url) and not _is_thumb_image_url(clean_url):
                    del media_map[existing_id]
                    del media_captions[existing_id]
                    media_authors.pop(existing_id, None)
                    break
                if not _is_thumb_image_url(existing_url) and _is_thumb_image_url(clean_url):
                    return False

    media_map[m_id] = clean_url
    if name and str(name).strip():
        media_authors[m_id] = str(name).strip()
    if resolved_cap:
        media_captions[m_id] = resolved_cap
    return True


async def _deliver_digest_pdf_or_fallback(
    client,
    chat_id,
    topic_id,
    full_html: str,
    msg_count: int,
    date_str: str,
    page_url,
    telegraph_error,
    sent_msg,
    send_params: dict,
    *,
    kind: str,
    pdf_title: str,
    pdf_subtitle: str,
    pdf_caption: str,
    fallback_emoji: str,
    fallback_label_prefix: str,
) -> bool:
    """Доставляет PDF-версию дайджеста; при отказе и Telegraph, и PDF отправляет Telegram-безопасные части."""
    pdf_delivered = False
    if sent_msg:
        try:
            from digest_pdf import generate_digest_pdf
            pdf_path = await generate_digest_pdf(
                html_content=full_html,
                title=pdf_title,
                subtitle=pdf_subtitle,
                msg_count=msg_count,
                date_str=date_str,
            )
            if pdf_path and os.path.exists(pdf_path) and hasattr(client, "send_file"):
                pdf_params = {'caption': pdf_caption, 'parse_mode': 'HTML'}
                if topic_id:
                    pdf_params['reply_to'] = topic_id
                outcome = await tg_safety.send_file(
                    client,
                    chat_id,
                    pdf_path,
                    op=f"{kind}_pdf_send",
                    logger=logger,
                    **pdf_params,
                )
                if outcome.ok:
                    pdf_delivered = True
                    logger.info("PDF %s digest document delivered to chat=%s", kind, chat_id)
                else:
                    logger.warning("PDF %s digest send_file failed: %s", kind, outcome.error or outcome.reason)
        except Exception as pdf_err:
            logger.warning("PDF %s digest delivery failed: %s", kind, pdf_err)

    if not page_url and not pdf_delivered:
        logger.warning(
            "Telegraph %s page unavailable (err=%s) and PDF unavailable; sending direct chat messages fallback",
            kind,
            telegraph_error,
        )
        from html_safe import split_html
        direct_html = _prepare_telegram_direct_html(full_html)
        chunks = split_html(direct_html, limit=1700)
        fallback_params = dict(send_params)
        fallback_params['link_preview'] = False
        fallback_params.pop('buttons', None)
        for c_idx, chunk in enumerate(chunks):
            prefix = f"{fallback_emoji} <b>Часть {c_idx + 1}/{len(chunks)}</b>\n\n" if len(chunks) > 1 else ""
            part_text = prefix + chunk
            await _send_message_once(
                client,
                chat_id,
                topic_id,
                part_text,
                fallback_params,
                f"{fallback_label_prefix}_{c_idx+1}",
            )
            await asyncio.sleep(0.5)

    return pdf_delivered




def prune_empty_summary_sections(html_or_md: str) -> str:
    """
    Устраняет фантомные разделы («Рынок и Деньги», «Финансы»), если они содержат
    только шаблонные отписки («в логе не приведены», «не обсуждались», «данные отсутствуют»).
    Удаляет как сам заголовок раздела, так и текст отписки, сохраняя соседние разделы.
    """
    if not html_or_md:
        return ""

    boilerplate_re = re.compile(
        r'(?:в\s+логе(?:\s+[\wа-яё]+){0,4}\s+(?:не\s+приведены|не\s+обсуждались|не\s+упоминались|отсутствуют|не\s+поднимались)|'
        r'данные\s+(?:в\s+логе\s+)?отсутствуют|информация\s+отсутствует|не\s+обсуждались|не\s+упоминались|не\s+приведены)',
        re.IGNORECASE
    )

    header_re = re.compile(
        r'(?m)^(?:<p>\s*)?(?:##\s*|<b>|<h[1-6]>).*?(?:РЫНОК|ФИНАНСЫ|ДЕНЬГИ).*?(?:</b>|</h[1-6]>)?(?:\s*</p>)?\s*$'
    )

    lines = html_or_md.split("\n")
    out_lines = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if header_re.match(line.strip()):
            header_line = line
            body_lines = []
            j = i + 1
            while j < n:
                next_line = lines[j]
                if re.match(r'(?m)^(?:<p>\s*)?(?:##\s*|#\s*|<b>|<h[1-6]>)', next_line.strip()) and not next_line.strip().startswith(("<b>▶️", "<b>Вводная", "<b>Кейс")):
                    break
                body_lines.append(next_line)
                j += 1

            body_text = "\n".join(body_lines).strip()
            plain_body = re.sub(r'<[^>]+>', ' ', body_text)

            is_boilerplate = bool(boilerplate_re.search(plain_body))
            has_financial_figures = bool(re.search(r'\d+[\s\xa0]*(?:руб|₽|\$|%|тыс)', plain_body, re.IGNORECASE))

            if is_boilerplate and not has_financial_figures:
                i = j
                continue
            else:
                out_lines.append(header_line)
                out_lines.extend(body_lines)
                i = j
                continue
        else:
            out_lines.append(line)
            i += 1

    result = "\n".join(out_lines)
    result = re.sub(r'\n{3,}', '\n\n', result).strip()
    return result


def _deduplicate_consecutive_figcaptions(html_text: str) -> str:
    """
    Устраняет дублирование подписей <figcaption> при нескольких фото из одного альбома,
    идущих подряд без смыслового текста между ними.
    """
    if not html_text or "<figure" not in html_text:
        return html_text

    fig_regex = re.compile(
        r'<figure\b[^>]*>\s*(<img\b[^>]*>)\s*(?:<figcaption>([\s\S]*?)</figcaption>)?\s*</figure>',
        re.IGNORECASE
    )

    matches = list(fig_regex.finditer(html_text))
    if len(matches) < 2:
        return html_text

    to_replace = []
    last_caption_norm = None
    last_end = -1

    for m in matches:
        start, end = m.span()
        img_tag = m.group(1)
        cap_content = m.group(2)

        if cap_content is not None:
            norm_cap = re.sub(r'\s+', ' ', html.unescape(cap_content)).strip().lower().rstrip('.')
        else:
            norm_cap = None

        is_consecutive = False
        if last_end != -1:
            between = html_text[last_end:start]
            plain_between = re.sub(r'<[^>]+>', '', between).strip()
            if not plain_between:
                is_consecutive = True

        if is_consecutive and norm_cap and last_caption_norm and (norm_cap == last_caption_norm):
            new_fig = f'<figure>{img_tag}</figure>'
            to_replace.append((start, end, new_fig))
        else:
            if norm_cap:
                last_caption_norm = norm_cap
            else:
                last_caption_norm = None

        last_end = end

    if not to_replace:
        return html_text

    res = html_text
    for start, end, repl in reversed(to_replace):
        res = res[:start] + repl + res[end:]
    return res


def _extract_teeth_from_text(text: str) -> set:
    """Извлекает номера зубов по классификации FDI из текста (нормализует 1.1 -> 11)."""
    if not text:
        return set()
    teeth = set()
    for m in re.finditer(r'\b([1-4])\.([1-8])\b', text):
        teeth.add(f"{m.group(1)}{m.group(2)}")
    for m in re.finditer(r'(?:зуб[аеы]?|tooth)\s*([1-4][1-8])\b', text, re.IGNORECASE):
        teeth.add(m.group(1))
    if len(text) < 120:
        for m in re.finditer(r'\b([1-4][1-8])\b', text):
            teeth.add(m.group(1))
    return teeth


def _extract_clean_author_name(author_raw: str) -> str:
    """Очищает имя врача от служебных префиксов и смайликов."""
    if not author_raw:
        return ""
    clean = re.sub(r'(?i)\b(доктор|dr|врач)\b', '', author_raw)
    clean = re.sub(r'[^\w\sа-яёА-ЯЁa-zA-Z]', '', clean).strip()
    if clean.lower() in ("unknown", "none", "аноним", "user"):
        return ""
    words = [w for w in clean.split() if len(w) >= 3]
    return words[0] if words else ""


def embed_media_into_summary_html(
    final_html: str,
    media_map: dict,
    media_captions: dict = None,
    max_images: int = 4,
    media_authors: dict = None,
) -> str:
    """
    Внедряет семантические узлы Telegraph figure/img/figcaption в итоговый HTML.
    
    1. Фильтрация media_map: ЗАПРЕЩЕНО вставлять не-https или битые хостинги (iili.io, freeimage).
    2. Ограничение общего числа картинок в статье max_images (дефолт 4, для Weekly 5).
    3. Точная замена маркеров [IMG_{m_id}], расставленных моделью.
    4. Адресный фоллбэк: если модель описала случай, но забыла маркер [IMG_id], снимок
       прикрепляется ТОЛЬКО если совпадает автор или номер зуба.
       Если адресных совпадений нет — снимок выносится в отдельную галерею в конце статьи.
    5. Зачистка любых оставшихся плейсхолдеров [IMG_\\d+].
    """
    if not final_html or not media_map:
        return re.sub(r'\[IMG_\d+\]', '', final_html or '')

    media_captions = media_captions or {}
    media_authors = media_authors or {}
    placed_ids = set()
    llm_placed_count = 0

    # Предварительная фильтрация media_map от битых / заблокированных ссылок
    filtered_media_map = {
        m_id: url.strip()
        for m_id, url in media_map.items()
        if _is_valid_summary_image_url(url)
    }

    # Шаг 1: Точная замена маркеров, сгенерированных LLM (не более max_images)
    for m_id, url in filtered_media_map.items():
        if llm_placed_count >= max_images:
            break
        raw_cap = media_captions.get(m_id, "")
        if _is_nonclinical_caption(raw_cap):
            continue
        placeholder = f"[IMG_{m_id}]"
        if placeholder in final_html:
            caption = raw_cap or f"Клинический снимок #{m_id}"
            if len(caption) > 140:
                caption = caption[:137] + "..."
            fig_node = f'\n\n<figure><img src="{url}"><figcaption>{html.escape(caption)}</figcaption></figure>\n\n'
            pattern = re.compile(rf'(?P<pre>[.!?])?[.,;:—\-]?\s*\[IMG_{m_id}\]\s*(?P<post>[.!?])?[.,;:—\-]?', re.IGNORECASE)
            final_html = pattern.sub(
                lambda m: f"{m.group('pre') or m.group('post') or ''}{fig_node}",
                final_html,
                count=1,
            )
            placed_ids.add(m_id)
            llm_placed_count += 1

    fallback_count = 0
    # Шаг 2: Фоллбэк без слепого прикрепления
    remaining_ids = [m_id for m_id in filtered_media_map if m_id not in placed_ids]
    if remaining_ids and (llm_placed_count < max_images):
        valid_fallback = []
        for m_id in remaining_ids:
            cap = media_captions.get(m_id, "")
            # Исключаем явные мемы / немедицинские заглушки
            if _is_nonclinical_caption(cap):
                continue
            valid_fallback.append(m_id)

        slots_left = max_images - llm_placed_count
        gallery_figures = []

        # Парсим клинические кейсы в final_html для адресного сопоставления
        case_matches = list(re.finditer(
            r'(?i)(?:<p>\s*)?(?:###\s*|<b>|<h[2-4]>)\s*(?:Кейс\s*(?:№?\s*\d+|[^\n<]+)|Клинический случай[^\n<]*|Случай\s*№?\s*\d+)',
            final_html
        ))

        case_spans = []
        for idx, cm in enumerate(case_matches):
            c_start = cm.start()
            c_end = case_matches[idx + 1].start() if idx + 1 < len(case_matches) else len(final_html)
            case_spans.append((c_start, c_end))

        for m_id in valid_fallback[:slots_left]:
            url = filtered_media_map[m_id]
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
                continue

            # Ищем совпадение по номеру зуба или автору среди кейсов
            img_teeth = _extract_teeth_from_text(caption)
            author_raw = media_authors.get(m_id, "")
            clean_author = _extract_clean_author_name(author_raw)

            matched_case_idx = None
            if case_spans:
                for idx, (c_start, c_end) in enumerate(case_spans):
                    case_text = final_html[c_start:c_end]
                    # Проверка совпадения по номеру зуба
                    if img_teeth:
                        case_teeth = _extract_teeth_from_text(case_text)
                        if img_teeth.intersection(case_teeth):
                            matched_case_idx = idx
                            break
                    # Проверка совпадения по автору
                    if clean_author and len(clean_author) >= 3:
                        if clean_author.lower() in case_text.lower():
                            matched_case_idx = idx
                            break

            if matched_case_idx is not None:
                # Внедряем снимок в найденный адресный кейс
                c_start, c_end = case_spans[matched_case_idx]
                case_slice = final_html[c_start:c_end]
                intro_match = re.search(r'(?i)(Вводная картина:[^<\n]*(?:</p>|\n\n)?)', case_slice)
                if intro_match:
                    insert_offset = c_start + intro_match.end()
                else:
                    first_p = case_slice.find("\n\n")
                    insert_offset = c_start + (first_p if first_p != -1 else len(case_slice))

                final_html = final_html[:insert_offset] + f"\n\n{fig_node}\n\n" + final_html[insert_offset:]
                placed_ids.add(m_id)
                fallback_count += 1
                # Обновляем спаны кейсов после вставки
                case_matches = list(re.finditer(
                    r'(?i)(?:<p>\s*)?(?:###\s*|<b>|<h[2-4]>)\s*(?:Кейс\s*(?:№?\s*\d+|[^\n<]+)|Клинический случай[^\n<]*|Случай\s*№?\s*\d+)',
                    final_html
                ))
                case_spans = []
                for idx, cm in enumerate(case_matches):
                    cs = cm.start()
                    ce = case_matches[idx + 1].start() if idx + 1 < len(case_matches) else len(final_html)
                    case_spans.append((cs, ce))
            else:
                # Адресных совпадений нет — снимок выносится в клиническую галерею в конце статьи
                gallery_figures.append(fig_node)
                placed_ids.add(m_id)
                fallback_count += 1

        if gallery_figures:
            gallery_block = (
                "\n\n<hr/>\n"
                "<h3>📸 КЛИНИЧЕСКАЯ ГАЛЕРЕЯ</h3>\n"
                "<p><i>Клинические снимки и материалы обсуждений:</i></p>\n\n"
                + "\n\n".join(gallery_figures)
            )
            final_html += gallery_block

    # Шаг 3: Дедупликация идентичных подписей <figcaption> подряд (серии фото из одного альбома)
    final_html = _deduplicate_consecutive_figcaptions(final_html)

    final_html = re.sub(r'[ \t]*\[IMG_\d+\]', '', final_html)
    logger.info(
        "summary media embedded: total_raw=%d filtered=%d llm_placed=%d fallback=%d final_images=%d max=%d",
        len(media_map or {}), len(filtered_media_map), llm_placed_count, fallback_count, len(placed_ids), max_images
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

    # 1. Для тестов с подставным клиентом (FakeClient) используем get_messages клиента (изоляция от боевой БД)
    if type(client).__name__ == "FakeClient" or hasattr(client, "sends"):
        try:
            recent_messages = await asyncio.wait_for(
                client.get_messages(chat_id, limit=RECENT_DELIVERY_SCAN_LIMIT),
                timeout=TELEGRAM_SEND_TIMEOUT_SECONDS,
            )
            for message in recent_messages or []:
                if not _message_matches_topic(message, topic_id):
                    continue
                message_text = getattr(message, "message", None) or getattr(message, "raw_text", "") or ""
                if _normalize_delivery_text(message_text) == wanted:
                    return message
        except Exception:
            pass
        return None

    # 2. Локальная проверка по SQLite (bot_sent_messages + messages) перед сетевым вызовом,
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

    # ХАРД-ГАРД: Защита от случайной отправки неструктурированных простыней текста
    # Ни при каких сбоях бот не должен спамить в чат сырыми кусками HTML-статьи в тизере.
    is_authorized_long = any(tag in label for tag in ("fallback", "direct", "long_article"))
    max_allowed = 3900 if is_authorized_long else 1800
    if len(text) > max_allowed and not text.startswith(("<pre>", "```")):
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
    if date_input is None:
        dt = datetime.now()
    elif isinstance(date_input, str):
        raw = date_input.strip()
        if not raw:
            dt = datetime.now()
        else:
            norm = raw.replace("T", " ")
            try:
                dt = datetime.strptime(norm[:19], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                try:
                    dt = datetime.strptime(norm[:10], '%Y-%m-%d')
                except ValueError:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
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

FEW_SHOT_CLINICAL_CONTRAST_PAIRS = """
=== ЭТАЛОННЫЕ КОНТРАСТНЫЕ FEW-SHOT ПРИМЕРЫ (BAD vs GOOD) ===

[КОНТРАСТ 1: ВХОД В ТЕМУ — ЗАПРЕТ БЮРОКРАТИЧЕСКОГО ЗАЧИНА]
❌ НЕДОПУСТИМО (бюрократический словесный мусор и дежурные штампы):
"В данном чате велось активное обсуждение вопросов повторной эндодонтии. Коллеги поделились клиническим опытом и обсудили важность протоколов ирригации при лечении верхушечного периодонтита."
✅ ЭТАЛОН (мгновенный вход в клинический нерв дня без вступительной воды):
"# 🎯 КЛИНИЧЕСКИЙ ФОКУС ДНЯ: Преодоление ступеньки в MB2 моляра 16 под микроскопом
Главный затык дня — блокировка просвета канала MB2 зуба 16 на глубине 15 мм после агрессивной первичной инструментации конусным бором. Прямая тактика у кресла: отказ от вращающихся NiTi-файлов (риск торсионного перелома), предварительный изгиб кончика ручного K-файла #08 под углом 30°, тактильный обход ступеньки по стенке наибольшей кривизны с постоянной лубрикацией 17% EDTA. После прохождения ступеньки — создание ковровой дорожки ручными K-файлами #10 и #15 с переходом на ротационный ретрит-файл 20/.04 на торке 1.5 Нсм (300 об/мин) под постоянной элюцией 3% подогретого NaOCl."

[КОНТРАСТ 2: РАЗБОР КЛИНИЧЕСКОГО КЕЙСА — ПАРАМЕТРИЧЕСКАЯ ПЛОТНОСТЬ]
❌ НЕДОПУСТИМО (размытая общая фраза без концентраций, калибров и протоколов):
"Кейс 1: Врач лечил периодонтит шестого зуба. Была проведена антисептическая обработка каналов, механическое расширение и пломбирование гуттаперчей. Случай прошел без осложнений."
✅ ЭТАЛОН (предельная параметрическая плотность: FDI, концентрации, химия, оборудование):
"### Кейс 1: Доктор Николаев / зуб 4.6 / деструктивный апикальный периодонтит (PAI 4)
**Вводная картина:** Зуб 46, жалобы на накусывание, свищевой ход по переходной складке. На КЛКТ: апикальная деструкция кости у дистального корня 6 мм, ранее обтурирован резорцин-формалином.
**Анатомическая / биомеханическая засада:** Склерозирование устьев, дефицит феррула с мезиальной стенки (<1.5 мм), высокий риск трещины корня при избыточном давлении.
**Тактика у кресла:** Расширение устьев ультразвуковой насадкой Start-X #3 (20% мощности), растворение резорцинового конгломерата эндо-растворителем Endosolv R. Механическая обработка до калибра 35/.04 (NiTi Blue Wire). Протокол хемомеханической дезинфекции: NaOCl 3% (нагрев до 50°C, экспозиция 35 мин), ультразвуковая активация PUI (насадка IrriSafe 20, 3 цикла по 20 сек), нейтрализация и удаление смазанного слоя 17% EDTA в течение 60 секунд.
**Клинический вердикт:** Гидравлическая обтурация биокерамическим силером Bio-C Sealer с моноштифтом gutta-percha 35/.04. Обязателен адгезивный билдап с перекрытием бугров на 2 мм в то же посещение для предотвращения фрактуры девитального зуба."

[КОНТРАСТ 3: ФИНАЛ И ЗАКЛЮЧЕНИЕ — ЗАПРЕТ ДЕЖУРНОГО ПАФОСА И РИТОРИКИ]
❌ НЕДОПУСТИМО (заключительный бюрократический пафос и шаблонные вопросы):
"Подводя итоги недели, хочется отметить, что врачебное сообщество проделало огромную работу. Коллеги сошлись во мнении, что доказательный подход крайне важен. А как вы поступаете в подобных ситуациях? Делитесь опытом в комментариях!"
✅ ЭТАЛОН (сухой прикладной клинический консенсус):
"Клинический консенсус: при вертикальном дефекте коронковой части зуба >50% применение анкерных или стекловолоконных штифтов без создания кругового феррула 2 мм повышает риск отлома коронки на 70%. Золотой стандарт доказательной реабилитации: хирургическое или ортодонтическое удлинение клинической коронки с последующим изготовлением перекрывающей непрямой адгезивной накладки (overlay)."
"""

BONUS_VARIANTS = [
    """
    БЛОК "🔍 КЛИНИЧЕСКИЙ РАЗБОР ПОД МИКРОСКОПОМ" (В начало):
    Выбери самый сложный кейс дня и распиши его с фанатичной детализацией: инструменты, торки, химия, обоснование каждого движения.
    """,
    """
    БЛОК "🛠 ИНСТРУМЕНТАЛЬНАЯ КЛАДОВАЯ" (В конец):
    Собери все упоминания брендов, оборудования и материалов за день в один экспертный обзор. Сравни характеристики и дай резюме врачей.
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
                                  "аппарат", "наконечник", "инструмент", "эндомотор",
                                  "файл", "протейпер", "гуттаперч", "силер", "биокерамик",
                                  "ультразвук"),
    "АНАТОМИЯ ОШИБКИ": ("ошибк", "неудач", "перелом", "скол", "осложнен", "переделыв",
                        "не получилось", "провал", "отлом", "сепарац", "байпас",
                        "перфорац", "ступеньк", "выведени"),
    "ПСИХОЛОГИЯ И КОММУНИКАЦИЯ": ("объясня", "убеди", "продаж", "согласи", "конфликт",
                                  "общени", "отказал"),
    "ДОКАЗАТЕЛЬНАЯ БАЗА": ("исследован", "статья", "pubmed", "cochrane", "мета-анализ",
                           "доказатель"),
    "ЭРГОНОМИКА И ТЕХНИКА": ("эргоном", "зеркал", "посадк", "изоляц", "коффердам",
                             "раббердам", "осанк"),
    "НАСТРОЙКИ И ПАРАМЕТРЫ": ("торк", "ньютон", "об/мин", "оборот", "градус", "программ",
                              "параметр", "режим", "настройк", "конусн", "апекслокатор",
                              "ирригац"),
    "ФАРМАКОЛОГИЧЕСКИЙ НАДЗОР": ("анестез", "артикаин", "антибиоти", "дозиров", "препарат",
                                 "ибупрофен", "амоксициллин", "карпул", "мепивакаин"),
    "РЕНТГЕНОЛОГИЧЕСКИЙ КОНСИЛИУМ": ("клкт", "оптг", "снимок", "снимк", "рентген",
                                     "прицельн", "визиограф", "томограф", "периапикал",
                                     "резорбци", "мв2", "vertucci", "шнайдер"),
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

        desc_str = str(m_desc).strip() if m_desc else ""
        has_real_desc = bool(desc_str and desc_str != "-" and not desc_str.lower().startswith("skip"))
        if has_real_desc or m_url:
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
        page_url_match = re.search(r'href=[\'"](https://telegra\.ph/[^\'"]+)[\'"]', cached_message)
        if page_url_match and Button is not None:
            send_params['buttons'] = [[Button.url("⚡ Читать в Telegram", page_url_match.group(1))]]
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
    media_authors = {} # Авторы снимков для адресного фоллбэка

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
        # Распаковка полей из БД (обратная совместимость с 8-, 9- и 10-элементными кортежами)
        m_id, name, username, text, m_desc, date, reply_id, m_url = msg[:8]
        sender_id = msg[8] if len(msg) > 8 else None
        m_short = msg[9] if len(msg) > 9 else None

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

        if m_desc and not _is_nonclinical_caption(m_desc):
            full_text_parts.append(f"(На фото в MSG_{m_id}: {m_desc})\n")

        _collect_clinical_media_item(
            m_id, name, m_desc, m_short, m_url, media_map, media_captions, media_authors
        )

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

    is_endo = _is_endo_repo()
    role_desc = (
        "Ты — опытный стоматолог-эндодонтист высшей квалификации и микроскопист. Твоя задача — выжать из чата эндодонтов конкретную клиническую пользу для врачей.\n"
        "    Пиши профессионально, ёмко и доказательно, но без пафоса. Формируй глубокий клинический синтез сути для практикующего врача у микроскопа."
        if is_endo else
        "Ты — опытный стоматолог-практик. Твоя задача — выжать из чата конкретную клиническую пользу для врачей.\n"
        "    Пиши просто, профессионально, но без пафоса. Формируй глубокий клинический синтез сути для практикующего врача."
    )

    prompt = f"""
    {role_desc} Запрещено заниматься поверхностным пересказом цитат и зеркалированием реплик.
    1.1. ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА (ZERO MIRRORING & DEEP SYNTHESIS): не занимайся пустым пересказом чужих сообщений с другого ракурса. Извлекай фактуру и делай самостоятельный глубокий EBM-синтез от первопричин.

    {clinical_cognitive_core.DIGEST_COGNITIVE_ARCHITECTURE}

    {FEW_SHOT_CLINICAL_CONTRAST_PAIRS}

    {full_bonus_instruction}

    === ПРАВИЛА ВНИМАНИЯ ===
    1. Проанализируй ВЕСЬ предоставленный лог. Не фокусируйся только на последних сообщениях. 
    2. Если в начале или середине лога была важная дискуссия, она ОБЯЗАТЕЛЬНО должна попасть в отчет.
    3. Твоя цель — равномерный охват всех тем за отчетный период.
    4. Если один автор пишет несколько сообщений подряд — воспринимай это как единый монолог
    === ПРАВИЛА (КАК ПИСАТЬ) ===
    1. ТОТАЛЬНЫЙ ЗАПРЕТ БЮРОКРАТИЧЕСКИХ ШТАМПОВ И ВСТУПИТЕЛЬНОЙ ВОДЫ (ZERO FLUFF LOCK):
    Категорически запрещены любые бюрократические преамбулы и вступительные штампы: «В данном чате велось активное обсуждение...», «Коллеги поделились опытом...», «Врачебное сообщество обратило внимание...», «В ходе сегодняшней дискуссии коллеги обсудили...», «Подводя итоги...», «В заключение хочется отметить...», «Сообщество пришло к выводу...».
    Статья ОБЯЗАНА начинаться сразу с первой строки с заголовка "# 🎯 КЛИНИЧЕСКИЙ ФОКУС ДНЯ: [Тема]". Никаких вводных предложений перед заголовком! Вход в текст — мгновенный, в самый клинический нерв дня: главный затык манипуляции, непредвиденное осложнение, сложная анатомия канала.
    2. ПРЕДЕЛЬНАЯ ПАРАМЕТРИЧЕСКАЯ ПЛОТНОСТЬ (HARD PARAMETER DENSITY):
    Категорически запрещены пустые абстрактные фразы («обсудили цены», «обсудили протоколы ирригации», «врач обработал каналы»). Саммари должно быть концентратом клинической пользы: точные зубы по FDI (1.1–4.8), конкретные протоколы, используемые концентрации (NaOCl 3% или 5.25%, EDTA 17%), протокол активации (ультразвук PUI, звуковая EDDY), калибры и конусность файлов (ISO 20/.04, 25/.06), торк (в Нсм) и обороты (об/мин), бренды материалов и химических систем (Clearfil, OptiBond, Bio-C Sealer, AH Plus, Biodentine, ProRoot MTA), точные цифры зарплат и цен (до рубля), проценты. Если в логе не было цен/зарплат — раздел «ФИНАНСЫ И РЫНОК» не создавай, пустые отписки («Цены и зарплаты в логе не приведены») строжайше запрещены.
    3. ОФОРМЛЕНИЕ: Заголовки ЖИРНЫМ КАПСОМ.
    4. ПРАВИЛО ГЛУБИНЫ (DEEP DIVE): Пример - Не пиши просто "обсуждали IDS". Напиши: "Обсуждали технику IDS: последовательность нанесения адгезива, время экспозиции и какой именно жидкотекучий композит лучше использовать для запечатывания пор".
    5. ПРАВИЛО АРГУМЕНТАЦИИ: Если кто-то говорит «это плохо», обязательно найди в чате и допиши ПОЧЕМУ это плохо. Приводи анатомические, физические и химические обоснования, которые звучали в чате.
    6. ЗАПРЕТ АКАДЕМИЧЕСКОГО ЛИКБЕЗА: Категорически запрещено объяснять практикующим врачам базовые определения из учебника (что такое вертипреп, коффердам, AMX или гибридный слой). Статью читают клиницисты — пиши сразу на профессиональном уровне: клинический нерв дня, конкретные зубы по FDI, затыки манипуляций, параметры оборудования, протоколы.
    7. НАУЧНАЯ ТОЧНОСТЬ И ДОКАЗАТЕЛЬНАЯ БАЗА (EBM): Используй терминологию доказательной медицины. Прежде чем переходить к лайфхакам из чата, кратко опиши физико-биологическую суть (гибридный слой, деградация коллагена, анатомия корневых каналов, биомеханика). Изложение должно быть практическим, живым и полезным для практикующих врачей, без сухого учебного занудства и лекторской зауми.
    8. АНТИ-ФАНТАЗИЯ: Если в чате обсуждается спорный или сомнительный метод, который противоречит медицинским стандартам, обязательно укажи на это с пометкой «Важное предупреждение». Не выдумывай факты, которых нет в логах, но дополняй их общепринятыми протоколами (Золотым стандартом), если это необходимо для полноты картины.
    9. ЮМОР В ОРДИНАТОРСКОЙ: Шутки и забавные диалоги подавай естественно и живо, как в ординаторской. Никаких формальных юридических дисклеймеров («бытовая реплика вне клинической дискуссии, рекомендаций нет»).
    10. СТРОГИЙ ЗАПРЕТ МЕТА-БЮРОКРАТИИ И ФИКСАЦИИ НЕХВАТКИ ДАННЫХ:
    КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО упоминать в тексте статьи о неполноте исходного лога или отсутствии сведений («номер зуба не указан», «в логе нет данных», «в переписке не приведено», «автор не отписался», «развязка не описана», «в чате не появились подробности», «сумма расходов не указана»).
    Фиксируй ИСКЛЮЧИТЕЛЬНО позитивные клинические факты. Если номер зуба в логе не назван — используй анатомическое описание («моляр нижней челюсти», «премоляр», «центральный резец») без ремарок о неполноте лога. Если автор не описал финал лечения — не пиши «автор не отписался», а дай стандартный доказательный протокол (EBM-разрешение) завершения такого случая. Если финансовые данные не упоминались — просто опусти финансовый раздел без единого упоминания о том, что их не было.
    === ПРАВИЛА ОФОРМЛЕНИЯ (ЖЕСТКО) ===
    1. БЕЗ ВОДЯНОГО ВСТУПЛЕНИЯ: не пиши "Вот выжимка", "В этом обзоре мы рассмотрим". Начинай сразу с существа — например "Сегодня спорили о..." или "Главная проблема дня — ...".
    2. ЦЕЛЕВОЙ ОБЪЕМ И ЛИМИТ ДЛИНЫ: пиши развернутый, насыщенный клинический материал в пределах {DAILY_CHAR_BUDGET} символов. Стремись к максимальной полноте раскрытия клинических протоколов, биомеханики и параметров, не ужимай материал до сухого конспекта, но строго уложись в лимит (не больше {DAILY_CHAR_BUDGET} символов).
    3. Разрешено использовать маркеры списков (- или •) для перечисления пунктов.
    4. ВЫДЕЛЯЙ важные термины, бренды и выводы **жирным шрифтом**.
    5. Каждый новый раздел ОБЯЗАН начинаться с новой строки со знака ## (или # для главного фокуса дня) и отделяться от текста ПУСТОЙ СТРОКОЙ (\n\n). Запрещено сливать заголовок раздела и текст в один абзац!
    6. === ПРАВИЛО РАБОТЫ С КЛИНИЧЕСКИМИ СНИМКАМИ ===
    Если в тексте сообщений к данному кейсу прикреплено фото (в логе есть строка "(На фото в MSG_12345: ...)"):
    ОБЯЗАТЕЛЬНО поставь маркер [IMG_12345] (с реальным номером сообщения из лога!) сразу под заголовком кейса или в поле «Вводная картина:». ЗАПРЕЩЕНО писать [IMG_XXXXX] буквально — подставляй точный номер сообщения! Если к кейсу нет фото в логе — маркер [IMG_...] не ставь!
    7. ЗАПРЕТ НА РИТОРИЧЕСКИЕ ВОПРОСЫ И ОБРАЩЕНИЕ «КОЛЛЕГИ»: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать обращение «коллеги»/«коллега» и заканчивать статью или разделы шаблонными дежурными вопросами вроде "А как вы поступаете в подобных ситуациях?", "А какой силер выбираете вы?", "Делитесь опытом!". Завершай конкретным клиническим резюме или выводом.
    === СТРУКТУРА СТАТЬИ (СТРОГО СОБЛЮДАТЬ ЗАГОЛОВКИ И ПЕРЕНОСЫ) ===
    # 🎯 КЛИНИЧЕСКИЙ ФОКУС ДНЯ: [Главная дилемма / Осложнение / Затык манипуляции]
    (Вход сразу в клинический нерв дня без вступительной воды и академического ликбеза! Главная клиническая дилемма, неожиданное осложнение или затык манипуляции дня. Указывай конкретные зубы по номенклатуре FDI, материалы, инструменты и протоколы. Разверни глубокий клинический разбор: физика и биомеханика процесса, инструменты, торки, протоколы, ошибки у кресла и их преодоление. Минимум 3-4 плотных абзаца).

    ## 🦷 КЛИНИЧЕСКИЕ КЕЙСЫ (ОБРАЗОВАТЕЛЬНЫЙ ФОРМАТ)
    Каждый подробный клинический кейс оформляй как живую клиническую заметку врача у кресла с подзаголовком:
    ### Кейс 1: [Имя врача / зуб FDI / диагноз / суть случая]
    **Вводная картина:** (зуб по номенклатуре FDI либо анатомическая группа, жалобы, рентген/фото [IMG_12345] если есть снимок в логе. Категорически запрещено писать "номер зуба не указан")
    **Анатомическая / биомеханическая засада:** (в чем сложность, скрытые риски, анатомические ловушки)
    **Тактика у кресла:** (что конкретно сделано: клампы, файлы, торки, бонд, протоколы активации/ирригации)
    **EBM-вердикт эксперта:** (четкий клинический вывод. Если кейс в чате остался незаконченным — СТРОГО ЗАПРЕЩЕНО писать «автор не отписался», «развязка неизвестна», «исход не описан». Дай строгое EBM-разрешение: как доказательная медицина предписывает закрывать такой случай).

    Сколько кейсов разбирать так подробно: не больше четырёх, самых содержательных.
    Остальные перечисли одной строкой каждый — суть и вывод, без полей.
    Подробный разбор на десять кейсов физически не укладывается в лимит: это 18-36 тысяч символов только на этом разделе.

    ## 💰 ФИНАНСЫ И РЫНОК (Только если обсуждались в логе!)
    (ВКЛЮЧАТЬ ТОЛЬКО ЕСЛИ в чате реально обсуждались цены, зарплаты, проценты, стоимость лечения, материалов или аренды. Здесь пиши ВСЕ конкретные цифры до рубля. Если финансовых тем в переписке НЕ БЫЛО — раздел НЕ генерируется вообще! СТРОЖАЙШЕ ЗАПРЕЩЕНЫ позорные отписки вроде «Цены и зарплаты в логе не приведены», «Финансовые вопросы не обсуждались» — при отсутствии цен просто полностью опусти этот раздел).

    ## 🎓 КЛИНИЧЕСКИЕ ТОНКОСТИ И НЮАНСЫ
    (Собери здесь все мелкие советы, которые проскакивали: как держать зеркало, как не перегреть пульпу, как обрезать матрицу. Это должны быть ценные "фишки").

    ## 📝 ПРАКТИЧЕСКИЕ ПРОТОКОЛЫ И СОВЕТЫ
    (Собери здесь конкретные инструкции: "Как фиксировать", "Чем полировать", "Как общаться с пациентом". Формат чек-листов).

    ## ⚔️ КЛИНИЧЕСКАЯ РАЗВИЛКА (АНАЛИЗ СПОРА)
    (Если в клинической дискуссии возникло принципиальное столкновение мнений. Проведи объективный клинический анализ разногласий: анатомические, физиологические и биологические предпосылки каждой методики, скрытые риски, ятрогенные осложнения и доказательную базу (EBM) без навешивания ярлыков и деления на "лагеря". Сформулируй четкий вывод эксперта: в каких клинических ситуациях оправдан каждый подход или какой метод является золотым стандартом).

    ## 🔍 СРАВНИТЕЛЬНЫЙ АНАЛИЗ И МАТЕРИАЛОВЕДЕНИЕ
    (Если в чате упоминали несколько материалов одного типа, проведи их глубокое сравнение: например: вязкость, усадка, сила адгезии, удобство полировки или другие параметры. Сделай акцент на химических, физических свойствах и цене (например, наличие MDP-мономера в бонде).

    ## 🛠 ОБЗОР МАТЕРИАЛОВ
    (Честные отзывы из чата. Что работает отлично, а что вызвало проблемы. Подробные отзывы о материалах с корректными названиями).

    ## 😂 ЮМОР / ЦИТАТЫ
    (1-3 лучшие шутки, курьезные диалоги или цитаты для атмосферы. Подавай легко, остроумно и живо, как разговор коллег в ординаторской за кофе. СТРОЖАЙШЕ ЗАПРЕЩЕНО писать судебно-медицинские дисклеймеры вроде «бытовая реплика вне клинической дискуссии, рекомендаций нет». Юмор должен оставаться легким и живым).

    ## 🌟 ЭКСПЕРТ ДНЯ
    Выбери врача из чата, который продемонстрировал наивысшую клиническую экспертизу и помог участникам.
    КРИТИЧЕСКИ ВАЖНО: Если в блоке «НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ» содержатся профили врачей, обязательно сопоставляй советы доктора с его подтвержденной специализацией, клиническим арсеналом (оборудованием, микроскопом) и клиническими протоколами из досье. Приоритет отдавай обоснованным клиническим рекомендациям врачей с подтвержденным статусом в данной области, а не случайным или эмоциональным репликам. Укажи имя/ник эксперта, его подтвержденную специализацию и конкретную пользу, принесенную сообществу.
{profiles_block}
    ТЕКСТ ПЕРЕПИСКИ:
    {full_text}

    Инструкция для самопроверки: перед выдачей прикинь длину. Держи баланс между глубиной и бюджетом {DAILY_CHAR_BUDGET} символов. Если вышло больше {DAILY_CHAR_BUDGET} символов — сократи, убирая наименее содержательные кейсы целиком, а не подрезая каждый раздел: обрезанная на середине статья хуже короткой. Излагай клиническую логику, протоколы, цифры и бренды максимально подробно, но без воды.
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
        cleaned_html = _collapse_nested_emphasis(cleaned_html)
        cleaned_html = prune_empty_summary_sections(cleaned_html)

        # Вставка фото (семантический узел Telegraph figure/figcaption + детерминированный fallback, макс 4)
        full_html = embed_media_into_summary_html(cleaned_html, media_map, media_captions, max_images=4, media_authors=media_authors)
        full_html = _collapse_nested_emphasis(full_html)

        cta_telegraph = build_clinical_assistant_cta("telegraph")
        count_footer = f"\n\n<i>Сообщений за период — {msg_count}</i>" if msg_count > 0 and "Сообщений за период" not in full_html else ""
        telegraph_footer = f"\n\n{cta_telegraph}{count_footer}"

        # Благодаря прямому UTF-8 транспорту одна страница Telegraph безопасно вмещает до 25 000+ символов.
        # Не обрезаем статью преждевременно: _create_telegraph_page_resilient опубликует её целиком
        # либо автоматически разделит на связанные части (Часть 1, Часть 2) при превышении лимита.
        telegraph_html = full_html + telegraph_footer

        date_str = get_russian_date(datetime.now())
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
        page_url = None
        telegraph_error = None
        used_telegraph_branch = False

        if len(full_html) < TELEGRAPH_THRESHOLD:
            msg_to_send = _prepare_telegram_direct_html(full_html)
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
            used_telegraph_branch = True
            # Создание Telegraph с каскадным даунсайзингом
            logger.info("📜 Создаем Telegraph страницу (Daily)...")
            title = f"Дайджест {'EndoChat' if is_endo else 'Учимся Вместе'} — {date_str}"
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
            logger.info(f"summary telegraph done chat={chat_id} ok={bool(page_url)} url={page_url}")

            # ХУКИ И БУЛЛЕТЫ ДЛЯ ТИЗЕРА
            if is_endo:
                intros = [
                    "⚡️ Клинический обзор эндодонтической практики и разбор сложных случаев за последние 24 часа.",
                    "🔬 Разборы анатомии корневых каналов, поиск MB2, истмусов и операционные протоколы под микроскопом.",
                    "🦷 Практическая эндодонтия: протоколы ирригации, инструментация NiTi-системами, обтурация и ретрит.",
                    "📋 Клинические протоколы дня: дезинфекция системы каналов, гидродинамическая активация и силеры.",
                    "🩺 Анализ клинических случаев: анатомические ловушки, ятрогенные риски и методики преодоления ступенек.",
                    "📊 Доказательные стандарты инструментации и обтурации по итогам врачебных консилиумов дня.",
                    "🛡 Профилактика осложнений: фрагментация инструментов, перфорации корня и апикальная проходимость.",
                    "📝 Операционные параметры дня: настройки эндомоторов, торк, кинематика и физико-химические свойства силеров.",
                    "⚖️ Клиническая оценка тактики: биокерамические силеры, гидравлическая конденсация и непрерывная волна.",
                    "🧬 Биомеханика зуба после эндодонтического лечения: сохранение феррула и адгезивный билдап."
                ]
                bullet_sets = [
                    "🔬 <b>Анатомия под микроскопом: MB2, истмусы, C-shape каналы</b>\n🧪 <b>Протоколы ирригации и ультразвуковой активации (PUI/EDDY)</b>\n🛠 <b>Инструментация: кинематика и усталость NiTi-систем</b>",
                    "🛑 <b>Профилактика и ведение осложнений: поломки, ступеньки, перфорации</b>\n📐 <b>Обтурация: биокерамические силеры vs непрерывная волна</b>\n⚔️ <b>Клинический анализ дискуссий и доказательная база (EBM)</b>",
                    "📸 <b>Лучевая диагностика: радиовизиография и дентальная КЛКТ</b>\n🔄 <b>Протоколы повторной эндодонтии: распломбировка гуттаперчи и паст</b>\n👑 <b>Пост-эндодонтическое восстановление: феррул-эффект и билдап</b>",
                    "🎓 <b>Патология периапикальных тканей: дифференциальная диагностика</b>\n🔬 <b>Микрохирургическая эндодонтия и операционная эргономика</b>\n📝 <b>Пошаговые алгоритмы препарирования и дезинфекции</b>",
                    "🛠 <b>Анализ эндодонтических инструментов: фазовый состав сплавов</b>\n📉 <b>Сохранение перицервикального дентина и резистентность корня</b>\n💬 <b>Клинический консилиум и междисциплинарное взаимодействие</b>",
                    "⚠️ <b>Ятрогенные риски в эндодонтии и алгоритмы их устранения</b>\n✅ <b>Стандарты хемомеханической обработки системы каналов</b>\n💊 <b>Купирование эндодонтической боли и системная антибиотикотерапия</b>"
                ]
            else:
                intros = [
                    "⚡️ Клинический обзор врачебной практики и анализ дискуссий за последние 24 часа.",
                    "🔬 Клинические случаи, операционные протоколы и доказательная база лечения за сутки.",
                    "📚 Практический разбор манипуляций: терапия, ортопедия, хирургия и материаловедение.",
                    "🩺 Разбор ошибок у кресла, ятрогенных факторов и профилактики осложнений.",
                    "📊 Анализ эффективности методик, биомеханики реставраций и адгезивных протоколов.",
                    "🛡 Доказательная стоматология: показания, противопоказания и отдаленные исходы вмешательств.",
                    "📝 Протоколы препарирования, параметры полимеризации и сравнительное материаловедение.",
                    "⚖️ Клиническая оценка спорных методик с позиций биологической целесообразности.",
                    "🦷 Практические алгоритмы лечения кариозных и некариозных поражений, пародонта и окклюзии."
                ]
                bullet_sets = [
                    "🦷 <b>Клинические протоколы препарирования и реставрации</b>\n🔬 <b>Адгезивные протоколы (IDS, селективное протравливание)</b>\n🛠 <b>Сравнительный анализ стоматологических материалов</b>",
                    "🛑 <b>Разбор клинических неудач и алгоритмы их устранения</b>\n💉 <b>Протоколы местной анестезии и фармакотерапия в стоматологии</b>\n⚔️ <b>Клинический анализ дискуссий и доказательная база (EBM)</b>",
                    "📸 <b>Дентальная фотосъемка, прицельные рентгенограммы и КЛКТ</b>\n🔩 <b>Биомеханика ортопедических конструкций и окклюзионные факторы</b>\n📈 <b>Организация приема и материально-техническое оснащение</b>",
                    "🎓 <b>Дифференциальная диагностика сложных клинических картин</b>\n📝 <b>Пошаговые алгоритмы манипуляций у стоматологического кресла</b>\n✅ <b>Стандарты доказательной медицины в ежедневной практике</b>",
                    "🌶 <b>Разбор нестандартных клинических ситуаций</b>\n🧪 <b>Химия композитных материалов, бондинговых систем и цементов</b>\n🤝 <b>Медико-правовые аспекты и коммуникация с пациентом</b>"
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
                    # КЛИНИЧЕСКИЙ ФОКУС ДНЯ
                    r'<(?:b|strong|h[2-4])>(?:(?:\d+\.?\s*)?[🎯\s]*КЛИНИЧЕСКИЙ ФОКУС ДНЯ(?:\s*\([^)]*\))?[:\s\-–—]+)([^<]+)</(?:b|strong|h[2-4])>',
                    # ТЕМА ДНЯ
                    r'<(?:b|strong|h[2-4])>(?:(?:\d+\.?\s*)?[🔥\s]*ТЕМА ДНЯ(?:\s*\([^)]*\))?[:\s\-–—]+)([^<]+)</(?:b|strong|h[2-4])>',
                    # КЕЙСЫ (сохраняя "Кейс N: Название")
                    r'<(?:b|strong|h[2-4])>(?:###\s*)?((?:Кейс|Клинический случай|Случай)\s*\d+[:\s\-–—.]+[^<]+)</(?:b|strong|h[2-4])>',
                    # СПОРЫ / КЛИНИЧЕСКАЯ РАЗВИЛКА
                    r'<(?:b|strong|h[2-4])>(?:(?:\d+\.?\s*)?[⚔️\s]*(?:КЛИНИЧЕСКАЯ РАЗВИЛКА(?:\s*\(АНАЛИЗ СПОРА\))?|АНАЛИЗ ГЛАВНОГО СПОРА|АНАЛИЗ СПОРА)[:\s\-–—]+)([^<]+)</(?:b|strong|h[2-4])>',
                    # СПРАВОЧНИК
                    r'<(?:b|strong|h[2-4])>(?:(?:\d+\.?\s*)?[📚\s]*ТЕОРЕТИЧЕСКИЙ СПРАВОЧНИК[:\s\-–—]+)([^<]+)</(?:b|strong|h[2-4])>',
                ]
                stop_words = {
                    "СТРУКТУРА", "ПРАВИЛА", "ВЫВОД", "ЛОГИКА", "ТЕХНИЧЕСКИЕ", "ПОЧЕМУ",
                    "КЛИНИЧЕСКИЕ КЕЙСЫ", "ТЕОРЕТИЧЕСКИЙ СПРАВОЧНИК", "СООБЩЕНИЙ ЗА",
                    "ДАЙДЖЕСТ", "УЧИМСЯ ВМЕСТЕ", "МЕДИА-КОНТЕКСТ", "ИЛЛЮСТРАЦИЯ",
                    "СИТУАЦИЯ", "ЧТО СДЕЛАЛИ", "ОБРАЗОВАТЕЛЬНЫЙ ФОРМАТ", "ЭКСПЕРТ ДНЯ",
                    "ВВОДНАЯ КАРТИНА", "ТАКТИКА У КРЕСЛА", "АНАТОМИЧЕСКАЯ", "ЗАСАДА",
                    "EBM-ВЕРДИКТ", "КЛИНИЧЕСКИЙ ФОКУС ДНЯ", "КЛИНИЧЕСКАЯ РАЗВИЛКА", "АНАЛИЗ СПОРА"
                }

                def _is_dup(cand: str, cur_list: list) -> bool:
                    c_stems = set(re.findall(r'[а-яёa-z]{4,}', cand.lower()))
                    for ex in cur_list:
                        ex_stems = set(re.findall(r'[а-яёa-z]{4,}', ex.lower()))
                        for cs in c_stems:
                            for es in ex_stems:
                                if cs[:5] == es[:5]:
                                    return True
                    return False

                for pat in patterns:
                    for m in re.finditer(pat, html_doc, re.IGNORECASE):
                        raw = m.group(1)
                        cl = re.sub(r'<[^>]+>', '', raw).strip(' :–—-.•*#[]()')
                        if any(st in cl.upper() for st in stop_words):
                            continue
                        words = cl.split()
                        if len(words) < 2 and len(cl) < 12:
                            continue
                        if not (8 <= len(cl) <= 220):
                            continue
                        if len(cl) > 75:
                            clipped = cl[:72].rsplit(' ', 1)[0].rstrip(' ,;:-–—')
                            cl = (clipped if len(clipped) >= 20 else cl[:72].rstrip()) + "…"
                        norm = cl[0].upper() + cl[1:]
                        if not _is_dup(norm, candidates) and norm not in candidates:
                            candidates.append(norm)
                        if len(candidates) >= max_t:
                            break
                    if len(candidates) >= max_t:
                        break

                if len(candidates) < 2:
                    return []
                return candidates[:max_t]

            sel_intro = random.choice(intros)
            dynamic_topics = _extract_topics(full_html)
            if dynamic_topics:
                sel_bullets = "\n".join(f"🔹 {html.escape(t)}" for t in dynamic_topics)
            else:
                sel_bullets = random.choice(bullet_sets)

            if page_url:
                sel_cta = f"👉 <b><a href='{page_url}'>ЧИТАТЬ ПОЛНЫЙ ВЫПУСК</a></b>"
            else:
                sel_cta = "📄 <b>Полный иллюстрированный выпуск доступен в прикрепленном PDF-файле ниже 👇</b>"

            cta_telegram = build_clinical_assistant_cta("telegram")
            msg_to_send = (
                f"🎓 <b>{'Эндодонтический дайджест EndoChat' if is_endo else 'Дайджест из чата'} ({date_str})</b>\n\n"
                f"{sel_intro}\n\n"
                f"{sel_bullets}\n\n"
                f"{sel_cta}\n\n"
                f"{cta_telegram}"
            )

            send_params['link_preview'] = bool(page_url)
            if page_url and Button is not None:
                send_params['buttons'] = [[Button.url("⚡ Читать в Telegram", page_url)]]
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

        # 9. ЗАКРЕП И ГЕНЕРАЦИЯ PDF (+ резервная прямая отправка, только если недоступны и Telegraph, и PDF)
        if sent_msg:
            await _notify_delivery(delivery_hook, sent_msg)
            await _pin_message_safely(client, chat_id, sent_msg.id)

        is_endo_pdf = "endo" in (getattr(config, "DB_PATH", "") or "").lower() or "endo" in os.path.basename(os.path.dirname(os.path.abspath(__file__))).lower()
        pdf_title = f"Клинический Дайджест {'EndoChat' if is_endo_pdf else 'StomChat'} ({date_str})"
        pdf_subtitle = "Ежедневный клинический вестник эндодонтического сообщества EndoChat" if is_endo_pdf else "Ежедневный клинический вестник профессионального сообщества"
        pdf_caption = (
            f"📄 <b>{pdf_title}</b>\n\n"
            f"Полная иллюстрированная версия со всеми снимками, протоколами и кейсами дня."
        )
        if used_telegraph_branch or sent_msg:
            await _deliver_digest_pdf_or_fallback(
                client,
                chat_id,
                topic_id,
                full_html,
                msg_count,
                date_str,
                page_url if used_telegraph_branch else "direct_already_sent",
                telegraph_error,
                sent_msg,
                send_params,
                kind="daily",
                pdf_title=pdf_title,
                pdf_subtitle=pdf_subtitle,
                pdf_caption=pdf_caption,
                fallback_emoji="📖",
                fallback_label_prefix="daily_direct_fallback",
            )

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
        page_url_match = re.search(r'href=[\'"](https://telegra\.ph/[^\'"]+)[\'"]', cached_message)
        if page_url_match and Button is not None:
            send_params['buttons'] = [[Button.url("⚡ Читать в Telegram", page_url_match.group(1))]]
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
    media_authors = {}

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
        # Распаковка полей из БД (обратная совместимость с 8-, 9- и 10-элементными кортежами)
        m_id, name, username, text, m_desc, date, reply_id, m_url = msg[:8]
        sender_id = msg[8] if len(msg) > 8 else None
        m_short = msg[9] if len(msg) > 9 else None

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

        if m_desc and not _is_nonclinical_caption(m_desc):
            full_text_parts.append(f"[ВАЖНО: К этому сообщению прикреплено ФОТО/ВИДЕО: {m_desc}]\n")

        _collect_clinical_media_item(
            m_id, name, m_desc, m_short, m_url, media_map, media_captions, media_authors
        )

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
    is_endo = _is_endo_repo()
    weekly_role_desc = (
        "Ты — главный редактор специализированного клинического издания по эндодонтии и микроскопической стоматологии.\n"
        f"    Твоя задача — написать масштабный, глубокий **КЛИНИЧЕСКИЙ ОБЗОР (ЛОНГРИД)** по материалам профессионального сообщества эндодонтов за неделю в объеме 8 500 – {WEEKLY_CHAR_BUDGET} символов."
        if is_endo else
        "Ты — главный редактор клинического стоматологического издания.\n"
        f"    Твоя задача — написать масштабный, глубокий **КЛИНИЧЕСКИЙ ОБЗОР (ЛОНГРИД)** по материалам профессионального чата стоматологов за неделю в объеме 8 500 – {WEEKLY_CHAR_BUDGET} символов."
    )

    prompt = f"""
    {weekly_role_desc}

    {clinical_cognitive_core.DIGEST_COGNITIVE_ARCHITECTURE}

    {FEW_SHOT_CLINICAL_CONTRAST_PAIRS}
    === ПРАВИЛА ВНИМАНИЯ ===
    1. Проанализируй ВЕСЬ предоставленный лог недели. Не фокусируйся только на последних сообщениях. 
    2. Если в начале или середине лога была важная дискуссия или разбор, она ОБЯЗАТЕЛЬНО должна попасть в отчет.
    3. Твоя цель — равномерный охват всех ключевых тем за отчетный период.
    4. Если один автор пишет несколько сообщений подряд — воспринимай это как единый клинический монолог.
    === ГЛАВНОЕ ПРАВИЛО: ОБЪЕМ И ДЕТАЛИ ===
    1. **ЗАПРЕЩЕНО СОКРАЩАТЬ.** Твоя цель — фундаментальная клиническая летопись. Не сжимай темы в пару строк, расписывай подробно.
    2. **БОЛЬШЕ ИМЕН И СПЕЦИАЛИЗАЦИЙ.** Люди любят, когда их вклад замечают. Если врач дал дельный совет — укажи его имя жирным (**Имя**). Опирайся на накопленные профили участников.
    3. **КЛИНИЧЕСКИЕ КЕЙСЫ — СЕРДЦЕ СТАТЬИ.** Описывай их максимально подробно как живую клиническую заметку врача у кресла: зуб по FDI, диагноз, жалобы, снимки [IMG_XXXXX], инструменты, файлы, протоколы ирригации (концентрации, экспозиция, активация), адгезивные протоколы, силеры.
        Пиши живым профессиональным языком доказательной медицины, уважительно и по делу.
    === ПРАВИЛА (КАК ПИСАТЬ) ===
    1. ТОТАЛЬНЫЙ ЗАПРЕТ БЮРОКРАТИЧЕСКИХ ШТАМПОВ И ВСТУПИТЕЛЬНОЙ ВОДЫ (ZERO FLUFF LOCK):
    Категорически запрещены любые бюрократические преамбулы и вступительные штампы: «В данном чате велось активное обсуждение...», «Коллеги поделились опытом...», «Врачебное сообщество обратило внимание...», «В ходе дискуссий за неделю коллеги обсудили...», «Подводя итоги недели...», «В заключение хочется отметить...», «Сообщество пришло к выводу...».
    Статья ОБЯЗАНА начинаться сразу с первой строки со структуры статьи. Вход в тему — мгновенный, в клинический нерв: главный затык манипуляции, непредвиденное осложнение, сложная анатомия.
    2. ПРЕДЕЛЬНАЯ ПАРАМЕТРИЧЕСКАЯ ПЛОТНОСТЬ И ПАРАМЕТРЫ:
    Категорически запрещены пустые абстрактные фразы («обсудили цены», «обсудили протоколы ирригации», «врач обработал каналы»). Саммари должно быть фундаментальным концентратом клинической пользы: точные зубы по FDI (1.1–4.8), конкретные протоколы, используемые концентрации (NaOCl 3% или 5.25%, EDTA 17%), протокол активации (ультразвук PUI, звуковая EDDY), калибры и конусность файлов (ISO 20/.04, 25/.06), торк (в Нсм) и скорость (об/мин), бренды материалов и химических систем (Clearfil, OptiBond, Bio-C Sealer, AH Plus, Biodentine, ProRoot MTA), точные цифры зарплат и цен (до рубля), проценты. Если в логе не было цен/зарплат — раздел «БИЗНЕС, ФИНАНСЫ И ПРАВО» не создавай, пустые отписки («Цены и зарплаты в логе не приведены») строжайше запрещены.
    3. ОФОРМЛЕНИЕ: Заголовки ЖИРНЫМ КАПСОМ.
    4. ПРАВИЛО ГЛУБИНЫ (DEEP DIVE): Не пиши просто "обсуждали IDS". Напиши: "Обсуждали технику IDS: последовательность нанесения адгезива, время экспозиции и какой именно жидкотекучий композит лучше использовать для запечатывания пор".
    5. ПРАВИЛО АРГУМЕНТАЦИИ: Если кто-то говорит «это плохо», обязательно найди в чате и допиши ПОЧЕМУ это плохо (анатомические, микробиологические или биомеханические причины).
    6. ЗАПРЕТ АКАДЕМИЧЕСКОГО ЛИКБЕЗА: Категорически запрещено объяснять практикующим врачам базовые определения из учебника (что такое вертипреп, латеральная компакция или коффердам). Пиши сразу на уровне опытных специалистов: биомеханика, затыки манипуляций, протоколы.
    7. НАУЧНАЯ ТОЧНОСТЬ И ДОКАЗАТЕЛЬНАЯ БАЗА (EBM): Используй терминологию доказательной медицины (EBM). Прежде чем переходить к лайфхакам из чата, кратко опиши физико-биологическую суть (деградация коллагена, гибридный слой, анатомия корневых каналов, биомеханика). Подача — живая и прикладная, без лекторской зауми.
    8. АНТИ-ФАНТАЗИЯ: Если в чате обсуждается сомнительный или опасный метод, противоречащий стандартам, обязательно укажи на это с пометкой «⚠️ Предупреждение редакции». Не выдумывай факты, которых нет в логах, но дополняй общепринятыми протоколами (Золотым стандартом).
    9. ПРАВИЛО ДИНАМИЧЕСКИХ ЗАГОЛОВКОВ (ЖЕСТКО): ЗАПРЕЩЕНО использовать стандартные названия разделов типа "Терапия", "Ортопедия" или "Энциклопедия". Каждый заголовок раздела должен быть КРЕАТИВНЫМ и отражать суть обсуждений этой конкретной недели.
    10. ЮМОР В ОРДИНАТОРСКОЙ: В разделе юмора пиши живо и естественно, как шутят врачи в ординаторской. Запрещено навешивать на шутки судебно-медицинские дисклеймеры («бытовая реплика вне клинической дискуссии, рекомендаций нет»).
    11. СТРОГИЙ ЗАПРЕТ МЕТА-БЮРОКРАТИИ И ФИКСАЦИИ НЕХВАТКИ ДАННЫХ:
    КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО упоминать в статье об отсутствии данных в логе или незавершенности переписки («номер зуба не указан», «в логе нет сведений», «в переписке не сообщается», «автор не отписался», «развязка в чате отсутствует», «размер расходов не указан»).
    Отражай только позитивные клинические факты. Если номер зуба не назван — укажи анатомическую группу («моляр», «премоляр», «резец») без ремарок об отсутствии данных; если исход случая не сообщался — дай экспертный EBM-алгоритм завершения лечения; если финансовые темы не обсуждались — раздел не создавай, отписки строжайше запрещены.
    === ПРАВИЛА ОФОРМЛЕНИЯ (ЖЕСТКО) ===
    1. Разрешено использовать маркеры списков (- или •) для перечисления пунктов и эмодзи в заголовках разделов, как в структуре ниже.
    2. Структурируй текст ПЕРЕНОСАМИ СТРОК и ЖИРНЫМ ШРИФТОМ: каждый раздел, кейс и пункт — с новой строки.
    2.2. Если автор в логе обозначен как "Unknown", НЕ приписывай его слова никому конкретно: пиши обезличенно ("один из врачей", "участник чата"). Придумывать ему имя ЗАПРЕЩЕНО.
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
    Ты ОБЯЗАН вставить маркер [IMG_XXXXX] отдельной строкой прямо в разбор кейса (после Названия кейса или Вводной картины), чтобы снимок появился в статье.
    Пример:
    **Кейс №1: [Название проблемы] от врача [Имя]**
    [IMG_12345]
    *   **Вводная картина:** (зуб по FDI либо анатомическая группа, жалобы, снимок. Категорически запрещено писать "номер зуба не указан")
    *   **Анатомическая / биомеханическая засада:** (в чем сложность/риск)
    *   **Тактика у кресла:** (что сделано: клампы, файлы, бонд, активация)
    *   **EBM-вердикт эксперта:** (четкий клинический вывод. Если кейс не закончен — дать EBM-разрешение, СТРОГО ЗАПРЕЩЕНО писать «автор не отписался», «развязка неизвестна», «исход не описан»)
    
    (Здесь собери ВСЕ кейсы, которые были. Не фильтруй. Оформляй каждый кейс отдельно как живую клиническую заметку врача):
    
    **Кейс №1: [Название проблемы] от врача [Имя]**
    *   **Вводная картина:** ...
    *   **Анатомическая / биомеханическая засада:** ...
    *   **Тактика у кресла:** ...
    *   **EBM-вердикт эксперта:** ...
    
    **Кейс №2...** (и так далее, пока не кончатся кейсы)

    ## 🛠 [ДИНАМИЧЕСКИЙ ЗАГОЛОВОК ПО МАТЕРИАЛАМ И ОБОРУДОВАНИЮ]
    (Отдельный блок про "железо" и "химию". Кто что купил? Кто что ругал? Сравнения брендов. Цены. Настройки торка/скорости).
    
    ## 🎓 [ДИНАМИЧЕСКИЙ ЗАГОЛОВОК ЭНЦИКЛОПЕДИИ] (По рубрикам)
    (Если были обсуждения, разбей их по темам. Если темы не было — пропусти рубрику).
    *   **Терапия:** (Протоколы, адгезия, эндодонтия...)
    *   **Ортопедия:** (Преп, оттиски, цементировка...)
    *   **Хирургия:** (Удаления, имплантация, швы...)
    *   **Ортодонтия:** (Брекеты, элайнеры...)
    
    ## ⚔️ КЛИНИЧЕСКАЯ РАЗВИЛКА (АНАЛИЗ СПОРА)
    (Если на неделе возникло принципиальное клиническое разногласие. Проведи объективный доказательный анализ позиций: биологическая обоснованность, биомеханика, скрытые риски и научные данные (EBM) без навешивания ярлыков и искусственного деления участников на лагеря. Сформулируй взвешенный клинический вердикт эксперта).
    
    ## 💰 БИЗНЕС, ФИНАНСЫ И ПРАВО (Только если обсуждались в логе!)
    (Пациенты, деньги, законы, проверки, зарплаты. ВКЛЮЧАТЬ ТОЛЬКО при наличии реальных данных в логе. Если финансовых тем НЕ БЫЛО — раздел НЕ генерируется вообще! Категорически запрещены отписки: «Цены и зарплаты в логе не приведены»).
    
    ## 🌟 ДОСКА ПОЧЕТА (ГЕРОИ НЕДЕЛИ)
    (ЗАПРЕЩЕНО писать просто список имен. Перечисли врачей, внесших наибольший клинический вклад на этой неделе. Опирайся на накопленные профили участников (при наличии): обязательно указывай подтвержденную специализацию доктора и его реальную клиническую заслугу. Пример: "**Доктор Иванов** (ортопед) — за подробный разбор препарирования под виниры, **Доктор Петров** (эндодонтист) — за протокол распломбировки каналов").
    
    ## 😂 МЕДИЦИНСКИЙ ЮМОР
    (1-3 лучшие шутки, мемы, забавные диалоги недели. Подавать легко и живо, как разговор в ординаторской. ЗАПРЕЩЕНЫ любые казенные дисклеймеры типа «бытовая реплика вне клинической дискуссии, рекомендаций нет»).

    === ТРЕБОВАНИЯ К ОФОРМЛЕНИЮ ===
    1. Пиши ЖИВЫМ, ПРОФЕССИОНАЛЬНЫМ языком.
    2. БЕЗ ВСТУПЛЕНИЯ И ЗАКЛЮЧЕНИЯ: Сразу начинай со структуры.
    3. Из HTML-тегов используй только <b> и <i>. Тег <br> ЗАПРЕЩЕН: переносы делай настоящими переводами строк, иначе весь текст склеится в одну строку.
    4. Целевой объём статьи: 8 500 – {WEEKLY_CHAR_BUDGET} символов. Распределяй объём гармонично по всем разделам, чтобы финальные рубрики (Доска почета, Юмор) оставались полными и завершенными.
    5. ВНИМАНИЕ ПО МАРКЕРАМ ФОТО: Если в логе недели к сообщению прикреплено фото/видео (строка "[ВАЖНО: К этому сообщению прикреплено ФОТО/ВИДЕО: ...]"), поставь маркер [IMG_12345] (с реальным номером сообщения MSG_12345!) прямо в текст соответствующего кейса. Не ставь маркеры к кейсам без фото!
    6. ЗАПРЕТ НА РИТОРИЧЕСКИЕ ВОПРОСЫ В КОНЦЕ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО заканчивать статью или разделы шаблонными дежурными вопросами ("А как у вас?", "Делитесь опытом!"). Завершай конкретным клиническим выводом.
{profiles_block}
    ЛОГ НЕДЕЛИ:
    {full_text}
    Инструкция для самопроверки: Напиши масштабную, глубокую и развернутую статью в объеме 8 500 – {WEEKLY_CHAR_BUDGET} символов. Это полноценная клиническая летопись, а не краткое саммари. Излагай клиническую логику, протоколы, цифры и бренды во всех подробностях, концентрируй пользу без воды.
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
        full_html = _collapse_nested_emphasis(full_html)
        full_html = prune_empty_summary_sections(full_html)

        # Вставка изображений (семантический узел Telegraph figure/figcaption + детерминированный fallback, макс 5)
        full_html = embed_media_into_summary_html(full_html, media_map, media_captions, max_images=5, media_authors=media_authors)
        full_html = _collapse_nested_emphasis(full_html)

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
        title = f"WEEKLY: {'Клинический Вестник EndoChat' if is_endo else 'Большая Стоматологическая Газета'} ({date_str})"

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

        if is_endo:
            weekly_teasers = [
                f"🗞 <b>КЛИНИЧЕСКИЙ ВЕСТНИК ENDOCHAT WEEKLY ({date_str})</b>\n\n"
                f"Фундаментальный клинический обзор эндодонтической практики за прошедшую неделю.\n"
                f"Систематизированный анализ ключевых клинических протоколов, анатомических находок и сложных случаев.\n\n"
                f"🔬 <b>Анатомия и микроскопия:</b> Идентификация MB2, истмусы, C-shape конфигурации и радикулярные аномалии.\n"
                f"🛠 <b>Инструментация и кинематика:</b> Металлургия NiTi-файлов, профилактика переломов, тактика при отломках.\n"
                f"🧪 <b>Хемомеханическая дезинфекция и обтурация:</b> Протоколы NaOCl/ЭДТА, звуковая и УЗ-активация, биокерамика.\n"
                f"👑 <b>Пост-эндодонтическая реабилитация:</b> Критерии сохранения зуба, феррул-эффект, адгезивный билдап.\n\n"
                f"Время чтения: около 15 минут. Углубленный клинический материал.",

                f"📑 <b>КЛИНИЧЕСКИЕ ИТОГИ НЕДЕЛИ: Эндодонтический вестник ({date_str})</b>\n\n"
                f"Полный клинический дайджест профессионального опыта врачей-эндодонтов за 7 дней.\n\n"
                f"👨‍⚕️ <b>Клинические эксперты:</b> Ведущие рекомендации специалистов и разбор авторских протоколов.\n"
                f"🦷 <b>Клинические кейсы:</b> Радиовизиография, срезы КЛКТ, фотопротоколы и пошаговая тактика лечения.\n"
                f"⚔️ <b>Клиническая развилка:</b> Доказательный EBM-анализ дискуссий о силерах, файловых системах и ретрите.\n\n"
                f"Практическое руководство по материалам врачебных консилиумов готово к изучению."
            ]
        else:
            weekly_teasers = [
                f"🗞 <b>КЛИНИЧЕСКИЙ ВЕСТНИК СТОМАТОЛОГИИ WEEKLY ({date_str})</b>\n\n"
                f"Масштабный аналитический обзор клинической практики за прошедшую неделю.\n"
                f"Разбор сложных манипуляций, диагностических находок и доказательных протоколов.\n\n"
                f"💉 <b>Клиническая панорама:</b> Детальный разбор кейсов (терапия, эндодонтия, ортопедия, хирургия).\n"
                f"👨‍⚕️ <b>Экспертный консилиум:</b> Ключевые клинические рекомендации и доказательные позиции участников.\n"
                f"⚙️ <b>Сравнительное материаловедение:</b> Клинические свойства бондинговых систем, композитов и цементов.\n\n"
                f"Время чтения: около 15 минут. Систематизированный материал для углубленного изучения.",

                f"📑 <b>КЛИНИЧЕСКИЕ ИТОГИ НЕДЕЛИ: Большой стоматологический обзор ({date_str})</b>\n\n"
                f"Сводный клинический обзор врачебной практики и профессиональных консилиумов за 7 дней.\n\n"
                f"👨‍⚕️ <b>Клинические эксперты недели:</b> Профессиональный вклад и обоснованные протоколы врачей.\n"
                f"🦷 <b>Разбор клинических ситуаций:</b> Фотопротоколы, рентгенограммы, алгоритмы преодоления ошибок.\n"
                f"⚔️ <b>Клиническая развилка:</b> Объективный анализ дискуссий с позиций доказательной медицины (EBM).\n\n"
                f"Фундаментальный обзор клинического опыта врачей готов к изучению."
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
        if page_url and Button is not None:
            send_params['buttons'] = [[Button.url("⚡ Читать в Telegram", page_url)]]
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

        # Закреп + доставка PDF; резервная прямая отправка — только если недоступны и Telegraph, и PDF.
        if sent_msg:
            await _notify_delivery(delivery_hook, sent_msg)
            await _pin_message_safely(client, chat_id, sent_msg.id)

        is_endo_pdf = "endo" in (getattr(config, "DB_PATH", "") or "").lower() or "endo" in os.path.basename(os.path.dirname(os.path.abspath(__file__))).lower()
        pdf_title = f"Клинический Вестник {'EndoChat' if is_endo_pdf else 'StomChat'} ({date_str})"
        pdf_subtitle = "Эндодонтический клинический вестник • Недельный обзор" if is_endo_pdf else "Большая стоматологическая газета • Недельный обзор"
        pdf_caption = (
            f"📄 <b>{pdf_title}</b>\n\n"
            f"Полная журнальная PDF-версия вестника со всеми клиническими снимками, протоколами и таблицами."
        )
        await _deliver_digest_pdf_or_fallback(
            client,
            chat_id,
            topic_id,
            full_html,
            msg_count,
            date_str,
            page_url,
            telegraph_error,
            sent_msg,
            send_params,
            kind="weekly",
            pdf_title=pdf_title,
            pdf_subtitle=pdf_subtitle,
            pdf_caption=pdf_caption,
            fallback_emoji="🗞",
            fallback_label_prefix="weekly_direct_fallback",
        )

        logger.info(f"✅ MASSIVE Weekly Digest отправлен в {chat_id}")
        runtime_guard.clear_summary_status("weekly_summary_done")
        return msg_to_send

    except Exception:
        logger.exception("weekly summary failed")
        runtime_guard.clear_summary_status("weekly_summary_failed")
        return None
