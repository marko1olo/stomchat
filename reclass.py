import asyncio
import aiosqlite
import json
import os
import re
import config
from google import genai
from google.genai import types
import time

DB_PATH = "stomat_wiki.db"
MODEL_ID = "models/gemma-3-27b-it"

# === УЛЬТИМАТИВНОЕ ДЕРЕВО ЗНАНИЙ 5.0 (ПОЛНОЕ) ===
KNOWLEDGE_TREE = """
1. ТЕРАПИЯ
   1.1. Эндодонтия: 
      1.1.1 Доступ и поиск каналов (МБ2), 
      1.1.2 Инструментация и системы файлов, 
      1.1.3 Ирригация и активация растворов, 
      1.1.4 Обтурация (Гуттаперча/Биокерамика), 
      1.1.5 Перелечивание (Ретрит/Ступеньки/Обломки), 
      1.1.6 Апекслокаторы и диагностика.
   1.2. Реставрация: 
      1.2.1 Адгезивные протоколы и IDS, 
      1.2.2 Спиртовой протокол и влажность дентина, 
      1.2.3 Морфология (Бугры/Фиссуры/Эмалевое кольцо), 
      1.2.4 Матричные системы (Клинья/Кольца/Матрицы), 
      1.2.5 Билдап и Штифты (СВШ/Анкеры), 
      1.2.6 Полировка и финишная обработка.
   1.3. Пародонтология и Профилактика: 
      1.3.1 Профгигиена (GBT/AirFlow/Ультразвук), 
      1.3.2 Консервативное лечение десен (SRP/Кюретаж), 
      1.3.3 Химическое и ламповое отбеливание.
2. ОРТОПЕДИЯ
   2.1. Конструкции: 
      2.1.1 Виниры (Керамика/Композит), 
      2.1.2 Коронки (Диоксид циркония/Emax/Металлокерамика), 
      2.1.3 Мостовидные протезы и консоли, 
      2.1.4 Микропротезирование (Inlay/Onlay/Overlay).
   2.2. Техника и Протоколы: 
      2.2.1 Вертикальное препарирование (BOPT/Vertiprep), 
      2.2.2 Традиционное уступное препарирование, 
      2.2.3 Оттиски (Силиконы/Полиэфиры), 
      2.2.4 Ретракция десны и гемостаз, 
      2.2.5 Временное протезирование (ПММА/Бисакрил), 
      2.2.6 Адгезивная и цементная фиксация (Fuji/Panavia).
   2.3. Гнатология: 
      2.3.1 Окклюзия, прикус и центральное соотношение (ЦС), 
      2.3.2 ВНЧС (МРТ/Диагностика/Сплинты), 
      2.3.3 Инструментальный анализ (Артикуляторы/Лицевые дуги).
   2.4. Съемное протезирование: 
      2.4.1 Полные и частичные съемные протезы (Акрил/Нейлон), 
      2.4.2 Бюгельное протезирование и замковые крепления, 
      2.4.3 Перебазировка и починка протезов.
3. ХИРУРГИЯ
   3.1. Амбулаторная хирургия: 
      3.1.1 Удаление зубов любой сложности (Восьмерки), 
      3.1.2 Апикальная микрохирургия (Резекция/Ретроградное пломбирование), 
      3.1.3 Зубосохраняющие операции (Гемисекция/Коронковое удлинение).
   3.2. Имплантация: 
      3.2.1 Планирование, шаблоны и навигационная хирургия, 
      3.2.2 Системы имплантатов и протоколы установки, 
      3.2.3 Ортопедические компоненты (Мультиюниты/Абатменты), 
      3.2.4 Осложнения (Периимплантит/Расфиксация винтов).
   3.3. Реконструктивная хирургия: 
      3.3.1 Мукогингивальная пластика (ССТ/СДТ/Пластика десны), 
      3.3.2 Костная пластика (НКР/Синус-лифтинг/Блоки).
4. ОРТОДОНТИЯ: 
   4.1.1 Брекет-системы и механика перемещения, 
   4.1.2 Элайнеры и цифровое планирование, 
   4.1.3 Ортодонтическая диагностика (ТРГ/Фотометрия).
5. ЦИФРОВЫЕ ТЕХНОЛОГИИ: 
   5.1.1 Интраоральное сканирование, 
   5.2.1 Цифровое моделирование (Exocad/3Shape), 
   5.3.1 Производство (3D-печать/Фрезеровка).
6. ОБЩЕЕ: 
   6.1.1 Эргономика и Оборудование (Микроскопы/Бинокуляры/Свет), 
   6.2.1 Фармакология (Анестезия/Антибиотики/НПВС), 
   6.3.1 Стоматологический фотопротокол.
7. МЕНЕДЖМЕНТ: 
   7.1.1 Экономика клиники (Зарплаты/Цены/Маркетинг), 
   7.2.1 Юридическая защита и документация (Медкарты/ИДС), 
   7.3.1 Психология общения и управление конфликтами.
"""

