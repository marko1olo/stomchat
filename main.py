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
import assistant
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import summarizer
from media_tools import extract_first_frame_async
try:
    import psutil
except Exception:
    psutil = None


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


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
MEDIA_FRAME_TIMEOUT_SECONDS = 60
MEDIA_WORKER_COUNT = max(1, _env_int("STOMCHAT_MEDIA_WORKERS", 1))
MEDIA_QUEUE_MAX_SIZE = max(MEDIA_WORKER_COUNT, _env_int("STOMCHAT_MEDIA_QUEUE_MAX", 128))
MEDIA_TEMP_DIR = os.getenv("STOMCHAT_MEDIA_TEMP_DIR", "temp_media")
MEDIA_RECOVERY_LIMIT = max(0, _env_int("STOMCHAT_MEDIA_RECOVERY_LIMIT", 5))
_media_queue = None
_media_worker_tasks = []

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

# Wrapper to track bot's own outgoing message IDs for safety wipe commands
original_send_message = bot_client.send_message
async def patched_send_message(*args, **kwargs):
    sent_msg = await original_send_message(*args, **kwargs)
    if sent_msg and hasattr(sent_msg, 'id') and hasattr(sent_msg, 'peer_id'):
        try:
            peer = sent_msg.peer_id
            chat_id = getattr(peer, 'channel_id', None) or getattr(peer, 'chat_id', None) or getattr(peer, 'user_id', None)
            if chat_id:
                if getattr(peer, 'channel_id', None):
                    chat_id = -1000000000000 - chat_id
                elif getattr(peer, 'chat_id', None):
                    chat_id = -chat_id
                import database
                await database.save_bot_sent_message(sent_msg.id, chat_id)
        except Exception as e:
            logger.error(f"Error saving bot outgoing message ID: {e}")
    return sent_msg

bot_client.send_message = patched_send_message


def start_media_analysis_workers():
    global _media_queue, _media_worker_tasks
    if _media_queue is None:
        _media_queue = asyncio.Queue(maxsize=MEDIA_QUEUE_MAX_SIZE)

    _media_worker_tasks = [task for task in _media_worker_tasks if not task.done()]
    while len(_media_worker_tasks) < MEDIA_WORKER_COUNT:
        worker_id = len(_media_worker_tasks) + 1
        _media_worker_tasks.append(
            runtime_guard.create_task(media_analysis_worker(worker_id), f"media_analysis_{worker_id}")
        )


async def stop_media_analysis_workers():
    global _media_worker_tasks
    if not _media_worker_tasks:
        return

    for task in _media_worker_tasks:
        task.cancel()
    await asyncio.gather(*_media_worker_tasks, return_exceptions=True)
    _media_worker_tasks = []


async def enqueue_media_analysis(message, msg_id, text, media_type_hint=None):
    if _media_queue is None:
        start_media_analysis_workers()

    try:
        _media_queue.put_nowait((message, msg_id, text, media_type_hint))
        logger.info("media analysis queued msg_id=%s queue_size=%s", msg_id, _media_queue.qsize())
    except asyncio.QueueFull:
        logger.error(
            "media analysis queue full; skipped msg_id=%s queue_size=%s max_size=%s",
            msg_id,
            _media_queue.qsize(),
            MEDIA_QUEUE_MAX_SIZE,
        )


