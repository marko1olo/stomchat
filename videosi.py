import asyncio
import aiosqlite
import re
import os
import json
import config
from google import genai
from google.genai import types
from datetime import datetime
import time

# Имя файла с твоим текстом (проверь, что он так называется!)
INPUT_FILE = "videos.txt" 
DB_PATH = "stomat_wiki.db"
MODEL_ID = "models/gemma-3-27b-it"

KNOWLEDGE_TREE = """
1. ТЕРАПИЯ
   1.1. Эндодонтия: 1.1.1 Доступ/МБ2, 1.1.2 Инструментация/Файлы, 1.1.3 Ирригация/Активация, 1.1.4 Обтурация, 1.1.5 Перелечивание, 1.1.6 Апекслокаторы.
   1.2. Реставрация: 1.2.1 Адгезивные протоколы, 1.2.2 Спиртовой протокол, 1.2.3 Морфология, 1.2.4 Матрицы/Клинья, 1.2.5 Билдап/Штифты, 1.2.6 Полировка.
   1.3. Пародонтология: 1.3.1 Профгигиена/GBT, 1.3.2 SRP/Кюретаж, 1.3.3 Отбеливание.
2. ОРТОПЕДИЯ
   2.1. Конструкции: 2.1.1 Виниры, 2.1.2 Коронки, 2.1.3 Мосты, 2.1.4 Накладки.
   2.2. Техника: 2.2.1 BOPT/Vertiprep, 2.2.2 Уступ, 2.2.3 Оттиски, 2.2.4 Ретракция, 2.2.5 Временные, 2.2.6 Фиксация/Цементы.
   2.3. Гнатология: 2.3.1 Окклюзия, 2.3.2 ВНЧС, 2.3.3 Артикуляторы.
   2.4. Съемное: 2.4.1 Полные/Акрил, 2.4.2 Бюгельные, 2.4.3 Перебазировка.
3. ХИРУРГИЯ
   3.1. Амбулаторная: 3.1.1 Удаление, 3.1.2 Резекция, 3.1.3 Зубосохраняющие.
   3.2. Имплантация: 3.2.1 Планирование, 3.2.2 Системы, 3.2.3 Мультиюниты, 3.2.4 Осложнения.
   3.3. Реконструкция: 3.3.1 Пластика десны, 3.3.2 Костная пластика.
4. ОРТОДОНТИЯ: 4.1.1 Брекеты, 4.1.2 Элайнеры, 4.1.3 Диагностика.
5. ЦИФРА: 5.1.1 Сканеры, 5.2.1 Exocad, 5.3.1 3D-печать.
6. ОБЩЕЕ: 6.1.1 Оборудование, 6.2.1 Фармакология, 6.3.1 Фотопротокол.
7. МЕНЕДЖМЕНТ: 7.1.1 Экономика, 7.2.1 Юридическое, 7.3.1 Психология.
"""

def clean_json_raw(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    return match.group(0) if match else text

async def classify_video(body):
    """Классификация с ротацией ключей."""
    prompt = f"""
    ТЫ — МЕДИЦИНСКИЙ КЛАССИФИКАТОР.
    Определи, к какой категории относится этот клинический разбор. Проанализировать текст и присвоить ему подходящие коды категорий.
    ДЕРЕВО: {KNOWLEDGE_TREE}
    ТЕКСТ: {body[:4000]}...
    ПРАВИЛА:
    1. ИСПОЛЬЗУЙ МУЛЬТИ-ТЕГИ: Если факт затрагивает несколько тем, укажи ВСЕ подходящие коды. 
    2. МАКСИМАЛЬНАЯ ТОЧНОСТЬ: Выбирай L3 коды (три цифры), если это возможно.
    3. ОГРАНИЧЕНИЕ: Не более 5 кодов на один факт.
    4. ФОРМАТ ОТВЕТА: СТРОГО JSON: {{"codes": ["X.X.X", "Y.Y.Y"]}}
    5. ТОЛЬКО ЦИФРЫ: В массив "codes" пиши ТОЛЬКО цифровой код. Названия текстом писать КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
    """
    
    # Пробуем все ключи по очереди, если один не сработал
    for api_key in config.GOOGLE_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=MODEL_ID,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.0, safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE")
        ])
            )
            if response and response.text:
                data = json.loads(clean_json_raw(response.text))
                return ", ".join(data.get("codes", ["10.1"]))
        except Exception:
            continue # Пробуем следующий ключ молча
            
    return "10.1" # Если все ключи сдохли

async def save_to_db_safe(params):
    """Безопасная запись в базу с повторными попытками (если база занята)."""
    while True:
        try:
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                await db.execute('''
                    INSERT INTO distilled_facts 
                    (category_code, content, source_ids, is_case, confidence, processed_at, is_reclassified)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                ''', params)
                await db.commit()
                return # Успех
        except aiosqlite.OperationalError:
            # Если база залочена (Locked), ждем секунду и пробуем снова
            print("   ⏳ База занята, жду...")
            await asyncio.sleep(1)

async def main():
    if not os.path.exists(INPUT_FILE):
        print(f"❌ Файл {INPUT_FILE} не найден! Создай его и вставь текст.")
        return

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # Разбиваем по номерам сообщений (ID на отдельной строке)
    parts = re.split(r'\n(\d{3,})\n', "\n" + content)
    
    print("🚀 Старт импорта ВИДЕО-САММАРИ...")
    
    count = 0
    # parts[0] пустой, дальше [ID, Текст, ID, Текст...]
    for i in range(1, len(parts), 2):
        msg_id = parts[i].strip()
        body = parts[i+1].strip()
        
        if not body: continue

        print(f"🔄 Обработка Видео MSG_{msg_id}...", end=" ")
        
        # 1. Классифицируем
        cat_code = await classify_video(body)
        
        # 2. Формируем красивый текст
        final_content = f"🎥 [ВИДЕО-ПРОТОКОЛ | MSG {msg_id}]\n\n{body}"
        
        # 3. Сохраняем (безопасно)
        await save_to_db_safe((
            cat_code, 
            final_content, 
            msg_id, 
            1,   # is_case
            100, # confidence
            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ))
        
        print(f"✅ ОК -> Категория {cat_code}")
        count += 1
        
        # Пауза, чтобы не душить API
        await asyncio.sleep(2) 
        
    print(f"\n🏁 Импорт завершен! Добавлено {count} протоколов.")

if __name__ == '__main__':
    asyncio.run(main())