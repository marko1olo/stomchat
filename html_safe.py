"""
Безопасная работа с HTML для Telegram — общий модуль.

Telegram отклоняет сообщение с неразбираемой разметкой ЦЕЛИКОМ, а не портит
фрагмент. Практически это значит: обрезали текст посреди тега — и дайджест не
доставлен, кнопка протокола ничего не делает, статья энциклопедии не открылась.
Логика нужна минимум в двух местах (summarizer и assistant), поэтому живёт
здесь, а не копиями.
"""
import html
import logging
import re

logger = logging.getLogger(__name__)

def html_to_plain(value):
    """HTML -> плоский текст с сохранением абзацев (для запасной отправки)."""
    plain = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    plain = re.sub(r"(?i)</p\s*>", "\n\n", plain)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = html.unescape(plain)
    plain = re.sub(r"[ \t]+", " ", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip()


# Признаки того, что Telegram не смог разобрать разметку. Обрабатывать их
# отдельно необходимо: на такую ошибку отчёт не уходит целиком, планировщик не
# помечает день отправленным и каждые 10 минут ЗАНОВО генерирует дайджест
# LLM-вызовом — до конца суток это десятки платных генераций, и ни одной

_VOID_TAGS = frozenset({"br", "img", "hr"})
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)")


_FULL_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")


def balance_html(fragment):
    """
    Возвращает (текст_без_непарных_закрывающих, список_незакрытых_тегов).

    Незакрытые теги идут в порядке от внешнего к внутреннему — дописывать их
    нужно в обратном.

    Прежний обход закрывающий тег учитывал только при совпадении с вершиной
    стека, иначе молча его игнорировал; открывающий оставался на стеке, и в
    конец дописывался лишний закрывающий. Непарные закрывающие теги вообще не
    убирались, а Telegram отклоняет сообщение и из-за них тоже — «Unmatched
    end tag». Здесь при несовпадении снимаем всё до парного элемента, как это
    делает разбор HTML, а закрывающий без пары выбрасываем: смысла он не несёт.
    """
    stack = []
    pieces = []
    position = 0
    for match in _FULL_TAG_RE.finditer(fragment):
        closing, name = match.group(1), match.group(2).lower()
        if name in _VOID_TAGS:
            continue
        if closing:
            if name in stack:
                del stack[stack.index(name):]
            else:
                # Непарный закрывающий: копируем текст до него и пропускаем сам тег.
                pieces.append(fragment[position:match.start()])
                position = match.end()
        else:
            stack.append(name)
    pieces.append(fragment[position:])
    return "".join(pieces), stack


def unclosed_tags(fragment):
    return balance_html(fragment)[1]


# Граница предложения: знак конца плюс ЛЮБОЙ пробельный символ. Искать только
# ". " (точку с пробелом) нельзя: живые статьи разделены переводами строк, и
# после точки стоит "\n\n". Замер по 30 длинным статьям вики: разрез уезжал
# назад до 759 символов, врач терял 28 686 символов там, где не влезало 19 607.
_SENTENCE_END_RE = re.compile(r"[.!?;]\s")
# Номер следующего пункта списка не должен остаться сиротой: разрез после
# "...в пустоту.\n\n5." оставлял висящее "5." без текста, и врач видел пункт,
# которого в статье нет.
_HANGING_NUMBER_RE = re.compile(r"\s*\n?\s*\d+\.\s*$")
_TRAILING_TOKEN_RE = re.compile(r"\S+$")


