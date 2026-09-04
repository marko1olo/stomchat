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

    # Обрезаем по границе абзаца, если она достаточно далеко.
    for marker in ("<p>", "<br>"):
        position = truncated.rfind(marker)
        if position > 2000:
            truncated = truncated[:position]
            break

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

    # 2. Превращаем заголовки ### в жирный
    text = re.sub(r'^#{1,6}\s+(.*)', r'<b>\1</b>', text, flags=re.MULTILINE)

    # 3. Маркеры списков убираем, но ЗАПОМИНАЕМ, что строка была пунктом.
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

        # Перед заголовком раздела — всегда пустая строка.
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
            append(clean_line, is_list_item)

    # 5. Финальная чистка
    text = "\n".join(final_lines)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