def clean_json_raw(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    return match.group(0) if match else text

async def classify_fact(client, content, f_id):
    prompt = f"""
    ТЫ — СТОМАТОЛОГИЧЕСКИЙ ЭКСПЕРТ-АНАЛИТИК.
    Твоя задача: проанализировать текст и присвоить ему подходящие коды категорий.

    ДЕРЕВО КАТЕГОРИЙ:
    {KNOWLEDGE_TREE}

    ТЕКСТ ДЛЯ КЛАССИФИКАЦИИ:
    {content}

    ПРАВИЛА:
    1. ИСПОЛЬЗУЙ МУЛЬТИ-ТЕГИ: Если факт затрагивает несколько тем, укажи ВСЕ подходящие коды. 
    2. МАКСИМАЛЬНАЯ ТОЧНОСТЬ: Выбирай L3 коды (три цифры), если это возможно.
    3. ОГРАНИЧЕНИЕ: Не более 5 кодов на один факт.
    4. ФОРМАТ ОТВЕТА: СТРОГО JSON: {{"codes": ["X.X.X", "Y.Y.Y"]}}
    5. ТОЛЬКО ЦИФРЫ: В массив "codes" пиши ТОЛЬКО цифровой код. Названия текстом писать КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.
    """
    try:
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
            raw_codes = data.get("codes", ["10.1"])
            
            clean_codes = []
            for c in raw_codes:
                # Регулярка вырезает только паттерны типа 1.1 или 1.1.1
                match = re.search(r'(\d+\.\d+(?:\.\d+)?)', str(c))
                if match:
                    clean_codes.append(match.group(1))
            
            return ", ".join(list(set(clean_codes))) if clean_codes else "10.1"
    except Exception as e:
        err = str(e).lower()
        if "429" in err or "limit" in err or "exhausted" in err:
            return "RETRY"
        print(f"   [!] Error on ID {f_id}: {err[:100]}")
        return None
    return "10.1"

async def init_db_schema():
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("ALTER TABLE distilled_facts ADD COLUMN is_reclassified BOOLEAN DEFAULT 0")
            await db.commit()
            print("--- Database updated: is_reclassified column added ---")
        except:
            pass # Колонку уже добавляли

async def main():
    await init_db_schema()
    
    if not os.path.exists(DB_PATH):
        print("Error: stomat_wiki.db not found.")
        return

    async with aiosqlite.connect(DB_PATH, timeout=60) as db:
        cursor = await db.execute("SELECT id, content FROM distilled_facts WHERE is_reclassified = 0")
        facts = await cursor.fetchall()
        total_remaining = len(facts)
        
        print(f"--- Processing {total_remaining} facts with Gemma 3 27B ---")
        
        key_idx = 0
        idx = 0
        while idx < len(facts):
            f_id, content = facts[idx]
            current_key = config.GOOGLE_KEYS[key_idx % len(config.GOOGLE_KEYS)]
            client = genai.Client(api_key=current_key)

            new_codes = await classify_fact(client, content, f_id)
            
            if new_codes == "RETRY":
                print(f"   [!] Key {key_idx % len(config.GOOGLE_KEYS) + 1} TPM limit. Rotating...")
                key_idx += 1
                await asyncio.sleep(2)
                continue # Retry same index

            if new_codes:
                await db.execute('UPDATE distilled_facts SET category_code = ?, is_reclassified = 1 WHERE id = ?', (new_codes, f_id))
                await db.commit()
                print(f"[{idx+1}/{total_remaining}] Key:{key_idx % len(config.GOOGLE_KEYS) + 1} | ID {f_id} -> {new_codes}")
                idx += 1 
                key_idx += 1
            
            # Safe interval to respect 15k TPM limit per key
            await asyncio.sleep(1.8) 

    print("\n--- BASE RECLASSIFIED SUCCESSFULLY ---")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")