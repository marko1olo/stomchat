"""
test_table_rendering_pipeline.py — Тесты рендеринга Markdown таблиц в PDF и Telegraph.

Проверяет:
1. Корректность парсинга Markdown таблиц в HTML (<table class="clinical-table">).
2. Защиту от семантической склейки строк до и после таблицы.
3. Адаптацию HTML таблиц для Telegraph API (преобразование в blockquote/карточки).
4. Валидность узлов Telegraph (отсутствие недопустимого тега table).
5. Корректность интеграции в журнальный шаблон PDF (CSS стили, отсутствие оборачивания в <p>).
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from html_safe import clean_markdown_to_html, convert_all_tables_in_html_to_telegraph
from blocking_tools import _sanitize_telegraph_nodes, _ALLOWED_TELEGRAPH_TAGS
from html_telegraph_poster.converter import convert_html_to_telegraph_format, OutputFormat
from digest_pdf import _build_journal_html

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


USER_SNIPPET = """Биокерамические материалы для ретроградного пломбирования
| Параметр | Well-Root PT | Well-Root Paste | Классический МТА (порошок/вода) |
| :--- | :--- | :--- | :--- |
| Форма выпуска | Premixed Putty (готовая плотная паста) | Инъекционная паста (шприц) | Порошок силиката кальция + дистиллированная вода |
| Время отверждения | ~2-4 часа | ~4 часа | ~3-4 часа |

Клинический вывод: готовые формы исключают ошибки замешивания.
"""

print("\n[1] Парсинг Markdown таблицы из примера пользователя")
html_result = clean_markdown_to_html(USER_SNIPPET)

check("Таблица преобразована в HTML", "<table" in html_result and "</table>" in html_result)
check("Контейнер .table-container присутствует", '<div class="table-container">' in html_result)
check("Класс clinical-table присутствует", 'class="clinical-table"' in html_result)
check("Заголовок таблицы сохранен", "<th>Параметр</th>" in html_result)
check("Колонка Well-Root PT на месте", "<th>Well-Root PT</th>" in html_result)
check("Ячейка Premixed Putty на месте", "Premixed Putty" in html_result)
check("Первая колонка параметров выделена", "<b>Форма выпуска</b>" in html_result)

print("\n[2] Защита от склейки со смежным текстом")
lines = [l.strip() for l in html_result.split("\n") if l.strip()]
check("Предшествующий текст не склеен с шапкой таблицы",
      lines[0] == "Биокерамические материалы для ретроградного пломбирования")
check("Последующий текст не затянут в таблицу",
      "Клинический вывод: готовые формы исключают ошибки замешивания." in lines[-1])

print("\n[3] Адаптация таблицы для Telegraph API")
telegraph_html = convert_all_tables_in_html_to_telegraph(html_result)
check("Тег <table> удален из Telegraph HTML", "<table" not in telegraph_html)
check("Блоки blockquote созданы", "<blockquote>" in telegraph_html)
check("Параметр 'Форма выпуска' в карточке", "▫️ <b>Форма выпуска</b>" in telegraph_html)
check("Well-Root PT отображается с буллетом", "• <b>Well-Root PT:</b>" in telegraph_html)

print("\n[4] Валидность Telegraph узлов (Node tree)")
nodes = convert_html_to_telegraph_format(telegraph_html, clean_html=True, output_format=OutputFormat.PYTHON_LIST)
sanitized = _sanitize_telegraph_nodes(nodes)
check("Узлы успешно сгенерированы", len(sanitized) > 0)

def extract_tags(node_list):
    tags = set()
    for n in node_list:
        if isinstance(n, dict):
            tags.add(n.get("tag", ""))
            tags.update(extract_tags(n.get("children", [])))
    return tags

found_tags = extract_tags(sanitized)
check("Нет тега <table> среди узлов Telegraph", "table" not in found_tags)
check("Все теги входят в _ALLOWED_TELEGRAPH_TAGS", found_tags.issubset(_ALLOWED_TELEGRAPH_TAGS),
      f"лишние теги: {found_tags - _ALLOWED_TELEGRAPH_TAGS}")

print("\n[5] Двухколоночные таблицы в Telegraph")
two_col_md = """| Препарат | Дозировка |
| :--- | :--- |
| Артикаин 4% | 7 мг/кг |
| Мепивакаин 3% | 4.4 мг/кг |
"""
two_col_html = clean_markdown_to_html(two_col_md)
two_col_tg = convert_all_tables_in_html_to_telegraph(two_col_html)
check("Двухколоночная таблица образует единый blockquote", two_col_tg.count("<blockquote>") == 1)
check("Артикаин внутри blockquote", "• <b>Артикаин 4%:</b> 7 мг/кг" in two_col_tg)

print("\n[6] Интеграция в полиграфический PDF-шаблон")
pdf_html = _build_journal_html(html_result, title="Тест PDF Таблицы")
check("Таблица не завернута в <p>", "<p><table" not in pdf_html and "<p><div class=\"table-container\">" not in pdf_html)
check("Внутри таблицы нет инъекций <br>", "<table class=\"clinical-table\"><br>" not in pdf_html)
check("CSS стили таблиц присутствуют в @style", "table.clinical-table" in pdf_html and "border-collapse: collapse" in pdf_html)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