def clip_at_sentence(text, limit):
    """
    Обрезает текст по границе предложения. Возвращает (текст, сколько НЕ показано).

    Единственная реализация на бот. Копий было три (assistant, web_lookup,
    distiller), и на 8 замеренных входах они расходились на 6; та из них, что
    резала статью врачу, приклеивала многоточие к тексту, который никто не
    обрезал, и приписывала «ещё 74 символа не поместились» к тексту, который
    помещается.

    Зачем вообще граница предложения: обрезка на полуслове — отдельный класс
    порчи. Замер по вике: 1 факт из 12 784 кончается буквой без знака конца
    предложения ('...использование клиньев и матриц в стоматологии'), и
    восстановить его уже нечем; в клиническом тексте «не более 3 мг/кг в су»
    читается как другая доза.

    Инварианты, каждый оплачен живым замером:
      * len(text) <= limit -> текст возвращается КАК ЕСТЬ, dropped = 0. Иначе
        модель видит «факт оборван» там, где он полный, и дописывает за него, а
        врач читает приписку-неправду.
      * len(результат) <= limit ВСЕГДА. Прежние копии отдавали limit+1 на 94 и
        93 входах из 264: обрезка «в бюджет» бюджет превышала.
      * len(результат) + dropped == len(text) — число в приписке врачу сходится.
      * обрыв не на границе предложения помечен многоточием, а оборванный токен
        убирается целиком: «не более 3 мг/кг в су…» читается как другая доза.
    """
    source = text or ""
    if limit <= 0:
        return "", len(source)
    if len(source) <= limit:
        return source, 0

    head = source[:limit]
    best = -1
    for match in _SENTENCE_END_RE.finditer(head):
        best = match.start()

    if best >= limit // 2:
        clipped = _HANGING_NUMBER_RE.sub("", head[:best + 1]).rstrip()
    else:
        cut = head.rfind(" ")
        if cut >= limit // 2:
            clipped = head[:cut] + "…"
        else:
            # Ни границы предложения, ни пробела во второй половине бюджета.
            # Возвращать head как есть нельзя: разрез приходится на середину
            # слова. Убираем оборванный токен целиком, а если весь head — один
            # токен, освобождаем место под метку обрыва, чтобы не выйти за предел.
            trimmed = _TRAILING_TOKEN_RE.sub("", head).rstrip()
            clipped = (trimmed or head[:limit - 1]) + "…"

    return clipped, len(source) - len(clipped)


def clip_at_sentence_text(text, limit):
    """
    Тот же разрез, только текст — для мест, где счётчик отброшенного не нужен.

    Это не вторая реализация и разойтись с первой не может: тело — ровно
    `clip_at_sentence(...)[0]`. Ровно тем и отличается от трёх копий, которые
    здесь свели в одну.
    """
    return clip_at_sentence(text, limit)[0]


def safe_cut_index(text, limit):
    """
    Наибольшая позиция не дальше limit, на которой резать безопасно.

    Резать нельзя ни внутри тега, ни внутри HTML-сущности. Telegram на
    нераспознанную разметку отклоняет ВЕСЬ отчёт, а не испорченный фрагмент,
    поэтому дайджест не доходит вообще — а планировщик, не пометив день
    отправленным, каждые 10 минут заново генерирует его LLM-вызовом.
    Проверено: срез ровно на "<a href=" давал в результате «<a href="h</a>».
    """
    cut = min(limit, len(text))
    if cut <= 0:
        return 0

    bracket = text.rfind("<", 0, cut)
    if bracket != -1 and text.find(">", bracket, cut) == -1:
        cut = bracket

    ampersand = text.rfind("&", 0, cut)
    if ampersand != -1 and cut - ampersand <= 10 and text.find(";", ampersand, cut) == -1:
        cut = ampersand

    return max(cut, 0)


def safe_truncate_html(html_str, max_len=9500):
    html_str = html_str or ""
    suffix = (
        "<br><br><b>[Отчет сокращен из-за лимитов Telegram]</b>"
        if max_len <= 4000
        else "<br><br><b>[Отчет сокращен из-за лимитов Telegraph]</b>"
    )

    if len(html_str) <= max_len:
        # Разметку правим и без обрезки: незакрытые и непарные теги оставляет
        # сама модель, а Telegram отклоняет такое сообщение точно так же.
        body, unclosed = balance_html(html_str)
        tail = "".join(f"</{tag}>" for tag in reversed(unclosed))
        if tail or body != html_str:
            logger.warning(
                "summary html was unbalanced: closed=%s stripped=%s chars",
                tail or "-", len(html_str) - len(body),
            )
        return body + tail

    # Место под закрывающие теги и суффикс резервируем ЗАРАНЕЕ, иначе результат
    # выходит за max_len — замер до правки: запрошено 3900, получено 3958.
    budget = max_len - len(suffix)
    reserve = sum(len(tag) + 3 for tag in unclosed_tags(html_str[:budget]))
    budget -= reserve
    if budget <= 0:
        return html_to_plain(html_str)[: max(0, max_len)]

    truncated = html_str[: safe_cut_index(html_str, budget)]

    # Ищем осмысленную границу обрезки (абзац, перевод строки, предложение, слово),
    # чтобы никогда не разрезать слово пополам (например, «...за детал[Отчет сокращен...]»).
    cut_pos = -1

    # 1. Граница абзаца / блока
    for marker in ("\n\n", "</p>", "<p>", "</figure>", "<br><br>", "<br/>\n", "<br>"):
        pos = truncated.rfind(marker)
        min_threshold = max(200, int(len(truncated) * 0.6))
        if pos >= min_threshold:
            cut_pos = pos + (len(marker) if marker in ("</p>", "</figure>") else 0)
            break

    # 2. Граница строки (отдельный пункт списка, автор и т.д.)
    if cut_pos == -1:
        pos = truncated.rfind("\n")
        min_threshold = max(200, int(len(truncated) * 0.7))
        if pos >= min_threshold:
            cut_pos = pos

    # 3. Граница предложения (.!?; с последующим пробелом/переносом)
    if cut_pos == -1:
        for m in _SENTENCE_END_RE.finditer(truncated):
            pos = m.end()
            min_threshold = max(200, int(len(truncated) * 0.75))
            if pos >= min_threshold:
                cut_pos = pos

    # 4. Граница слова (пробел)
    if cut_pos == -1:
        pos = truncated.rfind(" ")
        min_threshold = max(200, int(len(truncated) * 0.8))
        if pos >= min_threshold:
            cut_pos = pos

    if cut_pos > 0:
        truncated = truncated[: safe_cut_index(truncated, cut_pos)].rstrip()

    body, unclosed = balance_html(truncated)
    body += "".join(f"</{tag}>" for tag in reversed(unclosed))
    return body + suffix


