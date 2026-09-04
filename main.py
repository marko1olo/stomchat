import sys

# Кодировку stdio выставляем ДО первого импорта: config.py печатает статус с
# эмодзи прямо при импорте, и когда stdout не UTF-8 — запуск мимо start.bat,
# перенаправление в лог-файл, супервизор, планировщик задач — этот print
# падает с UnicodeEncodeError ещё до старта бота. В логе не остаётся ничего:
# логирование к тому моменту не настроено. Тот же класс отказа, что и с
# потерянным DENTAL_KEYWORDS — бот просто не поднимается.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import logging
from telethon import TelegramClient, events
from telethon import utils as telethon_utils
import config
import runtime_guard

runtime_guard.configure_logging()
logger = logging.getLogger(__name__)

import vision
import os
import re
import time
import asyncio
import json
from collections import deque
import database
import assistant
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import summarizer
from media_tools import (MEDIA_TEMP_DIR as media_temp_dir, clinical_media_kind,
                         extract_first_frame_async, image_document,
                         upload_clinical_image_async)
try:
    import psutil
except Exception:
    psutil = None

PROCESSED_MSG_IDS = []


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


MY_ID = 7716348189

# Числовой id бота как запасной вариант. Основной источник —
# assistant.BOT_ID, но он появляется только после init_assistant: до этого
# момента (и если get_me не прошёл) свои сообщения всё равно надо уметь
# опознавать, иначе бот отвечает на собственный дайджест.
FALLBACK_BOT_ID = _env_int("STOMCHAT_BOT_ID", 7971556097)

# Имя, по которому бота зовут в группе, пока assistant.BOT_USERNAME не
# отрезолвился. До этого литерал был единственным работающим способом:
# соседняя проверка f"@{assistant.BOT_ID}" сравнивала текст с числовым id и
# не срабатывала никогда.
FALLBACK_BOT_USERNAME = os.getenv("STOMCHAT_BOT_USERNAME", "stomchat_bot").lstrip("@").lower()

HEALTH_CHECK_INTERVAL_SECONDS = 300
HEALTH_FAILURE_LIMIT = 3
SCHEDULER_STATE_PATH = "bot_state.json"
SUMMARY_STATUS_CHECK_SECONDS = 60
# Терпение сторожа сводки обязано БЫТЬ БОЛЬШЕ самого долгого законного шага
# конвейера, иначе сторож стреляет в живую работу.
#
# Здесь стояло 1800, а генерации сводки разрешено GEMINI_GENERATION_TIMEOUT_SECONDS
# = 2100. Разбор путей: на одну попытку к провайдеру уходит timeout/3 = 700 с, и
# статус пишется ПЕРЕД каждой попыткой, поэтому при живом дочернем процессе
# разрыв между записями не превышает 700 с и сторож молчит. Но если ребёнок жив
# и молчит до первой попытки — завис на импорте или на DNS при создании
# клиента, — записей нет вовсе. Тогда на 1800 с сторож убивал процесс, хотя
# родитель отпустил бы вызов по своему таймауту на 2100 с и обработал отказ
# штатно: записал бы ошибку, снял флаг и вернул None. Вместо этого терялся
# дайджест И происходил перезапуск.
#
# Значение выведено из бюджета генерации, а не выбрано отдельным числом:
# разъехавшись, они вернут то же противоречие. Запас в 600 с покрывает
# публикацию (60 с), отправку по целям (90 с на цель) и закрепление (30 с).
# Сторож остаётся страховкой от настоящего зависания — просто перестаёт быть
# быстрее собственных таймаутов конвейера.
SUMMARY_STALE_SECONDS = summarizer.GEMINI_GENERATION_TIMEOUT_SECONDS + 600
START_TIMEOUT_SECONDS = 120
# Догоняющая синхронизация упирается в размер долга, а не в скорость сети.
# Порядок величины: по локальному снимку базы в чате около 228 сообщений в
# сутки, то есть неделя простоя — это порядка 1600 реплик, месяц — порядка 8000.
# Снимок локальный и может отставать от боевого, поэтому это оценка масштаба, а
# не факт о бое; сам вывод от неё не зависит.
#
# В прежние 300 с восемь тысяч укладывались только при 28 сообщениях в секунду.
# Не уложившись, синхронизация валила ВЕСЬ подъём: start.bat поднимал процесс
# заново, и так по кругу. Сообщения сохраняются по ходу, поэтому каждый заход
# продвигался, но снаружи это выглядит как «бот не запускается».
#
# Держать долгую синхронизацию безопасно: внутри цикла каждые 25 сообщений
# пишется heartbeat, и сторож видит процесс живым.
SYNC_HISTORY_TIMEOUT_SECONDS = 900
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 60
# Автор сообщения — необязательное обогащение: при отказе в базу пишется
# "Unknown", и таких строк уже 917. Держать здесь общий таймаут в 60 с нельзя:
# пяти зависших запросов хватало, чтобы съесть весь бюджет подъёма.
SYNC_SENDER_TIMEOUT_SECONDS = 10
MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 120
# Внешний потолок разбора снимка ОБЯЗАН вмещать внутренний бюджет каскада зрения,
# иначе резервные модели не пробуются никогда.
#
# Замер по константам vision.py и живому набору ключей: пул из трёх моделей
# перебирал ВСЕ ключи своего провайдера — 2 x 10 + 1 x 7 = 27 попыток по 33 с
# (запрос 30 плюс пауза троттлинга 3), до 891 с на один снимок. Потолок стоял
# 180 с, то есть в пять раз меньше: резервные модели каскада не пробовались
# никогда, а снимок после срабатывания внешнего таймаута получал в базе
# описание "-" — отметку «разобрано», — и второго шанса у него не было:
# get_pending_media_message_ids его больше не возвращает. Рентген коллеги
# терялся молча и навсегда.
#
# Поднимать потолок до 951 с было бы ХУЖЕ: воркер один, очередь на 128 снимков,
# и одно зависшее фото остановило бы разбор на четверть часа. Поэтому ограничен
# сам каскад — не более VISION_KEYS_PER_MODEL ключей на модель, — а потолок
# ВЫВЕДЕН из его фактического бюджета, а не задан отдельным числом: два
# независимых числа уже трижды за сессию разъезжались (test_budget_nesting.py).
# Запас 60 с — на подготовку снимка (до 45 с) и накладные.
MEDIA_ANALYSIS_TIMEOUT_SECONDS = max(
    180,
    _env_int("STOMCHAT_MEDIA_ANALYSIS_TIMEOUT_SECONDS", 0)
    or (vision.vision_cascade_budget_seconds() + 60),
)
MEDIA_FRAME_TIMEOUT_SECONDS = 60
MEDIA_WORKER_COUNT = max(1, _env_int("STOMCHAT_MEDIA_WORKERS", 1))
MEDIA_QUEUE_MAX_SIZE = max(MEDIA_WORKER_COUNT, _env_int("STOMCHAT_MEDIA_QUEUE_MAX", 128))
# Каталог общий на проект, объявлен в media_tools: его же используют
# уборщик, голосовой путь и медиа в личных сообщениях.
MEDIA_TEMP_DIR = media_temp_dir
MEDIA_RECOVERY_LIMIT = max(0, _env_int("STOMCHAT_MEDIA_RECOVERY_LIMIT", 5))
# Как часто доливать неразобранные снимки в очередь. Догон был однократным — при
# 745 накопленных и пяти за запуск это порядка 372 суток (14 перезапусков за 35
# суток по журналу), то есть практически никогда. Пятнадцать минут выбраны по
# пропускной способности: воркер один, разбор снимка десятки секунд, за такт
# уходит не больше MEDIA_RECOVERY_LIMIT — накопленное сходится за сутки-двое, а
# не за год, и очередь при этом не забивается.
MEDIA_RECOVERY_INTERVAL_SECONDS = max(60, _env_int("STOMCHAT_MEDIA_RECOVERY_INTERVAL", 900))
# Отметка «файл до Vision не дошёл». Пустое media_description означает «ещё не
# разбирали», и строка с ним возвращается из get_pending_media_message_ids на
# каждом запуске. Любое непустое значение снимает её с догона; отдельный текст
# (а не общее "-") нужен, чтобы отказ скачивания отличался в базе от отказа
# самого разбора.
MEDIA_UNAVAILABLE_MARK = "[медиа — файл не получен]"
_media_queue = None
_media_worker_tasks = []
_pending_albums = {}

# Что уже поставлено в очередь разбора В ЭТОМ процессе.
#
# Догон при старте (recover_pending_media_analysis) ищет в базе медиа с пустым
# media_description, а описание пишется только ПОСЛЕ разбора. Поэтому пять
# свежих снимков, которые sync_history поставил в очередь секунду назад, для
# догона выглядят необработанными — и он ставил ТЕ ЖЕ САМЫЕ ещё раз. Разбор
# идёт через платный Vision: на каждом рестарте до MEDIA_RECOVERY_LIMIT (5)
# снимков оплачивались дважды.
#
# Ограничение по длине как у HANDLED_CALLBACK_IDS: без него набор растёт на
# каждое медиа за всё время жизни процесса.
_QUEUED_MEDIA_IDS = set()
_QUEUED_MEDIA_ORDER = deque(maxlen=512)


def _remember_queued_media(msg_ids):
    for queued_id in msg_ids:
        if queued_id is None or queued_id in _QUEUED_MEDIA_IDS:
            continue
        if len(_QUEUED_MEDIA_ORDER) == _QUEUED_MEDIA_ORDER.maxlen:
            _QUEUED_MEDIA_IDS.discard(_QUEUED_MEDIA_ORDER[0])
        _QUEUED_MEDIA_ORDER.append(queued_id)
        _QUEUED_MEDIA_IDS.add(queued_id)


# Голосовые, которые в ЭТОМ процессе уже расшифрованы или уже отданы в
# расшифровку. Отдельно от PROCESSED_MSG_IDS: тот считает обработанные АПДЕЙТЫ, а
# здесь важно другое — одно голосовое не должно уехать в Whisper дважды. Теперь
# на него смотрят два пути (живой обработчик и догон офлайн-окна), и без общей
# отметки повторная доставка апдейта или второй проход синхронизации означали бы
# второй платный вызов И вторую «🎤 Транскрипцию» в чате.
#
# Ограничение по длине как у _QUEUED_MEDIA_IDS: без него набор растёт на каждое
# голосовое за всё время жизни процесса.
_TRANSCRIBED_MSG_IDS = set()
_TRANSCRIBED_ORDER = deque(maxlen=512)


def _remember_transcribed(msg_id):
    if msg_id is None or msg_id in _TRANSCRIBED_MSG_IDS:
        return
    if len(_TRANSCRIBED_ORDER) == _TRANSCRIBED_ORDER.maxlen:
        _TRANSCRIBED_MSG_IDS.discard(_TRANSCRIBED_ORDER[0])
    _TRANSCRIBED_ORDER.append(msg_id)
    _TRANSCRIBED_MSG_IDS.add(msg_id)


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

SCHEDULER_STATE_BAK_PATH = SCHEDULER_STATE_PATH + ".bak"

# Сколько дней хранить отметки о доставке отчётов. Корзины накапливались с
# первого запуска и не чистились никогда: на момент правки их 22 с 22 мая.
# Роста немного, но файл решает, отправлять ли дайджест, и разрастаться ему
# незачем.
SCHEDULER_DELIVERY_RETENTION_DAYS = 30


def _read_scheduler_file(path):
    try:
        with open(path, "r", encoding="utf-8") as state_file:
            data = json.load(state_file)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        # ValueError, а не только JSONDecodeError: повреждённый файл ловится и
        # раньше разбора JSON — UnicodeDecodeError летит из самого чтения, если
        # в файле оказались невалидные байты. Прежний перехват его пропускал,
        # и вместо отката на резервную копию падал весь цикл планировщика.
        return None


def load_scheduler_state_raw():
    # Этот файл — единственное, что помнит, ушёл ли сегодняшний дайджест.
    # Пустой или обрезанный файл читается как «ничего не отправляли», и отчёт
    # уходит в чат ВТОРОЙ раз, поэтому при неудаче пробуем резервную копию.
    state = _read_scheduler_file(SCHEDULER_STATE_PATH)
    if state is None:
        state = _read_scheduler_file(SCHEDULER_STATE_BAK_PATH)
        if state is not None:
            logger.warning("scheduler state unreadable, recovered from %s", SCHEDULER_STATE_BAK_PATH)
    return state or {}


