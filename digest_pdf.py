"""
digest_pdf.py — Генератор полиграфического PDF-вестника StomChat.

Преобразует аналитический лонгрид (HTML/Markdown) в сверстанный медицинский журнал
формата A4 на базе Playwright Chromium Headless:
- Поддержка впекания клинических рентгенограмм и фотопротоколов (CDN / локальные файлы).
- Медицинская верстка с разделителями разделов, бейджами и карточками кейсов.
- Колонтитулы со сквозной нумерацией страниц (@page CSS Paged Media).
- Изоляция ошибок: сбой рендера PDF не ломает публикацию в Telegraph.
"""

import asyncio
import base64
import html
import logging
import os
import re
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

PDF_OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "pdf_digests"))
LOGO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "assets", "docendo_logo.png"))


def _get_logo_base64() -> str:
    """Возвращает base64-строку официального логотипа Docendo Discimus, если файл существует."""
    if os.path.exists(LOGO_PATH):
        try:
            with open(LOGO_PATH, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.warning("Could not read logo image: %s", e)
    return ""


def _build_journal_html(
    html_body: str,
    title: str,
    subtitle: str = "",
    msg_count: int = 0,
    date_str: str = "",
) -> str:
    """Оборачивает фрагменты разбора в полноценный HTML-документ журнальной верстки."""
    now_date = date_str or datetime.now().strftime("%d.%m.%Y")
    clean_title = html.escape(title)
    clean_subtitle = html.escape(subtitle or "Клинический дайджест сообщества «Учимся вместе» (@docendobot)")
    logo_b64 = _get_logo_base64()

    # 1. Если во входящем HTML уже присутствует текстовый CTA ассистента (из Telegraph),
    # срезаем его, поскольку в конце журнала генерируется нативная полиграфическая карточка.
    html_body = re.sub(
        r'(?:<hr[^>]*>\s*)?<h3[^>]*>\s*🤖\s*Клинический ассистент[\s\S]*$',
        '',
        html_body,
        flags=re.IGNORECASE,
    ).strip()
    html_body = re.sub(
        r'(?:<hr[^>]*>\s*)?<p[^>]*>\s*🤖\s*(?:<b>|<strong>)?\s*Клинический ассистент[\s\S]*$',
        '',
        html_body,
        flags=re.IGNORECASE,
    ).strip()
    # Удаляем служебную пометку об обрезке Telegraph, если она попала в PDF
    html_body = re.sub(r'<p>\s*\[Отчет сокращен[^\]]*\]\s*</p>', '', html_body, flags=re.IGNORECASE)
    html_body = re.sub(r'\[Отчет сокращен[^\]]*\]', '', html_body, flags=re.IGNORECASE)
    html_body = re.sub(r'<p>\s*(?:<i>)?\s*Сообщений за период[\s\S]*?</p>', '', html_body, flags=re.IGNORECASE)

    # 1.1 Защита от повисшей пунктуации и пустых переносов после вставки медиа
    html_body = re.sub(r'<p>\s*(?:<br[^>]*>)?\s*</p>', '', html_body, flags=re.IGNORECASE)
    html_body = re.sub(r'<p>\s*[.,;:!?—\-]\s*(?:<br[^>]*>)?\s*</p>', '', html_body, flags=re.IGNORECASE)
    html_body = re.sub(r'(<p[^>]*>)\s*[.,;:!?—\-]\s*(?:<br[^>]*>)?\s*', r'\1', html_body, flags=re.IGNORECASE)
    html_body = re.sub(r'(<p[^>]*>)\s*(?:<br[^>]*>\s*)+', r'\1', html_body, flags=re.IGNORECASE)

    # 1.2 Нормализуем абзацы: если передан текст с \n\n (как из summarizer),
    # оборачиваем абзацы в <p>...</p>, аналогично пайплайну Telegraph.
    paragraphs = html_body.split('\n\n')
    formatted_body = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # Очищаем внутренний текст от тегов для проверки на повисшую пунктуацию
        inner_plain = re.sub(r'<[^>]+>', '', p).strip()
        if not inner_plain or re.match(r'^[.,;:!?—\-\s]+$', inner_plain):
            continue
        # Срезаем повисшие знаки препинания в самом начале абзаца
        p = re.sub(r'^(<p[^>]*>)?\s*[.,;:!?—\-]\s*', r'\1', p)
        if p.startswith(("<h", "<p", "<figure", "<blockquote", "<ul", "<ol", "<hr", "<img", "<div")):
            formatted_body.append(p)
        else:
            formatted_body.append(f"<p>{p.replace('\n', '<br>')}</p>")
    processed_body = "\n".join(formatted_body)

    # 2. Стилизуем блоки разделов (с эмодзи или номерами) в акцентные полосы
    SECTION_KEYWORDS = (
        r'ТЕОРЕТИЧЕСКИЙ СПРАВОЧНИК|ТЕМА ДНЯ|КЛИНИЧЕСКИЕ КЕЙСЫ|КЛИНИЧЕСКАЯ ПАНОРАМА|'
        r'РЫНОК И ДЕНЬГИ|КЛИНИЧЕСКИЕ ТОНКОСТИ|ПРАКТИЧЕСКИЕ ПРОТОКОЛЫ|АНАЛИЗ ГЛАВНОГО СПОРА|'
        r'СРАВНИТЕЛЬНЫЙ АНАЛИЗ|ОБЗОР МАТЕРИАЛОВ|ЮМОР|ЭКСПЕРТ ДНЯ|ИНСТРУМЕНТАЛЬНАЯ КЛАДОВАЯ|'
        r'ГЕРОИ|ДОСКА ПОЧЕТА|ГЛАВНАЯ ТЕМА|ОККЛЮЗИЯ|ХИРУРГИЯ|ЭНДОДОНТИЯ|ТЕРАПИЯ|ОРТОПЕДИЯ'
    )

    def _header_replacer(match):
        header_text = match.group(1).strip()
        header_text = re.sub(r'^##\s*', '', header_text).strip()
        rest = (match.group(2) if len(match.groups()) > 1 and match.group(2) else "").strip()
        rest = re.sub(r'^(?:[.,;:!?—\-]|<br[^>]*>|\s)+', '', rest).strip()
        rest_html = f"\n<p>{rest}</p>" if rest else ""
        return (
            f'<div class="section-banner">'
            f'  <div class="section-title">{header_text}</div>'
            f'</div>{rest_html}'
        )

    # 2.1 Известные именованные разделы (Daily и Weekly), даже если они слиты с текстом абзаца
    processed_body = re.sub(
        r'<(?:p|h[2-4])>\s*(?:<b>|<strong>)?\s*(?:##\s*)?('
        r'(?:[0-9\.]+\s*)?[\U00010000-\U0010ffff\u2000-\u32ff\ufe00-\ufe0f#]+\s*(?:' + SECTION_KEYWORDS + r')'
        r'[^<\n:]*'
        r'(?:\s*\([^)]*\))?'
        r'(?:\s*:[^<\n]*?[A-ZА-ЯЁ0-9\s/«»—\(\)\-\.\?]*)?'
        r')'
        r'(?:</b>|</strong>)?(?:\s*<br[^>]*>|\s+)?([\s\S]*?)</(?:p|h[2-4])>',
        _header_replacer,
        processed_body,
        flags=re.IGNORECASE,
    )

    # 2.2 Изолированные жирные заголовки разделов вида <b>1. 🔥 ...</b> или <b>## ⚡️ ...</b>
    processed_body = re.sub(
        r'<p>\s*(?:<b>|<strong>)\s*(?:##\s*)?((?:[0-9\.]+\s*)?[\U00010000-\U0010ffff\u2000-\u32ff\ufe00-\ufe0f#]+[^<]+)(?:</b>|</strong>)\s*</p>',
        lambda m: f'<div class="section-banner"><div class="section-title">{re.sub(r"^##\s*", "", m.group(1)).strip()}</div></div>',
        processed_body,
    )

    # 2.3 Оборачиваем раздел Доски Почета / Героев недели в неразрывный блок, чтобы не резать список на две страницы
    def _heroes_replacer(match):
        header = match.group(1)
        content = match.group(2)
        return (
            f'<div class="heroes-card">'
            f'  <div class="section-banner"><div class="section-title">{header}</div></div>\n'
            f'  {content}'
            f'</div>'
        )

    processed_body = re.sub(
        r'<div class="section-banner">\s*<div class="section-title">([^<]*(?:ДОСКА ПОЧЕТА|ГЕРОИ|ЭКСПЕРТЫ)[^<]*)</div>\s*</div>\s*([\s\S]*?)(?=(?:<div class="section-banner"|<div class="case-card"|<div class="journal-footer"|\Z))',
        _heroes_replacer,
        processed_body,
    )

    # 3. Стилизуем подзаголовки клинических кейсов в карточки с бейджем и нумерацией
    case_counter = 0
    def _case_replacer(match):
        nonlocal case_counter
        case_counter += 1
        case_title = match.group(1).strip()
        case_title = re.sub(r'^###\s*', '', case_title).strip()
        rest = (match.group(2) or "").strip()
        rest = re.sub(r'^(?:<br\s*/?>|\s)+', '', rest).strip()
        rest_html = f"\n<p>{rest}</p>" if rest else ""
        return (
            f'<div class="case-card">'
            f'  <div class="case-header">'
            f'    <span class="case-badge">🦷 КЛИНИЧЕСКИЙ СЛУЧАЙ #{case_counter}</span>'
            f'    <span class="case-title">{case_title}</span>'
            f'  </div>{rest_html}'
            f'</div>'
        )

    processed_body = re.sub(
        r'<p>\s*(?:<b>|<strong>)\s*(?:###\s*)?((?:(?:Кейс|Случай|Клинический кейс)\s*№?[0-9\.]*|▶️\s*СИТУАЦИЯ)[^<]+)(?:</b>|</strong>)(?:\s*<br\s*/?>|\s+)?([\s\S]*?)</p>',
        _case_replacer,
        processed_body,
        flags=re.IGNORECASE,
    )

    # 4. Сквозная академическая нумерация иллюстраций (Рис. 1, Рис. 2...)
    fig_idx = 0
    def _figure_numberer(match):
        nonlocal fig_idx
        fig_idx += 1
        img_tag = match.group(1)
        cap_text = match.group(2).strip()
        if not re.match(r'^(?:Рис(?:унок|\.)?|Фото)\s*\d+', cap_text, re.IGNORECASE):
            numbered_cap = f'<b>Рис. {fig_idx}.</b> {cap_text}'
        else:
            numbered_cap = cap_text
        return f'<figure>{img_tag}<figcaption>{numbered_cap}</figcaption></figure>'

    processed_body = re.sub(
        r'<figure>\s*(<img[^>]+>)\s*<figcaption>([\s\S]*?)</figcaption>\s*</figure>',
        _figure_numberer,
        processed_body,
    )

    # 5. Если две фигуры идут подряд, объединяем их в аккуратную двухколоночную галерею (парное сравнение)
    fig_pattern = r'(<figure>(?:(?!</?figure>)[\s\S])*?</figure>)'
    gallery_pattern = re.compile(rf'{fig_pattern}\s*{fig_pattern}')
    processed_body = gallery_pattern.sub(r'<div class="figures-gallery-2col">\1\2</div>', processed_body)

    # 6. Микротипографика: неразрывные пробелы после коротких предлогов и правильные тире
    def _apply_microtypography(t: str) -> str:
        # Неразрывный пробел после одно- и двухбуквенных предлогов/союзов
        t = re.sub(r'(?i)\b([ввоуиснапоподккоиздозаот])\s+', r'\1&nbsp;', t)
        # Длинное тире с неразрывным пробелом перед ним
        t = re.sub(r'(\s+)[-—–](\s+)', r'&nbsp;&mdash;\2', t)
        return t

    processed_body = re.sub(
        r'(<(?:p|figcaption|li|blockquote)[^>]*>)([\s\S]*?)(</(?:p|figcaption|li|blockquote)>)',
        lambda m: f"{m.group(1)}{_apply_microtypography(m.group(2))}{m.group(3)}",
        processed_body,
    )

    doc_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>{clean_title}</title>
    <style>
        @page {{
            size: A4 portrait;
            margin: 16mm 14mm 18mm 14mm;
            @bottom-left {{
                content: "Docendo Discimus • Клинический вестник «Учимся вместе»";
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                font-size: 8pt;
                color: #64748b;
            }}
            @bottom-right {{
                content: "Стр. " counter(page);
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                font-size: 8pt;
                font-weight: 600;
                color: #334155;
            }}
        }}

        * {{
            box-sizing: border-box;
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            font-size: 9.5pt;
            line-height: 1.52;
            color: #1e293b;
            background-color: #ffffff;
            margin: 0;
            padding: 0;
        }}

        /* Шапка журнала */
        .journal-header {{
            border-bottom: 3px solid #0284c7;
            padding-bottom: 12px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .journal-header-left {{
            display: flex;
            align-items: center;
            gap: 14px;
        }}

        .journal-emblem {{
            width: 50px;
            height: 50px;
            border-radius: 50%;
            border: 1.5px solid #cbd5e1;
            box-shadow: 0 2px 5px rgba(0, 0, 0, 0.08);
            object-fit: cover;
            flex-shrink: 0;
        }}

        .journal-brand {{
            flex: 1;
        }}

        .journal-logo {{
            font-size: 16pt;
            font-weight: 800;
            letter-spacing: -0.3px;
            color: #0f172a;
            text-transform: uppercase;
            margin: 0 0 3px 0;
        }}

        .journal-logo span {{
            color: #0284c7;
        }}

        .journal-subtitle {{
            font-size: 8.5pt;
            color: #475569;
            margin: 0;
            font-weight: 500;
        }}

        .journal-meta {{
            text-align: right;
            font-size: 8.5pt;
            color: #64748b;
        }}

        .meta-pill {{
            display: inline-block;
            background: #f1f5f9;
            border: 1px solid #cbd5e1;
            border-radius: 12px;
            padding: 3px 9px;
            font-weight: 600;
            color: #0f172a;
            margin-left: 4px;
        }}

        /* Инфо-панель выпуска */
        .issue-summary-bar {{
            background: linear-gradient(135deg, #f8fafc 0%, #edf2f7 100%);
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            padding: 8px 14px;
            margin-bottom: 18px;
            display: flex;
            justify-content: space-around;
            font-size: 8.5pt;
            color: #334155;
        }}

        .issue-summary-item strong {{
            color: #0284c7;
        }}

        /* Разделы статьи */
        .section-banner {{
            background: #f8fafc;
            border-left: 4px solid #0284c7;
            border-top: 1px solid #e2e8f0;
            border-right: 1px solid #e2e8f0;
            border-bottom: 1px solid #e2e8f0;
            border-radius: 4px;
            padding: 7px 12px;
            margin-top: 18px;
            margin-bottom: 10px;
            page-break-inside: avoid;
            break-inside: avoid;
            page-break-after: avoid;
            break-after: avoid;
        }}

        .section-title {{
            font-size: 10.5pt;
            font-weight: 700;
            color: #0f172a;
        }}

        /* Карточки клинических кейсов */
        .case-card {{
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-left: 4px solid #0284c7;
            border-radius: 6px;
            padding: 10px 14px 8px 14px;
            margin: 14px 0 14px 0;
            page-break-inside: avoid;
            break-inside: avoid;
            box-shadow: 0 1px 4px rgba(0, 0, 0, 0.04);
            clear: both;
        }}

        /* Карточка доски почета / героев недели */
        .heroes-card {{
            background: linear-gradient(135deg, #fffdf7 0%, #fefcf3 100%);
            border: 1px solid #fde68a;
            border-left: 4px solid #f59e0b;
            border-radius: 6px;
            padding: 12px 16px 10px 16px;
            margin: 16px 0;
            page-break-inside: avoid;
            break-inside: avoid;
            box-shadow: 0 1px 4px rgba(245, 158, 11, 0.08);
            clear: both;
        }}

        .heroes-card .section-banner {{
            background: transparent;
            border: none;
            padding: 0 0 8px 0;
            margin: 0 0 10px 0;
            border-bottom: 1px solid #fef3c7;
        }}

        .heroes-card .section-title {{
            color: #92400e;
            font-size: 11pt;
            font-weight: 800;
        }}

        .case-header {{
            margin: 0 0 8px 0;
            padding: 0 0 6px 0;
            border-bottom: 1px solid #f1f5f9;
            page-break-inside: avoid;
            break-inside: avoid;
            page-break-after: avoid;
            break-after: avoid;
            display: flex;
            align-items: center;
            gap: 10px;
            clear: both;
            width: 100%;
        }}

        .case-badge {{
            display: inline-flex;
            align-items: center;
            background: #0284c7;
            color: #ffffff;
            font-size: 7.5pt;
            font-weight: 700;
            padding: 2.5px 8px;
            border-radius: 4px;
            letter-spacing: 0.4px;
            text-transform: uppercase;
            white-space: nowrap;
        }}

        .case-title {{
            font-size: 10pt;
            font-weight: 700;
            color: #0f172a;
            line-height: 1.3;
        }}

        /* Основной текст */
        p {{
            margin: 0 0 8px 0;
            text-align: justify;
            text-justify: inter-word;
            text-align-last: left;
            hyphens: auto;
            -webkit-hyphens: auto;
        }}

        b, strong {{
            font-weight: 600;
            color: #0f172a;
        }}

        i, em {{
            color: #475569;
        }}

        /* Списки и буллеты */
        ul, ol {{
            margin: 4px 0 10px 18px;
            padding: 0;
        }}

        li {{
            margin-bottom: 4px;
        }}

        /* Двухколоночная галерея для парных клинических снимков */
        .figures-gallery-2col {{
            display: flex;
            gap: 14px;
            justify-content: center;
            align-items: flex-start;
            margin: 12px 0;
            page-break-inside: avoid;
            break-inside: avoid;
            clear: both;
            width: 100%;
        }}

        .figures-gallery-2col figure {{
            flex: 1;
            max-width: 48%;
            margin: 0;
            display: block;
        }}

        .figures-gallery-2col figure img {{
            max-height: 240px;
        }}

        /* Фигуры и клинические снимки */
        figure {{
            margin: 12px auto;
            text-align: center;
            page-break-inside: avoid;
            break-inside: avoid;
            max-width: 92%;
            display: block;
            clear: both;
        }}

        figure img {{
            max-width: 100%;
            max-height: 280px;
            border-radius: 6px;
            border: 1px solid #cbd5e1;
            box-shadow: 0 3px 6px -1px rgba(0, 0, 0, 0.08);
            object-fit: contain;
            background: #f8fafc;
        }}

        figcaption {{
            margin-top: 6px;
            font-size: 8pt;
            color: #475569;
            line-height: 1.35;
        }}

        figcaption b {{
            color: #0284c7;
            font-weight: 700;
        }}

        /* Цитаты и редакционные врезки */
        blockquote {{
            margin: 12px 0;
            padding: 8px 14px;
            border-left: 3.5px solid #0284c7;
            background: linear-gradient(135deg, #f0f9ff 0%, #f8fafc 100%);
            color: #1e293b;
            font-style: italic;
            border-radius: 0 6px 6px 0;
            page-break-inside: avoid;
            break-inside: avoid;
            font-size: 9pt;
            line-height: 1.5;
        }}

        blockquote b, blockquote strong {{
            color: #0369a1;
            font-style: normal;
        }}

        .journal-closing-block {{
            margin-top: 14px;
            page-break-inside: avoid;
            break-inside: avoid;
        }}

        /* Промо-блок клинического ассистента в ЛС */
        .assistant-promo-card {{
            margin: 0 0 8px 0;
            padding: 11px 15px;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #f8fafc;
            border-radius: 8px;
            border-left: 4px solid #38bdf8;
            page-break-inside: avoid;
            break-inside: avoid;
        }}

        .assistant-promo-title {{
            font-size: 10pt;
            font-weight: 800;
            letter-spacing: 0.5px;
            color: #38bdf8;
            margin-bottom: 5px;
        }}

        .assistant-promo-desc {{
            font-size: 8pt;
            color: #cbd5e1;
            margin-bottom: 8px;
            line-height: 1.35;
        }}

        .assistant-promo-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 7px;
            margin-bottom: 8px;
        }}

        .assistant-promo-item {{
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 6px;
            padding: 6px 9px;
            font-size: 7.5pt;
            line-height: 1.3;
            color: #e2e8f0;
        }}

        .assistant-promo-item b {{
            color: #7dd3fc;
            display: block;
            margin-bottom: 2px;
            font-size: 7.8pt;
        }}

        .assistant-promo-cta {{
            padding-top: 6px;
            border-top: 1px solid rgba(255, 255, 255, 0.12);
            text-align: center;
            font-size: 8pt;
            color: #cbd5e1;
        }}

        .assistant-promo-cta strong {{
            color: #38bdf8;
        }}

        /* Подвал документа */
        .journal-footer {{
            margin-top: 6px;
            padding-top: 6px;
            border-top: 1px solid #e2e8f0;
            font-size: 7.5pt;
            color: #94a3b8;
            text-align: center;
            page-break-inside: avoid;
            break-inside: avoid;
        }}
    </style>
</head>
<body>
    <div class="journal-header">
        <div class="journal-header-left">
            {f'<img class="journal-emblem" src="data:image/png;base64,{logo_b64}" alt="Docendo Discimus">' if logo_b64 else ''}
            <div class="journal-brand">
                <div class="journal-logo">DOCENDO <span>DISCIMUS</span> • КЛИНИЧЕСКИЙ ВЕСТНИК</div>
                <div class="journal-subtitle">{clean_subtitle}</div>
            </div>
        </div>
        <div class="journal-meta">
            <div>Выпуск от <strong>{now_date}</strong></div>
            {f'<div style="margin-top: 4px;"><span class="meta-pill">Сообщений: {msg_count}</span></div>' if msg_count else ''}
        </div>
    </div>

    <div class="issue-summary-bar">
        <div class="issue-summary-item">🔬 Стандарт: <strong>Evidence-Based Dentistry</strong></div>
        <div class="issue-summary-item">🦷 Сообщество: <strong>«Учимся вместе» (Docendo Discimus)</strong></div>
        <div class="issue-summary-item">📍 Источник: <strong>Практикующие врачи сообщества</strong></div>
    </div>

    <div class="journal-content">
        {processed_body}
    </div>

    <div class="journal-closing-block">
        <div class="assistant-promo-card">
            <div class="assistant-promo-title">🤖 КЛИНИЧЕСКИЙ АССИСТЕНТ @DOCENDOBOT • ПОМОЩНИК НА ПРИЕМЕ</div>
            <div class="assistant-promo-desc">
                Коллеги, бот сообщества — это ваш персональный клинический ассистент прямо в Telegram. Напишите ему в личные сообщения для мгновенного разбора кейса:
            </div>
            <div class="assistant-promo-grid">
                <div class="assistant-promo-item">
                    <b>🔬 Мульти-анализ снимков и фото (Vision)</b>
                    Присылайте в ЛС прицельные снимки, КЛКТ, ОПТГ и фотопротоколы (включая серии/альбомы). Бот оценивает краевое прилегание, периапикальные очаги, анатомию каналов и дефекты реставраций.
                </div>
                <div class="assistant-promo-item">
                    <b>💊 Фармакология и анестезия</b>
                    Точный расчет дозировок и карпул анестетиков по массе тела с учетом соматики (ASA, кардиоваскулярные патологии, беременность, дети), подбор антибиотикотерапии и премедикации.
                </div>
                <div class="assistant-promo-item">
                    <b>📚 Клинические протоколы (/protocols)</b>
                    Пошаговые алгоритмы доказательной стоматологии: BOPT, препарирование, протокол ирригации NaOCl/ЭДТА, закрытие перфораций МТА/Биодентин, адгезивная фиксация E.max.
                </div>
                <div class="assistant-promo-item">
                    <b>👤 Клиническая память врача (/profile)</b>
                    Бот помнит вашу специализацию, клинический почерк, стаж и арсенал оборудования, адаптируя все рекомендации под ваш стиль работы.
                </div>
            </div>
            <div class="assistant-promo-cta">
                👉 <strong>Напишите боту в личные сообщения: @docendobot</strong> — разберите клинический кейс или снимок прямо сейчас.
            </div>
        </div>

        <div class="journal-footer">
            Материалы подготовлены на основе клинических обсуждений врачебного сообщества «Учимся вместе» (Docendo Discimus).
            Предназначено исключительно для врачей-стоматологов в образовательных и профессиональных целях. Бот: @docendobot
        </div>
    </div>
</body>
</html>
"""
    return doc_html


async def generate_digest_pdf(
    html_content: str,
    title: str = "DOCENDO DISCIMUS • Клинический Вестник",
    subtitle: str = "",
    msg_count: int = 0,
    date_str: str = "",
    output_dir: str = PDF_OUTPUT_DIR,
) -> Optional[str]:
    """
    Генерирует PDF-документ журнального качества из HTML-саммари.

    Возвращает абсолютный путь к файлу PDF или None в случае ошибки.
    Безопасен к исключениям — не нарушает работу вызывающего конвейера.
    """
    if not html_content:
        logger.warning("generate_digest_pdf called with empty html_content")
        return None

    os.makedirs(output_dir, exist_ok=True)
    timestamp = int(time.time())
    safe_date = re.sub(r'[^0-9a-zA-Z_\-]+', '_', date_str) if date_str else str(timestamp)
    filename = f"StomChat_Digest_{safe_date}_{timestamp}.pdf"
    output_path = os.path.join(output_dir, filename)

    full_html = _build_journal_html(
        html_body=html_content,
        title=title,
        subtitle=subtitle,
        msg_count=msg_count,
        date_str=date_str,
    )

    start_time = time.time()
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                page = await browser.new_page()

                # Устанавливаем контент и ждем подгрузки изображений
                try:
                    await page.set_content(full_html, wait_until="networkidle", timeout=12000)
                except Exception:
                    # Фоллбэк: если CDN-картинки грузятся медленно, рендерим по событию load
                    logger.warning("networkidle timed out during PDF generation, falling back to load")
                    await page.set_content(full_html, wait_until="load", timeout=15000)

                # Рендерим в PDF с точными настройками печати
                await page.pdf(
                    path=output_path,
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=True,
                )
            finally:
                await browser.close()

        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            size_kb = os.path.getsize(output_path) / 1024
            elapsed = time.time() - start_time
            logger.info("✅ PDF digest generated successfully: %s (%.1f KB, %.2f s)", output_path, size_kb, elapsed)
            return output_path
        else:
            logger.error("PDF generation produced empty or missing file: %s", output_path)
            return None

    except Exception as e:
        logger.exception("Failed to generate PDF digest: %s", e)
        return None


def render_pdf_first_page_preview(pdf_path: str, preview_png_path: str) -> bool:
    """
    Рендерит первую страницу PDF в изображение PNG через PyMuPDF (fitz)
    для визуального контроля и аналитики качества верстки.
    """
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        if len(doc) == 0:
            return False
        page = doc[0]
        # Рендер с разрешением 150 DPI для четкости текста
        pix = page.get_pixmap(dpi=150)
        pix.save(preview_png_path)
        doc.close()
        return os.path.exists(preview_png_path)
    except Exception as e:
        logger.warning("Failed to render PDF preview: %s", e)
        return False
