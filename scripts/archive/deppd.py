import asyncio
import aiosqlite
import logging
import os
import re
from telethon import TelegramClient
from telethon.errors import FloodWaitError
import config
import sys

# Потоки в utf-8: в print ниже есть эмодзи, а cp1251-консоль Windows роняет на
# них сам print — инструмент умирал на первой строке, не сделав ничего. Та же
# идиома стоит в main.py; errors=replace гарантирует, что печать не бросит.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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


def media_kind(message):
    """Тип медиа для архива. Голосовое и аудио больше не сваливаются в 'file'.

    Прежде здесь стояло три ветки: photo, video, document -> 'file'. В Telethon
    ГОЛОСОВОЕ — это document с атрибутом audio(voice=True), и аудиофайл тоже
    document. То есть mime не сохранялся, и в архиве оба становились безликим
    'file'.

    Цена этого измерена по живой базе: media_type принимает ровно три значения —
    photo 14 754, video 804, file 444, а типов voice и audio НЕТ ВООБЩЕ. Значит
    среди 444 'file' лежат и документы (рентген присылают файлом — это норма для
    клинического чата), и голосовые, и аудио, и отличить их пост-фактум НЕЛЬЗЯ.
    Любая оценка потерянных диктовок по архиву поэтому ограничена сверху числом
    444, а точное число не восстановить.

    Порядок ветвей от частного к общему, как в media_tools.clinical_media_kind:
    голосовое проверяется РАНЬШЕ аудио и раньше document, иначе снова 'file'.
    Стикеры, гифки и кружки сюда не доходят — их отсекает is_garbage выше, и
    заводить им ветки значило бы делать вид, что они возможны.
    """
    if message is None:
        return None
    if getattr(message, "voice", None) is not None:
        return 'voice'
    if getattr(message, "audio", None) is not None:
        return 'audio'
    if getattr(message, "photo", None) is not None:
        return 'photo'
    if getattr(message, "video", None) is not None:
        return 'video'
    if getattr(message, "document", None) is not None:
        return 'file'
    return None

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
            
            # Определяем тип медиа. Разбор вынесен в media_kind, чтобы его можно
            # было проверить тестом: прежде три ветки стояли здесь, внутри цикла
            # по Telegram, и проверить их было нечем.
            m_type = media_kind(message)

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