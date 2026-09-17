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
import html
import logging
import os
import re
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

PDF_OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "pdf_digests"))


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
    clean_subtitle = html.escape(subtitle or "Еженедельный клинический дайджест профессионального сообщества")

    # Преобразуем заголовки разделов в оформленные блоки
    processed_body = html_body

    # Стилизуем блоки клинических кейсов для предотвращения разрыва страниц
    # Заменяем параграфы с заголовками секций на акцентные полосы
    def _header_replacer(match):
        header_text = match.group(1)
        return (
            f'<div class="section-banner">'
            f'  <div class="section-title">{header_text}</div>'
            f'</div>'
        )

    # Ищем жирные заголовки разделов вида <b>1. 🔥 ...</b> или <b>## ...</b>
    processed_body = re.sub(
        r'<p>\s*<b>([0-9\.]+\s*[📚🔥🦷💰🎓📝⚔️🔍🛠😂🌟#]+[^<]+)</b>\s*</p>',
        _header_replacer,
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
                content: "StomChat • Клинический вестник";
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
            align-items: flex-end;
        }}

        .journal-brand {{
            flex: 1;
        }}

        .journal-logo {{
            font-size: 19pt;
            font-weight: 800;
            letter-spacing: -0.5px;
            color: #0f172a;
            text-transform: uppercase;
            margin: 0 0 4px 0;
        }}

        .journal-logo span {{
            color: #0284c7;
        }}

        .journal-subtitle {{
            font-size: 9pt;
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
            page-break-after: avoid;
            break-after: avoid;
        }}

        .section-title {{
            font-size: 11pt;
            font-weight: 700;
            color: #0f172a;
        }}

        /* Основной текст */
        p {{
            margin: 0 0 8px 0;
            text-align: justify;
            text-justify: inter-word;
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

        /* Фигуры и клинические снимки */
        figure {{
            margin: 14px auto;
            text-align: center;
            page-break-inside: avoid;
            break-inside: avoid;
            max-width: 96%;
        }}

        figure img {{
            max-width: 100%;
            max-height: 360px;
            border-radius: 6px;
            border: 1px solid #cbd5e1;
            box-shadow: 0 3px 6px -1px rgba(0, 0, 0, 0.08);
            object-fit: contain;
            background: #f8fafc;
        }}

        figcaption {{
            margin-top: 5px;
            font-size: 8pt;
            color: #64748b;
            font-style: italic;
            line-height: 1.3;
        }}

        /* Цитаты и выноски */
        blockquote {{
            margin: 10px 0;
            padding: 6px 12px;
            border-left: 3px solid #94a3b8;
            background: #f8fafc;
            color: #334155;
            font-style: italic;
            border-radius: 0 4px 4px 0;
            page-break-inside: avoid;
            break-inside: avoid;
        }}

        /* Подвал документа */
        .journal-footer {{
            margin-top: 24px;
            padding-top: 10px;
            border-top: 1px solid #e2e8f0;
            font-size: 8pt;
            color: #94a3b8;
            text-align: center;
            page-break-inside: avoid;
            break-inside: avoid;
        }}
    </style>
</head>
<body>
    <div class="journal-header">
        <div class="journal-brand">
            <div class="journal-logo">Stom<span>Chat</span> • Клинический Вестник</div>
            <div class="journal-subtitle">{clean_subtitle}</div>
        </div>
        <div class="journal-meta">
            <div>Выпуск от <strong>{now_date}</strong></div>
            {f'<div style="margin-top: 4px;"><span class="meta-pill">Сообщений: {msg_count}</span></div>' if msg_count else ''}
        </div>
    </div>

    <div class="issue-summary-bar">
        <div class="issue-summary-item">🔬 Стандарт: <strong>Evidence-Based Dentistry</strong></div>
        <div class="issue-summary-item">🦷 Формат: <strong>Клинические протоколы и разборы</strong></div>
        <div class="issue-summary-item">📍 Источник: <strong>Практикующие врачи сообщества</strong></div>
    </div>

    <div class="journal-content">
        {processed_body}
    </div>

    <div class="journal-footer">
        Материалы подготовлены на основе клинических обсуждений сообщества StomChat AI.
        Предназначено исключительно для профессионального ознакомления врачами-стоматологами.
    </div>
</body>
</html>
"""
    return doc_html


async def generate_digest_pdf(
    html_content: str,
    title: str = "Клинический Вестник StomChat",
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
