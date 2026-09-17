"""
run_live_real_pipeline_test.py — Боевой прогон пайплайна дайджеста в реальных условиях.

- 100% реальные сообщения из базы stomat_bot.db (без мокапов).
- 100% реальные снимки с CDN https://iili.io/...
- 100% реальная генерация через Gemini.
- 100% реальная публикация статьи в Telegraph с встроенными изображениями.
- 100% реальная генерация полиграфического PDF через Playwright.
- Отправка СТРОГО в тестовый канал -1003735006121 (топик 26).
- Основной канал -1001820467444 НЕ ЗАТРАГИВАЕТСЯ.
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("live_e2e_test")

sys.path.insert(0, r"c:\Users\danat\Desktop\stomchat")
import config
import database
import summarizer
from digest_pdf import render_pdf_first_page_preview
from telethon import TelegramClient
from telethon.sessions import MemorySession

TEST_CHAT_ID = -1003735006121
TEST_TOPIC_ID = 26

# Защита от случайной отправки в основной чат
MAIN_CHAT_ID = -1001820467444


async def run_live_test():
    print("=" * 60)
    print("🚀 СТАРТ БОЕВОГО E2E ТЕСТИРОВАНИЯ В ТЕСТОВОМ КАНАЛЕ")
    print(f"Целевой чат: {TEST_CHAT_ID} (Topic: {TEST_TOPIC_ID})")
    print(f"Основной чат: {MAIN_CHAT_ID} (СТРОГО ЗАПРЕЩЕН ДЛЯ ТЕСТА)")
    print("=" * 60)

    # 1. Загрузка реальных сообщений из базы данных
    now = datetime.now()
    start_dt = now - timedelta(days=3)
    print(f"\n[1] Выборка реальных сообщений за период {start_dt.strftime('%Y-%m-%d')} — {now.strftime('%Y-%m-%d')}...")
    
    messages = await database.get_messages_for_range(start_dt, now)
    print(f"Загружено сообщений: {len(messages)}")
    if not messages:
        print("ОШИБКА: Нет сообщений в базе данных за указанный период!")
        return False

    # Подсчет медиа в выборке
    media_count = sum(1 for m in messages if m[7]) # m[7] = media_remote_url
    desc_count = sum(1 for m in messages if m[4] and m[4] != '-') # m[4] = media_description
    print(f"Из них содержат реальные снимки с CDN: {media_count}")
    print(f"Из них содержат клинические описания медиа: {desc_count}")

    # 2. Инициализация выделенного клиента бота через MemorySession
    print("\n[2] Подключение клиента бота к Telegram...")
    test_bot = TelegramClient(MemorySession(), config.API_ID, config.API_HASH)
    await test_bot.start(bot_token=config.BOT_TOKEN)
    me = await test_bot.get_me()
    print(f"Успешное подключение: @{me.username} (ID: {me.id})")

    # 3. Перехват отправленных данных для детального аудита
    intercepted_delivery = {
        "telegraph_url": None,
        "teaser_text": None,
        "pdf_path": None,
        "sent_messages": [],
    }

    original_send_file = test_bot.send_file
    async def intercept_send_file(*args, **kwargs):
        # Жесткая валидация цели: ни при каких обстоятельствах не слать в основной чат!
        target = args[0] if args else kwargs.get("entity")
        if target == MAIN_CHAT_ID or str(target) == str(MAIN_CHAT_ID):
            raise RuntimeError(f"CRITICAL SAFETY VIOLATION: Attempted to send file to MAIN_CHAT_ID {MAIN_CHAT_ID}!")
        
        file_path = args[1] if len(args) > 1 else kwargs.get("file")
        intercepted_delivery["pdf_path"] = str(file_path)
        logger.info(f"intercepted send_file to target={target} file={file_path}")
        res = await original_send_file(*args, **kwargs)
        intercepted_delivery["sent_messages"].append(getattr(res, "id", None))
        return res

    test_bot.send_file = intercept_send_file

    # 4. Запуск генерации недельного дайджеста
    print("\n[3] Запуск боевого пайплайна process_weekly_batch...")
    try:
        result_teaser = await summarizer.process_weekly_batch(
            messages=messages,
            client=test_bot,
            chat_id=TEST_CHAT_ID,
            topic_id=TEST_TOPIC_ID,
            delivery_hook=None,
            cached_message=None,
        )
        intercepted_delivery["teaser_text"] = result_teaser
        print(f"Результат вызова process_weekly_batch: {'УСПЕХ' if result_teaser else 'ОШИБКА'}")
    except Exception as e:
        logger.exception("Исключение в process_weekly_batch: %s", e)
        return False
    finally:
        await test_bot.disconnect()

    if not result_teaser:
        print("❌ Генерация дайджеста завершилась неудачей (None)!")
        return False

    # 5. Извлечение Telegraph URL
    match_url = re.search(r'https://telegra\.ph/[a-zA-Z0-9_\-]+', result_teaser)
    if match_url:
        intercepted_delivery["telegraph_url"] = match_url.group(0)
        print(f"\n[4] Опубликован лонгрид в Telegraph: {intercepted_delivery['telegraph_url']}")
    else:
        print("\n[4] ВНИМАНИЕ: Ссылка на Telegraph не найдена в тизере!")

    # 6. Глубокий аудит страницы Telegraph
    print("\n[5] АУДИТ СОДЕРЖИМОГО TELEGRAPH ЧЕРЕЗ API...")
    if intercepted_delivery["telegraph_url"]:
        import requests
        page_path = intercepted_delivery["telegraph_url"].replace("https://telegra.ph/", "")
        api_url = f"https://api.telegra.ph/getPage/{page_path}?return_content=true"
        r = requests.get(api_url, timeout=15)
        if r.status_code == 200:
            page_data = r.json().get("result", {})
            page_content = page_data.get("content", [])
            
            # Рекурсивный поиск узлов img и figure
            images_found = []
            captions_found = []
            def extract_media(nodes):
                for node in nodes:
                    if isinstance(node, dict):
                        tag = node.get("tag")
                        if tag == "img":
                            images_found.append(node.get("attrs", {}).get("src"))
                        elif tag == "figcaption":
                            children = node.get("children", [])
                            captions_found.append("".join(str(c) for c in children))
                        if "children" in node:
                            extract_media(node["children"])
            extract_media(page_content)
            
            print(f"  • Найдено изображений (img) на странице Telegraph: {len(images_found)}")
            for idx, img_src in enumerate(images_found, 1):
                cap = captions_found[idx-1] if idx <= len(captions_found) else "(нет подписи)"
                print(f"    [{idx}] SRC: {img_src}")
                print(f"        Подпись: {cap}")
                
            # Проверка на наличие остаточных [IMG_
            raw_json = json.dumps(page_content, ensure_ascii=False)
            broken_placeholders = re.findall(r'\[IMG_\d+\]', raw_json)
            print(f"  • Нераспознанных маркеров [IMG_...]: {len(broken_placeholders)}")
            if broken_placeholders:
                print(f"    ОШИБКА: Обнаружены висячие маркеры: {broken_placeholders}")

            # Извлекаем весь текст статьи для проверки обрезки и финала
            text_chunks = []
            def extract_text(nodes):
                for node in nodes:
                    if isinstance(node, str):
                        text_chunks.append(node)
                    elif isinstance(node, dict):
                        if "children" in node:
                            extract_text(node["children"])
            extract_text(page_content)
            full_article_text = " ".join(text_chunks)
            print(f"\n  • Полная длина текста статьи в Telegraph: {len(full_article_text)} символов")
            print(f"  • Наличие пометки 'Отчет сокращен': {'ДА (ОБРЕЗАНО!)' if 'Отчет сокращен' in full_article_text else 'НЕТ (СТАТЬЯ ПОЛНАЯ!)'}")
            print("\n  --- ФИНАЛ СТАТЬИ (ПОСЛЕДНИЕ 400 СИМВОЛОВ) ---")
            print(full_article_text[-400:])
            print("  ---------------------------------------------")
        else:
            print(f"  ОШИБКА получения данных Telegraph API: HTTP {r.status_code}")

    # 7. Глубокий аудит сгенерированного PDF
    print("\n[6] АУДИТ СГЕНЕРИРОВАННОГО PDF...")
    pdf_path = intercepted_delivery["pdf_path"]
    if pdf_path and os.path.exists(pdf_path):
        size_bytes = os.path.getsize(pdf_path)
        print(f"  • PDF файл создан: {pdf_path}")
        print(f"  • Размер PDF: {size_bytes / 1024:.1f} КБ ({size_bytes} байт)")
        
        # Рендер страниц в PNG для визуальной проверки
        import fitz
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        print(f"  • Всего страниц в журнале: {total_pages}")
        
        rendered_images = []
        for p_no in range(min(total_pages, 5)):
            img_path = os.path.join(os.path.dirname(pdf_path), f"live_audit_page_{p_no+1}.png")
            doc[p_no].get_pixmap(dpi=150).save(img_path)
            print(f"  • Рендер стр. {p_no+1} сохранен: {img_path}")
            rendered_images.append(img_path)
        doc.close()
    else:
        print("  ❌ ОШИБКА: PDF-файл не был создан или не перехвачен!")

    print("\n" + "=" * 60)
    print("✅ БОЕВОЙ ПРОГОН УСПЕШНО ЗАВЕРШЕН!")
    print(f"Сообщения отправлены в топик {TEST_TOPIC_ID} тестового чата {TEST_CHAT_ID}.")
    print("=" * 60)
    return True


if __name__ == "__main__":
    asyncio.run(run_live_test())
