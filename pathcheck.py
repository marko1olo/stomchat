import asyncio
import aiosqlite
import os

DB_PATH = "stomat_archive.db"

async def check_paths():
    if not os.path.exists(DB_PATH):
        print("❌ База архива не найдена!")
        return

    print("🕵️‍♂️ Запуск СЛУЧАЙНОЙ проверки 50 файлов по всей базе...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Берем 50 СЛУЧАЙНЫХ записей, у которых есть путь к файлу
        cursor = await db.execute('''
            SELECT msg_id, media_remote_url 
            FROM archive_messages 
            WHERE media_remote_url IS NOT NULL 
              AND media_remote_url != ''
              AND media_remote_url != 'SKIP_ERROR'
            ORDER BY RANDOM() 
            LIMIT 50
        ''')
        rows = await cursor.fetchall()
        
        if not rows:
            print("⚠️ В базе нет записей с путями.")
            return

        found = 0
        missing = 0
        
        print(f"\n{'MSG ID':<10} | {'СТАТУС':<8} | {'ПУТЬ НА ДИСКЕ'}")
        print("-" * 80)
        
        for msg_id, db_path in rows:
            # Адаптация слешей под Windows
            real_path = db_path.replace('/', os.sep).replace('\\', os.sep)
            
            if os.path.exists(real_path):
                status = "✅ OK"
                found += 1
            else:
                status = "❌ MISSING"
                missing += 1
            
            # Обрезаем длинные пути для консоли
            disp_path = (db_path[:60] + '..') if len(db_path) > 60 else db_path
            print(f"{msg_id:<10} | {status:<8} | {disp_path}")

    print("-" * 80)
    print(f"ИТОГ ПРОВЕРКИ: Найдено {found} из 50.")
    print(f"БИТЫХ ССЫЛОК: {missing}")
    
    if missing == 0:
        print("🎉 Структура идеальна. Можно публиковать.")
    else:
        print("⚠️ Есть проблемы. Если их много, придется пересканировать папку.")

if __name__ == '__main__':
    asyncio.run(check_paths())