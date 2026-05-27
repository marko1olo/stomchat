import asyncio
import aiosqlite
import os

ARCHIVE_DB = "stomat_archive.db"
WIKI_DB = "stomat_wiki.db"
OUTPUT_DIR = "wiki_final_review"

# ПОЛНАЯ КАРТА ИМЕН (Соответствует дереву 5.0)
CAT_MAP = {
   # 1. ТЕРАПИЯ
   "1.1.1": "Эндо_Доступ_МБ2",
   "1.1.2": "Эндо_Инструментация",
   "1.1.3": "Эндо_Ирригация",
   "1.1.4": "Эндо_Обтурация",
   "1.1.5": "Эндо_Перелечивание",
   "1.1.6": "Эндо_Диагностика",
   "1.2.1": "Реставрация_Адгезия_IDS",
   "1.2.2": "Реставрация_Спиртовой_протокол",
   "1.2.3": "Реставрация_Морфология",
   "1.2.4": "Реставрация_Матрицы",
   "1.2.5": "Реставрация_Билдап_Штифты",
   "1.2.6": "Реставрация_Полировка",
   "1.3.1": "Профгигиена_GBT",
   "1.3.2": "Пародонтология_SRP",
   "1.3.3": "Отбеливание",
   # 2. ОРТОПЕДИЯ
   "2.1.1": "Орто_Виниры",
   "2.1.2": "Орто_Коронки",
   "2.1.3": "Орто_Мосты",
   "2.1.4": "Орто_Микропротезирование",
   "2.2.1": "Орто_Техника_BOPT_Verti",
   "2.2.2": "Орто_Техника_Уступ",
   "2.2.3": "Орто_Оттиски",
   "2.2.4": "Орто_Ретракция",
   "2.2.5": "Орто_Временные",
   "2.2.6": "Орто_Фиксация_Цементы",
   "2.3.1": "Гнатология_Окклюзия",
   "2.3.2": "Гнатология_ВНЧС",
   "2.3.3": "Гнатология_Артикуляторы",
   "2.4.1": "Съемное_Полные",
   "2.4.2": "Съемное_Бюгельные",
   "2.4.3": "Съемное_Перебазировка",
   # 3. ХИРУРГИЯ
   "3.1.1": "Хирургия_Удаление",
   "3.1.2": "Хирургия_Апикальная",
   "3.1.3": "Хирургия_Зубосохраняющие",
   "3.2.1": "Имплантация_Планирование",
   "3.2.2": "Имплантация_Системы",
   "3.2.3": "Имплантация_Компоненты",
   "3.2.4": "Имплантация_Осложнения",
   "3.3.1": "Пластика_Десны",
   "3.3.2": "Костная_Пластика",
   # 4. ОРТОДОНТИЯ
   "4.1.1": "Ортодонтия_Брекеты",
   "4.1.2": "Ортодонтия_Элайнеры",
   "4.1.3": "Ортодонтия_Диагностика",
   # 5. ЦИФРА
   "5.1.1": "Цифра_Сканеры",
   "5.2.1": "Цифра_Exocad",
   "5.3.1": "Цифра_3D_Печать",
   # 6. ОБЩЕЕ
   "6.1.1": "Оборудование_Оптика",
   "6.2.1": "Фармакология",
   "6.3.1": "Фотопротокол",
   # 7. МЕНЕДЖМЕНТ
   "7.1.1": "Менеджмент_Экономика",
   "7.2.1": "Менеджмент_Юридическое",
   "7.3.1": "Менеджмент_Психология"
}

async def export_v7():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    print(f"🚀 Начинаю экспорт v7 (Мульти-теги и Имена)...")

    async with aiosqlite.connect(WIKI_DB, timeout=60) as db:
        await db.execute(f"ATTACH DATABASE '{ARCHIVE_DB}' AS archive")
        
        # Проходим по нашему эталонному словарю
        for cat_code, cat_name in CAT_MAP.items():
            print(f"📦 Сборка: {cat_code} ({cat_name})...", end=" ")
            
            # Ищем факты, где этот код встречается (через LIKE, так как там список)
            cursor = await db.execute('''
                SELECT f.content, f.source_ids, f.is_case, f.confidence
                FROM distilled_facts f 
                WHERE f.category_code LIKE ?
            ''', (f'%{cat_code}%',))
            
            facts = await cursor.fetchall()
            
            if not facts:
                print("пусто")
                continue

            # Имя файла: Код + Название
            safe_name = f"{cat_code}_{cat_name}".replace('/', '_').replace(' ', '_')
            file_path = os.path.join(OUTPUT_DIR, f"{safe_name}.txt")

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(f"=== {cat_code}: {cat_name.replace('_', ' ').upper()} ===\n")
                f.write(f"Найдено записей: {len(facts)}\n")
                f.write("="*60 + "\n\n")

                for content, s_ids, is_case, conf in facts:
                    type_str = "🌟 [ЭКСПЕРТНЫЙ КЕЙС]" if is_case else "📍 [ФАКТ]"
                    f.write(f"{type_str}\n")
                    f.write(f"{content}\n")
                    
                    # Подтягиваем фото
                    ids = [x.strip() for x in s_ids.split(',') if x.strip().isdigit()]
                    if ids:
                        ids_sql = ",".join(ids)
                        async with db.execute(f'''
                            SELECT msg_id, vision_description 
                            FROM archive.archive_messages 
                            WHERE msg_id IN ({ids_sql}) AND vision_processed=1 
                            AND vision_description NOT IN ('', 'SKIP', 'SKIP_ERROR')
                        ''') as c:
                            images = await c.fetchall()
                            for m_id, v_desc in images:
                                f.write(f"   📷 [ИЛЛЮСТРАЦИЯ: {m_id}] {v_desc}\n")
                    
                    f.write("-" * 40 + "\n")
            print(f"✅ ({len(facts)} шт.)")

    print(f"\n🏁 Готово! Папка: {OUTPUT_DIR}")

if __name__ == '__main__':
    asyncio.run(export_v7())