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
