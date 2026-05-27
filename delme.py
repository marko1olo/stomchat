from telethon import TelegramClient
import config
import asyncio

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