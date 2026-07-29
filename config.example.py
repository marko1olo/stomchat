# Шаблон конфигурации. Скопируйте его в config.py (config.py в .gitignore и на
# боевую машину через git не уезжает), а сами значения впишите в .env рядом.
#
# Имена ниже — КОНТРАКТ с кодом: он читает их как config.ИМЯ, и отсутствие
# любого из них даёт AttributeError. Цена промаха разная и она измерена:
#   * без API_ID или SOURCE_CHAT_ID падает импорт main.py (TelegramClient и
#     WATCHED_CHATS собираются на уровне модуля) — бот не поднимается совсем,
#     зато громко;
#   * без REPORT_TARGETS бот поднимается и выглядит живым, но рассылка молча
#     пуста: 749 врачей не получают ни дайджеста, ни недельной сводки, а в
#     журнале об этом одна строка.
# Полноту этого файла сторожит test_config_contract.py.
#
# Значений с рабочей машины здесь нет ни одного и быть не должно: ни токена, ни
# ключа, ни id чата, ни имени сессии. Всё это живёт только в .env.
import os
import sys
from dotenv import load_dotenv
import json

# Загружаем переменные из .env файла
load_dotenv()

# --- ФУНКЦИЯ ДЛЯ БЕЗОПАСНОГО ПОЛУЧЕНИЯ ПЕРЕМЕННЫХ ---
def get_env(key, default=None, required=False):
    value = os.getenv(key, default)
    if required and not value:
        print(f"ОШИБКА: Не найдена обязательная переменная окружения: {key}")
        print("Проверьте ваш файл .env")
        sys.exit(1)
    return value

# --- ЗАГРУЗКА И ВАЛИДАЦИЯ ---
BOT_TOKEN = get_env("TG_BOT_TOKEN", required=True)
# 1. Telegram Credentials
# int() обязателен: Telethon с API_ID-строкой не авторизуется, а ошибка вылезет
# уже в сети, где её примут за проблему связи. Лучше отказ на старте.
try:
    API_ID = int(get_env("TG_API_ID", required=True))
except ValueError:
    print("ОШИБКА: TG_API_ID должен быть числом.")
    sys.exit(1)

API_HASH = get_env("TG_API_HASH", required=True)
# Имя файла сессии Telethon. Пусто намеренно: имя с рабочей машины в версионном
# файле — утечка, которую .gitignore не прикрывает (там только сам *.session).
SESSION_NAME = get_env("TG_SESSION_NAME", "")

# 2. Настройки чатов
# Основной чат врачей. Если ID нет в конфиге, скрипт не упадет, но предупредит:
# при SOURCE_CHAT_ID=None бот не увидит ни одной реплики врачей и будет работать
# только в тестовом чате.
try:
    source_chat_raw = get_env("SOURCE_CHAT_ID")
    # Удаляем пробелы, приводим к int
    SOURCE_CHAT_ID = int(source_chat_raw) if source_chat_raw else None
except ValueError:
    # Само значение в текст не подставляем: id чата не должен попадать в stdout,
    # откуда его подберёт журнал запуска.
    print("ПРЕДУПРЕЖДЕНИЕ: SOURCE_CHAT_ID не число. Основной чат не отслеживается.")
    SOURCE_CHAT_ID = None

# Куда бот отвечает на служебные команды и куда шлёт технические сводки.
# Пусто — ответы уходить некуда, и врач видит тишину вместо ответа.
REPORT_CHAT_ID = get_env("REPORT_CHAT_ID", "")

# Кому уходят дайджест и недельная сводка. Формат в .env — одна строка JSON:
# REPORT_TARGETS=[{"chat_id": <id чата>, "topic_id": <id темы или null>}]
# Пустой список законен (рассылка выключена), но это и есть режим «никто ничего
# не получает» — проверьте, что тут именно то, что вы хотели.
try:
    targets_raw = get_env("REPORT_TARGETS", "[]")
    REPORT_TARGETS = json.loads(targets_raw)
except Exception as e:
    # Без эмодзи: config импортируется ДО настройки логирования, а cp1251-консоль
    # Windows на символе вне cp1251 роняет сам print — тогда в трейсбеке стоит
    # строка печати, а не настоящая ошибка .env, и причину не видно вообще.
    # Кириллица в cp1251 проходит.
    print(f"ОШИБКА разбора REPORT_TARGETS: {e}. Рассылка сводок не уйдёт никому.")
    REPORT_TARGETS = []

# 3. API Keys (Списки)
# В .env перечисляются через запятую: ключей несколько, чтобы упор в квоту одного
# не останавливал разбор чата целиком.
def parse_keys(key_string):
    if not key_string:
        return []
    # Разбиваем по запятой, удаляем пробелы
    return [k.strip() for k in key_string.split(',') if k.strip()]
# Модель для разбора снимков. Пусто — подпись к снимку не появится.
GROQ_VISION_MODEL = get_env("GROQ_VISION_MODEL", "")
GOOGLE_KEYS = parse_keys(get_env("GOOGLE_API_KEYS"))
GROQ_KEYS = parse_keys(get_env("GROQ_API_KEYS"))

if not GOOGLE_KEYS and not GROQ_KEYS:
    print("ВНИМАНИЕ: Не найдены ключи для нейросетей (GOOGLE или GROQ). Саммери работать не будет.")
# Токен Telegraph. Без него длинный дайджест не выносится на страницу и режется
# жёстким пределом Telegram в 4096 символов.
TELEGRAPH_TOKEN = get_env("TELEGRAPH_TOKEN")
# 4. Модели
# Имена моделей не зашиты в шаблон намеренно: провайдеры снимают модели с
# обслуживания, и зашитое имя тихо превращается в отказ генерации.
GEMINI_MODEL = get_env("GEMINI_MODEL", "")
GROQ_MODEL = get_env("GROQ_MODEL", "")
# --- НАСТРОЙКИ ПОИСКА ---
# Провайдер веб-поиска. Пусто — поиск по внешним источникам не работает, и на
# вопрос врача уйдёт ответ без ссылок.
SEARCH_PROVIDER = get_env("SEARCH_PROVIDER", "")
TAVILY_API_KEY = get_env("TAVILY_API_KEY")

if SEARCH_PROVIDER == "tavily" and not TAVILY_API_KEY:
    print("ВНИМАНИЕ: Выбран провайдер Tavily, но TAVILY_API_KEY не установлен. Поиск работать не будет.")

# Файл базы. Путь относительный: абсолютный путь с именем пользователя в
# версионном файле — та же утечка, что и имя сессии.
DB_PATH = get_env("DB_PATH", "stomat_bot.db")

SUMMARY_INTERVAL = 0
MAX_MSGS = 0
# Час отправки дайджеста. НЕ независим от main.DIGEST_WINDOW_START_HOUR: при
# REPORT_HOUR меньше 20 стык окон закрывает daily_window_start() через
# last_sent_date, но ноль здесь — это выпуск в полночь, а не «выключено».
REPORT_HOUR = 0

from dental_vocab import DENTAL_KEYWORDS


print("Конфигурация загружена.")
