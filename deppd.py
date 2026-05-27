import asyncio
import aiosqlite
import logging
import os
import re
from telethon import TelegramClient
from telethon.errors import FloodWaitError
import config

# Настройка логгера
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

ARCHIVE_DB_PATH = "stomat_archive.db"
DUMPER_SESSION = "dumper_session"

async def init_db():
    async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS archive_messages (
                msg_id INTEGER PRIMARY KEY,
                date TIMESTAMP,
                sender_id INTEGER,
                sender_name TEXT,
                sender_username TEXT,
                text TEXT,
                reply_to_msg_id INTEGER,
                has_media BOOLEAN,
                media_type TEXT,
                media_remote_url TEXT,
                vision_description TEXT,
                vision_processed BOOLEAN DEFAULT 0,
                category_l1 TEXT,
                category_l2 TEXT,
                category_l3 TEXT,
                is_processed_for_wiki BOOLEAN DEFAULT 0
            )
        ''')
        await db.commit()

def is_garbage(message):
    """Проверяет, является ли сообщение мусором."""
    # 1. Если это сервисный системный месседж
    if not message.sender_id: return True
    
    # 2. Если это стикер, гифка или кружок (видеосообщение)
    if message.sticker or message.gif or message.video_note: return True
    
    # 3. Если текста нет и это не фото/видео/файл
    if not message.message and not message.photo and not message.video and not message.document:
        return True
    
    # 4. Если текст слишком короткий (флуд типа 'ок', 'спс', '+')
    if message.message and len(message.message.strip()) < 4 and not message.photo:
        return True
        
    return False

async def main():
    await init_db()
    client = TelegramClient(DUMPER_SESSION, config.API_ID, config.API_HASH)
    await client.start()
    
    # 1. СМОТРИМ, НА ЧЕМ ОСТАНОВИЛИСЬ В ПРОШЛЫЙ РАЗ
    last_id = 0
    async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
        async with db.execute("SELECT MAX(msg_id) FROM archive_messages") as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                last_id = row[0]

    print(f"🚀 Запуск умного дампа. Последний ID в базе: {last_id}")
    if last_id > 0:
        print(f"🔄 РЕЖИМ ДОКАЧКИ: Забираем только новые сообщения ( > {last_id})...")
    else:
        print(f"🆕 ПОЛНАЯ ВЫГРУЗКА: База пуста, качаем всё с нуля...")
    
    count = 0
    ignored = 0
    batch = []
    
    # 2. reverse=True + min_id позволяют идти ХРОНОЛОГИЧЕСКИ от старого к новому
    # Мы пропускаем всё, что уже есть (min_id=last_id)
    async for message in client.iter_messages(config.SOURCE_CHAT_ID, min_id=last_id, reverse=True):
        try:
            if is_garbage(message):
                ignored += 1
                continue

            sender = await message.get_sender()
            
            # Сбор данных об авторе (с поддержкой анонимных групп)
            if hasattr(sender, 'first_name'):
                first_name = sender.first_name or ''
                last_name = sender.last_name or ''
                sender_name = f"{first_name} {last_name}".strip() or "Участник"
            elif hasattr(sender, 'title'):
                sender_name = sender.title or "Администрация"
            else:
                sender_name = "Админ"
                
            sender_username = getattr(sender, 'username', None)
            
            reply_to = message.reply_to.reply_to_msg_id if message.reply_to else None
            
            # Определяем тип медиа
            m_type = None
            if message.photo: m_type = 'photo'
            elif message.video: m_type = 'video'
            elif message.document: m_type = 'file'

            msg_data = (
                message.id,
                message.date.strftime('%Y-%m-%d %H:%M:%S'),
                message.sender_id,
                sender_name,
                sender_username,
                message.message or "",
                reply_to,
                bool(m_type),
                m_type
            )
            batch.append(msg_data)
            count += 1
            
            if len(batch) >= 500:
                async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
                    await db.executemany('''
                        INSERT OR IGNORE INTO archive_messages 
                        (msg_id, date, sender_id, sender_name, sender_username, text, reply_to_msg_id, has_media, media_type)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', batch)
                    await db.commit()
                print(f"📥 Сохранено {count} (пропущено мусора: {ignored}) | Дата: {message.date}")
                batch = []
                await asyncio.sleep(0.2)

        except FloodWaitError as e:
            print(f"⏳ FloodWait: ждем {e.seconds} сек...")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"❌ Ошибка на MSG_{message.id}: {e}")

    # Запись остатков (теперь без ошибок)
    if batch:
        async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
            await db.executemany('''
                INSERT OR IGNORE INTO archive_messages 
                (msg_id, date, sender_id, sender_name, sender_username, text, reply_to_msg_id, has_media, media_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', batch)
            await db.commit()

    print(f"✅ Готово! Сохранено полезных: {count}, Отфильтровано мусора: {ignored}")
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())