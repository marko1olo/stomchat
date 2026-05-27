import logging
from telethon import TelegramClient, events
import config
import runtime_guard

runtime_guard.configure_logging()
logger = logging.getLogger(__name__)

import vision
import os
import asyncio
import json
import database
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import cv2
import numpy as np
import summarizer
try:
    import psutil
except Exception:
    psutil = None
MY_ID = 7716348189
HEALTH_CHECK_INTERVAL_SECONDS = 300
HEALTH_FAILURE_LIMIT = 3
SCHEDULER_STATE_PATH = "bot_state.json"
SUMMARY_STATUS_CHECK_SECONDS = 60
SUMMARY_STALE_SECONDS = 1800
START_TIMEOUT_SECONDS = 120
SYNC_HISTORY_TIMEOUT_SECONDS = 300
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 60
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 120
MEDIA_ANALYSIS_TIMEOUT_SECONDS = 180

async def get_my_id():
    global MY_ID
    me = await client.get_me()
    MY_ID = me.id
    # Меняем на числовой ID только если в конфиге реально написано 'me'
    if str(config.REPORT_CHAT_ID).lower() == 'me':
        config.REPORT_CHAT_ID = MY_ID
        logger.info(f"✅ Отчеты будут слаться в личку (ID: {MY_ID})")
    else:
        # Если там число (ID группы), преобразуем в int для надежности
        config.REPORT_CHAT_ID = int(config.REPORT_CHAT_ID)
        logger.info(f"✅ Отчеты будут слаться в группу: {config.REPORT_CHAT_ID}")
last_summary_time = datetime.now()

def parse_state_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None

def load_scheduler_state_raw():
    try:
        with open(SCHEDULER_STATE_PATH, "r", encoding="utf-8") as state_file:
            return json.load(state_file)
    except (OSError, json.JSONDecodeError):
        return {}

def load_scheduler_state():
    state = load_scheduler_state_raw()
    if not state:
        return None, None

    return (
        parse_state_date(state.get("last_daily_date")),
        parse_state_date(state.get("last_weekly_date")),
    )

def save_scheduler_state(last_daily_date, last_weekly_date, deliveries=None):
    if deliveries is None:
        deliveries = load_scheduler_state_raw().get("deliveries", {})
    state = {
        "last_daily_date": last_daily_date.isoformat() if last_daily_date else None,
        "last_weekly_date": last_weekly_date.isoformat() if last_weekly_date else None,
        "deliveries": deliveries,
    }
    temp_path = SCHEDULER_STATE_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file, ensure_ascii=False, indent=2)
    os.replace(temp_path, SCHEDULER_STATE_PATH)

def target_delivery_key(chat_id, topic_id):
    topic = "main" if topic_id is None else str(topic_id)
    return f"{chat_id}:{topic}"

def delivery_bucket(report_kind, report_date):
    return f"{report_kind}:{report_date.isoformat()}"

def load_sent_targets(report_kind, report_date):
    deliveries = load_scheduler_state_raw().get("deliveries", {})
    bucket = deliveries.get(delivery_bucket(report_kind, report_date), {})
    if not isinstance(bucket, dict):
        return set()
    return {target_key for target_key, value in bucket.items() if value}

def mark_target_delivered(report_kind, report_date, target_key, last_daily_date, last_weekly_date, message_id=None):
    state = load_scheduler_state_raw()
    deliveries = state.get("deliveries", {})
    if not isinstance(deliveries, dict):
        deliveries = {}
    bucket_name = delivery_bucket(report_kind, report_date)
    bucket = deliveries.setdefault(bucket_name, {})
    bucket[target_key] = {
        "delivered_at": datetime.now().isoformat(timespec="seconds"),
        "message_id": message_id,
    }
    save_scheduler_state(last_daily_date, last_weekly_date, deliveries)

def parse_status_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None