def _prune_deliveries(deliveries):
    """Отметки доставки старше SCHEDULER_DELIVERY_RETENTION_DAYS не нужны."""
    if not isinstance(deliveries, dict):
        return {}
    cutoff = datetime.now().date() - timedelta(days=SCHEDULER_DELIVERY_RETENTION_DAYS)
    kept = {}
    for bucket_name, value in deliveries.items():
        bucket_date = parse_state_date(bucket_name.split(":", 1)[-1])
        if bucket_date is None or bucket_date >= cutoff:
            kept[bucket_name] = value
    return kept

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
        "deliveries": _prune_deliveries(deliveries),
    }
    temp_path = SCHEDULER_STATE_PATH + ".tmp"
    try:
        # os.replace атомарна, но без fsync содержимое временного файла может не
        # дойти до диска раньше переименования: после сбоя питания на месте
        # состояния оказывается пустой файл, «дайджест не отправлялся», и отчёт
        # уходит в чат второй раз. Та же связка, что уже стоит в
        # assistant.save_state: временный файл, fsync, замена, резервная копия.
        with open(temp_path, "w", encoding="utf-8") as state_file:
            json.dump(state, state_file, ensure_ascii=False, indent=2)
            state_file.flush()
            os.fsync(state_file.fileno())
        if os.path.exists(SCHEDULER_STATE_PATH):
            try:
                os.replace(SCHEDULER_STATE_PATH, SCHEDULER_STATE_BAK_PATH)
            except OSError as backup_err:
                logger.warning("scheduler state backup failed: %s", backup_err)
        os.replace(temp_path, SCHEDULER_STATE_PATH)
        return True
    except OSError as write_err:
        # Молча провалить запись нельзя: в памяти день уже помечен отправленным,
        # а после перезапуска отчёт уйдёт повторно.
        logger.error("SCHEDULER STATE NOT SAVED: %s — отчёт может уйти повторно", write_err)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False

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
        cleanup_temp_media()
        await asyncio.sleep(900)


TEMP_MEDIA_MAX_AGE_SECONDS = 6 * 3600


def cleanup_temp_media(max_age_seconds=TEMP_MEDIA_MAX_AGE_SECONDS):
    """
    Подметает temp_media от файлов, переживших свою обработку.

    Штатные пути уборки есть, но они не покрывают обрыв download_media по
    таймауту (файл уже создан, а путь наверх не вернулся), падение извлечения
    кадра и убийство процесса сторожем. Чистки по расписанию не было вообще:
    на момент добавления в каталоге лежало 69 файлов на 43.6 МБ, включая
    13 нулевых, самый старый — почти полугодовой давности.

    Уборка по возрасту, а не по имени: так покрываются все пути утечки сразу,
    и активная обработка не задевается — 6 часов сильно больше любого таймаута.
    """
    removed = 0
    freed = 0
    try:
        # Каталог берём из MEDIA_TEMP_DIR, а не из литерала. Переменная
        # STOMCHAT_MEDIA_TEMP_DIR соблюдалась ТОЛЬКО на скачивании: уборщик
        # подметал пустой "temp_media", а файлы копились в настроенном каталоге
        # вечно — вместе с обрывками скачиваний и голосовыми.
        if not os.path.isdir(MEDIA_TEMP_DIR):
            return 0
        cutoff = time.time() - max_age_seconds
        for name in os.listdir(MEDIA_TEMP_DIR):
            path = os.path.join(MEDIA_TEMP_DIR, name)
            try:
                if not os.path.isfile(path) or os.path.getmtime(path) > cutoff:
                    continue
                size = os.path.getsize(path)
                os.remove(path)
                removed += 1
                freed += size
            except OSError:
                # Файл может быть занят активной обработкой — заберём в следующий раз.
                continue
    except Exception as exc:
        logger.warning("temp_media_cleanup_error %s", exc)
    if removed:
        logger.info(f"temp_media cleanup: removed {removed} stale files, freed {freed/1e6:.1f} MB")
    return removed


async def heartbeat_task():
    # Единственный фоновый цикл, у которого не было try/except (остальные
    # обёрнуты). write_heartbeat делает os.replace и на Windows ловит
    # PermissionError, если файл в этот момент держит антивирус/индексатор;
    # после 5 ретраев он поднимает OSError. Таск умирал навсегда, heartbeat
    # переставал обновляться, и через WATCHDOG_STALE_SECONDS сторож убивал
    # процесс — как правило, посреди генерации саммари или анализа снимка.
    while True:
        try:
            runtime_guard.write_heartbeat("heartbeat")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Heartbeat write failed, continuing: {e}")
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

# Час, с которого начинается дневное окно. Он НЕ независим от
# config.REPORT_HOUR: пока это были две несвязанные константы, стык окон
# держался на совпадении чисел. При REPORT_HOUR=22 (текущий бой) окна
# перекрываются на 2 часа — по локальному снимку это 15.8% трафика, то есть
# вечер уходит в дайджест ДВАЖДЫ, но не теряется. А при REPORT_HOUR меньше 20
# (в config.example.py стоит 0) между выпусками возникает дыра: окно кончается в
# REPORT_HOUR, а следующее начинается только в 20:00 того же дня — до 20 часов
# переписки в сутки не попадают ни в один выпуск. daily_window_start() связывает
# их через last_sent_date, поэтому дыра закрывается при любом REPORT_HOUR.
DIGEST_WINDOW_START_HOUR = 20

# Насколько глубоко догоняем пропущенные выпуски. Без ограничения месяц простоя
# уехал бы одним запросом в промпт: по локальному снимку это порядка 8000
# сообщений против 228 в обычные сутки (худшая измеренная тройка дней — 1458).
# Ограничение делает потерю видимой в журнале вместо того, чтобы отдать
# генерации заведомо неподъёмный лог; за пределами трёх суток период всё равно
# покрывает недельный отчёт.
DIGEST_CATCHUP_MAX_DAYS = 3

WEEKLY_REPORT_HOUR = 10
WEEKLY_WINDOW_DAYS = 7
# Догон недельного отчёта тоже ограничен: 10 суток вместо 7 — это запас на
# пропущенный понедельник, а не бесконечное окно.
WEEKLY_CATCHUP_MAX_DAYS = 10
# Двух выпусков за трое суток быть не должно. Без этой границы догон, отработав
# в воскресенье, выпускал бы вторую газету в понедельник — на почти том же
# материале и с отдельной страницей Telegraph.
WEEKLY_MIN_GAP_DAYS = 3


