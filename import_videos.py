import asyncio
import aiosqlite
import re
import config
from google import genai
from google.genai import types
from datetime import datetime

# Файл, куда ты скопировал текст
INPUT_FILE = "videos.txt"
DB_PATH = "stomat_wiki.db"

# Дерево для классификации (нейронка сама решит, куда положить кейс)
KNOWLEDGE_TREE = """
1. ТЕРАПИЯ (1.1 Эндо, 1.2 Реставрация/Пломбы, 1.3 Изоляция/Коффердам, 1.4 Адгезия)
2. ОРТОПЕДИЯ (2.1 Виниры, 2.2 Коронки/Преп, 2.3 Временные, 2.4 Оттиски/Скан)
3. ХИРУРГИЯ (3.1 Удаление, 3.2 Имплантация, 3.3 Мягкие ткани)
5. ЦИФРА (5.1 Сканеры, 5.2 Exocad)
6. ОБЩЕЕ (6.1 Эргономика, 6.2 Фото протокол, 6.3 Материаловедение)
"""

async def get_category(text):
    """Определяет категорию кейса через Gemma 3."""
    if not config.GOOGLE_KEYS: return "10.1"
    
    # Используем Gemma 3, так как она у тебя работает
    model_id = "models/gemma-3-27b-it"
    
    # Берем первый ключ (нагрузка маленькая, всего 20 запросов)
    client = genai.Client(api_key=config.GOOGLE_KEYS[0])
    
    try:
        prompt = f"""
        Определи код категории для этого стоматологического кейса.
        Дерево категорий:
        {KNOWLEDGE_TREE}
        
        Текст кейса:
        {text[:1000]}...
        
        Твоя задача: Вернуть ТОЛЬКО цифры кода (например: 2.2). Никаких слов.
        """
        
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )
        
        if response.text:
            # Ищем цифры (на всякий случай чистим ответ)
            match = re.search(r'\d+\.\d+(\.\d+)?', response.text)
            return match.group(0) if match else "10.1"
    except Exception as e:
        print(f"⚠️ Ошибка классификации: {e}")
    
    return "10.1"

async def main():
    print(f"📖 Читаю {INPUT_FILE}...")
    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"❌ Файл {INPUT_FILE} не найден! Создай его и вставь туда текст.")
        return

    # 1. Разбиваем текст по номерам сообщений.
    # Ищем строки, где есть только цифры (это ID), и используем их как разделитель.
    # Регулярка ищет: Перенос строки -> Число (ID) -> Перенос строки
    # parts будет списком: [пусто, ID_1, Текст_1, ID_2, Текст_2...]
    parts = re.split(r'\n(\d{3,})\n', "\n" + content)
    
    count = 0
    # timeout=30 заставляет скрипт ждать (а не падать), если база занята Дистиллятором
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        print("🚀 Начинаю импорт в Базу Знаний...")
        
        # Проходим по списку с шагом 2 (ID, Текст)
        for i in range(1, len(parts), 2):
            msg_id = parts[i].strip()
            body = parts[i+1].strip()
            
            if not body: continue

            # Формируем красивый заголовок из первой строки текста
            first_line = body.split('\n')[0]
            # Убираем лишние слова типа "Это детальный разбор..." если они в начале
            title_candidate = body.split('\n')[1] if len(body.split('\n')) > 1 else first_line
            
            print(f"🔄 Обработка MSG_{msg_id}...", end=" ")
            
            # Определяем категорию
            cat_code = await get_category(body)
            
            # Добавляем красивую шапку, чтобы в боте это выделялось
            final_content = f"🎥 <b>[ВИДЕО-ПРОТОКОЛ | MSG {msg_id}]</b>\n\n{body}"
            
            # Вставляем в базу
            await db.execute('''
                INSERT INTO distilled_facts 
                (category_code, content, source_ids, is_case, confidence, processed_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                cat_code, 
                final_content, 
                msg_id, 
                1,   # Это кейс (True)
                100, # Максимальное доверие (100%)
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            ))
            print(f"✅ ОК -> Категория {cat_code}")
            count += 1
        
        await db.commit()
    
    print("-" * 40)
    print(f"🏁 Импорт завершен! Добавлено {count} экспертных протоколов.")
    print("Теперь они доступны в Базе Знаний с высшим приоритетом.")

if __name__ == '__main__':
    asyncio.run(main())