async def runtime_telemetry_task():
    while True:
        try:
            runtime_guard.write_heartbeat("runtime_telemetry")
            if psutil is None:
                logger.info("runtime_memory psutil_unavailable")
            else:
                process = psutil.Process(os.getpid())
                info = process.memory_info()
                try:
                    full_info = process.memory_full_info()
                except Exception:
                    full_info = info
                private_bytes = (
                    getattr(full_info, "private", None)
                    or getattr(full_info, "uss", None)
                    or getattr(info, "rss", 0)
                )
                try:
                    open_files = len(process.open_files())
                except Exception:
                    open_files = -1
                logger.info(
                    "runtime_memory pid=%s rss_mb=%.2f private_mb=%.2f vms_mb=%.2f threads=%s open_files=%s",
                    os.getpid(),
                    getattr(info, "rss", 0) / 1024 / 1024,
                    private_bytes / 1024 / 1024,
                    getattr(info, "vms", 0) / 1024 / 1024,
                    process.num_threads(),
                    open_files,
                )
        except Exception as exc:
            logger.warning("runtime_memory_error %s", exc)
        await asyncio.sleep(900)

async def heartbeat_task():
    while True:
        runtime_guard.write_heartbeat("heartbeat")
        await asyncio.sleep(runtime_guard.HEARTBEAT_INTERVAL_SECONDS)

async def summary_watchdog_task():
    while True:
        await asyncio.sleep(SUMMARY_STATUS_CHECK_SECONDS)
        try:
            status = runtime_guard.read_summary_status()
            if not status.get("active"):
                continue

            updated_at = parse_status_utc(status.get("utc"))
            if not updated_at:
                continue

            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if age <= SUMMARY_STALE_SECONDS:
                continue

            logger.error(
                "summary watchdog forcing restart: stage=%s kind=%s chat=%s age=%.1fs status=%s",
                status.get("stage"),
                status.get("kind"),
                status.get("chat_id"),
                age,
                status,
            )
            runtime_guard.dump_runtime_state("summary_watchdog_stale")
            os._exit(79)
        except Exception:
            logger.exception("summary watchdog failed")

