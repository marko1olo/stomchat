import config
from telethon import TelegramClient, events
import asyncio
from datetime import datetime

async def main():
    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
    await client.start()

    print(f"🕵️‍♂️ Подключаюсь к чату {config.SOURCE_CHAT_ID} для анализа...")
    
    total_msgs = 0
    photos = 0
    videos = 0
    texts = 0
    first_date = None
    last_date = None

    # Итерируемся по истории (быстро, только заголовки)
    async for msg in client.iter_messages(config.SOURCE_CHAT_ID):
        total_msgs += 1
        
        if not last_date: last_date = msg.date # Самое свежее
        first_date = msg.date # Будет обновляться до самого старого
        
        if msg.photo:
            photos += 1
        elif msg.video:
            videos += 1
        elif msg.text:
            texts += 1
        
        # Визуализация прогресса каждые 1000 сообщений
        if total_msgs % 1000 == 0:
            print(f"   Просканировано: {total_msgs}...")

    print("\n" + "="*40)
    print(f"📊 ИТОГИ АНАЛИЗА ЧАТА:")
    print(f"📅 Период: с {first_date.strftime('%d.%m.%Y')} по {last_date.strftime('%d.%m.%Y')}")
    print(f"📨 Всего сообщений: {total_msgs}")
    print(f"📝 Текстовых: {texts}")
    print(f"📸 Фотографий: {photos}")
    print(f"📹 Видео: {videos}")
    print("="*40)

    # Расчет времени обработки Vision (при лимите 5 шт/мин)
    if photos > 0:
        minutes_needed = photos / 5
        hours_needed = minutes_needed / 60
        print(f"⏳ Расчетное время обработки фото (5 шт/мин): {int(minutes_needed)} мин ({hours_needed:.1f} ч)")

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())