# Запас под закрывающие теги в конце каждой части. Telegram понимает считаные
# теги, вложенность неглубокая, поэтому 96 символов покрывают её с избытком.
_CLOSER_RESERVE = 96


def _preferred_cut(text, start, hard_end):
    """
    Где резать: по границе абзаца, иначе строки, иначе слова.

    Результат всё равно прогоняется через safe_cut_index — граница абзаца
    может оказаться внутри тега, если разметка кривая.
    """
    if hard_end >= len(text):
        return len(text)

    window = text[start:hard_end]
    for separator in ("\n\n", "\n", " "):
        position = window.rfind(separator)
        # Слишком ранний разрыв делает куски рваными: требуем хотя бы половину.
        if position > len(window) // 2:
            return safe_cut_index(text, start + position + len(separator))
    return safe_cut_index(text, hard_end)


def split_html(text, limit=4000):
    """
    Режет размеченный текст на части, каждая из которых валидна САМА ПО СЕБЕ.

    Прежний разделитель резал по абзацам и не следил за тегами. Ответ вида
    "<b>Заголовок\n\nтекст</b>" давал первую часть с незакрытым <b> и вторую
    с непарным </b> — Telegram отклонял ОБЕ, и врач терял весь ответ на свой
    клинический вопрос. Одиночный длинный абзац и вовсе рубился срезом
    p[i:i+4000], то есть мог разорвать тег или HTML-сущность.

    Незакрытые теги закрываются в конце части и переоткрываются в начале
    следующей, поэтому форматирование не рвётся на стыке.
    """
    text = (text or "").strip()
    if not text:
        return []

    if len(text) <= limit:
        body, unclosed = balance_html(text)
        return [body + "".join(f"</{tag}>" for tag in reversed(unclosed))]

    chunks = []
    carry = []
    position = 0
    while position < len(text):
        prefix = "".join(f"<{tag}>" for tag in carry)
        budget = limit - len(prefix) - _CLOSER_RESERVE
        if budget <= 0:
            budget = max(1, limit - len(prefix))

        end = _preferred_cut(text, position, position + budget)
        if end <= position:
            # Резать безопасно негде — отдаём остаток плоским текстом, чтобы не
            # зациклиться и не потерять содержание.
            chunks.append(html_to_plain(text[position:])[:limit])
            break

        body, unclosed = balance_html(prefix + text[position:end])
        closers = "".join(f"</{tag}>" for tag in reversed(unclosed))
        piece = body + closers
        if len(piece) > limit:
            piece = html_to_plain(piece)[:limit]
            unclosed = []
        chunks.append(piece.strip())
        carry = unclosed
        position = end

    return [chunk for chunk in chunks if chunk]


def split_html_for_telegraph(html_str, max_len=11000):
    """
    Разделяет лонгрид на части для Telegraph, чтобы избежать ошибки CONTENT_TOO_BIG (64 KB)
    и не обрезать контент. Каждая часть валидна, со сбалансированными тегами.
    """
    html_str = (html_str or "").strip()
    if not html_str:
        return []
    if len(html_str) <= max_len:
        body, unclosed = balance_html(html_str)
        return [body + "".join(f"</{tag}>" for tag in reversed(unclosed))]
    return split_html(html_str, limit=max_len)