async def scheduler_task(bot_client):
    """Рассылка по всем целям из конфига."""
    # Загружаем цели из конфига (они должны быть в формате JSON списка в .env)
    # Пример в .env: REPORT_TARGETS=[{"chat_id": -100123, "topic_id": null}, {"chat_id": -100456, "topic_id": 390}]
    try:
        targets = config.REPORT_TARGETS
        if not isinstance(targets, list):
            targets = []
    except:
        targets = []

    logger.info(f"📅 Планировщик активен. Целей: {len(targets)}")
    last_sent_date, last_weekly_date = load_scheduler_state()

    while True:
        try:
            now = datetime.now()
            
            # 1. ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ (Daily)
            # Проверка времени (REPORT_HOUR) и того, что сегодня еще не отправляли
            if now.hour >= config.REPORT_HOUR and last_sent_date != now.date():
                
                # Окно 26 часов
                end_time = now
                start_time = (now - timedelta(days=1)).replace(hour=20, minute=0, second=0)
                
                messages = await asyncio.wait_for(
                    database.get_messages_for_daily_summary(start_time, end_time, min_count=100),
                    timeout=30,
                )
                
                if messages:
                    logger.info(f"🔥 Daily контент готов ({len(messages)} шт). Рассылка...")
                    
                    # Кэш для текста (чтобы генерировать 1 раз на все чаты)
                    generated_cache = None
                    sent_targets = load_sent_targets("daily", now.date())
                    target_keys = [
                        target_delivery_key(target.get('chat_id'), target.get('topic_id'))
                        for target in targets
                        if target.get('chat_id')
                    ]

                    # Проходим по всем целям
                    for target in targets:
                        tgt_chat = target.get('chat_id')
                        tgt_topic = target.get('topic_id')
                        
                        if not tgt_chat: continue
                        tgt_key = target_delivery_key(tgt_chat, tgt_topic)
                        if tgt_key in sent_targets:
                            logger.info("Daily target already delivered; skip duplicate target=%s", tgt_key)
                            continue
                        
                        try:
                            logger.info(f"📤 Отправка Daily в {tgt_chat} (Topic: {tgt_topic})...")

                            async def daily_delivery_hook(sent_message, target_key=tgt_key):
                                sent_targets.add(target_key)
                                mark_target_delivered(
                                    "daily",
                                    now.date(),
                                    target_key,
                                    last_sent_date,
                                    last_weekly_date,
                                    getattr(sent_message, "id", None),
                                )

                            # Передаем кэш и сохраняем результат
                            result_text = await summarizer.process_summary_batch(
                                messages,
                                bot_client,
                                chat_id=tgt_chat,
                                topic_id=tgt_topic,
                                msg_count=len(messages),
                                cached_message=generated_cache,
                                delivery_hook=daily_delivery_hook,
                            )
                            
                            # Если генерация прошла успешно, запоминаем текст для следующих кругов
                            if result_text:
                                if tgt_key not in sent_targets:
                                    sent_targets.add(tgt_key)
                                    mark_target_delivered("daily", now.date(), tgt_key, last_sent_date, last_weekly_date)
                                if not generated_cache:
                                    generated_cache = result_text
                                
                        except Exception:
                            logger.exception(f"Daily send failed chat={tgt_chat}")
                    
                    if target_keys and all(target_key in sent_targets for target_key in target_keys):
                        # Помечаем сообщения прочитанными 1 раз после всех рассылок
                        msg_ids = [m[0] for m in messages]
                        await asyncio.wait_for(database.mark_messages_as_summarized(msg_ids), timeout=30)
                        
                        last_sent_date = now.date()
                        save_scheduler_state(last_sent_date, last_weekly_date)
                        logger.info("✅ Ежедневная рассылка завершена.")
                    else:
                        missing_targets = [target_key for target_key in target_keys if target_key not in sent_targets]
                        logger.error("Daily was not delivered to all targets; missing=%s messages remain unsummarized.", missing_targets)

            # 2. ЕЖЕНЕДЕЛЬНАЯ ГАЗЕТА (Weekly)
            # Запуск: Понедельник (weekday == 0), 10:00 утра
            if now.weekday() == 0 and now.hour >= 10 and last_weekly_date != now.date():
                logger.info("🗞 Наступило время Weekly отчета (Понедельник, 10:00)...")
                
                # Период: последние 7 полных дней
                end_weekly = now
                start_weekly = now - timedelta(days=7)
                
                # Получаем сообщения за диапазон
                weekly_messages = await asyncio.wait_for(
                    database.get_messages_for_range(start_weekly, end_weekly),
                    timeout=30,
                )
                
                if weekly_messages:
                    logger.info(f"💎 Weekly контент готов ({len(weekly_messages)} шт). Рассылка...")
                    weekly_sent_targets = load_sent_targets("weekly", now.date())
                    weekly_target_keys = [
                        target_delivery_key(target.get('chat_id'), target.get('topic_id'))
                        for target in targets
                        if target.get('chat_id')
                    ]
                     
                    for target in targets:
                        tgt_chat = target.get('chat_id')
                        tgt_topic = target.get('topic_id')
                        
                        if not tgt_chat: continue
                        tgt_key = target_delivery_key(tgt_chat, tgt_topic)
                        if tgt_key in weekly_sent_targets:
                            logger.info("Weekly target already delivered; skip duplicate target=%s", tgt_key)
                            continue
                         
                        try:
                            logger.info(f"📤 Отправка Weekly в {tgt_chat} (Topic: {tgt_topic})...")

                            async def weekly_delivery_hook(sent_message, target_key=tgt_key):
                                weekly_sent_targets.add(target_key)
                                mark_target_delivered(
                                    "weekly",
                                    now.date(),
                                    target_key,
                                    last_sent_date,
                                    last_weekly_date,
                                    getattr(sent_message, "id", None),
                                )

                            result_text = await summarizer.process_weekly_batch(
                                weekly_messages,
                                bot_client,
                                chat_id=tgt_chat,
                                topic_id=tgt_topic,
                                delivery_hook=weekly_delivery_hook,
                            )
                            if result_text:
                                if tgt_key not in weekly_sent_targets:
                                    weekly_sent_targets.add(tgt_key)
                                    mark_target_delivered("weekly", now.date(), tgt_key, last_sent_date, last_weekly_date)
                        except Exception:
                            logger.exception(f"Weekly send failed chat={tgt_chat}")
                     
                    if weekly_target_keys and all(target_key in weekly_sent_targets for target_key in weekly_target_keys):
                        last_weekly_date = now.date()
                        save_scheduler_state(last_sent_date, last_weekly_date)
                        logger.info("✅ Еженедельная рассылка (Weekly) завершена.")
                    else:
                        missing_targets = [target_key for target_key in weekly_target_keys if target_key not in weekly_sent_targets]
                        logger.error("Weekly was not delivered to all targets; missing=%s scheduler state not advanced.", missing_targets)
                
            await asyncio.sleep(600) # Проверка каждые 10 минут
        except Exception as e:
            logger.error(f"Ошибка планировщика: {e}")
            await asyncio.sleep(60)