def daily_window_start(now, last_sent_date):
    """
    Нижняя граница окна дневного дайджеста.

    Раньше она считалась только от now: «(now - 1 день) в 20:00». Пока выпуски
    идут каждый день, этого хватает, но last_sent_date хранится ровно для
    другого случая — пропущенного дня. Если вчерашний дайджест не ушёл (бот
    лежал, цель отвалилась, сообщений было меньше порога), то сегодняшнее окно
    всё равно начиналось с 20:00 вчера, и промежуток от конца последнего выпуска
    (позавчера, REPORT_HOUR) до вчерашних 20:00 — порядка 22 часов — не попадал
    НИ В ОДИН выпуск. Добор по is_summarized этого не спасает: он включается
    только когда в окне меньше min_count сообщений, и берёт лишь недостачу.

    Границы остаются наивными локальными: в UTC их переводит database._date_text,
    и подменять эту трактовку здесь нельзя.
    """
    base_start = (now - timedelta(days=1)).replace(
        hour=DIGEST_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    if not last_sent_date:
        return base_start
    # Прошлый выпуск закончился не раньше REPORT_HOUR своего дня — оттуда и
    # продолжаем. Небольшое перекрытие лучше пропуска: повтор врачи увидят,
    # потерянные сутки — нет.
    resume_from = datetime.combine(last_sent_date, datetime.min.time()).replace(
        hour=int(config.REPORT_HOUR) % 24
    )
    floor_start = now - timedelta(days=DIGEST_CATCHUP_MAX_DAYS)
    return max(floor_start, min(base_start, resume_from))


def weekly_report_due(now, last_weekly_date):
    """
    Пора ли выпускать недельный отчёт.

    Условие было только «понедельник, 10:00, сегодня ещё не отправляли».
    Пропущенный понедельник — упавший бот, недоставленная цель — терял выпуск
    НАВСЕГДА: следующий понедельник берёт окно в 7 дней и до пропущенной недели
    не достаёт. last_weekly_date хранится в том же файле состояния, что и
    дневная дата, и здесь он и нужен.
    """
    if now.hour < WEEKLY_REPORT_HOUR:
        return False
    if last_weekly_date == now.date():
        return False
    if last_weekly_date is not None and (now.date() - last_weekly_date).days < WEEKLY_MIN_GAP_DAYS:
        return False
    if now.weekday() == 0:
        return True
    # Догон: неделя с последнего выпуска прошла, а понедельника мы не увидели.
    if last_weekly_date is None:
        return False
    return (now.date() - last_weekly_date).days >= WEEKLY_WINDOW_DAYS


def weekly_window_start(now, last_weekly_date):
    """Нижняя граница недельного окна — с догоном по last_weekly_date."""
    base_start = now - timedelta(days=WEEKLY_WINDOW_DAYS)
    if not last_weekly_date:
        return base_start
    resume_from = datetime.combine(last_weekly_date, datetime.min.time()).replace(
        hour=WEEKLY_REPORT_HOUR
    )
    floor_start = now - timedelta(days=WEEKLY_CATCHUP_MAX_DAYS)
    return max(floor_start, min(base_start, resume_from))


def resolve_report_targets():
    """Цели рассылки из конфига — с громким отказом вместо молчаливого нуля.

    Прежде здесь стоял голый `except: targets = []`. Любая ошибка — и планировщик
    поднимался с нулём целей: в журнале бодрое «Планировщик активен. Целей: 0»,
    а ни один врач не получал ни дайджеста, ни недельной сводки, и узнать об этом
    было неоткуда. Ошибку формы конфиг вообще не ловит: `json.loads` на
    `REPORT_TARGETS={"chat_id": -100}` отдаёт валидный dict, а не список.
    """
    # Формат в .env: REPORT_TARGETS=[{"chat_id": -100123, "topic_id": null}, {"chat_id": -100456, "topic_id": 390}]
    try:
        raw = config.REPORT_TARGETS
    except Exception as exc:
        logger.error(
            "REPORT_TARGETS не прочитан (%s: %s) — ни дайджест, ни недельная "
            "сводка не уйдут НИКОМУ",
            type(exc).__name__, exc, exc_info=True,
        )
        return []

    if not isinstance(raw, list):
        logger.error(
            "REPORT_TARGETS не список, а %s — рассылка не уйдёт НИКОМУ. "
            'Ожидается [{"chat_id": -100123, "topic_id": null}]',
            type(raw).__name__,
        )
        return []

    targets = []
    for position, target in enumerate(raw):
        # Битая цель уносила ВСЮ рассылку: в `.test` обработчике элемент-строка
        # падал на `target.get` с AttributeError. Пропускаем ровно её, остальные
        # чаты сводку получают.
        if not isinstance(target, dict) or "chat_id" not in target:
            logger.error(
                "REPORT_TARGETS[%d] пропущен (%r): нет chat_id — этот чат "
                "сводок не получит",
                position, target,
            )
            continue
        targets.append(target)

    if raw and not targets:
        logger.error(
            "REPORT_TARGETS: все %d целей битые — рассылка не уйдёт НИКОМУ",
            len(raw),
        )
    return targets


async def scheduler_task(bot_client):
    """Рассылка по всем целям из конфига."""
    targets = resolve_report_targets()

    logger.info(f"📅 Планировщик активен. Целей: {len(targets)}")
    last_sent_date, last_weekly_date = load_scheduler_state()
    # Кэш сгенерированного дайджеста живёт МЕЖДУ кругами цикла, а не внутри
    # одного: см. комментарий на присваивании generated_cache ниже.
    daily_cache_date = None
    daily_cache_text = None

    while True:
        try:
            now = datetime.now()

            # 1. ЕЖЕДНЕВНЫЙ ДАЙДЖЕСТ (Daily)
            # Проверка времени (REPORT_HOUR) и того, что сегодня еще не отправляли
            if now.hour >= config.REPORT_HOUR and last_sent_date != now.date():

                # Окно: от конца прошлого выпуска (либо 20:00 вчера) до сейчас.
                end_time = now
                start_time = daily_window_start(now, last_sent_date)
                logger.info(
                    "Daily окно: %s -> %s (last_sent=%s)",
                    start_time, end_time, last_sent_date,
                )

                messages = await asyncio.wait_for(
                    database.get_messages_for_daily_summary(start_time, end_time, min_count=100),
                    timeout=30,
                )
                
                if messages:
                    logger.info(f"🔥 Daily контент готов ({len(messages)} шт). Рассылка...")
                    
                    # Кэш для текста (чтобы генерировать 1 раз на все чаты).
                    #
                    # Он обязан переживать круг цикла. Пока generated_cache
                    # создавался здесь заново, сломанная цель означала ПОЛНУЮ
                    # повторную генерацию каждые 10 минут: last_sent_date не
                    # продвигается, пока не доставлено во все цели, поэтому
                    # следующий заход снова шёл в генерацию — платный вызов LLM
                    # и новая страница Telegraph на каждый круг, до полуночи.
                    if daily_cache_date != now.date():
                        daily_cache_date = now.date()
                        daily_cache_text = None
                    generated_cache = daily_cache_text
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
                                    daily_cache_text = result_text

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
            # Запуск: Понедельник (weekday == 0), 10:00 утра — либо догон, если
            # понедельник был пропущен (см. weekly_report_due).
            if weekly_report_due(now, last_weekly_date):
                logger.info(
                    "🗞 Наступило время Weekly отчета (weekday=%s, last_weekly=%s)...",
                    now.weekday(), last_weekly_date,
                )

                # Период: последние 7 полных дней, с догоном по last_weekly_date
                end_weekly = now
                start_weekly = weekly_window_start(now, last_weekly_date)

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

# Потолок на один проход пингов. Своих таймаутов у этих двух вызовов не было ни
# одного: зависший send_message (Telethon держит request_retries=10 и спит на
# FloodWait) останавливал ВЕСЬ цикл пингов навсегда и совершенно молча — таск
# жив, sleep(3600) не наступает, в журнале ни строки.
#
# Значение выведено из бюджета законной работы, а не выбрано отдельно:
# MAX_PINGS_PER_CYCLE = 5 пингов, на каждый до 60 с генерации (timeout=60 в
# assistant) плюс отправка с ретраями и сном на FloodWait — порядка 600 с.
# Двойной запас даёт 1200 с. Даже если оба прохода выберут потолок целиком,
# 2400 с меньше часового сна, то есть почасовая частота пингов сохраняется.
PING_PHASE_TIMEOUT_SECONDS = 1200


async def pm_ping_scheduler_task(bot_client):
    """Задача периодической проверки неактивности пользователей в ЛС и отправки им пинга."""
    logger.info("📅 Планировщик пингов в ЛС активен.")
    while True:
        try:
            await asyncio.wait_for(
                assistant.check_and_send_pm_pings(bot_client),
                timeout=PING_PHASE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "PM pings timed out after %ss — проход прерван, цикл продолжается",
                PING_PHASE_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.error(f"Error in pm_ping_scheduler_task (PM pings): {e}")
        await asyncio.sleep(3600)  # Проверка каждый час

# Порог, после которого telethon перестаёт спать на FloodWait и поднимает
# исключение. По умолчанию он равен 60 с и НЕ задавался, а при request_retries=10
# это до 600 секунд сна ВНУТРИ одного await send_message — молча, потому что
# строка про сон идёт уровнем INFO у логгера telethon.client.users, а
# runtime_guard приглушает telethon до ERROR. Десять минут внутри одного вызова
# означают десять минут под удерживаемым замком диалога: следующее сообщение
# того же врача встаёт в очередь за ним.
#
# СВЕРЕНО С ИСХОДНИКАМИ БИБЛИОТЕКИ, не по памяти (telethon 1.42.0):
#   client/telegrambaseclient.py:254  flood_sleep_threshold: int = 60  — да, 60;
#   client/telegrambaseclient.py:249  request_retries: int = 5  — библиотечный
#     дефолт пять, десять ставим МЫ ниже при создании клиентов;
#   client/users.py:70   for attempt in retry_range(self._request_retries)
#   client/users.py:120  if e.seconds <= self.flood_sleep_threshold:
#   client/users.py:122      await asyncio.sleep(e.seconds)
# То есть сон действительно происходит ВНУТРИ одной попытки внутри одного await,
# логируется через self._log[__name__] на INFO, и повторяется до request_retries
# раз. Прежде это было записано как «верю агенту, помечаю непроверенным» —
# теперь проверено по коду. Оговорка сохраняется: «10 x (30 + 20) = 500 с» это
# ВЕРХНЯЯ ОЦЕНКА, потому что 30 с приходят из транспортного слоя, а не из этого
# цикла; сам цикл гарантирует только «до request_retries снов по порогу».
#
# Ноль здесь был бы хуже, а не лучше: FloodWait стал бы прилетать во все ~120
# вызовов assistant.py, где стоит голый except, и короткая задержка в пять секунд
# из «медленно, но доставлено» превратилась бы в «молча не доставлено». Поэтому
# порог небольшой, но не нулевой: короткое ожидание пересиживаем, длинное отдаём
# вызывающему. Полную границу по времени даёт только бюджет на вызове —
# tg_safety.guard; пока он подключён не везде, это ограничение сверху, а не
# гарантия.
#
# ДОЛГ P2 врач: в main.py tg_safety не подключён НИ В ОДНОЙ точке -> замер
#   29 июля 2026 разбором ast: 11 вызовов Telegram, из них 0 через tg_safety и 5
#   меняющих чат (:1352 публикация расшифровки голосового, :1437 отказ
#   распознавания, :2010 подтверждение закладки, :2082 и :2086 удаление
#   сообщений); у каждого голого await верхняя оценка сна внутри telethon
#   10 x (30 + 20) = 500 с при живом клиенте, и врач видит зависший бот без
#   единой строки в журнале. Для сравнения: assistant.py прошёл на tg_safety 11
#   вызовов из 93 (12%), summarizer.py — 0 из 4. Закрывать не здесь: перевод
#   отправок на tg_safety — работа лана доставки, а этот файл только объявляет
#   порог, поэтому долг помечен, а не починен.
TELETHON_FLOOD_SLEEP_THRESHOLD = max(0, _env_int("STOMCHAT_FLOOD_SLEEP_THRESHOLD", 20))

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
    flood_sleep_threshold=TELETHON_FLOOD_SLEEP_THRESHOLD,
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
    flood_sleep_threshold=TELETHON_FLOOD_SLEEP_THRESHOLD,
)

# Wrapper to track bot's own outgoing message IDs for safety wipe commands
original_send_message = bot_client.send_message
async def patched_send_message(*args, **kwargs):
    text = None
    if len(args) > 1:
        text = args[1]
    elif "message" in kwargs:
        text = kwargs.get("message")

    has_file = bool(kwargs.get("file") or (len(args) > 2 and args[2]))
    if not has_file:
        if not text or not (str(text) if text is not None else "").strip():
            entity = args[0] if len(args) > 0 else kwargs.get("entity")
            logger.warning(f"bot_client.send_message: aborted sending empty message to entity={entity}")
            return None

    sent_msg = await original_send_message(*args, **kwargs)
    if sent_msg and hasattr(sent_msg, 'id') and hasattr(sent_msg, 'peer_id'):
        try:
            # Пересчёт peer -> chat_id отдаём самой библиотеке. Ручная
            # арифметика здесь была верной (проверено на PeerChannel/PeerChat/
            # PeerUser), но она повторяет utils.get_peer_id и молча разойдётся
            # с ним, если Telethon добавит новый тип peer.
            chat_id = telethon_utils.get_peer_id(sent_msg.peer_id)
            if chat_id:
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


async def enqueue_media_analysis(messages, msg_id, text, media_type_hint=None, bulk=False):
    """
    Ставит медиа в очередь разбора. Возвращает True, если место нашлось.

    bulk=True — постановка пачкой из догоняющей синхронизации: там переполнение
    очереди штатно и не должно писать строку ERROR на каждый снимок.
    Непоставленные никуда не пропадают: в базе у них пустое media_description,
    и их подбирает recover_pending_media_analysis при следующих запусках.
    """
    # Вызываем безусловно, а не только при _media_queue is None.
    # start_media_analysis_workers() идемпотентна: она отбрасывает завершившиеся
    # таски и добирает недостающие. Раньше её звали лишь на старте, поэтому
    # умерший воркер (по умолчанию он один) не поднимался никогда: очередь молча
    # заполнялась до предела, и дальше ВСЁ медиа уходило в logger.error без
    # анализа — до следующего перезапуска процесса.
    start_media_analysis_workers()

    try:
        _media_queue.put_nowait((messages, msg_id, text, media_type_hint))
        # Запоминаем ВСЕ сообщения пачки, а не только msg_id: у альбома в базе
        # своя строка на каждый снимок, и догон нашёл бы остальные по пустому
        # описанию. Отмечаем только после успешной постановки — непоставленные
        # (QueueFull) догон обязан подобрать.
        _remember_queued_media([getattr(m, "id", None) for m in messages] + [msg_id])
        logger.info("media analysis queued msg_id=%s queue_size=%s", msg_id, _media_queue.qsize())
        return True
    except asyncio.QueueFull:
        # В живой работе переполнение — авария: снимок коллеги остался без
        # разбора. В догоняющей синхронизации оно ОЖИДАЕМО (см. bulk=True):
        # за месяц простоя набираются сотни медиа (по локальному снимку около
        # 985) при очереди на 128,
        # и 857 строк ERROR подряд только топят журнал.
        if bulk:
            return False
        logger.error(
            "media analysis queue full; skipped msg_id=%s queue_size=%s max_size=%s",
            msg_id,
            _media_queue.qsize(),
            MEDIA_QUEUE_MAX_SIZE,
        )
        return False


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

    # Отбрасываем то, что уже стоит в очереди этого запуска. sync_history шёл
    # прямо перед догоном и поставил свежие снимки сам, а описание в базе
    # появится только ПОСЛЕ разбора — по базе они всё ещё «ожидающие». Без этого
    # отсева пять самых свежих снимков уезжали в платный Vision дважды на каждом
    # рестарте. Фильтруем до запроса в Telegram: заодно не тянем те же файлы.
    pending = [row for row in pending if row[0] not in _QUEUED_MEDIA_IDS]
    if not pending:
        logger.info("pending media recovery: всё уже в очереди этого запуска, догон не нужен")
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
        # Подсказка из базы (media_type строкой) остаётся в силе: строка попала в
        # выборку именно потому, что медиа у неё было. Но если Telegram отдал
        # сообщение, судим по нему тем же единым правилом, что и живой путь.
        if not (clinical_media_kind(message) or media_type_hint):
            logger.info("pending media recovery skipped msg_id=%s: telegram media missing", msg_id)
            continue
        # bulk=True: для догона переполнение очереди ШТАТНО — строка остаётся в
        # базе с пустым описанием, и её подберёт следующий запуск. Без флага
        # каждое непоставленное писало logger.error на пути, где ничего не
        # потеряно. И считаем поставленным только то, что действительно
        # поставилось: queued += 1 стоял безусловно, поэтому сводная строка
        # врала при полной очереди.
        if await enqueue_media_analysis(
            [message],
            msg_id,
            id_to_text.get(msg_id) or message.message or "",
            media_type_hint=media_type_hint,
            bulk=True,
        ):
            queued += 1

    logger.info("pending media recovery queued=%s scanned=%s", queued, len(pending))
    return queued


async def media_recovery_task():
    """
    Периодический догон неразобранных снимков.

    Догон вызывался РОВНО ОДИН раз за запуск и брал MEDIA_RECOVERY_LIMIT строк.
    Замер по копии боевой базы: 745 снимков без описания, самый старый от
    2026-01-29. При пяти за запуск это 149 перезапусков, а перезапусков по
    журналу выходит 14 за 35 суток — то есть порядка 372 суток, чтобы разобрать
    накопленное. Практически это «никогда»: врач присылает рентген, бот молчит,
    и никто не знает, что снимок стоит в очереди длиной в год.

    Очередь разбора живёт в памяти и умирает вместе с процессом, поэтому
    восстановление обязано быть повторяющимся, а не однократным.

    Темп ограничен не числом, а пропускной способностью воркера: за такт
    доливаем не больше MEDIA_RECOVERY_LIMIT и только если в очереди есть место.
    Воркер один, разбор снимка — десятки секунд, так что очередь сама держит
    темп. Каждый разбор идёт через платный Vision, и это осознанная цена: снимок
    без описания выпадает и из дайджеста, и из справки, то есть клинический
    материал теряется целиком.

    Когда неразобранного не остаётся, задача молчит: в журнал пишем только такты,
    на которых что-то реально поставлено.
    """
    if MEDIA_RECOVERY_LIMIT <= 0:
        logger.info("media recovery disabled (MEDIA_RECOVERY_LIMIT=0)")
        return

    while True:
        await asyncio.sleep(MEDIA_RECOVERY_INTERVAL_SECONDS)
        try:
            if _media_queue is not None and _media_queue.full():
                continue
            queued = await recover_pending_media_analysis()
            if queued:
                logger.info("media recovery tick queued=%s", queued)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("media recovery tick failed")


async def media_analysis_worker(worker_id):
    while True:
        messages, msg_id, text, media_type_hint = await _media_queue.get()
        try:
            await process_media_message(messages, msg_id, text, media_type_hint=media_type_hint)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("media analysis worker failed worker=%s msg_id=%s", worker_id, msg_id)
            # Mark as permanently failed so recover_pending_media_analysis stops retrying on every restart
            try:
                await database.update_media_description(msg_id, "[медиа — ошибка анализа]")
            except Exception as mark_err:
                logger.warning("Could not mark failed media msg_id=%s: %s", msg_id, mark_err)
        finally:
            _media_queue.task_done()


def bot_mention_names():
    """
    Имена, по которым бота зовут в группе, — реальное и запасное.

    Проверка вида f"@{assistant.BOT_ID}" была мёртвой: BOT_ID это числовой id,
    и строка «@7971556097» в сообщениях не встречается никогда. Фактически
    работал только зашитый литерал «@stomchat_bot», то есть при смене имени
    бота обращения перестали бы распознаваться совершенно молча.
    """
    names = []
    resolved = getattr(assistant, "BOT_USERNAME", None)
    if resolved:
        names.append(resolved.lower())
    if FALLBACK_BOT_USERNAME and FALLBACK_BOT_USERNAME not in names:
        names.append(FALLBACK_BOT_USERNAME)
    return names


def strip_bot_mention(text):
    """
    Возвращает (было_ли_упоминание, текст_без_упоминания).

    Граница слова обязательна: «@stomchat_bot_old» — это другой аккаунт, и
    засчитывать его за обращение к нам нельзя.
    """
    if not text:
        return False, ""
    found = False
    cleaned = text
    for name in bot_mention_names():
        pattern = re.compile(rf"(?i)@{re.escape(name)}\b")
        if pattern.search(cleaned):
            found = True
            cleaned = pattern.sub(" ", cleaned)
    return found, " ".join(cleaned.split())


def _remove_temp_file(path):
    if not path:
        return
    try:
        path = os.fspath(path)
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("temporary media cleanup failed path=%s: %s", path, exc)


VOICE_DOWNLOAD_TIMEOUT_SECONDS = 120
VOICE_TRANSCRIBE_TIMEOUT_SECONDS = 60

# Накладные ОДНОГО голосового поверх скачивания и самой расшифровки: запас на
# подъём подпроцесса расшифровки (blocking_tools._SUBPROCESS_STARTUP_SLACK_SECONDS
# = 10 с, родитель ждёт на столько дольше, чем выдал ребёнку) плюс правка
# терминов (blocking_tools.correct_dental_transcription_async, свой timeout=20 по
# умолчанию) плюс запас на файловые операции. Числа не выдуманы здесь, а взяты из
# чужих бюджетов; вложенность проверяет test_voice_offline.py — расхождение двух
# независимых чисел в этом проекте всплывало четыре раза (test_budget_nesting.py).
VOICE_ITEM_OVERHEAD_SECONDS = 45
# Худший случай одного голосового целиком. Из слагаемых, а не отдельным числом.
VOICE_ITEM_BUDGET_SECONDS = (
    VOICE_DOWNLOAD_TIMEOUT_SECONDS + VOICE_TRANSCRIBE_TIMEOUT_SECONDS + VOICE_ITEM_OVERHEAD_SECONDS
)
# Сколько голосовых из офлайн-окна догоняем за один заход.
#
# Предел обязателен: окна догона по журналу доходят до 587 сообщений (bot.log,
# июль; максимум мая по bot.log.1 — 134), а расшифровка платная и медленная.
# Очередь без предела означала бы часы Whisper на каждом подъёме. Отброшенное НЕ
# исчезает молча: id уходят в журнал, а строка в базе получает
# VOICE_UNRECOGNIZED_MARK.
VOICE_BACKLOG_MAX_ITEMS = max(0, _env_int("STOMCHAT_VOICE_BACKLOG_MAX", 10))
# Общий бюджет захода догона. Выведен так, чтобы в него гарантированно влезало
# минимум два худших голосовых: догон, в который не влезает даже одна диктовка, —
# это не догон. Каждое отдельное голосовое ограничено МИНИМУМОМ из своего бюджета
# и ОСТАТКА общего (см. transcribe_voice_backlog), поэтому внутренний срок не
# может пережить внешний ни при каком значении переменной окружения.
VOICE_BACKLOG_TOTAL_SECONDS = max(
    VOICE_ITEM_BUDGET_SECONDS * 2,
    _env_int("STOMCHAT_VOICE_BACKLOG_SECONDS", 1800),
)
# Ниже этого остатка следующее голосовое не начинается. Попытка «на 0.2 с»
# провалится гарантированно, но заплатит скачиванием, запуском подпроцесса и
# строкой в журнале: без порога исчерпанный бюджет превращался бы не в один
# честный отказ «времени не осталось», а в десять бессмысленных попыток.
VOICE_BACKLOG_MIN_ITEM_SECONDS = 15

# «Аудио разобрано, речи в нём нет». Единственный класс отказа, о котором в
# группе говорить не надо: терять нечего, а реплика бота на каждый чужой шум —
# это шум в чате 749 врачей. Все остальные причины — НАШ отказ, и о нём врач
# обязан узнать сразу (в ЛС так и сделано, assistant.py).
VOICE_FAILURE_SILENCE = "silence"

# Отметка «голосовое было, расшифровки нет».
#
# Без неё строка голосового в базе неотличима от стикера или пустого сообщения:
# text='', has_media=0, media_type=NULL — никакого признака, что это была
# диктовка врача. Замер по stomat_archive.db: 395 строк без подписи, и
# восстановить пост-фактум нельзя ни одну. Отметка ставится ТОЛЬКО на нашем
# отказе и только в пустую строку — подпись к аудиофайлу не затирается.
VOICE_UNRECOGNIZED_MARK = "[голосовое — расшифровка не получена]"

# Whisper на тишине, шуме и обрывках стабильно выдаёт один и тот же набор
# фраз. Публиковать их как расшифровку нельзя: в чате это выглядит как реплика
# коллеги, а в базе становится текстом сообщения и уезжает в дайджест.
SILENCE_HALLUCINATIONS = {
    "you", "thank you", "bye", "подпишитесь",
    "продолжение следует", "редактор субтитров", "субтитры",
    "youtube", "собачья чушь", "спасибо",
}


def is_voice_message(message):
    """
    Голосовое или присланный аудиофайл. Правило ОДНО на все места пути.

    Догоняющая синхронизация про голосовые не знала вообще, а живой обработчик
    решал сам двумя строками у себя внутри. clinical_media_kind сюда не годится:
    для голосового он возвращает None — это не клинический снимок, — и именно
    из-за этого голосовое не попадало ни в очередь разбора медиа, ни в догон
    неразобранного (тот выбирает строки с has_media = 1).

    Проверка на MagicMock сохранена: у мока «есть» любой атрибут, и подставное
    текстовое сообщение в тестах иначе считалось бы голосовым.
    """
    for attribute in ("voice", "audio"):
        value = getattr(message, attribute, None)
        if value is not None and type(value).__name__ != "MagicMock":
            return True
    return False


async def transcribe_group_voice(message):
    """
    Расшифровывает голосовое из группы. Возвращает (текст, причина_отказа).

    Причина ВОЗВРАЩАЕТСЯ, а не только пишется в журнал. Раньше все шесть тупиков
    отдавали одно и то же None, и вызывающий не мог отличить «в аудио нет речи»
    от «мы не смогли его обработать». В группе это выливалось в полную тишину на
    любом отказе: врач видел своё голосовое и был уверен, что бот его услышал, —
    а в базе оставался text='' и случай выпадал из дайджеста, из контекста
    ответов и из поиска. В ЛС на том же отказе бот отвечает (assistant.py).

    VOICE_FAILURE_SILENCE означает «аудио дошло и разобрано, речи в нём нет».
    Всё остальное — наш отказ, о котором в чате надо сказать вслух.

    download_media собственного таймаута не имеет: на подвисшей загрузке
    обработчик стоял неограниченно долго, а вместе с ним стояло и всё, что
    шло после него в том же таске.
    """
    temp_path = None
    msg_id = getattr(message, "id", None)
    try:
        # Тот же каталог, что у остального медиа: иначе голосовые ложатся туда,
        # куда уборщик не заходит.
        os.makedirs(MEDIA_TEMP_DIR, exist_ok=True)
        temp_path = await asyncio.wait_for(
            message.download_media(file="temp_media/"),
            timeout=VOICE_DOWNLOAD_TIMEOUT_SECONDS,
        )
        if not temp_path or not os.path.exists(temp_path):
            # Раньше этот тупик не оставлял вообще ничего: ни строки в журнале,
            # ни ответа врачу. Отличить его от «Whisper не смог» было нельзя.
            logger.warning("voice download produced no file msg_id=%s", msg_id)
            return None, "скачивание не дало файла"

        import blocking_tools
        transcribed, error = await blocking_tools.transcribe_audio_async(
            temp_path, timeout=VOICE_TRANSCRIBE_TIMEOUT_SECONDS
        )
        if error:
            logger.warning("voice transcription failed msg_id=%s: %s", msg_id, error)
            return None, f"whisper: {error}"
        if not transcribed:
            return None, VOICE_FAILURE_SILENCE

        corrected = await blocking_tools.correct_dental_transcription_async(transcribed.strip())
        # Правка терминов идёт через LLM и вполне может вернуть пустое —
        # тогда остаётся сырая расшифровка, а не молчание.
        text = (corrected or transcribed).strip()
        if not text:
            return None, VOICE_FAILURE_SILENCE

        if text.lower().rstrip(".").rstrip(",").strip() in SILENCE_HALLUCINATIONS:
            logger.info("voice transcription discarded as silence hallucination msg_id=%s", msg_id)
            return None, VOICE_FAILURE_SILENCE
        return text, None
    except asyncio.TimeoutError:
        logger.warning("voice download timed out msg_id=%s", msg_id)
        return None, f"таймаут скачивания {VOICE_DOWNLOAD_TIMEOUT_SECONDS} с"
    except asyncio.CancelledError:
        # Отмену пропускаем наружу: её выставляет внешний бюджет догона, и
        # превращать её в «отказ расшифровки» нельзя — задача обязана умереть.
        raise
    except Exception as audio_err:
        logger.error(f"Error handling group voice message: {audio_err}")
        return None, f"{type(audio_err).__name__}: {audio_err}"
    finally:
        _remove_temp_file(temp_path)


async def _publish_voice_transcription(chat_id, msg_id, text):
    """Публикует расшифровку реплаем на исходное голосовое."""
    try:
        # Регистрировать сообщение в bot_sent_messages здесь не нужно:
        # bot_client.send_message обёрнут patched_send_message, который делает
        # это для КАЖДОЙ отправки бота.
        await bot_client.send_message(
            entity=chat_id,
            message=f"🎤 <b>[Транскрипция голосового]:</b> «{text}»",
            reply_to=msg_id,
            parse_mode='html',
        )
        return True
    except Exception as send_err:
        logger.error("failed to post voice transcription msg_id=%s: %s", msg_id, send_err)
        return False


# Окно между жалобами на отказ расшифровки. Отказы приходят не по одному:
# причина у них общая (упал ключ Whisper, умер подпроцесс, не нашёлся ffmpeg), а
# диктовка идёт серями — по архиву 68 серий из двух и более голосовых подряд,
# самая длинная 19. Без окна сломанный Whisper выдал бы 19 строк «не удалось
# распознать» подряд в чат на 749 врачей, то есть превратил полезное признание
# в спам.
VOICE_FAILURE_NOTICE_COOLDOWN_SECONDS = max(
    60, _env_int("STOMCHAT_VOICE_FAILURE_NOTICE_COOLDOWN_SECONDS", 600))
# Сколько ключей помним. Ключ — пара (чат, автор), а личных диалогов столько же,
# сколько врачей, так что границу ставим и выбрасываем самую старую запись.
_VOICE_FAILURE_NOTICE_MAX_KEYS = 512
# (chat_id, sender_id) -> [момент последней жалобы (monotonic), подавлено с тех пор]
_VOICE_FAILURE_NOTICE = {}


def _voice_failure_notice_allowed(chat_id, sender_id=None):
    """
    Пора ли снова жаловаться. Возвращает (можно, сколько подавлено).

    Окно держится на пару (чат, АВТОР), а не на чат целиком. Узнать об отказе
    должен тот, чьё голосовое не распознали: при окне на весь чат сломанный
    Whisper сообщал бы первому врачу и молчал остальным, а они как раз и
    остались бы в уверенности, что бот их услышал. Серия от одного врача при
    этом всё равно схлопывается в одну строку.

    Часы берутся монотонные, а не стенные: скачок стенных часов назад запер бы
    жалобы на величину скачка — ровно тот дефект, который в этом проекте уже
    запирал пассивный триггер на восемь тысяч лет по метке «9999-12-31».
    """
    key = (chat_id, sender_id)
    now = time.monotonic()
    record = _VOICE_FAILURE_NOTICE.get(key)
    if record is None or now - record[0] >= VOICE_FAILURE_NOTICE_COOLDOWN_SECONDS:
        suppressed = record[1] if record else 0
        if len(_VOICE_FAILURE_NOTICE) >= _VOICE_FAILURE_NOTICE_MAX_KEYS and record is None:
            # Выбрасываем самую старую запись, а не молча растём.
            oldest = min(_VOICE_FAILURE_NOTICE, key=lambda item: _VOICE_FAILURE_NOTICE[item][0])
            _VOICE_FAILURE_NOTICE.pop(oldest, None)
        _VOICE_FAILURE_NOTICE[key] = [now, 0]
        return True, suppressed
    record[1] += 1
    return False, record[1]


async def _notify_voice_failure(chat_id, msg_id, reason, sender_id=None):
    """
    Одна строка в чат на НАШ отказ расшифровки, не чаще раза в окно.

    Все тупики голосового пути молчали: врач видел своё голосовое, был уверен,
    что бот его услышал, и диктовку не повторял — а в базе оставалась пустая
    строка, то есть случай выпадал из дайджеста, из контекста и из поиска. В ЛС
    ровно на этом месте бот отвечает («❌ Не удалось распознать аудио»,
    assistant.py), в группе не отвечал.

    Текст один на все причины: врачу важно только «услышали или нет», разбор
    причины — дело журнала, куда она и уходит рядом. Подавленные жалобы не
    исчезают: их число уходит в журнал и в следующую жалобу, потому что «не
    распознано одно» и «не распознано двадцать» — это разные новости.
    """
    allowed, suppressed = _voice_failure_notice_allowed(chat_id, sender_id)
    if not allowed:
        logger.warning("voice failure NOT notified (окно тишины) msg_id=%s reason=%s "
                       "подавлено подряд=%s chat=%s sender=%s",
                       msg_id, reason, suppressed, chat_id, sender_id)
        return False

    logger.warning("voice failure notified msg_id=%s reason=%s подавлено до этого=%s",
                   msg_id, reason, suppressed)
    tail = ""
    if suppressed:
        tail = (f" Пока молчал, не распознал ещё {suppressed} — "
                "видимо, распознавание лежит.")
    try:
        await bot_client.send_message(
            entity=chat_id,
            # Формулировка та же, что в ЛС (assistant.py), и «аудио», а не
            # «голосовое»: этим же путём идут присланные аудиофайлы.
            message="❌ <i>Не удалось распознать аудио. "
                    "Повторите или напишите текстом." + tail + "</i>",
            reply_to=msg_id,
            parse_mode='html',
        )
        return True
    except Exception as send_err:
        logger.error("failed to notify voice failure msg_id=%s: %s", msg_id, send_err)
        return False


async def _store_voice_text(msg_id, text, repair=None):
    """
    Дописывает расшифровку в базу и ПРОВЕРЯЕТ, что строка нашлась.

    Результат update_message_text не проверялся, а функция возвращает rowcount и
    0 при отсутствии строки, гася исключения внутрь лога базы (database.py). То
    есть при провалившемся save_message — а рядом на этот случай стоит громкий
    MESSAGE NOT PERSISTED — транскрипция уходила в чат, но в базу не попадала
    ВООБЩЕ, и ни одной строки об этом в журнале не появлялось. Класс тот же, что
    уже закрыли для save_message; update_message_text остался без проверки.

    repair — поля save_message для восстановления пропавшей строки. Вставка
    безопасна: save_message это INSERT OR IGNORE, существующую строку она не
    перезапишет. Без repair (догон, где полей автора на руках нет) остаётся
    громкая запись в журнал.
    """
    updated = await asyncio.wait_for(database.update_message_text(msg_id, text), timeout=30)
    if updated:
        return True

    logger.error(
        "VOICE TEXT NOT PERSISTED msg_id=%s — строки в базе нет, расшифровка не "
        "попадёт ни в дайджест, ни в контекст, ни в поиск",
        msg_id,
    )
    if not repair:
        return False

    saved = await asyncio.wait_for(database.save_message(**repair), timeout=30)
    if saved:
        logger.info("voice row re-inserted with transcription msg_id=%s", msg_id)
        return True
    logger.error("voice row repair failed msg_id=%s — расшифровка потеряна", msg_id)
    return False


async def _mark_voice_unrecognized(messages, reason):
    """
    Ставит в базе отметку «голосовое было, расшифровки нет».

    Зачем вообще: строка голосового (text='', has_media=0, media_type=NULL)
    неотличима от стикера и от пустого сообщения, поэтому потеря диктовки была
    невидима и невосстановима — замер по stomat_archive.db: 395 таких строк, и ни
    одну нельзя опознать как голосовое пост-фактум. С отметкой пропажа видна и в
    дайджесте, и глазами в базе.

    Непустой текст НЕ затирается: у аудиофайла бывает подпись, а расшифровка
    могла уже дойти другим путём. Поэтому сначала читаем строку.
    """
    marked = 0
    for message in messages:
        msg_id = getattr(message, "id", None)
        if msg_id is None:
            continue
        try:
            row = await asyncio.wait_for(database.get_text_by_id(msg_id), timeout=30)
            if row is None:
                logger.error(
                    "voice mark skipped msg_id=%s: строки в базе нет — сообщение не сохранилось",
                    msg_id,
                )
                continue
            if (row[1] or "").strip():
                continue
            if await _store_voice_text(msg_id, VOICE_UNRECOGNIZED_MARK):
                marked += 1
        except Exception:
            logger.exception("voice mark failed msg_id=%s", msg_id)
    if marked:
        logger.info("voice marked as unrecognized count=%s reason=%s", marked, reason)
    return marked


async def transcribe_voice_backlog(messages, source="sync"):
    """
    Расшифровывает голосовые, накопившиеся за офлайн. Возвращает число успешных.

    Чего не было: догон сохранял голосовое как обычное сообщение и на этом
    заканчивал. clinical_media_kind для голосового отдаёт None, значит
    has_media=0 — и в очередь разбора медиа оно не попадало ни из sync_history,
    ни из recover_pending_media_analysis (тот выбирает строки с has_media = 1 и
    пустым описанием). transcribe_group_voice в догоне не вызывался ни разу.
    Расшифровку получало РОВНО ОДНО голосовое окна — последнее сообщение, и то
    лишь потому, что sync_history прогоняет last_synced_message через
    handle_new_message. Остальные терялись молча и невосстановимо.

    Замер (stomat_archive.db, 117 847 сообщений; размеры окон — из bot.log):
    395 строк голосовых/аудио. При окне 17 сообщений (среднее мая) расшифровку
    получали 20, терялось 375 — 94.9%; при окне 37 (среднее июля) терялось 389;
    при окне 587 (максимум июля) — все 395. Диктовка идёт сериями: 68 серий из
    двух и более голосовых подряд, самая длинная — 19.

    Пределы обязательны и оба настоящие: не больше VOICE_BACKLOG_MAX_ITEMS
    голосовых и не дольше VOICE_BACKLOG_TOTAL_SECONDS на весь заход. Берём
    ПОСЛЕДНИЕ: свежую диктовку в чате ещё обсуждают. Отброшенное не исчезает
    молча — id уходят в журнал, а строки получают VOICE_UNRECOGNIZED_MARK.

    Отказы догона в чат не отправляются: после подъёма их может быть десяток
    подряд, и чат коллег получил бы десять «не распознал» про сообщения часовой
    давности. Живой путь отвечает врачу сразу — там отказ один и врач ещё ждёт.
    """
    if not messages:
        return 0

    fresh = []
    for message in messages:
        msg_id = getattr(message, "id", None)
        if msg_id is None or msg_id in _TRANSCRIBED_MSG_IDS:
            continue
        fresh.append(message)
    if not fresh:
        logger.info("voice backlog source=%s: всё уже расшифровано в этом запуске", source)
        return 0

    fresh.sort(key=lambda item: getattr(item, "id", 0))
    if VOICE_BACKLOG_MAX_ITEMS <= 0:
        work, dropped = [], list(fresh)
    else:
        work, dropped = fresh[-VOICE_BACKLOG_MAX_ITEMS:], fresh[:-VOICE_BACKLOG_MAX_ITEMS]

    # Отмечаем ДО первого скачивания, как это делает постановка медиа в очередь:
    # повторная доставка апдейта или второй проход синхронизации не должны
    # расшифровать и опубликовать то же голосовое второй раз.
    for message in work + dropped:
        _remember_transcribed(message.id)

    if dropped:
        logger.warning(
            "voice backlog truncated source=%s dropped=%s max=%s ids=%s — эти "
            "голосовые расшифрованы НЕ БУДУТ, в базе у них отметка «%s»",
            source, len(dropped), VOICE_BACKLOG_MAX_ITEMS,
            [message.id for message in dropped][:20], VOICE_UNRECOGNIZED_MARK,
        )
        await _mark_voice_unrecognized(dropped, "предел догона")

    deadline = asyncio.get_running_loop().time() + VOICE_BACKLOG_TOTAL_SECONDS
    done = 0
    failed = 0
    for index, message in enumerate(work):
        left = deadline - asyncio.get_running_loop().time()
        # Внутренний срок = МИНИМУМ из бюджета одного голосового и остатка
        # общего. Внутренний бюджет, не влезающий во внешний, — тот самый класс
        # дефекта, который в этом проекте всплывал четыре раза.
        item_budget = min(VOICE_ITEM_BUDGET_SECONDS, left)
        if item_budget < VOICE_BACKLOG_MIN_ITEM_SECONDS:
            rest = work[index:]
            logger.warning(
                "voice backlog out of budget source=%s limit=%s с dropped=%s ids=%s — "
                "остаток окна расшифрован не будет",
                source, VOICE_BACKLOG_TOTAL_SECONDS, len(rest),
                [item.id for item in rest][:20],
            )
            await _mark_voice_unrecognized(rest, "бюджет догона исчерпан")
            break

        try:
            text, failure = await asyncio.wait_for(
                transcribe_group_voice(message), timeout=item_budget
            )
        except asyncio.TimeoutError:
            text, failure = None, f"таймаут догона {item_budget:.0f} с"
        except asyncio.CancelledError:
            raise
        except Exception as backlog_err:
            text, failure = None, f"{type(backlog_err).__name__}: {backlog_err}"

        if text:
            stored = await _store_voice_text(message.id, text)
            await _publish_voice_transcription(
                getattr(message, "chat_id", None) or config.SOURCE_CHAT_ID, message.id, text
            )
            done += 1
            logger.info(
                "voice backlog transcribed msg_id=%s stored=%s chars=%s", message.id, stored, len(text)
            )
        else:
            failed += 1
            logger.warning("voice backlog item failed msg_id=%s reason=%s", message.id, failure)
            if failure != VOICE_FAILURE_SILENCE:
                await _mark_voice_unrecognized([message], failure)

    logger.info(
        "voice backlog done source=%s расшифровано=%s отказов=%s отброшено=%s из %s",
        source, done, failed, len(dropped), len(fresh),
    )
    return done


async def _mark_media_processed(messages, msg_id, reason):
    """
    Последняя отметка «медиа разобрано» на аварийных путях. Возвращает число строк.

    Зачем громко: провал самой этой отметки прежде глотался `except Exception:
    pass` — двумя одинаковыми блоками, на таймауте и на исключении. А отметка тут
    ровно для того, чтобы строка не осталась в состоянии «ещё не разбирали»:
    recover_pending_media_analysis такую строку тянет из Telegram и качает файл
    ЗАНОВО на каждом рестарте, вечно, тратя бюджет зрения на сообщение, которое
    уже признано неразбираемым. То есть молчал именно тот отказ, который и
    заводит бесконечный круг.
    """
    marked = 0
    try:
        for message in messages:
            await asyncio.wait_for(
                database.update_media_description(message.id, "-"), timeout=10
            )
            marked += 1
    except Exception as exc:
        logger.error(
            "media msg_id=%s НЕ отмечен как разобранный (%s; отмечено %s из %s; "
            "%s: %s) — строка остаётся в догоне и будет качаться заново на каждом "
            "рестарте",
            msg_id, reason, marked, len(messages), type(exc).__name__, exc,
        )
    return marked


async def process_media_message(messages, msg_id, text, media_type_hint=None):
    files_to_analyze = []
    # Чьи именно снимки дошли до Vision. Нужно потому, что описание одно на
    # вызов, а строк в базе столько же, сколько сообщений в альбоме.
    analyzed_msg_ids = set()
    msg_file_map = {}
    media_description = None
    temp_paths = []

    try:
        os.makedirs(MEDIA_TEMP_DIR, exist_ok=True)
        for message in messages:
            try:
                temp_path = await asyncio.wait_for(
                    message.download_media(file=os.path.join(MEDIA_TEMP_DIR, "")),
                    timeout=MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                )
                if temp_path:
                    temp_paths.append(temp_path)

                    if message.photo or image_document(message) is not None or media_type_hint == "photo":
                        files_to_analyze.append(temp_path)
                        analyzed_msg_ids.add(message.id)
                        msg_file_map[message.id] = temp_path
                    elif message.video or media_type_hint == "video":
                        logger.info(f"🎞️ Извлечение первого кадра из видео {message.id}...")
                        frame_path = await extract_first_frame_async(
                            temp_path,
                            timeout=MEDIA_FRAME_TIMEOUT_SECONDS,
                        )
                        if frame_path:
                            files_to_analyze.append(frame_path)
                            temp_paths.append(frame_path)
                            analyzed_msg_ids.add(message.id)
                            msg_file_map[message.id] = frame_path
            except Exception as e:
                logger.warning(f"Failed to process a media item in album {msg_id}: {type(e).__name__}: {e}")

        if files_to_analyze:
            logger.info(f"📸 Анализ медиа (файлов: {len(files_to_analyze)}) в сообщении {msg_id}...")
            # Determine if the media message is a passive background upload or an active bot call
            is_passive_image = True
            if text:
                t_low = text.lower()
                if "бот" in t_low or "@" in t_low:
                    is_passive_image = False

            media_description = await asyncio.wait_for(
                vision.describe_image(files_to_analyze, caption=text, is_passive=is_passive_image),
                timeout=MEDIA_ANALYSIS_TIMEOUT_SECONDS,
            )
            if media_description:
                for message in messages:
                    # Описание относится ровно к тем файлам, которые попали в
                    # Vision. При частичном провале альбома (скачался один
                    # снимок из пяти) оно записывалось во ВСЕ строки: в дайджест
                    # и в контекст ассистента уходило «на снимке перфорация» под
                    # четырьмя чужими снимками, которых никто не видел.
                    row_description = (
                        media_description if message.id in analyzed_msg_ids else MEDIA_UNAVAILABLE_MARK
                    )
                    await asyncio.wait_for(
                        database.update_media_description(message.id, row_description),
                        timeout=30,
                    )
                if len(analyzed_msg_ids) < len(messages):
                    logger.warning(
                        "media album partially prepared msg_id=%s analyzed=%s of %s — "
                        "остальным строкам поставлена отметка недоступности",
                        msg_id, len(analyzed_msg_ids), len(messages),
                    )
                logger.info(f"📝 Описание готово: {media_description}")
                logger.info("message_media_preview msg_id=%s text=%s", msg_id, media_description)

                # Сохранение снимков на постоянный CDN для отображения в Telegraph-дайджестах
                for analyzed_id, fpath in msg_file_map.items():
                    try:
                        cdn_url = await upload_clinical_image_async(fpath)
                        if cdn_url:
                            await database.update_media_remote_url(analyzed_id, cdn_url)
                            logger.info("media_cdn_saved msg_id=%s url=%s", analyzed_id, cdn_url)
                    except Exception as cdn_err:
                        logger.warning("media_cdn_failed msg_id=%s: %s", analyzed_id, cdn_err)
                
                # Запуск медиа-ассистента
                async def run_media_assistant_safe():
                    try:
                        await assistant.check_and_trigger_assistant_media(
                            bot_client, messages[0], msg_id, text, media_description
                        )
                    except Exception as e:
                        logger.exception(f"Unexpected error in run_media_assistant_safe: {e}")
                runtime_guard.create_task(run_media_assistant_safe(), name=f"media_{msg_id}")
            else:
                logger.info("media analysis returned empty description msg_id=%s, marking as processed", msg_id)
                for message in messages:
                    await asyncio.wait_for(
                        database.update_media_description(message.id, "-"),
                        timeout=30,
                    )
        else:
            # Ни один файл не скачался и не подготовился. В базу при этом не
            # писалось НИЧЕГО: строка оставалась с пустым media_description, то
            # есть «ещё не разбирали» — и recover_pending_media_analysis тянул её
            # из Telegram и качал заново на КАЖДОМ рестарте, вечно. Все прочие
            # тупики (пустое описание, таймаут, исключение) отметку ставят;
            # именно эта ветка её не ставила.
            logger.warning(
                "media unavailable msg_id=%s: ни один из %s файлов не подготовлен, "
                "ставлю отметку, иначе строка вечно в догоне",
                msg_id, len(messages),
            )
            for message in messages:
                await asyncio.wait_for(
                    database.update_media_description(message.id, MEDIA_UNAVAILABLE_MARK),
                    timeout=30,
                )

    except asyncio.TimeoutError:
        logger.warning("media processing timeout msg_id=%s, marking as processed to avoid loop", msg_id)
        await _mark_media_processed(messages, msg_id, "таймаут обработки")
    except Exception:
        logger.exception("Ошибка обработки медиа %s, marking as processed to avoid loop", msg_id)
        await _mark_media_processed(messages, msg_id, "исключение при обработке")
    finally:
        for p in temp_paths:
            _remove_temp_file(p)


# None в списке чатов недопустим: Telethon пытается резолвить его в
# EventBuilder.resolve, падает ВНЕ per-callback try, и цикл регистрации
# хендлеров обрывается — ни один обработчик не срабатывает вообще, полностью
# молча. config допускает SOURCE_CHAT_ID = None (только warning в stdout),
# поэтому отфильтровываем здесь.
WATCHED_CHATS = [c for c in (config.SOURCE_CHAT_ID, assistant.TEST_CHAT_ID) if c is not None]

# В таблицу messages пишется ТОЛЬКО основной чат, и колонки chat_id в ней нет.
# Поэтому правки и удаления применяются к базе исключительно из него: id
# сообщений уникальны лишь в пределах чата, и удаление #4821 в тестовом чате
# снесло бы строку с тем же номером из основного.
SAVED_CHATS = [config.SOURCE_CHAT_ID] if config.SOURCE_CHAT_ID else []

if not config.SOURCE_CHAT_ID:
    logger.error(
        "SOURCE_CHAT_ID не задан — основной чат не отслеживается. "
        "Проверьте .env, иначе бот будет работать только в тестовом чате."
    )


@client.on(events.NewMessage(chats=WATCHED_CHATS))
async def handle_new_message(event):
    """Обработчик новых сообщений в целевом чате."""
    try:
        msg_id = event.message.id
        
        global PROCESSED_MSG_IDS
        if msg_id in PROCESSED_MSG_IDS:
            logger.info(f"Deduplicator: Skipping already processed msg_id={msg_id}")
            return
        PROCESSED_MSG_IDS.append(msg_id)
        if len(PROCESSED_MSG_IDS) > 500:
            PROCESSED_MSG_IDS.pop(0)
        sender_id = event.sender_id
        
        # Флаг, является ли отправителем сам бот
        is_bot = False
        if sender_id == FALLBACK_BOT_ID or (assistant.BOT_ID and sender_id == assistant.BOT_ID):
            is_bot = True
            
        sender = None
        try:
            sender = await asyncio.wait_for(
                event.get_sender(),
                timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("sender lookup failed msg_id=%s sender_id=%s: %s", msg_id, sender_id, exc)

        # Бот — свой или чужой — не должен запускать триггеры НИ В ОДНОМ чате.
        # Раньше эта проверка стояла ВНУТРИ блока `if chat_id == SOURCE_CHAT_ID`,
        # а хендлер подписан на два чата. Во втором — том, куда бот сам постит
        # дайджесты, — фильтра не было: собственный дайджест прилетал как новое
        # сообщение, проходил пассивный триаж, и бот комментировал сам себя.
        # Его комментарий приходил снова; каждый круг — платный LLM-вызов.
        is_any_bot = is_bot or bool(sender and getattr(sender, 'bot', False))

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

        # Group Voice Note / Audio processing.
        # Правило вынесено в is_voice_message: то же самое нужно догоняющей
        # синхронизации, а два независимых определения одного признака в этом
        # файле уже расходились (media_type/clinical_media_kind).
        is_audio = is_voice_message(event.message)

        # Получаем ID сообщения, на которое ответили (если есть)
        reply_to_msg_id = None
        if event.message.reply_to:
            reply_to_msg_id = event.message.reply_to.reply_to_msg_id

        # Проверка медиа через единое правило clinical_media_kind: снимок-документ
        # (несжатый рентген/КТ) считается фото, а превью ссылок, гифки, кружки и
        # видеостикеры отсекаются. Раньше здесь стояло
        # `photo or video or image_document`, и все четыре вида постороннего
        # медиа уезжали в платный Vision как клинический снимок.
        snapshot_document = image_document(event.message)
        media_type = clinical_media_kind(event.message)
        has_media = media_type is not None
        media_description = None

        # Строка в базе появляется НЕМЕДЛЕННО, до любой тяжёлой обработки.
        if event.chat_id == config.SOURCE_CHAT_ID:
            saved = await asyncio.wait_for(
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
            # Раньше отказ записи был неотличим от успеха: save_message молча
            # возвращала None, и обработчик шёл дальше как ни в чём не бывало.
            # Сообщение при этом выпадало из дайджестов, из контекста ассистента
            # и из поиска по истории — навсегда и без единой заметной строки в
            # логе. Отвечать врачу продолжаем: молчать хуже, чем ответить.
            if not saved:
                logger.error(
                    "MESSAGE NOT PERSISTED msg_id=%s chat=%s sender=%s — "
                    "оно не попадёт ни в дайджест, ни в контекст",
                    msg_id, event.chat_id, sender_id,
                )

        # Расшифровка голосового идёт ПОСЛЕ сохранения, а не до него.
        #
        # Раньше строка доезжала до базы только по завершении расшифровки:
        # сначала скачивание без таймаута, затем Whisper (до 60 с), затем
        # правка терминов. Всё это время сообщения в базе не существовало, а
        # health_watchdog раз в 5 минут сверяет максимальный id в чате с
        # максимальным в базе. Попав в это окно, он объявлял сообщение
        # пропущенным и запускал sync_history, который прогонял его через
        # обработчик второй раз — в чат уходила вторая «🎤 Транскрипция», а
        # Whisper отрабатывал дважды за то же голосовое.
        #
        # Расшифрованный текст догоняется в базу отдельным UPDATE, поэтому
        # обрыв на середине больше не теряет сообщение целиком — теряется
        # только расшифровка, ровно как при обычном сбое Whisper.
        #
        # Голосовые из офлайн-окна догоняет transcribe_voice_backlog, поэтому
        # здесь стоит общая отметка _TRANSCRIBED_MSG_IDS: одно голосовое не
        # должно уехать в платный Whisper дважды и не должно получить вторую
        # «🎤 Транскрипцию» в чате.
        if is_audio and not is_any_bot and msg_id not in _TRANSCRIBED_MSG_IDS:
            _remember_transcribed(msg_id)
            transcribed_text, voice_failure = await transcribe_group_voice(event.message)
            if transcribed_text:
                text = transcribed_text
                if event.chat_id == config.SOURCE_CHAT_ID:
                    # Результат записи ПРОВЕРЯЕТСЯ: без проверки транскрипция
                    # уходила в чат и не доезжала до базы совершенно молча.
                    # Поля для восстановления строки — те же, что у save_message
                    # выше, и на руках они уже есть.
                    await _store_voice_text(
                        msg_id,
                        text,
                        repair={
                            "msg_id": msg_id,
                            "reply_to_msg_id": reply_to_msg_id,
                            "sender_id": sender_id,
                            "sender_name": sender_name,
                            "sender_username": sender_username,
                            "text": text,
                            "date": date,
                            "has_media": has_media,
                            "media_type": media_type,
                        },
                    )
                await _publish_voice_transcription(event.chat_id, msg_id, text)
            elif voice_failure and voice_failure != VOICE_FAILURE_SILENCE:
                # Наш отказ: врачу отвечаем вслух, а в базе оставляем отметку —
                # иначе строка неотличима от стикера и диктовка теряется без
                # следа. На VOICE_FAILURE_SILENCE не делаем ни того, ни другого:
                # аудио разобрано, речи в нём нет, терять нечего.
                if event.chat_id == config.SOURCE_CHAT_ID:
                    await _mark_voice_unrecognized([event.message], voice_failure)
                await _notify_voice_failure(event.chat_id, msg_id, voice_failure,
                                            sender_id=sender_id)

        if event.chat_id == config.SOURCE_CHAT_ID:
            # Сообщение бота уже сохранено в базу выше — дальше ничего не делаем.
            if is_any_bot:
                if not is_bot:
                    logger.info(f"Deduplicator: Message is from another bot (sender_id={sender_id}, username={sender_username}). Skipping triggers.")
                return

            # Check bookmark saving command
            cmd_clean = text.strip().lower()
            if cmd_clean in ("/save", "/сохранить") and reply_to_msg_id:
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
            # Не запускаем авто-анализ медиа, если подпись является интерактивной командой (чтобы избежать двойного ответа)
            is_media_command = False
            if text:
                cmd_test = text.strip().lower()
                is_media_command = cmd_test.startswith(("/", "итог", "викторина", "опрос", "удалить", "wipe"))

            if (event.photo or event.video or snapshot_document is not None) and not is_media_command:
                if getattr(event, "grouped_id", None):
                    if event.grouped_id not in _pending_albums:
                        _pending_albums[event.grouped_id] = []
                        
                        async def _process_album_after_delay(g_id, b_client):
                            last_len = 0
                            while True:
                                await asyncio.sleep(3.0)
                                current_len = len(_pending_albums.get(g_id, []))
                                if current_len == 0 or current_len == last_len:
                                    break
                                last_len = current_len
                                
                            msgs = _pending_albums.pop(g_id, [])
                            if msgs:
                                # .message, а не .text: Message.text отдаёт текст С
                                # РАЗМЕТКОЙ клиентского parse_mode (у telethon по
                                # умолчанию markdown). Прогон разведки: .text даёт
                                # "**Кейс**: перфорация", .message — "Кейс:
                                # перфорация". Остальной код (handle_new_message,
                                # sync_history, догон) берёт .message, и подпись
                                # альбома выбивалась из общего правила: разметка
                                # уходила в промпт зрения как «контекст от автора»
                                # и участвовала в решении is_passive по вхождению
                                # «бот»/«@». Отдельно: у сообщения без привязанного
                                # клиента .text возвращает None, и подпись альбома
                                # терялась целиком.
                                combined_text = "\n".join([m.message for m in msgs if m.message]).strip()
                                await enqueue_media_analysis(msgs, msgs[0].id, combined_text)
                                
                        runtime_guard.create_task(_process_album_after_delay(event.grouped_id, bot_client), name=f"album_{event.grouped_id}")
                        
                    _pending_albums[event.grouped_id].append(event.message)
                else:
                    await enqueue_media_analysis([event.message], msg_id, text)
        # Групповые команды и модерация
        async def run_group_features():
            try:
                cmd = text.strip()
                cmd_lower = cmd.lower()
                
                # 0. Экстренное удаление сообщений (админское)
                if cmd_lower in ("/wipe", "/delete", "/del") and reply_to_msg_id:
                    try:
                        is_super_admin = False
                        if event.sender_id in (7716348189, 1890028643):
                            is_super_admin = True
                        else:
                            permissions = await event.client.get_permissions(event.chat_id, event.sender_id)
                            if permissions.is_admin:
                                is_super_admin = True
                                
                        if is_super_admin:
                            # Split deletion to avoid complete failure if bot cannot delete user's command message
                            try:
                                await bot_client.delete_messages(event.chat_id, [reply_to_msg_id])
                            except Exception as e1:
                                logger.warning(f"Failed to delete replied message {reply_to_msg_id}: {e1}")
                            try:
                                await bot_client.delete_messages(event.chat_id, [msg_id])
                            except Exception as e2:
                                logger.warning(f"Failed to delete command message {msg_id}: {e2}")
                                
                            await database.remove_bot_sent_message(reply_to_msg_id)
                            return True
                    except Exception as delete_exc:
                        logger.error(f"Failed to execute inline admin delete: {delete_exc}")

                # 1. Сводка/Саммари обсуждения — ТОЛЬКО по команде со слешем.
                #
                # Здесь стояло startswith(..., "итог") без слеша, и «итог» — не
                # команда, а обычное слово профессиональной речи. Замер по живому
                # архиву (107 316 реплик с текстом): под этот триггер попадают
                # ДЕВЯТЬ реплик врачей и НИ ОДНОЙ настоящей команды. Среди них
                # «Итог каков. Про пищевой комок тут написана чушь?», «Итого
                # 5000₽ за ед обходится работа с керамикой», «Итог всех мытарств!
                # 500 евро эндо и билд ап». На каждой из них бот влезал в чужой
                # спор с непрошеной сводкой всего обсуждения — и это платная
                # генерация, а правило проекта прямо обратное: бот заговаривает
                # только строго по теме и по просьбе.
                if cmd_lower.startswith(("/summary", "/итог", "/sum")):
                    await assistant.handle_group_summary(bot_client, event, reply_to_msg_id)
                    return True
                
                # 2. Прямой запрос к боту
                mentioned, without_mention = strip_bot_mention(cmd)
                if cmd_lower.startswith("/ask ") or mentioned:
                    if cmd_lower.startswith("/ask "):
                        question = cmd[5:].strip()
                    else:
                        question = without_mention

                    if question:
                        await assistant.handle_group_direct_ask(bot_client, event, question)
                    return True
                
                # 3. Викторина/Опрос в группе — тоже только со слешем.
                # Тот же класс: «Опрос» одним словом в архиве встречается как
                # обычная реплика (msg 128077), а викторина — это платная
                # генерация и заметный шум в чате 749 врачей. Совпадение было
                # точное, не по подстроке, поэтому цена ниже, чем у сводки, но
                # оснований отвечать на слово «опрос» викториной всё равно нет.
                if cmd_lower in ("/poll", "/кейс"):
                    await assistant.handle_group_quiz(bot_client, event)
                    return True
                
                # 4. Толковый словарь (объяснение терминов)
                if cmd_lower.startswith(("/what ", "/что ")):
                    term = cmd[6:].strip() if cmd_lower.startswith("/what ") else cmd[5:].strip()
                    if term:
                        await assistant.handle_term_explainer(bot_client, event, term)
                    return True

                pass
            except Exception as e:
                logger.exception(f"Error executing group feature: {e}")
            return False

        # Умный авто-ассистент (поддерживает как основной чат, так и диалог в тестовом топике)
        async def run_assistant_safe():
            try:
                if await run_group_features():
                    return
                # Если сообщение содержит медиа (фото/видео/снимок-документ),
                # пропускаем текстовый триггер — его обработает медиа-ассистент.
                if event.photo or event.video or snapshot_document is not None:
                    return
                # Запускаем авто-ассистента. Если он сработал и ответил (вернул True), то mention_trigger пропускаем, чтобы не было двойных ответов
                replied = await assistant.check_and_trigger_assistant(
                    bot_client, event, msg_id, text, reply_to_msg_id,
                    sender_first_name=sender_first_name
                )
                if not replied:
                    # Обращение по имени. Комментарий здесь говорил «always shadow
                    # mode until promoted», и это УЖЕ НЕВЕРНО: в assistant.py стоит
                    # BOT_MENTION_SHADOW_MODE = False с пометкой «выкачено в
                    # боевой», то есть ответ уходит врачу, а не в теневой журнал.
                    # Цена расхождения не в поведении, а в следующем читателе: он
                    # либо пойдёт искать непромотированный режим, которого нет,
                    # либо поверит комментарию и решит, что врач ответа не видит.
                    replied_mention = await assistant.check_bot_mention_trigger(
                        bot_client, event, msg_id, text, sender_first_name=sender_first_name
                    )
                    # Если бот не ответил ни как пассивный, ни по упоминанию, проверяем рефери!
                    if not replied_mention:
                        await assistant.check_and_trigger_referee(bot_client, event, text)
            except Exception as e:
                logger.exception(f"Unexpected error in run_assistant_safe: {e}")
                
        # Второй чат до блока SOURCE_CHAT_ID не доходит, поэтому страхуемся здесь:
        # без этой проверки бот отвечал на собственные дайджесты.
        if is_any_bot:
            logger.info(f"Skipping assistant triggers for bot-authored msg_id={msg_id} in chat {event.chat_id}.")
        else:
            runtime_guard.create_task(run_assistant_safe(), name=f"assistant_{msg_id}")
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


# Правки и удаления в Telegram до базы не доезжали вообще: обработчиков не
# существовало, а database.delete_messages_by_ids лежала мёртвым кодом без
# единого вызова. Практически это значит, что бот годами оперирует первой
# редакцией сообщения и хранит удалённые коллегами посты — цитирует их в
# ответах, тянет в контекст и включает в дайджест.
EDIT_RESAVE_RETRY_SECONDS = 2.0


@client.on(events.MessageEdited(chats=SAVED_CHATS))
async def handle_edited_message(event):
    """Догоняет правку сообщения: в базе должна лежать текущая редакция."""
    try:
        if event.chat_id != config.SOURCE_CHAT_ID:
            return

        msg_id = event.message.id
        new_text = event.message.message or ""

        updated = await asyncio.wait_for(
            database.update_message_text(msg_id, new_text), timeout=30
        )

        # Гонка с сохранением: Telegram присылает UpdateEditMessage и просто
        # так — например, когда через секунду подгружается превью ссылки. Если
        # исходный save_message в этот момент ещё в полёте, правка попадает в
        # ноль строк, а следом записывается СТАРЫЙ текст. Одна отложенная
        # попытка закрывает окно; дальше молчим — сообщения может не быть в
        # базе законно (правка поста старше бота, правка в другом чате).
        if not updated:
            await asyncio.sleep(EDIT_RESAVE_RETRY_SECONDS)
            updated = await asyncio.wait_for(
                database.update_message_text(msg_id, new_text), timeout=30
            )

        if updated:
            logger.info(
                "message edited msg_id=%s new_len=%s", msg_id, len(new_text)
            )
        else:
            # info, а не debug: корневой уровень INFO, и на 131 219 строк всех
            # журналов на диске записей DEBUG ровно НОЛЬ — то есть эта строка не
            # появлялась ни разу. А она отвечает на живой вопрос: «врач
            # поправил свой пост, почему бот цитирует старый текст».
            logger.info("edit for unknown msg_id=%s ignored (строки в базе нет: "
                        "пост старше бота, чужой чат или save_message не доехал)",
                        msg_id)
    except Exception:
        logger.exception("edited message handler failed")


@client.on(events.MessageDeleted(chats=SAVED_CHATS))
async def handle_deleted_messages(event):
    """Убирает удалённые сообщения из базы, чтобы бот перестал их цитировать."""
    try:
        # Для супергрупп Telegram присылает UpdateDeleteChannelMessages с
        # известным каналом. Удаления без чата (личка, обычные группы) до
        # обработчика не доходят из-за chats-фильтра — и правильно: применить
        # их к таблице без chat_id значило бы снести чужие строки.
        if event.chat_id != config.SOURCE_CHAT_ID:
            return

        deleted_ids = [i for i in (event.deleted_ids or []) if i]
        if not deleted_ids:
            return

        removed, bot_removed = await asyncio.wait_for(
            database.delete_messages_by_ids(deleted_ids, chat_id=event.chat_id),
            timeout=30,
        )
        if removed or bot_removed:
            logger.info(
                "messages deleted in chat: reported=%s purged=%s bot_rows=%s",
                len(deleted_ids), removed, bot_removed,
            )
    except Exception:
        logger.exception("deleted message handler failed")


# Сообщения одного пользователя обрабатываются строго по очереди.
# Telethon диспатчит апдейты конкурентно (sequential_updates=False), и каждое
# ЛС уходило в отдельный detached-таск. Два ответа врача во время /case шли
# параллельно: оба читали current_step = N, оба писали N+1 через
# INSERT OR REPLACE — шаг проглатывался, история одного из обменов терялась,
# и приходили два ответа экзаменатора по разным веткам.
_PM_USER_LOCKS = {}


def _pm_user_lock(user_id):
    lock = _PM_USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _PM_USER_LOCKS[user_id] = lock
        # Словарь ограничен: иначе он растёт на каждого нового собеседника
        # и никогда не чистится. Незанятые замки можно выбрасывать свободно.
        if len(_PM_USER_LOCKS) > 500:
            for stale_id in [k for k, v in _PM_USER_LOCKS.items()
                             if k != user_id and not v.locked()][:250]:
                _PM_USER_LOCKS.pop(stale_id, None)
    return lock


@bot_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def handle_private_message(event):
    """Обработчик входящих личных сообщений (ЛС) бота."""
    async def run_pm_safe():
        try:
            async with _pm_user_lock(event.chat_id):
                await assistant.handle_private_message(bot_client, event)
        except Exception as e:
            logger.exception(f"Unexpected error in PM message handler: {e}")

    runtime_guard.create_task(run_pm_safe(), name=f"pm_{event.message.id}")

# Обработанные нажатия кнопок. Дедупликации не было: двойной тап по варианту
# викторины обрабатывался дважды — двойная смена стиля, двойной edit_message,
# потенциально двойной зачёт ответа.
HANDLED_CALLBACK_IDS = deque(maxlen=500)
_HANDLED_CALLBACK_SET = set()


@bot_client.on(events.CallbackQuery)
async def handle_callback_query(event):
    """Централизованный диспетчер нажатий на инлайн-кнопки (навигация nav:*, меню menu:*, викторины quiz:*, кейсы case:*, калькулятор calc:*, стиль style:*)."""
    cb_id = getattr(event, "id", None)
    if cb_id is not None:
        if cb_id in _HANDLED_CALLBACK_SET:
            logger.info(f"Deduplicator: skipping repeated callback id={cb_id}")
            try:
                await event.answer()
            except Exception as exc:
                # Не сняли спиннер на повторном нажатии: у врача кнопка крутится
                # до таймаута клиента, и он жмёт ещё раз — новый повтор.
                logger.debug("callback dedup: answer не прошёл id=%s (%s: %s)",
                             cb_id, type(exc).__name__, exc)
            return
        if len(HANDLED_CALLBACK_IDS) == HANDLED_CALLBACK_IDS.maxlen:
            _HANDLED_CALLBACK_SET.discard(HANDLED_CALLBACK_IDS[0])
        HANDLED_CALLBACK_IDS.append(cb_id)
        _HANDLED_CALLBACK_SET.add(cb_id)

    answered = False
    try:
        await assistant.handle_quiz_callback(bot_client, event)
        answered = True
    except Exception as e:
        if "MessageNotModifiedError" in type(e).__name__ or "Content of the message was not modified" in str(e):
            pass
        else:
            logger.exception(f"Unexpected error in CallbackQuery handler: {e}")
    finally:
        # event.answer() вызывается в начале обработки внутри handle_quiz_callback. Если
        # исключение случалось раньше, ответ Telegram не уходил и у врача
        # крутился спиннер на кнопке до таймаута клиента.
        if not answered:
            try:
                await event.answer()
            except Exception as exc:
                # Последняя попытка снять спиннер провалилась — врач видит вечно
                # крутящуюся кнопку и не знает, что ответ уже не придёт.
                logger.warning("callback: спиннер не снят (%s: %s) — у врача "
                               "кнопка крутится до таймаута клиента",
                               type(exc).__name__, exc)

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
    
    # Ищем, какой топик назначен для этого чата в REPORT_TARGETS.
    # Через resolve_report_targets, иначе элемент-не-словарь ронял обработчик
    # на AttributeError вместо ответа врачу.
    target_topic = None
    for target in resolve_report_targets():
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
            except Exception as exc:
                # Голый `except:` здесь ловил и CancelledError с KeyboardInterrupt,
                # то есть остановка бота выглядела как «нет прав на удаление».
                # Сама неудача безобидна — служебная строка просто остаётся.
                logger.debug("weekly: служебное сообщение не удалено (%s: %s)",
                             type(exc).__name__, exc)
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
class TelethonEventAdapter:
    def __init__(self, message):
        self.message = message
        self.client = message.client
        
    def __getattr__(self, name):
        return getattr(self.message, name)
        
    async def get_sender(self):
        return await self.message.get_sender()

async def sync_history():
    """Докачивает сообщения, пропущенные во время офлайна."""
    last_id = await asyncio.wait_for(database.get_last_msg_id(), timeout=30)
    if last_id == 0:
        logger.info("🆕 База пуста, синхронизация пропущена.")
        return

    logger.info(f"🔄 Проверка пропущенных сообщений с ID {last_id}...")
    count = 0
    last_synced_message = None
    
    synced_albums = {}
    synced_singles = []
    # Голосовые окна собираем ОТДЕЛЬНО от медиа: clinical_media_kind для них
    # отдаёт None, поэтому ни в synced_singles, ни в догон неразобранного
    # (has_media = 1) они не попадают, и без этого списка расшифровку получало
    # ровно одно голосовое окна — последнее.
    synced_voices = []
    # Автора спрашиваем один раз на догоняющий проход, а не на каждое
    # сообщение. Уникальных отправителей в чате сотни (по локальному снимку —
    # 749), а догонять приходится тысячи реплик: без кэша одна и та же справка
    # запрашивалась бы сотни раз. Неудачи кэшируются тоже — у удалённого
    # аккаунта запрос падает всегда, и платить за него на каждой его реплике
    # незачем.
    sender_cache = {}

    # Запрашиваем сообщения, которые ID которых больше последнего в базе
    async for message in client.iter_messages(config.SOURCE_CHAT_ID, min_id=last_id, reverse=True):
        try:
            # Используем ту же логику парсинга, что в handle_new_message
            cache_key = message.sender_id
            if cache_key is not None and cache_key in sender_cache:
                sender_name, sender_username = sender_cache[cache_key]
            else:
                sender = None
                try:
                    sender = await asyncio.wait_for(
                        message.get_sender(),
                        timeout=SYNC_SENDER_TIMEOUT_SECONDS,
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

                if cache_key is not None:
                    sender_cache[cache_key] = (sender_name, sender_username)


            reply_to_id = message.reply_to.reply_to_msg_id if message.reply_to else None
            
            # То же единое правило, что и в живом обработчике: догон не должен
            # тащить в платный Vision превью ссылок, гифки, кружки и стикеры.
            # Отсев целиком внутри clinical_media_kind: она сама вызывает
            # image_document (media_tools.py:385) и до него отбрасывает стикер,
            # кружок, гифку и web_preview. Отдельный вызов image_document здесь
            # стоял, но результат никуда не шёл — читалось как «фильтр забыли
            # применить», хотя он применён.
            media_type = clinical_media_kind(message)
            has_media = media_type is not None
            
            synced_ok = await asyncio.wait_for(
                database.save_message(
                    msg_id=message.id,
                    reply_to_msg_id=reply_to_id,
                    sender_id=message.sender_id,
                    sender_name=sender_name,
                    sender_username=sender_username,
                    text=message.message or "",
                    date=message.date,
                    has_media=has_media,
                    media_type=media_type
                ),
                timeout=30,
            )
            # Здесь потеря особенно дорога: догоняем как раз то, чего в базе нет,
            # и следующий проход синхронизации стартует уже от MAX(msg_id) —
            # то есть выше этой дыры. Второго шанса не будет.
            if not synced_ok:
                logger.error(
                    "MESSAGE NOT PERSISTED during sync msg_id=%s — пропуск останется в базе",
                    message.id,
                )

            if has_media:
                if getattr(message, 'grouped_id', None):
                    g_id = message.grouped_id
                    if g_id not in synced_albums:
                        synced_albums[g_id] = []
                    synced_albums[g_id].append(message)
                else:
                    synced_singles.append(message)
            elif is_voice_message(message):
                synced_voices.append(message)

            last_synced_message = message
            count += 1
            if count % 25 == 0:
                runtime_guard.write_heartbeat("sync_history")
        except Exception as e:
            logger.error(f"Ошибка синхронизации сообщения {message.id}: {e}")
    
    # Медиа из догона ставим пачкой. Очередь вмещает MEDIA_QUEUE_MAX_SIZE, а за
    # месяц простоя набираются сотни снимков — влезет не всё, и это штатно:
    # непоставленные остаются в базе с пустым media_description и достаются
    # оттуда recover_pending_media_analysis на следующих запусках. Считаем
    # отложенные и пишем ОДНУ сводную строку вместо сотен ERROR подряд.
    media_queued = 0
    media_deferred = 0

    for g_id, msgs in synced_albums.items():
        try:
            combined_text = "\n".join([m.message for m in msgs if m.message]).strip()
            if await enqueue_media_analysis(msgs, msgs[0].id, combined_text, bulk=True):
                media_queued += 1
            else:
                media_deferred += 1
        except Exception as e:
            logger.error(f"Failed to enqueue synced album {g_id}: {e}")

    for msg in synced_singles:
        try:
            if await enqueue_media_analysis([msg], msg.id, msg.message or "", bulk=True):
                media_queued += 1
            else:
                media_deferred += 1
        except Exception as e:
            logger.error(f"Failed to enqueue synced single message {msg.id}: {e}")

    if media_queued or media_deferred:
        logger.info(
            "sync media analysis queued=%s deferred=%s queue_max=%s "
            "(отложенные ждут в базе с пустым описанием, их подберёт "
            "recover_pending_media_analysis)",
            media_queued, media_deferred, MEDIA_QUEUE_MAX_SIZE,
        )

    # Голосовые окна отдаём ОТДЕЛЬНОЙ задаче, а не расшифровываем здесь.
    #
    # Внутри sync_history этого делать нельзя: подъём ждёт синхронизацию ровно
    # SYNC_HISTORY_TIMEOUT_SECONDS (900 с) и по таймауту роняет весь start_bot, а
    # десять голосовых по VOICE_ITEM_BUDGET_SECONDS — это до 2250 с. Бот ушёл бы
    # в цикл перезапусков ровно так же, как уходил при потолке синхронизации
    # 300 с. Отдельная задача со своим бюджетом подъём не срывает: сообщения уже
    # в базе, расшифровка догоняется отдельным UPDATE.
    if synced_voices:
        logger.info(
            "sync voice backlog: голосовых в окне %s (предел за заход %s)",
            len(synced_voices), VOICE_BACKLOG_MAX_ITEMS,
        )
        runtime_guard.create_task(
            transcribe_voice_backlog(synced_voices, source="sync"),
            name=f"voice_backlog_{synced_voices[-1].id}",
        )

    if count > 0:
        logger.info(f"✅ Синхронизация завершена. Докачано {count} сообщений.")
        if last_synced_message:
            # Медиа выше уже поставлено в очередь анализа. Прогонять последнее
            # сообщение ещё и через handle_new_message нельзя: оно скачается и
            # проанализируется повторно, а голосовое будет заново расшифровано
            # И ПОВТОРНО ОПУБЛИКОВАНО в чат — пользователи видели дубль
            # транскрипции при каждом рестарте и каждом прогоне watchdog'а.
            already_enqueued = {m.id for m in synced_singles}
            for msgs in synced_albums.values():
                already_enqueued.update(m.id for m in msgs)
            # Голосовые уже отданы в догон расшифровки. Прогон последнего из них
            # через handle_new_message расшифровал бы его вторым платным вызовом
            # и опубликовал транскрипцию дважды.
            already_enqueued.update(m.id for m in synced_voices)

            if last_synced_message.id in already_enqueued:
                logger.info(
                    f"🚀 Последнее пропущенное msg_id={last_synced_message.id} уже "
                    f"в очереди анализа медиа — повторно не обрабатываем."
                )
            else:
                logger.info(f"🚀 Обрабатываем последнее пропущенное сообщение msg_id={last_synced_message.id}...")
                adapter_event = TelethonEventAdapter(last_synced_message)
                runtime_guard.create_task(handle_new_message(adapter_event), name=f"sync_process_{last_synced_message.id}")
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
            # Тип обязателен: у asyncio.TimeoutError пустой str(), и в журнале
            # оставалось «health_check failed 1/3: » — четыре записи из четырёх
            # без единого слова о причине, притом что это путь к принудительному
            # перезапуску процесса. exc_info даёт стек: этот блок накрывает и
            # обращения к Telegram, и синхронизацию истории, и разбор ответа, а
            # различить их по тексту исключения нельзя.
            logger.error(
                "health_check failed %s/%s: %s: %s",
                failure_count,
                HEALTH_FAILURE_LIMIT,
                type(exc).__name__,
                exc,
                exc_info=True,
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

    # Сердцебиение запускается ЗДЕСЬ, а не в конце вместе с остальными задачами.
    #
    # Сторож убивает процесс, если heartbeat не обновлялся WATCHDOG_STALE_SECONDS
    # (300 с). До этой правки между write_heartbeat("start_bot") и запуском
    # heartbeat_task шёл весь сетевой подъём, и его собственные таймауты
    # складывались в бюджет БОЛЬШЕ сторожевого: client.start() 120 +
    # bot_client.start() 120 + get_my_id() 60 плюс init_assistant — свыше 300 с.
    # На медленной сети, то есть ровно тогда, когда подъём и без того труден,
    # сторож стрелял в процесс посреди подключения, start.bat поднимал его
    # заново, и бот уходил в цикл перезапусков, ни разу не встав.
    #
    # Держать сердцебиение с самого начала безопасно: каждый шаг подъёма и так
    # ограничен своим asyncio.wait_for, зависнуть навсегда ни один не может, а
    # сторож проверяет живость цикла событий — во время подключения цикл жив.
    runtime_guard.create_task(heartbeat_task(), "heartbeat")

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
    
    # heartbeat уже запущен в начале start_bot — до сетевого подъёма.
    runtime_guard.create_task(scheduler_task(bot_client), "scheduler")
    runtime_guard.create_task(pm_ping_scheduler_task(bot_client), "pm_ping_scheduler")
    runtime_guard.create_task(runtime_telemetry_task(), "runtime_telemetry")
    runtime_guard.create_task(summary_watchdog_task(), "summary_watchdog")
    runtime_guard.create_task(health_watchdog_task(), "health_watchdog")
    runtime_guard.create_task(media_recovery_task(), "media_recovery")
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
