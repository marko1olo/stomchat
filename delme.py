from telethon import TelegramClient
import config
import asyncio
import sys

# Потоки в utf-8: в print ниже есть эмодзи, а cp1251-консоль Windows роняет на
# них сам print — инструмент умирал на первой строке, не сделав ничего. Та же
# идиома стоит в main.py; errors=replace гарантирует, что печать не бросит.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


async def main():
    client = TelegramClient(config.SESSION_NAME, config.API_ID, config.API_HASH)
    await client.start()
    
    print("\n🔍 СПИСОК ТВОИХ ЧАТОВ И ИХ ID:")
    print("-" * 50)
    
    async for dialog in client.iter_dialogs(limit=80):
        # Печатаем Имя чата и его ID
        print(f"[{dialog.id}] --- {dialog.name}")
        
    print("-" * 50)
    print("Найди в списке свой 'testchat', скопируй ID (вместе с минусом) и вставь в .env")
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())