# 1. Клиент Юзербота (Твой аккаунт) - только слушает
client = TelegramClient(
    config.SESSION_NAME,
    config.API_ID,
    config.API_HASH,
    timeout=30,
    request_retries=10,
    connection_retries=1000,
    retry_delay=5,
    auto_reconnect=True,
    catch_up=True,
)

# 2. Клиент Бота - только пишет и крепит
bot_client = TelegramClient(
    'bot_session',
    config.API_ID,
    config.API_HASH,
    timeout=30,
    request_retries=10,
    connection_retries=1000,
    retry_delay=5,
    auto_reconnect=True,
)
@client.on(events.NewMessage(chats=config.SOURCE_CHAT_ID))
async def handle_new_message(event):
    """Обработчик новых сообщений в целевом чате."""
    try:
        msg_id = event.message.id
        sender_id = event.sender_id
        sender = None
        try:
            sender = await asyncio.wait_for(
                event.get_sender(),
                timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("sender lookup failed msg_id=%s sender_id=%s: %s", msg_id, sender_id, exc)

        # Сбор расширенных данных об авторе (с обработкой анонимных админов)
        if sender is None:
            sender_name = "Unknown"
            sender_username = None
        elif hasattr(sender, 'first_name'):
            # Это обычный пользователь
            first_name = sender.first_name or ''
            last_name = sender.last_name or ''
            sender_name = f"{first_name} {last_name}".strip() or "Участник"
            sender_username = getattr(sender, 'username', None)
        elif hasattr(sender, 'title'):
            # Это сообщение от имени группы/канала
            sender_name = sender.title or "Администрация"
            sender_username = getattr(sender, 'username', None)
        else:
            sender_name = "Админ"
            sender_username = getattr(sender, 'username', None)

        text = event.message.message or ""
        date = event.message.date
        
        # Получаем ID сообщения, на которое ответили (если есть)
        reply_to_msg_id = None
        if event.message.reply_to:
            reply_to_msg_id = event.message.reply_to.reply_to_msg_id

        # Проверка медиа
        has_media = event.message.photo is not None
        media_type = "photo" if has_media else None
        media_description = None

        # Сохраняем расширенный набор данных (базовая запись)
        await asyncio.wait_for(
            database.save_message(
                msg_id=msg_id,
                reply_to_msg_id=reply_to_msg_id,
                sender_id=sender_id,
                sender_name=sender_name,
                sender_username=sender_username,
                text=text,
                date=date,
                has_media=has_media,
                media_type=media_type
            ),
            timeout=30,
        )

        # Анализ медиа (фото, видео), игнорируя стикеры/гифки
        if event.photo or event.video:
            file_to_analyze = None
            is_video = False
            temp_path = None # Инициализируем переменную
            
            try:
                # 1. СКАЧИВАНИЕ
                temp_path = await asyncio.wait_for(
                    event.download_media(),
                    timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                )
                
                # 2. ПОДГОТОВКА ФАЙЛА ДЛЯ АНАЛИЗА
                if event.photo:
                    file_to_analyze = temp_path
                elif event.video:
                    is_video = True
                    logger.info(f"🎞️ Извлечение первого кадра из видео {event.id}...")
                    loop = asyncio.get_running_loop() # Получаем loop здесь
                    file_to_analyze = await asyncio.wait_for(
                        loop.run_in_executor(None, extract_first_frame, temp_path),
                        timeout=60,
                    )
                
                # 3. АНАЛИЗ (если есть что анализировать)
                if file_to_analyze:
                    logger.info(f"📸 Анализ медиа в сообщении {msg_id}...")
                    media_description = await asyncio.wait_for(
                        vision.describe_image(file_to_analyze, caption=text),
                        timeout=MEDIA_ANALYSIS_TIMEOUT_SECONDS,
                    )
                    await asyncio.wait_for(
                        database.update_media_description(msg_id, media_description),
                        timeout=30,
                    )
                    logger.info(f"📝 Описание готово: {media_description}")

            except Exception as e:
                logger.error(f"Ошибка обработки медиа {msg_id}: {e}")
            finally:
                # 4. ОЧИСТКА ВРЕМЕННЫХ ФАЙЛОВ
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
                if is_video and file_to_analyze and os.path.exists(file_to_analyze):
                    os.remove(file_to_analyze)

        # --- НАЧАЛО НОВОГО БЛОКА ЛОГИРОВАНИЯ ---
        log_msg = f"📥 [Чат: {event.chat_id}] MSG_{msg_id} от {sender_name}"
        if sender_username:
            log_msg += f" (@{sender_username})"
        if has_media:
            log_msg += f" [МЕДИА]"
        
        logger.info(log_msg)
        
        if text:
            clean_text = text.replace('\n', ' ')[:70]
            logger.info("message_text_preview msg_id=%s text=%s", msg_id, clean_text)
        if media_description:
            logger.info("message_media_preview msg_id=%s text=%s", msg_id, media_description)
        # --- КОНЕЦ НОВОГО БЛОКА ЛОГИРОВАНИЯ ---

    except Exception:
        logger.exception("message handler failed")
@client.on(events.NewMessage(pattern=r'\.dump', outgoing=True))
async def dump_handler(event):
    await event.edit("📦 <b>Начинаю тестовую выкачку истории...</b>", parse_mode='HTML')
    count = 0
    async for message in client.iter_messages(config.SOURCE_CHAT_ID, limit=500):
        # Здесь мы просто проверяем доступ
        count += 1
    await event.edit(f"✅ Успешно прочитано {count} последних сообщений. Доступ к архиву есть.")
@client.on(events.NewMessage(pattern=r'\.id', outgoing=True))
async def get_chat_id(event):
    chat_id = event.chat_id
    # Пытаемся достать ID топика
    topic_id = None
    if event.reply_to and event.reply_to.reply_to_top_id:
        topic_id = event.reply_to.reply_to_top_id
    elif event.reply_to:
        topic_id = event.reply_to.reply_to_msg_id
    
    # В Telethon для топиков часто используется просто reply_to_msg_id самого первого сообщения ветки
    # Если мы пишем просто в топик, то reply_to_msg_id сообщения, отправленного в топик, часто указывает на thread_id
    
    text = f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>"
    if topic_id:
        text += f"\n📂 <b>Topic ID:</b> <code>{topic_id}</code>"
    else:
        text += "\n(Это не топик или я не смог определить ID ветки. Попробуй ответить на любое сообщение внутри топика командой .id)"
        
    await event.edit(text, parse_mode='HTML')
@client.on(events.NewMessage(pattern=r'\.test', outgoing=True))
async def manual_test_handler(event):
    current_chat_id = event.chat_id
    
    # Ищем, какой топик назначен для этого чата в REPORT_TARGETS
    target_topic = None
    for target in config.REPORT_TARGETS:
        if target.get('chat_id') == current_chat_id:
            target_topic = target.get('topic_id')
            break

    await event.edit(f"🧪 <b>Тест кэша (Topic: {target_topic})...</b>", parse_mode='HTML')
    
    msgs = await database.get_last_n_messages(300)
    
    # 1. Генерация (передаем найденный target_topic)
    start = datetime.now()
    msg1 = await summarizer.process_summary_batch(
        msgs, bot_client, current_chat_id, 
        topic_id=target_topic, # <-- Теперь передаем топик!
        msg_count=len(msgs)
    )
    t1 = (datetime.now() - start).total_seconds()
    
    if not msg1: return

    # 2. Кэш
    start = datetime.now()
    # Меняем 'chat' на 'current_chat_id'
    msg2 = await summarizer.process_summary_batch(msgs, bot_client, current_chat_id, msg_count=len(msgs), cached_message=msg1)
    t2 = (datetime.now() - start).total_seconds()

    status = "✅ ОК" if msg1 == msg2 and t2 < 5.0 else "❌ Ошибка"
    await event.respond(f"{status}\nГенерация: {t1:.1f}с\nКэш: {t2:.3f}с\nСовпадение: {msg1 == msg2}")
@client.on(events.NewMessage(pattern=r'\.weekly', outgoing=True))
async def manual_weekly_test(event):
    """Ручной запуск Еженедельной Газеты (Тест)."""
    chat_id = event.chat_id
    
    # Пытаемся определить топик, если это супергруппа
    topic_id = None
    if event.reply_to and event.reply_to.reply_to_top_id:
        topic_id = event.reply_to.reply_to_top_id
    elif event.reply_to:
        topic_id = event.reply_to.reply_to_msg_id
    
    # 1. Визуальное уведомление
    await event.edit(f"🗞 <b>Готовлю тестовый WEEKLY за 7 дней...</b>\nTarget Chat: <code>{chat_id}</code>\nTopic ID: <code>{topic_id}</code>", parse_mode='HTML')
    
    try:
        # 2. Берем диапазон (7 дней)
        end_time = datetime.now()
        start_time = end_time - timedelta(days=7)
        
        # 3. Достаем сообщения
        messages = await database.get_messages_for_range(start_time, end_time)
        
        if not messages:
            await event.edit("❌ Сообщений за неделю не найдено (или база пуста).")
            return

        await event.edit(f"🗞 <b>Анализирую {len(messages)} сообщений...</b>\nПишу лонгрид...", parse_mode='HTML')

        # 4. Запускаем генерацию
        result = await summarizer.process_weekly_batch(messages, bot_client, chat_id, topic_id=topic_id)
        
        if result:
            try:
                await event.delete() # Удаляем служебное сообщение ".weekly"
            except:
                pass # Если нет прав на удаление, просто оставляем
        else:
            await event.edit("❌ Ошибка генерации (вернулся None). Проверь логи.")
            
    except Exception as e:
        logger.error(f"Manual Weekly Error: {e}")
        await event.edit(f"❌ Ошибка: {e}")
def extract_first_frame(video_path):
    """Извлекает первый кадр из видео и сохраняет как JPEG."""
    try:
        vid_cap = cv2.VideoCapture(video_path)
        success, image = vid_cap.read()
        vid_cap.release()
        if success:
            frame_path = video_path + ".jpg"
            cv2.imwrite(frame_path, image)
            return frame_path
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения кадра: {e}")
    return None
async def sync_history():
    """Докачивает сообщения, пропущенные во время офлайна."""
    last_id = await asyncio.wait_for(database.get_last_msg_id(), timeout=30)
    if last_id == 0:
        logger.info("🆕 База пуста, синхронизация пропущена.")
        return

    logger.info(f"🔄 Проверка пропущенных сообщений с ID {last_id}...")
    count = 0
    
    # Запрашиваем сообщения, которые ID которых больше последнего в базе
    async for message in client.iter_messages(config.SOURCE_CHAT_ID, min_id=last_id, reverse=True):
        try:
            # Используем ту же логику парсинга, что в handle_new_message
            sender = None
            try:
                sender = await asyncio.wait_for(
                    message.get_sender(),
                    timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                logger.warning("sync sender lookup failed msg_id=%s sender_id=%s: %s", message.id, message.sender_id, exc)

            if sender is None:
                sender_name = "Unknown"
                sender_username = None
            elif hasattr(sender, 'title'):
                sender_name = sender.title or "Администрация"
                sender_username = getattr(sender, 'username', None)
            else:
                first_name = getattr(sender, 'first_name', '') or ''
                last_name = getattr(sender, 'last_name', '') or ''
                sender_name = f"{first_name} {last_name}".strip() or "Unknown"
                sender_username = getattr(sender, 'username', None)
            
            reply_to_id = message.reply_to.reply_to_msg_id if message.reply_to else None
            
            await asyncio.wait_for(
                database.save_message(
                    msg_id=message.id,
                    reply_to_msg_id=reply_to_id,
                    sender_id=message.sender_id,
                    sender_name=sender_name,
                    sender_username=sender_username,
                    text=message.message or "",
                    date=message.date,
                    has_media=message.photo is not None,
                    media_type="photo" if message.photo else None
                ),
                timeout=30,
            )
            count += 1
            if count % 25 == 0:
                runtime_guard.write_heartbeat("sync_history")
        except Exception as e:
            logger.error(f"Ошибка синхронизации сообщения {message.id}: {e}")
    
    if count > 0:
        logger.info(f"✅ Синхронизация завершена. Докачано {count} сообщений.")
    else:
        logger.info("✅ Пропущенных сообщений не обнаружено.")

async def health_watchdog_task():
    """Контролирует, что Telethon реально получает новые сообщения, а не просто живит процесс."""
    failure_count = 0

    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

        try:
            if not client.is_connected():
                raise ConnectionError("Telethon user client disconnected")

            if config.SOURCE_CHAT_ID is None:
                logger.warning("health_check пропущен: SOURCE_CHAT_ID не задан")
                failure_count = 0
                continue

            latest_messages = await asyncio.wait_for(
                client.get_messages(config.SOURCE_CHAT_ID, limit=1),
                timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )
            if latest_messages:
                remote_id = latest_messages[0].id
                local_id = await asyncio.wait_for(database.get_last_msg_id(), timeout=30)

                if remote_id > local_id:
                    logger.warning(
                        "health_check нашел пропущенные сообщения: remote=%s local=%s. Запускаю sync_history.",
                        remote_id,
                        local_id,
                    )
                    await asyncio.wait_for(sync_history(), timeout=SYNC_HISTORY_TIMEOUT_SECONDS)

            failure_count = 0
        except Exception as exc:
            failure_count += 1
            logger.error(
                "health_check failed %s/%s: %s",
                failure_count,
                HEALTH_FAILURE_LIMIT,
                exc,
            )

            if failure_count >= HEALTH_FAILURE_LIMIT:
                logger.error("health_check forcing restart: отключаю client, start.bat перезапустит процесс")
                runtime_guard.dump_runtime_state("health_check_failure_limit")
                await bot_client.disconnect()
                await client.disconnect()
                return

# --- ОБНОВЛЕННЫЙ START_BOT ---
async def start_bot():
    """Запуск бота и инициализация всех систем."""
    runtime_guard.start_watchdog()
    runtime_guard.write_heartbeat("start_bot")
    runtime_guard.clear_summary_status("startup")
    logger.info("🚀 Инициализация базы данных...")
    await asyncio.wait_for(database.init_db(), timeout=30)
    
    logger.info("🔗 Подключение к Telegram...")
    await asyncio.wait_for(client.start(), timeout=START_TIMEOUT_SECONDS)
    logger.info("🤖 Подключение bot client...")
    try:
        await asyncio.wait_for(bot_client.start(bot_token=config.BOT_TOKEN), timeout=START_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("❌ Bot client не подключился. Выход для перезапуска через start.bat.")
        await client.disconnect()
        raise
    await asyncio.wait_for(get_my_id(), timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS)
    # СИНХРОНИЗАЦИЯ ПЕРЕД ЗАПУСКОМ СЛУШАТЕЛЯ
    await asyncio.wait_for(sync_history(), timeout=SYNC_HISTORY_TIMEOUT_SECONDS)
    
    runtime_guard.create_task(heartbeat_task(), "heartbeat")
    runtime_guard.create_task(scheduler_task(bot_client), "scheduler")
    runtime_guard.create_task(runtime_telemetry_task(), "runtime_telemetry")
    runtime_guard.create_task(summary_watchdog_task(), "summary_watchdog")
    runtime_guard.create_task(health_watchdog_task(), "health_watchdog")
    logger.info("bot started, history synchronized, chat listener active")
    try:
        await client.run_until_disconnected()
    finally:
        runtime_guard.stop_watchdog()
if __name__ == '__main__':
    try:
        client.loop.run_until_complete(start_bot())
    except KeyboardInterrupt:
        logger.info("bot stopped by user")
    except Exception:
        logger.exception("fatal bot crash")
        runtime_guard.dump_runtime_state("fatal_bot_crash")
        raise