def clean_markdown_to_html(text):
    """Преобразует Markdown разметку в безопасный валидный HTML с семантической склейкой строк."""
    if not text:
        return ""

    # 1. Сначала превращаем Markdown-жирный в HTML-жирный
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)

    # 2. Обработка заголовков ###, списков и маркеров изображений
    marker_re = re.compile(r'^[ \t]*[\-•*+—▶️🛑✅🌟]+\s*')
    heading_re = re.compile(r'^[ \t]*#{1,6}\s+(.*)')
    image_re = re.compile(r'^[ \t]*\[IMG_\d+\][ \t]*$')

    lines = []
    list_flags = []
    heading_flags = []
    image_flags = []

    for raw_line in text.split('\n'):
        line_clean = raw_line.strip()
        is_img = bool(image_re.match(line_clean))

        m_head = heading_re.match(raw_line)
        if m_head:
            content = m_head.group(1).strip()
            if not (content.startswith("<b>") and content.endswith("</b>")):
                content = f"<b>{content}</b>"
            lines.append(content)
            list_flags.append(False)
            heading_flags.append(True)
            image_flags.append(False)
            continue

        without_marker = marker_re.sub('', raw_line)
        list_flags.append(without_marker != raw_line)
        lines.append(without_marker)
        heading_flags.append(False)
        image_flags.append(is_img)

    # 3. АЛГОРИТМ СЕМАНТИЧЕСКОЙ СКЛЕЙКИ
    final_lines = []
    final_is_list = []
    final_is_heading = []
    final_is_image = []

    # Жесткие терминаторы (конец мысли)
    hard_stops = ".!?:;"
    # Союзы и знаки продолжения
    conjunctions = r'\b(и|а|но|или|к|в|с|от|до|за|для|на|по)\s*$'

    def looks_like_heading(rendered, plain):
        stripped = rendered.strip()
        is_emoji_case_or_num = bool(re.match(r'^(?:[📚🔥🦷💰🎓📝⚔️🔍🛠😂🌟⚡🔬☕💎🚀🛡🧱⚖️#\d]|Кейс\s*№?|Случай\s*№?|Клинический)', plain, re.IGNORECASE))
        max_len = 140 if is_emoji_case_or_num else 60
        return (
            stripped.startswith("<b>")
            and stripped.endswith("</b>")
            and "</b>" not in stripped[:-4]
            and len(plain) < max_len
            and plain[-1:] not in (",", ":", ";")
        )

    def append(rendered, is_list_item, is_heading_item, is_img_item):
        final_lines.append(rendered)
        final_is_list.append(is_list_item)
        final_is_heading.append(is_heading_item)
        final_is_image.append(is_img_item)

    for line, is_list_item, is_orig_heading, is_img in zip(lines, list_flags, heading_flags, image_flags):
        clean_line = line.strip()

        if not clean_line:
            if final_lines and final_lines[-1] != "":
                last_txt = re.sub(r'<[^>]+>', '', final_lines[-1]).strip()
                # Разрыв только если точка/двоеточие в конце
                if last_txt and last_txt[-1] in hard_stops:
                    append("", False, False, False)
            continue

        stripped_curr = re.sub(r'<[^>]+>', '', clean_line).strip()
        current_is_heading = is_orig_heading or looks_like_heading(clean_line, stripped_curr)

        # Перед заголовком или картинкой — всегда пустая строка
        if (current_is_heading or is_img) and final_lines and final_lines[-1] != "":
            append("", False, False, False)

        if not final_lines or final_lines[-1] == "":
            append(clean_line, is_list_item, current_is_heading, is_img)
            continue

        prev_line = final_lines[-1]
        prev_is_heading = final_is_heading[-1]
        prev_is_img = final_is_image[-1]
        stripped_prev = re.sub(r'<[^>]+>', '', prev_line).strip()

        # Картинки и заголовки НИКОГДА не склеиваются с соседними строками
        if is_img or prev_is_img or current_is_heading or prev_is_heading:
            append("", False, False, False)
            append(clean_line, is_list_item, current_is_heading, is_img)
            continue

        if not stripped_prev or not stripped_curr:
            append(clean_line, is_list_item, current_is_heading, is_img)
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
        is_header_like = looks_like_heading(prev_line, stripped_prev)
        if is_header_like:
            if stripped_prev[-1] == "," or re.search(conjunctions, stripped_prev, re.I):
                should_join = True
            else:
                if not stripped_curr[0].islower():
                    should_join = False

        # Заголовок раздела не приклеивается к предыдущему тексту никогда.
        if current_is_heading:
            should_join = False

        # Два пункта списка — две строки.
        if is_list_item and final_is_list and final_is_list[-1]:
            should_join = False

        if should_join:
            final_lines[-1] = f"{prev_line} {clean_line}"
            final_is_list[-1] = final_is_list[-1] or is_list_item
        else:
            append(clean_line, is_list_item, current_is_heading, is_img)

    # 4. Финальная чистка
    text = "\n".join(final_lines)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


