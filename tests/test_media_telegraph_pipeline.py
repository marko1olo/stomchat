"""
test_media_telegraph_pipeline.py — Тестирование пайплайна вставки клинических снимков в Telegraph.

Проверяет:
1. Корректную замену маркеров [IMG_{m_id}] на семантические ноды Telegraph <figure><img...><figcaption>.
2. Экранирование спецсимволов и обрезку слишком длинных подписей в figcaption.
3. Детерминированный fallback: автоматическое встраивание клинических снимков в секцию кейсов,
   даже если LLM забыла прописать плейсхолдер.
4. Отсутствие утечки неиспользованных тегов [IMG_...] в опубликованный текст.
5. Наличие строгих инструкций [IMG_XXXXX] в системных промптах daily и weekly в summarizer.py.
"""

import html
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, r"c:\Users\danat\Desktop\stomchat")
from summarizer import embed_media_into_summary_html

PASS = []
FAIL = []

def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print(f"  [OK] {name}")
    else:
        FAIL.append((name, detail))
        print(f"  [FAIL] {name} -- {detail}")


def test_explicit_placeholder_replacement():
    print("\n--- ТЕСТ 1: Точная замена маркеров LLM ---")
    html_input = (
        "<p><b>2. 🦷 КЛИНИЧЕСКИЕ КЕЙСЫ</b></p>\n\n"
        "<p><b>▶️ СИТУАЦИЯ:</b> Пациент обратился с жалобами на боли.</p>\n\n"
        "[IMG_101]\n\n"
        "<p><b>ЧТО СДЕЛАЛИ:</b> Проведена инструментация.</p>"
    )
    media_map = {101: "https://iili.io/test101.jpg"}
    media_captions = {101: "Прицельный снимок зуба 4.6 <тест & валидация>"}

    result = embed_media_into_summary_html(html_input, media_map, media_captions)

    check(
        "Маркер [IMG_101] заменен",
        "[IMG_101]" not in result,
        f"Маркер остался в тексте: {result}"
    )
    check(
        "Тег figure присутствует",
        "<figure><img src=\"https://iili.io/test101.jpg\">" in result,
        f"Тег figure не найден: {result}"
    )
    check(
        "Спецсимволы в figcaption экранированы",
        "&lt;тест &amp; валидация&gt;" in result,
        f"Экранирование не сработало: {result}"
    )


def test_caption_truncation():
    print("\n--- ТЕСТ 2: Обрезка длинных подписей ---")
    long_desc = "А" * 200
    html_input = "<p>Разбор случая</p>\n\n[IMG_202]"
    media_map = {202: "https://iili.io/test202.jpg"}
    media_captions = {202: long_desc}

    result = embed_media_into_summary_html(html_input, media_map, media_captions)
    
    # figcaption должен быть обрезан до 140 символов (137 + '...')
    match = re.search(r'<figcaption>(.*?)</figcaption>', result)
    check("Figcaption найден", bool(match))
    if match:
        caption = match.group(1)
        check("Длина figcaption <= 140", len(caption) <= 140, f"Длина: {len(caption)}")
        check("Figcaption оканчивается на '...'", caption.endswith("..."))


def test_deterministic_fallback_injection():
    print("\n--- ТЕСТ 3: Детерминированный фоллбэк при забытом тест-маркере ---")
    # Модель описала кейс, но НЕ вставила [IMG_303]
    html_input = (
        "<p><b>2. 🦷 КЛИНИЧЕСКИЕ КЕЙСЫ</b></p>\n\n"
        "<p><b>▶️ СИТУАЦИЯ:</b> Пациент 42 лет, перфорация дна полости зуба.</p>\n\n"
        "<p><b>ЧТО СДЕЛАЛИ:</b> Закрытие МТА под контролем микроскопа.</p>"
    )
    media_map = {303: "https://iili.io/test303.jpg"}
    media_captions = {303: "Рентгенограмма: перфорация дна зуба 3.6"}

    result = embed_media_into_summary_html(html_input, media_map, media_captions)

    check(
        "Снимок 303 внедрен через fallback",
        "https://iili.io/test303.jpg" in result,
        f"Снимок отсутствует: {result}"
    )
    check(
        "Снимок смонтирован внутри раздела кейсов",
        result.find("КЛИНИЧЕСКИЕ КЕЙСЫ") < result.find("https://iili.io/test303.jpg"),
        "Снимок улетел в неожиданное место"
    )


def test_unplaced_tags_cleanup():
    print("\n--- ТЕСТ 4: Зачистка некорректных плейсхолдеров ---")
    html_input = "<p>Кейс с опечаткой модели [IMG_999999] в тексте.</p>"
    media_map = {}  # нет такого фото

    result = embed_media_into_summary_html(html_input, media_map)
    check(
        "Фантомный маркер [IMG_999999] удален",
        "[IMG_999999]" not in result,
        f"Маркер остался: {result}"
    )


def test_prompts_contain_image_rule():
    print("\n--- ТЕСТ 5: Проверка наличия инструкций в промптах summarizer.py ---")
    with open(r"c:\Users\danat\Desktop\stomchat\summarizer.py", "r", encoding="utf-8") as f:
        src = f.read()

    check(
        "Daily prompt содержит ПРАВИЛО РАБОТЫ С КЛИНИЧЕСКИМИ СНИМКАМИ",
        "ПРАВИЛО РАБОТЫ С КЛИНИЧЕСКИМИ СНИМКАМИ" in src,
        "Правило отсутствует в файле"
    )
    check(
        "Промпты предписывают маркер [IMG_XXXXX]",
        "[IMG_XXXXX]" in src,
        "Инструкция по маркеру отсутствует"
    )


if __name__ == "__main__":
    print("=== ЗАПУСК RED TEAM ТЕСТОВ ПАЙПЛАЙНА КАРТИНОК ===")
    test_explicit_placeholder_replacement()
    test_caption_truncation()
    test_deterministic_fallback_injection()
    test_unplaced_tags_cleanup()
    test_prompts_contain_image_rule()

    print("\n" + "=" * 50)
    print(f"ИТОГ: {len(PASS)} PASSED, {len(FAIL)} FAILED")
    if FAIL:
        for f, d in FAIL:
            print(f"  FAILED: {f} -- {d}")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED SUCCESSFULLY!")
        sys.exit(0)
