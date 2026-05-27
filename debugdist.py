import asyncio
import distiller
import aiosqlite
import json

async def test_run():
    print("🔬 ЗАПУСК ТЕСТОВОЙ ДИСТИЛЛЯЦИИ...")
    
    # 1. Берем последние 50 сообщений из архива для теста
    # (Берем те, где есть текст и потенциально готов Vision)
    async with aiosqlite.connect("stomat_archive.db") as db:
        cursor = await db.execute('''
            SELECT msg_id, date, sender_name, text, vision_description, media_remote_url 
            FROM archive_messages 
            WHERE text != '' 
            ORDER BY msg_id DESC 
            LIMIT 100
        ''')
        rows = await cursor.fetchall()
        # Переворачиваем, чтобы был правильный порядок времени
        messages = rows[::-1]

    if not messages:
        print("❌ В базе архива нет сообщений для теста.")
        return

    print(f"📥 Загружено {len(messages)} сообщений для анализа.")
    
    # 2. Вызываем основной механизм Сита
    facts = await distiller.process_batch(messages)

    # 3. Красивый вывод результата
    print("\n" + "="*100)
    print("💎 ИЗВЛЕЧЕННЫЕ ЗНАНИЯ:")
    print("="*100)

    if not facts:
        print("☹️ Нейросеть не нашла ценных клинических фактов в этой пачке.")
    else:
        for i, fact in enumerate(facts, 1):
            print(f"\n📍 ФАКТ №{i}")
            print(f"📂 Категория: {fact.get('c', '???')}")
            print(f"📝 Суть: {fact.get('f', '')}")
            print(f"🔗 Сообщения-источники: {fact.get('s', [])}")
            print(f"🏆 Кейс: {'Да' if fact.get('case') else 'Нет'}")
            print("-" * 30)

    print("\n✅ Тест завершен. База данных не изменялась.")

if __name__ == '__main__':
    asyncio.run(test_run())