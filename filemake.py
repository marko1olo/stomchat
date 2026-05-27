import asyncio
import aiosqlite
import os
import logging

# Настройки
DB_PATH = "stomat_wiki.db"
OUTPUT_DIR = "wiki_review"

async def export_by_categories():
    # Создаем папку для обзора, если её нет
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
        print(f"📁 Создана папка {OUTPUT_DIR}")

    print("🚀 Начинаю экспорт фактов по категориям...")

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        # 1. Получаем все уникальные категории, которые уже есть в базе
        cursor = await db.execute('SELECT DISTINCT category_code FROM distilled_facts ORDER BY category_code')
        categories = await cursor.fetchall()
        
        if not categories:
            print("⚠️ В базе пока нет извлеченных фактов.")
            return

        for (cat_code,) in categories:
            # Очищаем код категории для имени файла (убираем лишние точки в конце, если есть)
            safe_cat_code = str(cat_code).strip('.')
            if not safe_cat_code: safe_cat_code = "unclassified"
            
            file_path = os.path.join(OUTPUT_DIR, f"{safe_cat_code}.txt")
            
            # 2. Вытаскиваем все факты по этой категории
            cursor_facts = await db.execute('''
                SELECT content, source_ids, is_case, confidence 
                FROM distilled_facts 
                WHERE category_code = ? 
                ORDER BY processed_at ASC
            ''', (cat_code,))
            facts = await cursor_facts.fetchall()

            print(f"📦 Категория {safe_cat_code}: записываю {len(facts)} фактов...")

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(f"=== КАТЕГОРИЯ {safe_cat_code} ===\n")
                f.write(f"Всего записей: {len(facts)}\n")
                f.write("="*40 + "\n\n")

                for i, (content, sources, is_case, conf) in enumerate(facts, 1):
                    prefix = "[КЕЙС/КУРС]" if is_case else "[ФАКТ]"
                    f.write(f"📍 ЗАПИСЬ №{i} {prefix}\n")
                    f.write(f"Доверие: {conf}%\n")
                    f.write(f"Источники (MSG IDs): {sources}\n")
                    f.write(f"ТЕКСТ:\n{content}\n")
                    f.write("-" * 30 + "\n\n")

    print(f"\n✅ Экспорт завершен! Проверь папку {OUTPUT_DIR}")

if __name__ == '__main__':
    asyncio.run(export_by_categories())