async def recover_pending_media_analysis():
    if MEDIA_RECOVERY_LIMIT <= 0:
        return

    try:
        pending = await asyncio.wait_for(
            database.get_pending_media_message_ids(MEDIA_RECOVERY_LIMIT),
            timeout=30,
        )
    except Exception:
        logger.exception("pending media recovery lookup failed")
        return

    if not pending:
        return

    id_to_text = {msg_id: text for msg_id, text, _media_type in pending}
    id_to_media_type = {msg_id: media_type for msg_id, _text, media_type in pending}
    ids = list(id_to_text.keys())
    try:
        messages = await asyncio.wait_for(
            client.get_messages(config.SOURCE_CHAT_ID, ids=ids),
            timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("pending media recovery telegram fetch failed")
        return

    if not isinstance(messages, list):
        messages = [messages]

    queued = 0
    for message in messages:
        if not message:
            continue
        msg_id = message.id
        media_type_hint = id_to_media_type.get(msg_id)
        if not (message.photo or message.video or media_type_hint):
            logger.info("pending media recovery skipped msg_id=%s: telegram media missing", msg_id)
            continue
        await enqueue_media_analysis(
            message,
            msg_id,
            id_to_text.get(msg_id) or message.message or "",
            media_type_hint=media_type_hint,
        )
        queued += 1

    logger.info("pending media recovery queued=%s scanned=%s", queued, len(pending))


async def media_analysis_worker(worker_id):
    while True:
        message, msg_id, text, media_type_hint = await _media_queue.get()
        try:
            await process_media_message(message, msg_id, text, media_type_hint=media_type_hint)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("media analysis worker failed worker=%s msg_id=%s", worker_id, msg_id)
        finally:
            _media_queue.task_done()


def _remove_temp_file(path):
    if not path:
        return
    try:
        path = os.fspath(path)
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("temporary media cleanup failed path=%s: %s", path, exc)


async def process_media_message(message, msg_id, text, media_type_hint=None):
    file_to_analyze = None
    is_video = False
    media_description = None
    temp_path = None

    try:
        os.makedirs(MEDIA_TEMP_DIR, exist_ok=True)
        temp_path = await asyncio.wait_for(
            message.download_media(file=os.path.join(MEDIA_TEMP_DIR, "")),
            timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
        )

        if message.photo or media_type_hint == "photo":
            file_to_analyze = temp_path
        elif message.video or media_type_hint == "video":
            is_video = True
            logger.info(f"🎞️ Извлечение первого кадра из видео {msg_id}...")
            file_to_analyze = await extract_first_frame_async(
                temp_path,
                timeout=MEDIA_FRAME_TIMEOUT_SECONDS,
            )

        if file_to_analyze:
            logger.info(f"📸 Анализ медиа в сообщении {msg_id}...")
            media_description = await asyncio.wait_for(
                vision.describe_image(file_to_analyze, caption=text),
                timeout=MEDIA_ANALYSIS_TIMEOUT_SECONDS,
            )
            if media_description:
                await asyncio.wait_for(
                    database.update_media_description(msg_id, media_description),
                    timeout=30,
                )
                logger.info(f"📝 Описание готово: {media_description}")
                logger.info("message_media_preview msg_id=%s text=%s", msg_id, media_description)
                
                # Запуск медиа-ассистента
                async def run_media_assistant_safe():
                    try:
                        await assistant.check_and_trigger_assistant_media(
                            bot_client, message, msg_id, text, media_description
                        )
                    except Exception as e:
                        logger.exception(f"Unexpected error in run_media_assistant_safe: {e}")
                asyncio.create_task(run_media_assistant_safe())
            else:
                logger.info("media analysis returned empty description msg_id=%s, marking as processed", msg_id)
                await asyncio.wait_for(
                    database.update_media_description(msg_id, "-"),
                    timeout=30,
                )

    except asyncio.TimeoutError:
        logger.warning("media processing timeout msg_id=%s, marking as processed to avoid loop", msg_id)
        try:
            await asyncio.wait_for(
                database.update_media_description(msg_id, "-"),
                timeout=10,
            )
        except Exception:
            pass
    except Exception:
        logger.exception("Ошибка обработки медиа %s, marking as processed to avoid loop", msg_id)
        try:
            await asyncio.wait_for(
                database.update_media_description(msg_id, "-"),
                timeout=10,
            )
        except Exception:
            pass
    finally:
        _remove_temp_file(temp_path)
        if is_video and file_to_analyze != temp_path:
            _remove_temp_file(file_to_analyze)


@client.on(events.NewMessage(chats=[config.SOURCE_CHAT_ID, -1003735006121]))
async def handle_new_message(event):
    """Обработчик новых сообщений в целевом чате."""
    try:
        msg_id = event.message.id
        sender_id = event.sender_id
        
        # Игнорируем сообщения от самого бота во избежание самоциклирования
        if sender_id == 7971556097 or (assistant.BOT_ID and sender_id == assistant.BOT_ID):
            return
            
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
            sender_first_name = None
        elif hasattr(sender, 'first_name'):
            # Это обычный пользователь
            first_name = sender.first_name or ''
            last_name = sender.last_name or ''
            sender_first_name = first_name or None
            sender_name = f"{first_name} {last_name}".strip() or "Участник"
            sender_username = getattr(sender, 'username', None)
        elif hasattr(sender, 'title'):
            # Это сообщение от имени группы/канала
            sender_name = sender.title or "Администрация"
            sender_username = getattr(sender, 'username', None)
            sender_first_name = sender_name
        else:
            sender_name = "Админ"
            sender_username = getattr(sender, 'username', None)
            sender_first_name = None

        text = event.message.message or ""
        date = event.message.date

        # Group Voice Note / Audio processing
        is_voice = hasattr(event.message, "voice") and event.message.voice is not None and type(event.message.voice).__name__ != "MagicMock"
        is_audio_file = hasattr(event.message, "audio") and event.message.audio is not None and type(event.message.audio).__name__ != "MagicMock"
        is_audio = is_voice or is_audio_file
        
        if is_audio:
            os.makedirs("temp_media", exist_ok=True)
            temp_path = None
            try:
                temp_path = await event.message.download_media(file="temp_media/")
                if temp_path and os.path.exists(temp_path):
                    import blocking_tools
                    transcribed, error = await blocking_tools.transcribe_audio_async(temp_path, timeout=60)
                    if not error and transcribed:
                        raw_trans = transcribed.strip()
                        transcribed_text = await blocking_tools.correct_dental_transcription_async(raw_trans)
                        silence_hallucinations = {
                            "you", "thank you", "bye", "подпишитесь", 
                            "продолжение следует", "редактор субтитров", "субтитры", 
                            "youtube", "собачья чушь", "спасибо"
                        }
                        clean_trans = transcribed_text.lower().rstrip(".").rstrip(",")
                        if clean_trans not in silence_hallucinations:
                            text = transcribed_text
                            await bot_client.send_message(
                                entity=event.chat_id,
                                message=f"🎤 <b>[Транскрипция голосового]:</b> «{text}»",
                                reply_to=msg_id,
                                parse_mode='html'
                            )
            except Exception as audio_err:
                logger.error(f"Error handling group voice message: {audio_err}")
            finally:
                if temp_path and os.path.exists(temp_path):
                    try: os.remove(temp_path)
                    except Exception: pass
        
        # Получаем ID сообщения, на которое ответили (если есть)
        reply_to_msg_id = None
        if event.message.reply_to:
            reply_to_msg_id = event.message.reply_to.reply_to_msg_id

        # Проверка медиа
        has_media = event.message.photo is not None or event.message.video is not None
        if event.message.photo:
            media_type = "photo"
        elif event.message.video:
            media_type = "video"
        else:
            media_type = None
        media_description = None

        # Сохраняем и анализируем только для целевого (основного) чата
        if event.chat_id == config.SOURCE_CHAT_ID:
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

            # Check bookmark saving command
            cmd_clean = text.strip().lower()
            if cmd_clean in ("/save", "/сохранить", "сохранить") and reply_to_msg_id:
                try:
                    parent_msg = await event.client.get_messages(event.chat_id, ids=reply_to_msg_id)
                    if parent_msg:
                        db_desc = await database.get_media_description(reply_to_msg_id)
                        p_text = parent_msg.message or ""
                        p_has_media = parent_msg.photo is not None or parent_msg.video is not None
                        p_sender = await parent_msg.get_sender()
                        
                        if p_sender is None:
                            p_sender_name = "Unknown"
                        elif hasattr(p_sender, 'first_name'):
                            p_sender_name = f"{getattr(p_sender, 'first_name', '') or ''} {getattr(p_sender, 'last_name', '') or ''}".strip() or "Участник"
                        elif hasattr(p_sender, 'title'):
                            p_sender_name = p_sender.title or "Администрация"
                        else:
                            p_sender_name = "Админ"

                        await database.save_clinical_bookmark(
                            saved_by_user_id=sender_id,
                            msg_id=reply_to_msg_id,
                            chat_id=event.chat_id,
                            sender_name=p_sender_name,
                            text=p_text,
                            has_media=p_has_media,
                            media_description=db_desc or "",
                            date=parent_msg.date
                        )
                        
                        confirm_text = "📌 <b>Клинический пост сохранен в ваши закладки!</b>\nВы можете просмотреть и найти его в ЛС бота по команде /bookmarks."
                        await bot_client.send_message(
                            entity=event.chat_id,
                            message=confirm_text,
                            reply_to=msg_id,
                            parse_mode='html'
                        )
                except Exception as bookmark_exc:
                    logger.error(f"Failed to save clinical bookmark: {bookmark_exc}")

            # Анализ медиа (фото, видео), игнорируя стикеры/гифки
            if event.photo or event.video:
                await enqueue_media_analysis(event.message, msg_id, text)
        # Групповые команды и модерация
        async def run_group_features():
            try:
                cmd = text.strip()
                cmd_lower = cmd.lower()
                
                # 0. Экстренное удаление сообщений (админское)
                if cmd_lower in ("/wipe", "/delete", "/del", "удалить", "wipe") and reply_to_msg_id:
                    try:
                        is_super_admin = False
                        if event.sender_id in (7716348189, 1890028643):
                            is_super_admin = True
                        else:
                            permissions = await event.client.get_permissions(event.chat_id, event.sender_id)
                            if permissions.is_admin:
                                is_super_admin = True
                                
                        if is_super_admin:
                            await bot_client.delete_messages(event.chat_id, [reply_to_msg_id, msg_id])
                            import database
                            await database.remove_bot_sent_message(reply_to_msg_id)
                            return True
                    except Exception as delete_exc:
                        logger.error(f"Failed to execute inline admin delete: {delete_exc}")

                # 1. Сводка/Саммари обсуждения
                if cmd_lower.startswith(("/summary", "/итог", "/sum", "итог")):
                    await assistant.handle_group_summary(bot_client, event, reply_to_msg_id)
                    return True
                
                # 2. Прямой запрос к боту
                if cmd_lower.startswith("/ask ") or (assistant.BOT_ID and f"@{assistant.BOT_ID}" in cmd) or "@stomchat_bot" in cmd_lower:
                    question = cmd
                    if cmd_lower.startswith("/ask "):
                        question = cmd[5:].strip()
                    elif assistant.BOT_ID and f"@{assistant.BOT_ID}" in cmd:
                        question = cmd.replace(f"@{assistant.BOT_ID}", "").strip()
                    elif "@stomchat_bot" in cmd_lower:
                        import re
                        question = re.sub(r'(?i)@stomchat_bot', '', cmd).strip()
                    
                    if question:
                        await assistant.handle_group_direct_ask(bot_client, event, question)
                    return True
                
                # 3. Викторина/Опрос в группе
                if cmd_lower in ("/poll", "/кейс", "викторина", "опрос"):
                    await assistant.handle_group_quiz(bot_client, event)
                    return True
                
                # 4. Толковый словарь (объяснение терминов)
                if cmd_lower.startswith(("/what ", "/что ")):
                    term = cmd[6:].strip() if cmd_lower.startswith("/what ") else cmd[5:].strip()
                    if term:
                        await assistant.handle_term_explainer(bot_client, event, term)
                    return True

                # 5. Пассивный клинический рефери (проверка конфликтов)
                # Запускается асинхронно, не мешает стандартному ассистенту
                asyncio.create_task(assistant.check_and_trigger_referee(bot_client, event, text))
                
            except Exception as e:
                logger.exception(f"Error executing group feature: {e}")
            return False

        # Умный авто-ассистент (поддерживает как основной чат, так и диалог в тестовом топике)
        async def run_assistant_safe():
            try:
                if await run_group_features():
                    return
                await assistant.check_and_trigger_assistant(
                    bot_client, event, msg_id, text, reply_to_msg_id,
                    sender_first_name=sender_first_name
                )
                # Bot-mention trigger (always shadow mode until promoted)
                await assistant.check_bot_mention_trigger(
                    bot_client, event, msg_id, text, sender_first_name=sender_first_name
                )
            except Exception as e:
                logger.exception(f"Unexpected error in run_assistant_safe: {e}")
                
        asyncio.create_task(run_assistant_safe())
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

@bot_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def handle_private_message(event):
    """Обработчик входящих личных сообщений (ЛС) бота."""
    async def run_pm_safe():
        try:
            await assistant.handle_private_message(bot_client, event)
        except Exception as e:
            logger.exception(f"Unexpected error in PM message handler: {e}")
            
    asyncio.create_task(run_pm_safe())

@bot_client.on(events.CallbackQuery)
async def handle_callback_query(event):
    """Обработчик нажатий на инлайн-кнопки (викторины)."""
    try:
        await assistant.handle_quiz_callback(bot_client, event)
    except Exception as e:
        logger.exception(f"Unexpected error in CallbackQuery handler: {e}")

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
    path, error = __import__("media_tools")._extract_frame_sync(video_path)
    if error:
        logger.error("frame extraction failed: %s", error)
    return path
    """Извлекает первый кадр из видео и сохраняет как JPEG."""
    vid_cap = None
    try:
        vid_cap = cv2.VideoCapture(video_path)
        success, image = vid_cap.read()
        if success:
            frame_path = video_path + ".jpg"
            if cv2.imwrite(frame_path, image):
                return frame_path
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения кадра: {e}")
    finally:
        if vid_cap is not None:
            vid_cap.release()
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
    start_media_analysis_workers()
    logger.info("🤖 Подключение bot client...")
    try:
        await asyncio.wait_for(bot_client.start(bot_token=config.BOT_TOKEN), timeout=START_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("❌ Bot client не подключился. Выход для перезапуска через start.bat.")
        await stop_media_analysis_workers()
        await client.disconnect()
        raise
    await asyncio.wait_for(get_my_id(), timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS)
    logger.info("🤖 Инициализация авто-ассистента...")
    await assistant.init_assistant(bot_client)
    # СИНХРОНИЗАЦИЯ ПЕРЕД ЗАПУСКОМ СЛУШАТЕЛЯ
    await asyncio.wait_for(sync_history(), timeout=SYNC_HISTORY_TIMEOUT_SECONDS)
    await recover_pending_media_analysis()
    
    runtime_guard.create_task(heartbeat_task(), "heartbeat")
    runtime_guard.create_task(scheduler_task(bot_client), "scheduler")
    runtime_guard.create_task(runtime_telemetry_task(), "runtime_telemetry")
    runtime_guard.create_task(summary_watchdog_task(), "summary_watchdog")
    runtime_guard.create_task(health_watchdog_task(), "health_watchdog")
    logger.info("bot started, history synchronized, chat listener active")
    try:
        await client.run_until_disconnected()
    finally:
        await stop_media_analysis_workers()
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
