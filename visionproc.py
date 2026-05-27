import asyncio
import aiosqlite
import logging
import os
import cv2
import httpx
import base64
import random
import shutil
from PIL import Image
from telethon import TelegramClient
import config
import vision 
from telethon.tl.types import MessageMediaWebPage

# === НАСТРОЙКИ ===
ARCHIVE_DB_PATH = "stomat_archive.db"
SESSION_NAME = "vision_session"
TEMP_DIR = "temp_media"
UPLOADED_DIR = "uploaded_media" 

# === ИНИЦИАЛИЗАЦИЯ ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)
if not os.path.exists(UPLOADED_DIR):
    os.makedirs(UPLOADED_DIR)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

def extract_frame(video_path):
    """Вырезает первый кадр из видео с помощью OpenCV."""
    try:
        vid_cap = cv2.VideoCapture(video_path)
        success, image = vid_cap.read()
        vid_cap.release()
        if success:
            frame_path = video_path + ".jpg"
            cv2.imwrite(frame_path, image)
            return frame_path
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения кадра (OpenCV): {e}")
    return None

def resize_and_save_final(source_path, dest_dir, max_size=768):
    """Ресайзит изображение до max_size и сохраняет в целевую папку."""
    try:
        with Image.open(source_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
                
            if max(img.size) > max_size:
                img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            
            # Сохраняем с оптимизацией
            final_path = os.path.join(dest_dir, os.path.basename(source_path))
            img.save(final_path, 'JPEG', quality=85, optimize=True)
            return final_path
    except Exception as e:
        logger.error(f"Ошибка ресайза для сохранения: {e}")
        # Если ресайз упал, просто копируем оригинал
        try:
            final_path = os.path.join(dest_dir, os.path.basename(source_path))
            shutil.copy(source_path, final_path)
            return final_path
        except Exception as copy_err:
            logger.error(f"Ошибка копирования файла: {copy_err}")
    return None

# Функция process_with_groq удалена в пользу vision.describe_image

# === ОСНОВНАЯ ЛОГИКА ===

async def main():
    """Главный цикл обработки медиа из архива."""
    # СБРОС ФЛАГОВ: Даем второй шанс тем, кто был помечен как обработанный, но не имеет описания
    print("🔄 Проверка и сброс зависших задач...")
    async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
        await db.execute('''
            UPDATE archive_messages 
            SET vision_processed = 0 
            WHERE media_type IN ('photo', 'video') 
            AND vision_processed = 1 
            AND (vision_description IS NULL OR vision_description = '')
        ''')
        await db.commit()
    
    client = TelegramClient(SESSION_NAME, config.API_ID, config.API_HASH)
    await client.start()
    
    print("👀 Vision-Комбайн (ЛОКАЛЬНЫЙ РЕЖИМ) запущен. Начинаю обработку медиа...")

    while True:
        file_path = None
        final_img_path = None
        
        async with aiosqlite.connect(ARCHIVE_DB_PATH) as db:
            # Берем строго те, что еще не обрабатывались (vision_processed = 0)
            cursor = await db.execute('''
                SELECT msg_id, text, media_type FROM archive_messages 
                WHERE media_type IN ('photo', 'video') AND vision_processed = 0 
                ORDER BY msg_id ASC LIMIT 1
            ''')
            row = await cursor.fetchone()
            
            if not row:
                print("✅ Все фото и видео из архива успешно обработаны!")
                break
            
            msg_id, text, m_type = row
            print(f"📸 Обработка MSG_{msg_id} ({m_type})...")

            # Ставим флаг processed = 1 заранее, но description останется NULL при ошибке
            await db.execute('UPDATE archive_messages SET vision_processed = 1 WHERE msg_id = ?', (msg_id,))
            await db.commit()

            try:
                # 1. Получаем объект сообщения с таймаутом
                msg = await asyncio.wait_for(client.get_messages(config.SOURCE_CHAT_ID, ids=msg_id), timeout=20)
                if not msg or not msg.media:
                    logger.warning(f"Медиа в MSG_{msg_id} не найдено.")
                    await db.execute('UPDATE archive_messages SET vision_description = "SKIP_EMPTY", vision_processed = 1 WHERE msg_id = ?', (msg_id,))
                    await db.commit()
                    continue

                if isinstance(msg.media, MessageMediaWebPage):
                    logger.info(f"MSG_{msg_id} это ссылка, а не файл. Пропускаю.")
                    await db.execute('UPDATE archive_messages SET vision_description = "SKIP_LINK", vision_processed = 1 WHERE msg_id = ?', (msg_id,))
                    await db.commit()
                    continue

                # 2. Скачиваем (лимит 50 МБ для уверенного захвата метаданных видео)
                if m_type == 'video':
                    temp_video_path = os.path.join(TEMP_DIR, f"{msg_id}.mp4")
                    with open(temp_video_path, 'wb') as f:
                        async for chunk in client.iter_download(msg.media, request_size=1024*1024, limit=50):
                            f.write(chunk)
                    file_path = temp_video_path
                    
                    if os.path.exists(file_path):
                        final_img_path = extract_frame(file_path)
                        
                elif m_type == 'photo':
                    file_path = await asyncio.wait_for(msg.download_media(file=os.path.join(TEMP_DIR, "")), timeout=60)
                    final_img_path = file_path
                try:
                    msg = await asyncio.wait_for(client.get_messages(config.SOURCE_CHAT_ID, ids=msg_id), timeout=30)
                except asyncio.TimeoutError:
                    logger.warning(f"Таймаут получения MSG_{msg_id}")
                    continue

                if not msg or not msg.media:
                    logger.warning(f"Медиа в MSG_{msg_id} не найдено.")
                    await db.execute('UPDATE archive_messages SET vision_description = "SKIP_EMPTY", vision_processed = 1 WHERE msg_id = ?', (msg_id,))
                    await db.commit()
                    continue

                # ФИЛЬТР ССЫЛОК (WebPage), которые Телетон видит как медиа
                if isinstance(msg.media, MessageMediaWebPage):
                    logger.info(f"MSG_{msg_id} — это ссылка, файл отсутствует. Пропускаю.")
                    await db.execute('UPDATE archive_messages SET vision_description = "SKIP_LINK", vision_processed = 1 WHERE msg_id = ?', (msg_id,))
                    await db.commit()
                    continue

                # 2. Скачивание (уже существующий у тебя код)
                if m_type == 'video':
                    temp_video_path = os.path.join(TEMP_DIR, f"{msg_id}.mp4")
                    with open(temp_video_path, 'wb') as f:
                        async for chunk in client.iter_download(msg.media, request_size=1024*1024, limit=50):
                            f.write(chunk)
                    file_path = temp_video_path
                    if os.path.exists(file_path):
                        final_img_path = extract_frame(file_path)
                elif m_type == 'photo':
                    file_path = await asyncio.wait_for(msg.download_media(file=os.path.join(TEMP_DIR, "")), timeout=60)
                    final_img_path = file_path
                
                # 3. Обрабатываем, если кадр получен
                if final_img_path and os.path.exists(final_img_path):
                    
                    # ИСПОЛЬЗУЕМ vision.describe_image (с ротацией ключей и кулдаунами)
                    description = await vision.describe_image(final_img_path, caption=text)
                    
                    if description:
                        # Сохранение в постоянную папку
                        saved_path = resize_and_save_final(final_img_path, UPLOADED_DIR, max_size=768)
                        
                        if saved_path:
                            # Запись в БД (описание и путь)
                            await db.execute('''
                                UPDATE archive_messages 
                                SET vision_description = ?, media_remote_url = ?, vision_processed = 1 
                                WHERE msg_id = ?
                            ''', (description, saved_path, msg_id))
                            await db.commit()
                            print(f"   ∟ Готово: {description[:50]}... | Сохранено: {saved_path}")
                        else:
                            logger.error(f"Ошибка сохранения финального файла для MSG_{msg_id}.")
                    else:
                        # Если описание не получено (битый файл или лимиты)
                        # Помечаем как SKIP, чтобы не застревать на этом сообщении вечно
                        print(f"   ⚠️ Описание не получено (API лимит или битый файл) для MSG_{msg_id}. Пропускаю.")
                        await db.execute('''
                            UPDATE archive_messages 
                            SET vision_description = "SKIP_UNREADABLE", vision_processed = 1 
                            WHERE msg_id = ?
                        ''', (msg_id,))
                        await db.commit()
                else:
                    logger.warning(f"Не удалось извлечь изображение из MSG_{msg_id}")
                    # Помечаем как пропущенное с ошибкой, чтобы пойти дальше
                    await db.execute('''
                        UPDATE archive_messages 
                        SET vision_description = "SKIP_ERROR", vision_processed = 1 
                        WHERE msg_id = ?
                    ''', (msg_id,))
                    await db.commit()

            except Exception as e:
                logger.error(f"❌ Ошибка MSG_{msg_id}: {e}")
                # Гарантируем продвижение даже при фатальной ошибке кода
                await db.execute('UPDATE archive_messages SET vision_processed = 1 WHERE msg_id = ?', (msg_id,))
                await db.commit()
            
            finally:
                # Очистка временных файлов
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
                if final_img_path and final_img_path != file_path and os.path.exists(final_img_path):
                    os.remove(final_img_path)
        
        # Пауза
        await asyncio.sleep(random.uniform(12, 15))

    await client.disconnect()
    print("🏁 Работа Vision-Комбайна завершена.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Процесс остановлен пользователем.")