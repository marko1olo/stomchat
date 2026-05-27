import asyncio
import aiosqlite
import json
import logging
import re
import random
import httpx
import config
from datetime import datetime
import gemini_knowledge

# === НАСТРОЙКА ЛОГИРОВАНИЯ ===
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("distiller.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# === КОНФИГУРАЦИЯ ===
ARCHIVE_DB = "stomat_archive.db"
WIKI_DB = "stomat_wiki.db"

# === УЛЬТИМАТИВНОЕ ДЕРЕВО ЗНАНИЙ 4.0 ===
KNOWLEDGE_TREE = """
1. ТЕРАПИЯ (Therapy)
   1.1. Эндодонтия (1.1.1 Доступ/Поиск/МБ2, 1.1.2 Инструментация/Файлы, 1.1.3 Ирригация/Активация, 1.1.4 Обтурация/Биокерамика, 1.1.5 Перелечивание/Ступеньки)
   1.2. Реставрация (1.2.1 Адгезивные протоколы/IDS, 1.2.2 Морфология/Бугры/Фронт, 1.2.3 Инструментарий/Матрицы, 1.2.4 Полировка/Финиш)
   1.3. Профилактика и Гигиена (1.3.1 Профгигиена/GBT, 1.3.2 Отбеливание/Чувствительность)
2. ОРТОПЕДИЯ (Orthopedics)
   2.1. Конструкции (2.1.1 Виниры, 2.1.2 Накладки/Вкладки, 2.1.3 Коронки/Мосты, 2.1.4 Съемное протезирование)
   2.2. Техника (2.2.1 Препарирование/Уступы/Вертипреп, 2.2.2 Ретракция/Оттиски, 2.2.3 Фиксация)
   2.3. Гнатология (2.3.1 ВНЧС/Мышцы/Диагностика, 2.3.2 Сплинт-терапия/Каппы)
3. ХИРУРГИЯ И ИМПЛАНТАЦИЯ (Surgery)
   3.1. Амбулаторная хирургия (3.1.1 Удаление/Восьмерки, 3.1.2 Пародонтология/Пластика десны/ССТ/ФДМ)
   3.2. Имплантация (3.2.1 Планирование/Шаблоны, 3.2.2 Протоколы/Нагрузка, 3.2.3 Осложнения/Периимплантит)
   3.3. Костная пластика (3.3.1 Синус-лифтинг, 3.3.2 НКР/Мембраны/Пины)
4. ОРТОДОНТИЯ (Orthodontics)
   4.1. Аппаратурное лечение (4.1.1 Брекеты/Механика, 4.1.2 Элайнеры/Аттачменты)
   4.2. Диагностика (4.2.1 ТРГ/Расчеты/Фотометрия)
5. ЦИФРОВЫЕ ТЕХНОЛОГИИ (Digital)
   5.1. Сканирование и Фото (5.1.1 Интраоральные сканеры, 5.1.2 Фотопротокол)
   5.2. CAD/CAM (5.2.1 Моделирование/Exocad, 5.2.2 3D Печать/Фрезеровка/Спекание)
   5.3. Оборудование (5.3.1 Оптика/Микроскопы, 5.3.2 Наконечники/Моторы/Лазеры)
6. ДИАГНОСТИКА И ОБЩАЯ МЕДИЦИНА
   6.1. Рентгенология (6.1.1 Анализ КЛКТ/ОПТГ)
   6.2. Фармакология (6.2.1 Анестезия/Антибиотики/НПВС, 6.2.2 Общий статус/Аллергии)
7. МЕНЕДЖМЕНТ И ПРАВО (7.1 Экономика/Зарплаты, 7.2 Юридическое/Медкарта/ИДС, 7.3 Психология/Конфликты)
8. ДЕТСКАЯ СТОМАТОЛОГИЯ (8.1 Прием детей/Коронки/Седация)
9. МАТЕРИАЛОВЕДЕНИЕ (9.1 Обзоры брендов/Честные отзывы)
10. UNCLASSIFIED (10.1 Прочее/Юмор/Кулуары)
"""

async def init_wiki_db():
    async with aiosqlite.connect(WIKI_DB) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS distilled_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_code TEXT,
                content TEXT,
                source_ids TEXT,
                media_links TEXT,
                is_case BOOLEAN,
                confidence INTEGER,
                processed_at TIMESTAMP
            )
        ''')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_cat ON distilled_facts(category_code)')
        await db.commit()

async def call_groq_llama(prompt):
    keys = config.GROQ_KEYS 
    random.shuffle(keys)
    
    for api_key in keys:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {
                                "role": "system", 
                                "content": (
                                    "Ты — эксперт-стоматолог с академическим бэкграундом. Твоя задача: переводить хаотичные диалоги врачей в структурированную базу профессиональных знаний.\n"
                                    "ПРАВИЛА ГЛУБИНЫ:\n"
                                    "1. НЕТ ВОДЕ: Вырезай любые вводные слова ('важно', 'нужно', 'следует'). Начинай сразу с сути.\n"
                                    "2. МАКСИМУМ ДЕТАЛЕЙ: Если врач упомянул бренд, модель инструмента, конкретный торк, концентрацию раствора или фамилию автора методики — эти данные ОБЯЗАНЫ быть в факте.\n"
                                    "3. СИНТЕЗ ТЕХНОЛОГИИ: Если обсуждение метода идет в 5 сообщениях, собери их в один плотный абзац, описывающий полный процесс. Не дроби связанные мысли.\n"
                                    "4. КЛИНИЧЕСКИЙ КОНТЕКСТ: Описывай не просто действие, а условие. Не 'гнуть файл', а 'при прохождении ступеньки в МБ2 гнуть файл 'X' на 30 градусов'.\n"
                                    f"Используй ТОЛЬКО КРАТКИЕ ЦИФРОВЫЕ КОДЫ из этого дерева: {KNOWLEDGE_TREE}. Тебе ЗАПРЕЩЕНО придумывать новые индексы."
                                )
                            },
                            {"role": "user", "content": prompt}
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.0
                    }
                )
                if response.status_code == 200:
                    return response.json()['choices'][0]['message']['content']
                elif response.status_code == 429:
                    continue
        except Exception as e:
            logger.error(f"Ошибка Groq: {e}")
            continue
    return None
def clean_json_string(text):
    """Очищает строку от мусора нейронки (маркеры кода, пояснения) для парсинга JSON."""
    if not text: return ""
    # Удаляем блоки кода ```json ... ```
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```', '', text)
    # Ищем первый символ { и последний }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        return text[start:end+1]
    return text
async def process_batch(messages):
    formatted_msgs = ""
    for m in messages:
        # m = (id, date, name, text, vision_desc, media_url)
        img_info = f" [ИЗОБРАЖЕНИЕ: {m[4]}]" if m[4] else ""
        formatted_msgs += f"MSG_{m[0]} | {m[2]}: {m[3]}{img_info}\n"

    prompt = f"""
    Ты — редактор "Стоматологической Википедии". Твоя задача: переработать чат врачей в профессиональные медицинские статьи.
    
    === ТРЕБОВАНИЯ К ТЕКСТУ (ЖЕСТКО) ===
    1. НИКАКИХ ЦИТАТ: Запрещено писать "Врач сказал", "Он говорит". Пиши сухим техническим языком.
    2. НИКАКОГО ФЛУДА: Убирай мат, сленг ("засрал", "баба Зина") и личные мнения. Оставляй только суть метода.
    3. СИНТЕЗ: Объединяй диалог из 10 сообщений в ОДНУ глубокую статью-инструкцию.
    4. ТОЛЬКО КОДЫ: В поле "c" используй ТОЛЬКО существующие коды из дерева (например, 1.1.2 или 3.2.1). Создавать свои индексы (1.10.1 и т.д.) КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО.

    === ПРИМЕР КАЧЕСТВА ===
    "Методика BOPT (Biologically Oriented Preparation Technique): позволяет добиться прироста мягких тканей и коррекции зенитов за счет создания вертикального уступа без финишной линии..."

    ЛОГ СООБЩЕНИЙ:
    {formatted_msgs}

    Выдай JSON (facts: [ {{c: "код_из_дерева", f: "ТЕКСТ_СТАТЬИ", s: [ID], case: bool}} ]).
    Если полезной информации нет — верни {{"facts": []}}.
    """
    
    # Используем специализированный клиент Gemini 2.5 Pro
    loop = asyncio.get_running_loop()
    raw_res = await loop.run_in_executor(None, gemini_knowledge.generate_fact_json, prompt)
    
    if not raw_res: return []
    
    try:
        # ОБЯЗАТЕЛЬНО: Очищаем ответ от маркдауна ```json ... ```
        clean_res = clean_json_string(raw_res)
        data = json.loads(clean_res)
        return data.get("facts", [])
    except Exception as e:
        # Логирование без эмодзи для Windows
        logger.error(f"Error parsing JSON: {e} | Raw preview: {raw_res[:100]}")
        return []

async def main():
    await init_wiki_db()
    
    BATCH_SIZE = 80 
    OVERLAP = 8 # Увеличил нахлест для лучшей связки краев
    
    # СЧЕТЧИК ПРОГРЕССА
    async with aiosqlite.connect(ARCHIVE_DB) as db:
        async with db.execute('SELECT COUNT(*) FROM archive_messages WHERE is_processed_for_wiki = 0 AND (has_media = 0 OR vision_processed = 1)') as cursor:
            row = await cursor.fetchone()
            total_todo = row[0] if row else 0

    print(f"💎 Сито запущено. Всего сообщений к обработке: {total_todo}")
    processed_count = 0

    last_id = 0
    while True:
        async with aiosqlite.connect(ARCHIVE_DB, timeout=30) as db:
            cursor = await db.execute('''
                SELECT msg_id, date, sender_name, text, vision_description, media_remote_url 
                FROM archive_messages 
                WHERE is_processed_for_wiki = 0
                AND (has_media = 0 OR vision_processed = 1)
                ORDER BY msg_id ASC 
                LIMIT ?
            ''', (BATCH_SIZE,))
            rows = await cursor.fetchall()
            
            if not rows:
                print("🏁 Архив полностью обработан!")
                break
            
            # Прогресс
            processed_count += len(rows)
            progress = (processed_count / total_todo * 100) if total_todo > 0 else 100
            print(f"\n📊 ПРОГРЕСС: {progress:.2f}% ({processed_count}/{total_todo}) | Пачка: {len(rows)} шт.")
            
            # Обработка
            facts = await process_batch(rows)
            
            if facts:
                async with aiosqlite.connect(WIKI_DB, timeout=30) as wiki:
                    print("\n" + "="*60)
                    for f in facts:
                        content = f.get('f', '')
                        cat = f.get('c', '10.1')
                        
                        # === ПОЛНЫЙ ВЫВОД ФАКТА В КОНСОЛЬ ===
                        print(f"💡 [{cat}] {content}")
                        print("-" * 30)
                        
                        await wiki.execute('''
                            INSERT INTO distilled_facts (category_code, content, source_ids, media_links, is_case, confidence, processed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            cat,
                            content,
                            ",".join(map(str, f.get('s', []))),
                            ",".join(f.get('m', [])) if f.get('m') else "",
                            f.get('case', False),
                            10,
                            datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        ))
                    await wiki.commit()
                    print("="*60 + "\n")
                print(f"   ✅ Сохранено фактов: {len(facts)}")
            else:
                print("   (Пусто - нет ценной информации в пачке)")

            # Пометка прочитанными (с учетом нахлеста)
            mark_count = len(rows) - OVERLAP if len(rows) == BATCH_SIZE else len(rows)
            ids_to_mark = [r[0] for r in rows[:mark_count]]
            
            async with aiosqlite.connect(ARCHIVE_DB, timeout=30) as db_mark:
                await db_mark.executemany(
                    'UPDATE archive_messages SET is_processed_for_wiki = 1 WHERE msg_id = ?',
                    [(m_id,) for m_id in ids_to_mark]
                )
                await db_mark.commit()
            
            # ВАЖНО: Увеличиваем паузу до 15 секунд, чтобы ключи восстанавливали RPM (запросы в минуту)
            # Это уменьшит количество ошибок 429
            print("⏳ Остываем 5 сек...")
            await asyncio.sleep(5)

if __name__ == '__main__':
    asyncio.run(main())