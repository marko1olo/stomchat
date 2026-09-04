import asyncio
import contextlib
import copy
from datetime import datetime, timedelta
import html
import json
import logging
import math
import os
import random
import re
import sqlite3
import threading
import time

from cachetools import TTLCache

import blocking_tools
import config
import database
from dental_vocab import (
    DENTAL_KEYWORDS as DENTAL_KEYWORDS,
    SHORT_DENTAL_TERMS,
    has_dental_term,
    is_dental_keyword,
)
import html_safe
import media_tools
import taxonomy
import tg_safety
import user_memory
import vision
# Слой качества веб-поиска: разбор выдачи, отсев рекламы клиник, заземлённый
# ответ со ссылками. Импорт безвреден — ни сети, ни конфига, ни логирования
# (это сторожит test_web_lookup.py разбором дерева импортов).
import web_lookup

generate_gemini_text_async = getattr(blocking_tools, "generate_gemini_text_async", None)
generate_pm_supplement_async = getattr(blocking_tools, "generate_pm_supplement_async", None)

logger = logging.getLogger("assistant")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(SCRIPT_DIR, "assistant_state.json")
LOG_PATH = os.path.join(SCRIPT_DIR, "shadow_assistant.log")
TEST_CHAT_ID = -1003735006121
TEST_TOPIC_ID = 26

SHADOW_TESTING = os.getenv("SHADOW_TESTING", "False").lower() in ("true", "1", "yes")
BOT_ID = None
# @username бота. Резолвится вместе с BOT_ID: в группе к боту обращаются по
# имени, а не по числовому id, и без этого поля единственным способом узнать
# имя оставался литерал, зашитый в main.py.
BOT_USERNAME = None
LAST_REFEREE_RUN = datetime(2000, 1, 1)
USER_COOLDOWNS = TTLCache(maxsize=10000, ttl=86400) # 24 часа
REPLIED_MSG_IDS = TTLCache(maxsize=50000, ttl=604800) # 7 дней

# Бюджет доставки готовой сводки. Без него сгенерированная (и уже оплаченная)
# сводка не уходит НИКОМУ: клиент настроен как timeout=30, request_retries=10,
# flood_sleep_threshold=20 (main.py:883, 900-910), то есть один await висит до
# 500 с, родительского срока на этом пути нет вовсе, и в журнале об этом ни
# строки. Число не новое: столько же в summarizer.TELEGRAM_SEND_TIMEOUT_SECONDS
# и tg_safety.DEFAULT_TIMEOUT_SECONDS — второе число рядом разъехалось бы.
SUMMARY_DELIVERY_TIMEOUT_SECONDS = 90

# Бюджет правки сообщения по нажатию инлайн-кнопки. Спиннер на кнопке снимает
# event.answer() строкой ниже, поэтому зависший edit_message означает вечно
# крутящуюся кнопку: врач думает, что бот считает, и жмёт снова. 25 = 20 + 5 =
# flood_sleep_threshold + retry_delay (main.py:883, 907) — короткое ожидание
# telethon пересиживает сам, а платить за вторую из десяти внутренних попыток
# врач не должен. Меньше бюджета сводки (90) и общего сетевого потолка (60).
CALLBACK_EDIT_TIMEOUT_SECONDS = 25

# Бюджет последнего слова врачу на пути разбора присланного файла. Это ЕДИНСТВЕННОЕ
# сообщение, которым врач узнаёт, что снимок не открылся, и до этой правки оно шло
# голым bot_client.edit_message без срока: при зависшем Telegram врач остаётся перед
# статусом «Скачиваю и анализирую… Подождите» навсегда, замок на пользователя не
# отпускается (main.py:2272), и все его следующие вопросы не обрабатываются до
# перезапуска процесса — а в журнале об этом ни строки, зависание не исключение.
# 45 = половина бюджета доставки (90): отказ короче ответа и обязан уложиться
# быстрее, чем врач напишет следующее сообщение. Выводится из числа рядом, чтобы
# два срока на одном пути не разъехались.
PM_STATUS_EDIT_TIMEOUT_SECONDS = SUMMARY_DELIVERY_TIMEOUT_SECONDS // 2

# --- Бюджет команды веб-поиска (/web, /найди) --------------------------------
#
# Считается СЛОЖЕНИЕМ этапов, а не задаётся числом: класс «внутренний срок больше
# внешнего» в этом проекте всплывал четыре раза, и каждый раз ровно потому, что
# два разумных числа лежали в разных файлах и никто их не сопоставлял.
#
# Арифметика (числа — из web_lookup, где выведены из бюджета ребёнка плюс запаса
# на подъём подпроцесса, blocking_tools._SUBPROCESS_STARTUP_SLACK_SECONDS = 10):
#   статус врачу                        20
#   поиск: (45 + 10) x 2 попытки       110   = web_lookup.SEARCH_TOTAL_COST_SECONDS
#   генерация ответа: 90 + 10          100
#   троттлинг LLM-шлюза (3 с)            5
#   доставка ответа                     90
#   уборка статусного сообщения         15
#   ---------------------------------------
#   итого                              340
# Поиск (110) строго меньше общего срока (340), и генерация получает ОСТАТОК, а не
# своё желаемое число: run_lookup считает дедлайн один раз и вычитает из него всё.
WEB_STATUS_TIMEOUT_SECONDS = 20
WEB_STATUS_CLEANUP_TIMEOUT_SECONDS = 15
WEB_DELIVERY_TIMEOUT_SECONDS = SUMMARY_DELIVERY_TIMEOUT_SECONDS
WEB_LOOKUP_BUDGET_SECONDS = web_lookup.LOOKUP_TOTAL_COST_SECONDS
WEB_COMMAND_TIMEOUT_SECONDS = (
    WEB_STATUS_TIMEOUT_SECONDS
    + WEB_LOOKUP_BUDGET_SECONDS
    + WEB_DELIVERY_TIMEOUT_SECONDS
    + WEB_STATUS_CLEANUP_TIMEOUT_SECONDS
)
# Внешний поиск — запрос к чужому сервису и целый подпроцесс. Без паузы один врач,
# задавший пять вопросов подряд, сжигает квоту провайдера на весь чат из 749
# человек. Столько же, сколько у /итог и прямого вопроса в группе.
WEB_COOLDOWN_SECONDS = 30
# Заголовок ответа. Врач обязан видеть, что это НЕ база знаний чата, а открытые
# источники: доверие к утверждению у них разное, и путать их нельзя.
WEB_ANSWER_HEADER = "🌐 <b>По открытым источникам</b>\n\n"

# Глубина памяти диалога в ЛС
PM_HISTORY_LIMIT = 50

STYLE_PROMPTS = {
    "colleague_friendly": "Твой стиль общения — сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности и без эмодзи-кривляния.",
    "clinical_dry": "Твой стиль общения — сухие клинические факты. Отвечай максимально строго, академично, лаконично и по делу. Категорически ЗАПРЕЩЕНЫ любые шутки, каламбуры, смайлы, метафоры или лирические отступления. Только голая наука, стандарты EBM, дозировки и анатомические обоснования. Никаких смайлов вообще.",
    "humor_cynic": "Твой стиль общения — ироничный стоматолог-циник с легким профессиональным юмором. Ты понимаешь реалии врачебных будней, профессиональный юмор про сложные каналы, перелечивания и пациентов, но сохраняешь такт и клиническую грамотность. Тон: живой, ироничный, профессиональный, без панибратства и без дурацких эмодзи."
}

# Стиль по умолчанию: для него отдельная вставка в промпт не нужна — тон
# «коллега-эксперт» и так задан основными правилами.
DEFAULT_STYLE = "colleague_friendly"


def style_instruction_block(selected_style):
    """
    Вставка про стиль общения для промпта в общем чате.

    Здесь стоял if ровно на один стиль: clinical_dry. Врач, выбравший в /style
    «Ироничный циник», в общем чате не получал ничего — настройка молча не
    работала, хотя кнопка есть и в меню, и в /help. В ЛС тот же выбор
    учитывался через STYLE_PROMPTS, то есть бот вёл себя по-разному в двух
    местах при одной и той же настройке.

    Строгий текст для clinical_dry сохранён как был: он жёстче словарного и
    держит запрет на смайлы, который модель иначе нарушает.
    """
    if selected_style == "clinical_dry":
        return (
            "\n[КРИТИЧЕСКИЙ СТИЛЬ: Твой собеседник предпочитает строгие клинические факты. "
            "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать шутки, каламбуры, сарказм, иронию, смайлики и воду. "
            "Отвечай максимально сухо, строго научно и профессионально, оперируя только доказанными фактами. "
            "Не пиши никаких смайликов вообще!]\n"
        )
    if selected_style and selected_style != DEFAULT_STYLE and selected_style in STYLE_PROMPTS:
        return f"\n[СТИЛЬ ОБЩЕНИЯ: {STYLE_PROMPTS[selected_style]}]\n"
    return ""


AD_HINTS = [
    "\n\n<i>💡 Кстати, вы можете прислать мне рентген-снимок или задать клинический вопрос в ЛС — там я помню историю диалога и общаюсь тет-а-тет.</i>",
    "\n\n<i>💡 Если хотите обсудить сложный случай приватно, пишите в ЛС. Там я храню глубокую память диалога и не отвлекаю коллег в общей группе.</i>",
    "\n\n<i>💡 В ЛС я работаю как персональный ассистент: принимаю голосовые сообщения, ищу статьи в PubMed и храню ваши закладки.</i>",
    "\n\n<i>💡 Присылайте снимки (ОПТГ, КЛКТ, прицельные) — в ЛС разберу клинический случай с анализом патологии.</i>",
]

# Контекстные подсказки по теме ответа
AD_HINTS_CONTEXTUAL = {
    "anesthesia": "\n\n<i>💡 Напишите мне в ЛС вес пациента и препарат — рассчитаю точные дозы и карпулы за секунду.</i>",
    "antibiotic": "\n\n<i>💡 В ЛС могу подобрать схему антибиотикопрофилактики или постоперационного курса с учётом аллергий и соматики пациента.</i>",
    "pubmed": "\n\n<i>💡 Хотите свежие статьи PubMed по этой теме? Напишите мне в ЛС — сделаю поиск с источниками и ссылками.</i>",
    "implant": "\n\n<i>💡 В ЛС могу разобрать рентген-снимок имплантата или подобрать протокол нагрузки по вашему клиническому случаю.</i>",
    "endo": "\n\n<i>💡 Пришлите прицельный или КЛКТ в ЛС — разберу анатомию каналов, рабочую длину и сложность эндодонтического лечения.</i>",
    "xray": "\n\n<i>💡 Пришлите рентген-снимок в ЛС — опишу патологию, плотность, периапикальный статус и дам дифдиагноз.</i>",
}


ENABLE_AD_HINTS = False  # По умолчанию выключено. Поставьте True для включения контекстных подсказок.


def get_ad_hint(reply_text: str = "") -> str:
    """
    Возвращает контекстно-зависимый хинт о ЛС, исходя из темы ответа бота.
    Если ENABLE_AD_HINTS = False — реклама полностью отключена.
    """
    if not ENABLE_AD_HINTS:
        return ""
    if not reply_text:
        return random.choice(AD_HINTS)
    low = reply_text.lower()
    if any(w in low for w in ["артикаин", "мепивакаин", "лидокаин", "ультракаин", "карпул", "анестези"]):
        return AD_HINTS_CONTEXTUAL["anesthesia"]
    if any(w in low for w in ["амоксициллин", "клиндамицин", "антибиотик", "метронидазол", "профилактик"]):
        return AD_HINTS_CONTEXTUAL["antibiotic"]
    if any(w in low for w in ["pubmed", "пабмед", "cochrane", "кохран", "метаанализ", "исследован"]):
        return AD_HINTS_CONTEXTUAL["pubmed"]
    if any(w in low for w in ["имплантат", "имплант", "периимплантит", "нагрузк"]):
        return AD_HINTS_CONTEXTUAL["implant"]
    if any(w in low for w in ["корневой канал", "пульп", "эндодонт", "ирригац", "гуттаперч", "апекс"]):
        return AD_HINTS_CONTEXTUAL["endo"]
    if any(w in low for w in ["рентген", "снимок", "оптг", "клкт", "прицельн"]):
        return AD_HINTS_CONTEXTUAL["xray"]
    return random.choice(AD_HINTS)

async def generate_user_portrait(user_id):
    try:
        # Загружаем последние сообщения пользователя из группы
        msgs = await database.get_user_recent_group_messages(user_id, limit=50)
        if not msgs or len(msgs) < 3:
            return "Недостаточно сообщений в общей группе для анализа клинического профиля."
            
        context_str = "\n".join([f"- {m}" for m in msgs])
        prompt = f"""Ты — ИИ-аналитик профессионального сообщества врачей-стоматологов StomChat.
Проанализируй список сообщений врача-стоматолога в общем чате и составь его краткий профессиональный портрет в 1-2 предложениях (не более 300 символов).

Задачи:
1. Определи специализацию врача (например: терапевт, хирург-имплантолог, ортопед, детский стоматолог, ортодонт, гнатолог).
2. Выдели темы и материалы, о которых он чаще всего пишет или спрашивает (например: вертипреп, эндодонтия, адгезивы, коффердам, КЛКТ).
3. Пиши лаконично, профессионально, только факты.

Сообщения врача:
{context_str}

Вывод (строго 1-2 предложения):
"""
        status_ctx = {"kind": "llama_triage", "thinking_level": "LOW"}
        response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=20)
        if error or not response or not getattr(response, "text", None):
            return "Недостаточно сообщений в общей группе для анализа клинического профиля."
        return response.text.strip()
    except Exception as e:
        logger.error(f"Error generating user portrait for {user_id}: {e}")
        return "Ошибка при составлении клинического профиля."

async def check_dialogue_continuation_triage(dialogue_chain, recent_chat=None):
    try:
        context_str = "\n".join(dialogue_chain)
        recent_chat_str = "\n".join(recent_chat) if recent_chat else "(нет недавних сообщений)"
        
        triage_prompt = f"""Ты — ИИ-координатор профессионального стоматологического чата "StomChat".
В чате идет дискуссия с участием нашего ИИ-ассистента (Бота). 
Бот собирается ответить на сообщение из цепочки диалога:
{context_str}

Текущее живое обсуждение в группе (последние сообщения чата прямо сейчас):
{recent_chat_str}

Задачи анализа:
1. Проверь, не сместилась ли тема обсуждения в группе. Если в последних сообщениях чата люди уже активно обсуждают другую тему, совершенно не связанную с цепочкой диалога бота, выведи NO (встревать со старой темой — это спам).
2. Оцени характер реплики пользователя. Если пользователь просто спорит с Ботом, иронизирует, троллит, выражает недовольство Ботом (например, пишет "да ладно", "чушь", "все понятно", "хватит спамить") — выведи NO.
3. Бот должен продолжить диалог (YES) только если пользователь задает конкретный содержательный клинический или технический вопрос по делу, и эта тема все еще актуальна в последних сообщениях чата.

Выведи строго одно слово:
YES — если Боту уместно ответить прямо сейчас.
NO — если Боту лучше промолчать (тема сменилась или идет пустой спор/троллинг).
"""
        triage_ctx = {"kind": "llama_triage", "thinking_level": "LOW"}
        response, error = await generate_gemini_text_async(triage_prompt, triage_ctx, timeout=8)
        if error or not response or not getattr(response, "text", None):
            return False
        res = response.text.strip().upper()
        return res.startswith("YES")
    except Exception as e:
        logger.error(f"Error in dialogue continuation triage: {e}")
        return False

def check_user_cooldown(chat_id, user_id, command, seconds=30):
    """
    Сколько секунд осталось ждать. 0 — можно работать, отсчёт начат заново.

    Округление ВВЕРХ, а не int(). int() отбрасывает дробную часть, и последняя
    секунда окна теряется целиком: замер на живом вызове — два обращения подряд
    к pm_chat (seconds=5) дают elapsed=0.0001 и int(4.9999) = 4, то есть врачу
    обещают 4 секунды при фактических 5. Хуже другое: при elapsed=4.5 остаток
    0.5 превращается в int(0.5) = 0, а все вызывающие читают 0 как «кулдауна
    нет» (`if cooldown > 0`, `if not check_user_cooldown(...)`). Отметка времени
    при этом НЕ обновляется — она переписывается только на выходе из функции
    после `if`. Итог: в последнюю секунду каждого окна запрос проходит, и
    подряд идущие сообщения могут проскакивать вечно, сдвигаясь на эту секунду.
    Для /quiz (60 с) и pm_rate_notice (30 с) это та же дыра, только шире.

    ceil даёт минимум 1, пока окно не закрылось, поэтому «осталось 0» теперь
    означает ровно то, что написано.
    """
    key = (chat_id, user_id, command)
    now = datetime.now()
    if key in USER_COOLDOWNS:
        elapsed = (now - USER_COOLDOWNS[key]).total_seconds()
        if elapsed < seconds:
            return math.ceil(seconds - elapsed)
    USER_COOLDOWNS[key] = now
    return 0

TELEGRAM_MESSAGE_LIMIT = 4000


async def send_message_chunks_async(bot_client, chat_id, text, **kwargs):
    """
    Отправляет длинный ответ частями, каждая из которых валидна сама по себе.

    Прежняя версия резала по абзацам, не следя за тегами: ответ с <b> через
    границу абзаца давал часть с незакрытым тегом и часть с непарным
    закрывающим — Telegram отклонял ОБЕ, и врач терял ответ на клинический
    вопрос целиком. Одиночный длинный абзац рубился срезом p[i:i+4000], то
    есть мог разорвать тег или HTML-сущность.

    Разбиение вынесено в html_safe: незакрытые теги закрываются в конце части
    и переоткрываются в начале следующей.
    """
    for chunk in html_safe.split_html(text, limit=TELEGRAM_MESSAGE_LIMIT):
        await tg_safety.send_message(bot_client, chat_id, chunk, logger=logger, **kwargs)

async def resolve_bot_identity(bot_client):
    """
    Определяет id и @username бота. Возвращает True при успехе.

    Имя нужно отдельно от id: в группе бота зовут «@имя», а не числом, и до
    сих пор единственным работающим способом его узнать был литерал
    "@stomchat_bot", зашитый в main.py.
    """
    global BOT_ID, BOT_USERNAME
    try:
        me = await bot_client.get_me()
    except Exception as e:
        logger.error(f"Failed to resolve bot identity: {e}")
        return False
    BOT_ID = me.id
    BOT_USERNAME = (getattr(me, "username", None) or "").lstrip("@").lower() or None
    return True


# Групповые команды. Разбирает их main.run_group_features, а объявлены они
# ЗДЕСЬ и один раз: отсюда собирается меню Telegram для групп, отсюда же врач
# читает их в /help. До этого объявления не было нигде — ни в меню, ни в /help,
# ни в правиле 11 промпта (замер: 29 обрабатываемых команд, видно 13). То есть
# 749 врачей могли попросить сводку обсуждения или викторину и не узнать об этом
# никогда.
#
# Поля: каноническое имя (только оно попадает в меню Telegram — там разрешены
# лишь латиница, цифры и подчёркивание), псевдонимы в порядке разбора, вид
# сравнения и описание для меню.
#   "exact"  — совпадение целиком, как `cmd_lower in ("/poll", "/кейс")`;
#   "prefix" — начало строки, как `cmd_lower.startswith(("/summary", ...))`;
#   "arg"    — начало строки И аргумент через пробел, как `startswith("/ask ")`.
GROUP_COMMANDS = (
    ("summary", ("/summary", "/итог", "/sum"), "prefix",
     "Сводка обсуждения в чате (синонимы: /итог, /sum)"),
    ("ask", ("/ask",), "arg",
     "Задать боту клинический вопрос прямо в чате"),
    ("poll", ("/poll", "/кейс"), "exact",
     "Клиническая викторина для чата (синоним: /кейс)"),
    ("what", ("/what", "/что"), "arg",
     "Коротко объяснить термин (синоним: /что)"),
    ("save", ("/save", "/сохранить"), "exact",
     "Ответом на пост — сохранить его в свои закладки"),
    ("del", ("/del", "/delete", "/wipe"), "exact",
     "Только админам: удалить пост, на который вы ответили"),
)

# Где работают групповые команды. Врачу это надо сказать словами: в ЛС он их
# набирал и не получал ничего осмысленного — текст уходил в платную генерацию
# как клинический вопрос.
GROUP_COMMANDS_HINT = (
    "👥 <i>Эта команда работает в общем чате сообщества, а не в личке. "
    "Наберите её там — сводку, викторину и закладки бот делает по чату.</i>"
)


def resolve_group_command(text):
    """Каноническое имя групповой команды по тексту сообщения либо None.

    Сравнение повторяет разбор в main.run_group_features. Нужно затем, чтобы
    меню группы и /help объявляли ровно то, что бот исполняет: пункт меню, на
    который бот молчит, хуже отсутствия пункта — врач решит, что бот сломан.
    """
    cmd = (text or "").strip().lower()
    if not cmd.startswith("/"):
        return None
    for canonical, aliases, kind, _description in GROUP_COMMANDS:
        for alias in aliases:
            if kind == "exact" and cmd == alias:
                return canonical
            if kind == "prefix" and cmd.startswith(alias):
                return canonical
            # Команде с аргументом пустой вызов не подходит: main.py на
            # `/ask` без вопроса не делает ничего, и обещать обратное нельзя.
            if kind == "arg" and cmd.startswith(alias + " ") and cmd[len(alias) + 1:].strip():
                return canonical
    return None


class UserIntent:
    """Представление распознанного намерения пользователя (Zero-Slash Routing)."""
    def __init__(self, name: str | None, query: str = ""):
        self.name = name
        self.query = query

    def __eq__(self, other):
        if isinstance(other, str):
            return self.name == other
        if isinstance(other, UserIntent):
            return self.name == other.name and self.query == other.query
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return (self.name, self.query) == (other[0], other[1])
        if other is None:
            return self.name is None
        return False

    def __iter__(self):
        return iter((self.name, self.query))

    def __getitem__(self, index):
        return (self.name, self.query)[index]

    def __bool__(self):
        return self.name is not None

    def __str__(self):
        return self.name or ""

    def __repr__(self):
        return f"UserIntent(name={self.name!r}, query={self.query!r})"


# Intent Constants
INTENT_WEB_SEARCH = "INTENT_WEB_SEARCH"
INTENT_CALCULATOR = "INTENT_CALCULATOR"
INTENT_QUIZ = "INTENT_QUIZ"
INTENT_CASE = "INTENT_CASE"
INTENT_BOOKMARKS = "INTENT_BOOKMARKS"
INTENT_STYLE = "INTENT_STYLE"
INTENT_MENU = "INTENT_MENU"
INTENT_HELP = "INTENT_HELP"


def detect_user_intent(text: str) -> UserIntent:
    """
    Определяет клиническое намерение пользователя на естественном языке (Zero-Slash Routing).
    Позволяет врачу использовать бота без обязательного набора слэш-команд.
    """
    if not text or not isinstance(text, str):
        return UserIntent(None)

    clean = text.strip()
    norm = clean.lower()
    norm_no_punct = re.sub(r'[?!.,;:]+$', '', norm).strip()

    # 1. INTENT_MENU / INTENT_HELP
    is_clinical_emergency_help = bool(
        re.search(r'\b(первая|неотложная|скорая|доврачебная|оказание)\s+помощ', norm) or 
        re.search(r'\bпомощь\s+(при|пациент|взросл|дет)', norm)
    )

    if not is_clinical_emergency_help:
        if norm_no_punct in ('меню', 'главное меню', 'открой меню', 'покажи меню', 'кнопки меню',
                             'вызови меню', 'назад в меню', 'открой главное меню', 'покажи главное меню',
                             '/menu', '/меню', '/start', '⌨️ меню') or \
           re.match(r'^(?:открой|покажи|вызови|перейди\s+в|вернуться\s+в|назад\s+в)?\s*(?:главное\s+)?меню$', norm_no_punct):
            return UserIntent(INTENT_MENU)

        if norm_no_punct in ('помощь', 'хелп', 'help', 'справка', 'памятка', 'инструкция', 'инструкция к боту', 'команды', 'список команд', '/help') or \
           re.match(r'^(что\s+ты\s+умеешь|что\s+ты\s+можешь|что\s+умеешь|что\s+можешь|твои\s+возможности|возможности\s+бота)$', norm_no_punct) or \
           re.match(r'^(как\s+тобой\s+пользоваться|как\s+пользоваться\s+ботом|как\s+пользоваться)$', norm_no_punct) or \
           re.match(r'^(список\s+команд|какие\s+команды)$', norm_no_punct):
            return UserIntent(INTENT_MENU)

    # 2. INTENT_STYLE
    if norm_no_punct in ('стиль', 'стиль общения', 'настройка стиля', 'настройки стиля', 'выбор стиля', '/style') or \
       re.match(r'^(смени|сменить|измени|изменить|поменяй|поменять|переключи|переключить|настрой|настроить|выбери|выбрать)\s+(стиль|стиль\s+общения|тон|тон\s+общения)$', norm_no_punct) or \
       re.match(r'^хочу\s+другой\s+(стиль|тон)$', norm_no_punct) or \
       re.match(r'^(смени|поменяй|измени)\s+тон$', norm_no_punct) or \
       re.match(r'^(настройка|настройки|выбор)\s+(стиля|тона)$', norm_no_punct):
        return UserIntent(INTENT_STYLE)

    # 3. INTENT_BOOKMARKS
    bm_match = re.match(r'^(?:покажи\s+|открой\s+|список\s+|где\s+)?(?:мои\s+)?(?:клинические\s+)?(закладки|сохраненки|сохранёнки|сохраненные|сохранённые)(?:\s+(.*))?$', norm_no_punct)
    if bm_match:
        query_arg = (bm_match.group(2) or '').strip()
        if query_arg in ('посты', 'сообщения', 'статьи'):
            query_arg = ''
        return UserIntent(INTENT_BOOKMARKS, query_arg)

    if re.match(r'^(что|покажи\s+что)\s+я\s+(сохранил|сохранял|сохранила|сохраняла)(\s+(.*))?$', norm_no_punct):
        m = re.match(r'^(что|покажи\s+что)\s+я\s+(сохранил|сохранял|сохранила|сохраняла)(\s+(.*))?$', norm_no_punct)
        query_arg = (m.group(4) or '').strip()
        return UserIntent(INTENT_BOOKMARKS, query_arg)

    if re.match(r'^(сохраненные|сохранённые)\s+(посты|сообщения|статьи)(\s+(.*))?$', norm_no_punct):
        m = re.match(r'^(сохраненные|сохранённые)\s+(посты|сообщения|статьи)(\s+(.*))?$', norm_no_punct)
        query_arg = (m.group(4) or '').strip()
        return UserIntent(INTENT_BOOKMARKS, query_arg)

    # Быстрые кнопки постоянной клавиатуры
    if norm_no_punct in ('💊 препараты и дозы', 'препараты и дозы', '💊 препараты', 'дозы препаратов'):
        return UserIntent(INTENT_CALCULATOR, clean)

    if norm_no_punct in ('🔍 найти статью', 'найти статью', '🔍 найти', 'найти статьи'):
        return UserIntent(INTENT_WEB_SEARCH, '')

    if norm_no_punct in ('⭐ закладки', '⭐ мои закладки'):
        return UserIntent(INTENT_BOOKMARKS, '')

    # 4. INTENT_QUIZ
    if not re.match(r'^(мой\s+ответ|ответ|вариант)\s+[a-dа-г]\b', norm):
        if norm_no_punct in ('викторина', 'квиз', 'клиническая викторина', 'стоматологический квиз', 'тест по стоматологии', 'клинический квиз', 'клиническая задача', '/quiz') or \
           re.match(r'^(давай|хочу|запусти|проведи|сыграем\s+в|поиграем\s+в|го)\s+(клиническую\s+)?(викторину|квиз)$', norm_no_punct) or \
           re.match(r'^(проверь|проэкзаменуй|протестируй)\s+(мои\s+)?(знания|меня)$', norm_no_punct) or \
           re.match(r'^(дай|задай|хочу)\s+(мне\s+)?(клинический\s+)?(вопрос|задачу|задачку|тест)$', norm_no_punct) or \
           re.match(r'^хочу\s+тест$', norm_no_punct):
            return UserIntent(INTENT_QUIZ)

    # 5. INTENT_CASE
    is_case_description = bool(
        re.search(r'пациент(ка)?\s+\d+', norm) or
        re.search(r'жалобы\s+на', norm) or
        re.search(r'у\s+меня\s+(кейс|клинический\s+случай|пациент)', norm) or
        re.search(r'разбор\s+(кейса|случая)', norm) or
        re.search(r'клинический\s+случай\s*:', norm) or
        re.search(r'\b(зуб|зуба|зубе|зубом)\s+\d{2}\b', norm) or
        re.search(r'\b\d{2}\s+(зуб|зуба|зубе|зубом)\b', norm)
    )
    if not is_case_description:
        if norm_no_punct in ('клинический кейс', 'клинический симулятор', 'симулятор', 'интерактивный кейс', 'диагностический симулятор', 'симулятор кейсов', '/case') or \
           re.match(r'^(давай|хочу|запусти|начни|начать|сыграем\s+в|поиграем\s+в|го|включи)\s+(клинический\s+)?(кейс|симулятор)$', norm_no_punct) or \
           re.match(r'^(давай|хочу|запусти|начни|начать)\s+клинический\s+случай$', norm_no_punct) or \
           re.match(r'^(сыграем|поиграем|сыграть|поиграть)\s+в\s+диагностику$', norm_no_punct) or \
           re.match(r'^(сыграть|поиграть)\s+в\s+(кейс|симулятор)$', norm_no_punct) or \
           re.match(r'^(давай|хочу|запусти|включи|начни|начать)\s+симулятор$', norm_no_punct) or \
           re.match(r'^начать\s+кейс$', norm_no_punct):
            return UserIntent(INTENT_CASE)

    # 6. INTENT_CALCULATOR
    _anesthetic_drugs = r'(артикаин|ультракаин|убистезин|септонест|скандонест|мепивакаин|лидокаин|новокаин|бупивакаин|примакаин|брилокаин|анестетик)'
    if norm_no_punct in ('калькулятор', 'калькулятор анестезии', 'шпаргалка по анестезии', 'расчет анестезии', 'расчёт анестезии', 'дозы анестетиков', 'максимальная доза анестезии', 'расчет карпул', 'расчёт карпул', '/calc'):
        return UserIntent(INTENT_CALCULATOR, clean)

    if re.match(r'^(посчитай|рассчитай|расчет|расчёт|калькулятор)\s+(мне\s+)?(анестези[юия]|дозировк[уи]|доз[уа]|карпул[ы]?)\b', norm_no_punct) or \
       re.match(r'^(посчитай|рассчитай)\s+доз[уа]\b', norm_no_punct) or \
       re.match(r'^(сколько|какая)\s+(карпул|дозировка|доза|максимальная\s+доза)\b', norm_no_punct) or \
       re.match(r'^(максимальная\s+доза|дозировка|доза)\s+', norm_no_punct) or \
       re.match(rf'^(дозировк[аиеу]?|доза|дозы|расчет|расчёт)\s+(препарата\s+)?{_anesthetic_drugs}[а-я]*\b', norm_no_punct) or \
       re.match(rf'^(посчитай|рассчитай)\s+(дозировку\s+)?{_anesthetic_drugs}[а-я]*\b', norm_no_punct) or \
       re.match(r'^(дозировка|расчет|расчёт)\s+анестезии\b', norm_no_punct):
        return UserIntent(INTENT_CALCULATOR, clean)

    # 7. INTENT_WEB_SEARCH
    m_goog = re.match(r'^(погугли|загугли|гугли|погуглить)\s*(.*)$', norm, re.IGNORECASE)
    if m_goog:
        q = clean[len(m_goog.group(1)):].strip()
        return UserIntent(INTENT_WEB_SEARCH, q)

    m_net = re.match(r'^(поищи|найди|поиск)\s+в\s+(интернете|инете|сети|гугле|google)\s*(.*)$', norm, re.IGNORECASE)
    if m_net:
        prefix_len = len(norm) - len(m_net.group(3))
        q = clean[prefix_len:].strip()
        return UserIntent(INTENT_WEB_SEARCH, q)

    m_art = re.match(r'^(найди|поищи)\s+статьи\s+(про|о|об|по|для)?\s*(.*)$', norm, re.IGNORECASE)
    if m_art:
        raw_q = m_art.group(3).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    m_pub_full = re.match(r'^(статьи\s+на\s+pubmed|публикации\s+на\s+pubmed)\s+(про|о|об|по|для)?\s*(.*)$', norm, re.IGNORECASE)
    if m_pub_full:
        raw_q = m_pub_full.group(3).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    m_pub = re.match(r'^(что|посмотри\s+что)\s+(говорит|пишет|есть\s+в)\s+pubmed\s*(про|о|об|по)?\s*(.*)$', norm, re.IGNORECASE)
    if m_pub:
        raw_q = m_pub.group(4).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    m_pub_short = re.match(r'^(pubmed|пабмед)\s*[:\s]\s*(.*)$', norm, re.IGNORECASE)
    if m_pub_short:
        q = m_pub_short.group(2).strip()
        return UserIntent(INTENT_WEB_SEARCH, q)

    m_res = re.match(r'^(какие|есть\s+ли)\s+(свежие|новые|последние|научные)\s+исследования\s+(по|про|о|об)?\s*(.*)$', norm, re.IGNORECASE)
    if m_res:
        raw_q = m_res.group(4).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    m_proto = re.match(r'^(найди|поищи)\s+протокол\s*(.*)$', norm, re.IGNORECASE)
    if m_proto:
        raw_q = m_proto.group(2).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    return UserIntent(None)


async def classify_pm_intent_semantic_async(text: str) -> dict:
    """
    Анализирует естественный язык врача через быстрый LLM-триаж.
    Определяет, нужен ли внешний веб-поиск/PubMed, расчет анестетика, квиз,
    симулятор или обычная клиническая консультация.
    """
    if not text or len(text.strip()) < 5:
        return {"intent": "CLINICAL_CHAT", "confidence": 0.0}

    prompt = f"""Ты — интеллектуальный координатор стоматологического клинического ассистента StomChat.
Твоя задача — классифицировать запрос врача-стоматолога в личных сообщениях и определить требуемый клинический модуль.

Категории намерений (intent):
1. "WEB_SEARCH": врачу нужны актуальные статьи, исследования, метаанализы, данные PubMed/Cochrane, клинические протоколы (BOPT, вертипреп, адгезия, ирригация и т.д.) или поиск доказательной базы в сети.
2. "CALCULATOR": запрос на расчет дозировок местных анестетиков (артикаин, мепивакаин, лидокаин и др.), количества карпул, пределов по весу или возрасту.
3. "QUIZ": запрос на прохождение викторины, теста, экзаменационных вопросов по стоматологии для проверки знаний.
4. "CASE": запрос на запуск интерактивного симулятора / разбор виртуального клинического случая по шагам (диагностическая игра). ВНИМАНИЕ: если врач описывает СВОЕГО реального пациента для консультации ("у меня пациент 45 лет, зуб 3.6..."), это НЕ симулятор, а "CLINICAL_CHAT"!
5. "BOOKMARKS": запрос на просмотр сохраненных клинических постов или закладок.
6. "CLINICAL_CHAT": обычная клиническая консультация, диагностика, тактика лечения, интерпретация симптомов, рекомендации коллеге.

Запрос врача:
"{text}"

Ответь СТРОГО в формате JSON без Markdown:
{{"intent": "WEB_SEARCH|CALCULATOR|QUIZ|CASE|BOOKMARKS|CLINICAL_CHAT", "search_query": "поисковый запрос или null", "drug": "articaine|mepivacaine|lidocaine|null", "weight_kg": null, "confidence": 1.0}}"""

    status_ctx = {"kind": "pm_chat", "thinking_level": "LOW"}
    try:
        resp, err = await generate_gemini_text_async(prompt, status_ctx, timeout=12)
        if err or not resp:
            return {"intent": "CLINICAL_CHAT", "confidence": 0.0, "error": err}
        
        raw_text = resp.text.strip() if hasattr(resp, "text") else str(resp).strip()
        m = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if m:
            raw_text = m.group(0)
        data = json.loads(raw_text)
        return data
    except Exception as e:
        logger.warning(f"Semantic intent triage failed: {e}")
        return {"intent": "CLINICAL_CHAT", "confidence": 0.0, "error": str(e)}


def calculate_anesthesia_instant(text: str) -> str | None:
    """
    Выполняет мгновенный клинический расчет дозировки анестезии и допустимого количества карпул
    на основе веса пациента и выбранного препарата (артикаин 4%, мепивакаин 3%, лидокаин 2%).
    """
    if not text:
        return None

    lower = text.lower()

    # 1. Распознавание препарата
    drug = None
    if re.search(r'\b(?:артикаин|ультракаин|убистезин|септонест|брилокаин)\w*', lower):
        drug = "articaine"
    elif re.search(r'\b(?:мепивакаин|скандонест|мепивастезин|мепидонт)\w*', lower):
        drug = "mepivacaine"
    elif re.search(r'\b(?:лидокаин|ксилокаин|ксилонор)\w*', lower):
        drug = "lidocaine"

    # 2. Определение возраста/статуса
    is_child = bool(re.search(r'\b(?:ребен|детск|ребёнк|малыш|детям)\w*', lower))

    # 3. Извлечение веса
    weight = None
    weight_match = re.search(r'\b(\d+(?:[.,]\d+)?)\s*(?:кг|kg|килограмм\w*)\b', lower)
    if weight_match:
        try:
            weight = float(weight_match.group(1).replace(',', '.'))
        except ValueError:
            weight = None
    else:
        num_match = re.search(r'(?:на|для|вес|весом)\s+(\d+(?:[.,]\d+)?)\b', lower)
        if num_match:
            try:
                val = float(num_match.group(1).replace(',', '.'))
                if 5 <= val <= 250:
                    weight = val
            except ValueError:
                pass

    if weight is None:
        if drug == "articaine":
            return (
                "🧮 <b>Расчет дозировки: Артикаин 4% (1:100 000 / 1:200 000)</b>\n\n"
                "• <b>Норма:</b> 7 мг/кг (взрослые), 5 мг/кг (дети).\n"
                "• <b>Абсолютный максимум:</b> не более <b>500 мг</b> (≈ 7.3 карпулы по 1.7 мл).\n"
                "• <b>1 карпула 1.7 мл 4%:</b> = <b>68 мг</b> артикаина гидрохлорида.\n\n"
                "💡 <i>Укажите вес пациента для точного расчета, например: «сколько карпул артикаина на 70 кг» или «артикаин ребенок 20 кг».</i>"
            )
        elif drug == "mepivacaine":
            return (
                "🧮 <b>Расчет дозировки: Мепивакаин 3% (Скандонест без вазоконстриктора)</b>\n\n"
                "• <b>Норма:</b> 4.4 мг/кг.\n"
                "• <b>Абсолютный максимум:</b> не более <b>400 мг</b> (≈ 7.4 карпулы по 1.8 мл).\n"
                "• <b>1 карпула 1.8 мл 3%:</b> = <b>54 мг</b> мепивакаина гидрохлорида.\n\n"
                "💡 <i>Укажите вес пациента для точного расчета, например: «дозировка скандонеста на 60 кг» или «скандонест ребенку 20 кг».</i>"
            )
        elif drug == "lidocaine":
            return (
                "🧮 <b>Расчет дозировки: Лидокаин 2% (с адреналином)</b>\n\n"
                "• <b>Норма:</b> 7 мг/кг (взрослые), 4.4 мг/кг (дети).\n"
                "• <b>Абсолютный максимум:</b> не более <b>500 мг</b> (≈ 13.8 карпул по 1.8 мл).\n"
                "• <b>1 карпула 1.8 мл 2%:</b> = <b>36 мг</b> лидокаина гидрохлорида.\n\n"
                "💡 <i>Укажите вес пациента для точного расчета, например: «лидокаин на 70 кг».</i>"
            )
        return None

    if weight < 3 or weight > 300:
        return f"⚠️ <i>Указан некорректный вес ({weight} кг). Пожалуйста, укажите реальный вес пациента.</i>"

    if weight < 35:
        is_child = True

    if drug == "articaine" or drug is None:
        drug_name = "Артикаин 4% (1:100 000 / 1:200 000)"
        carpsize = 1.7
        mg_per_carp = 68.0
        mg_per_kg = 5.0 if is_child else 7.0
        abs_max_mg = 500.0
    elif drug == "mepivacaine":
        drug_name = "Мепивакаин 3% (Скандонест без адреналина)"
        carpsize = 1.8
        mg_per_carp = 54.0
        mg_per_kg = 4.4
        abs_max_mg = 400.0
    elif drug == "lidocaine":
        drug_name = "Лидокаин 2% (с адреналином)"
        carpsize = 1.8
        mg_per_carp = 36.0
        mg_per_kg = 4.4 if is_child else 7.0
        abs_max_mg = 500.0

    calc_by_weight = weight * mg_per_kg
    effective_max_mg = min(calc_by_weight, abs_max_mg)
    hit_ceiling = calc_by_weight >= abs_max_mg
    ceiling_weight_threshold = abs_max_mg / mg_per_kg

    max_carpules_exact = effective_max_mg / mg_per_carp
    safe_carpules_floor = int(max_carpules_exact)

    # Правильное склонение в родительном падеже: "до N карпул / карпулы"
    rem100 = safe_carpules_floor % 100
    rem10 = safe_carpules_floor % 10
    if rem100 in (11, 12, 13, 14):
        carp_declension = f"{safe_carpules_floor} карпул"
    elif rem10 == 1:
        carp_declension = f"{safe_carpules_floor} карпулы"
    else:
        carp_declension = f"{safe_carpules_floor} карпул"

    category_str = "Ребёнок" if is_child else "Взрослый"

    out = [
        f"🧮 <b>Клинический расчет анестезии: {drug_name}</b>\n",
        f"👤 <b>Пациент:</b> {category_str}, вес <b>{weight:g} кг</b>",
        f"📏 <b>Норма расчета:</b> {mg_per_kg} мг/кг (абсолютный потолок: {abs_max_mg:g} мг)\n",
        "📊 <b>Математический расчет:</b>",
        f"• По весу ({weight:g} кг × {mg_per_kg} мг/кг) = <b>{calc_by_weight:g} мг</b>",
    ]

    if hit_ceiling:
        out.append(
            f"• ⚠️ <b>Сработал абсолютный потолок {abs_max_mg:g} мг</b> "
            f"(для данного препарата наступает уже при весе ≥ {ceiling_weight_threshold:.1f} кг)."
        )
        out.append(f"• <b>Итоговый допустимый предел:</b> <b>{effective_max_mg:g} мг</b>\n")
    else:
        out.append(f"• <b>Итоговый допустимый предел:</b> <b>{effective_max_mg:g} мг</b> (не превышает потолок {abs_max_mg:g} мг)\n")

    out.extend([
        f"💉 <b>Допустимое количество карпул (по {carpsize} мл = {mg_per_carp:g} мг):</b>",
        f"• Точное значение: <code>{effective_max_mg:g} / {mg_per_carp:g}</code> = <b>{max_carpules_exact:.2f} карпул</b>",
        f"• <b>Безопасный максимум:</b> <b>до {carp_declension}</b> ({safe_carpules_floor * mg_per_carp:g} мг)\n",
        "⚠️ <i>Примечание: Это максимальная доза для соматически здорового пациента. "
        "При коморбидности (сердечно-сосудистые патологии, печеночная/почечная недостаточность) "
        "дозировку следует снижать, а также строго контролировать дозу вазоконстриктора (адреналина)!</i>"
    ])

    return "\n".join(out)


def build_main_menu_markup():
    """Строит инлайн-клавиатуру главного меню — 3 реальные кнопки врача."""
    from telethon import Button
    return [
        [Button.inline("💊 Препараты и дозы", data="nav:calc"),   Button.inline("🔬 Разобрать снимок", data="nav:xray")],
        [Button.inline("🔍 Найти статью / протокол", data="nav:web")],
        [Button.inline("💬 Задать клинический вопрос", data="nav:chat")],
        [Button.inline("⭐ Мои закладки", data="nav:bookmarks"), Button.inline("⚙️ Настройки", data="nav:settings")],
    ]


def build_reply_keyboard():
    """Строит постоянную нижнюю ReplyKeyboardMarkup — только нужные врачу кнопки."""
    from telethon import types
    return types.ReplyKeyboardMarkup(
        rows=[
            types.KeyboardButtonRow(buttons=[
                types.KeyboardButton(text="💊 Препараты и дозы"),
                types.KeyboardButton(text="🔍 Найти статью")
            ]),
            types.KeyboardButtonRow(buttons=[
                types.KeyboardButton(text="⭐ Закладки"),
                types.KeyboardButton(text="⌨️ Меню")
            ]),
        ],
        resize=True,
        single_use=False,
        persistent=True
    )


# Алиасы функций клавиатур для совместимости
get_main_reply_keyboard = build_reply_keyboard
get_main_inline_keyboard = build_main_menu_markup


MAIN_MENU_TEXT = (
    "👋 <b>StomChat AI — клинический ассистент для врача-стоматолога</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "Просто напишите свой вопрос — или выберите что нужно:\n\n"
    "💊 <b>Препараты и дозы</b> — анестетики, антибиотики, НПВС по весу и соматике\n"
    "🔬 <b>Разобрать снимок</b> — пришлите рентген, ОПТГ или КЛКТ\n"
    "🔍 <b>Найти статью / протокол</b> — поиск в PubMed, Cochrane, гайдлайны\n"
    "💬 <b>Клинический вопрос</b> — консультация по диагнозу, тактике, технике\n"
    "⭐ <b>Мои закладки</b> — посты, сохранённые из группы\n\n"
    "<i>Принимаю голосовые сообщения, фото, снимки. "
    f"Помню последние {PM_HISTORY_LIMIT} сообщений диалога.</i>"
)


def get_main_menu_card_text() -> str:
    """Возвращает форматированный текст карточки Главного меню."""
    return MAIN_MENU_TEXT


async def init_assistant(bot_client):
    global BOT_ID
    try:
        await resolve_bot_identity(bot_client)
        logger.info(f"Assistant initialized with BOT_ID: {BOT_ID} (@{BOT_USERNAME})")

        # Set inline bot command suggestions in Telegram UI
        from telethon import functions, types
        # Scope'ов три, потому что список работающих команд у врача в ЛС и в
        # общем чате РАЗНЫЙ. Раньше регистрировался только Default, и по правилам
        # Telegram (порядок разбора scope'ов: peer -> peer_admins -> chats ->
        # chat_admins -> users -> default) в группе врач видел те же 13 команд
        # ЛС — ни одна из которых в группе не обрабатывается, — и ни одной из
        # шести, которые там работают.
        #
        # BotCommandScopeChats (все группы), а не scope на рабочий чат: бот
        # слушает два чата (SOURCE_CHAT_ID и тестовый топик), групповые команды
        # разбираются в обоих, а peer-scope требует резолва сущности — лишний
        # сетевой вызов на подъёме, чей отказ оставил бы меню группы пустым.
        # BotCommandScopeChatAdmins повторяет тот же список ПЛЮС удаляющую
        # команду: у админа chat_admins перекрывает chats целиком, и без
        # повтора админ потерял бы сводку и викторину из меню.
        menus = [
            ("ЛС", types.BotCommandScopeDefault(), [
                types.BotCommand(command='start', description='Запустить приветствие и инициализировать бота'),
                types.BotCommand(command='help', description='Показать памятку по работе с ассистентом'),
                types.BotCommand(command='protocols', description='Показать доступные клинические протоколы в базе'),
                # /wiki и /style реализованы и перечислены в /help, но в меню
                # их не было — а меню это единственная поверхность, где врач
                # видит команды, ничего не читая. Две рабочих функции просто не
                # находились.
                types.BotCommand(command='wiki', description='Открыть стоматологическую энциклопедию'),
                types.BotCommand(command='calc', description='Открыть шпаргалку-калькулятор анестезии'),
                types.BotCommand(command='quiz', description='Запустить клиническую викторину'),
                types.BotCommand(command='stats', description='Показать популярные темы обсуждений в чате'),
                types.BotCommand(command='bookmarks', description='Показать сохраненные вами клинические закладки'),
                types.BotCommand(command='search', description='Прямой поиск по базе знаний стоматологии'),
                # /web добавлен в меню вместе с самой командой. Без этой строки он
                # был только в тексте /help, и test_commands_surface поймал это
                # сразу: меню — единственная поверхность, где врач видит команду,
                # ничего не читая, и ровно так уже терялись рабочие /wiki и /style.
                # Корпус кончается февралём 2026, поэтому именно этой командой врач
                # достаёт то, чего в базе нет и не появится.
                types.BotCommand(command='web', description='Найти в интернете с ссылками на источники'),
                types.BotCommand(command='case', description='Запустить интерактивный клинический симулятор'),
                types.BotCommand(command='abort', description='Сбросить активный клинический симулятор'),
                types.BotCommand(command='style', description='Настроить стиль общения ассистента'),
            ]),
            # Групповые команды. Обработчик у них в main.run_group_features, и до
            # этой регистрации врач в чате не видел ни одной из них.
            ("общий чат", types.BotCommandScopeChats(), [
                types.BotCommand(command='summary', description='Сводка обсуждения в чате (или /итог, /sum)'),
                types.BotCommand(command='ask', description='Задать боту клинический вопрос в чате'),
                types.BotCommand(command='poll', description='Клиническая викторина для чата (или /кейс)'),
                types.BotCommand(command='what', description='Коротко объяснить термин (или /что)'),
                types.BotCommand(command='save', description='Ответом на пост — сохранить его в закладки'),
            ]),
            # Тот же список плюс удаляющая команда: у админа chat_admins
            # перекрывает chats целиком, поэтому повтор обязателен.
            ("админы чата", types.BotCommandScopeChatAdmins(), [
                types.BotCommand(command='summary', description='Сводка обсуждения в чате (или /итог, /sum)'),
                types.BotCommand(command='ask', description='Задать боту клинический вопрос в чате'),
                types.BotCommand(command='poll', description='Клиническая викторина для чата (или /кейс)'),
                types.BotCommand(command='what', description='Коротко объяснить термин (или /что)'),
                types.BotCommand(command='save', description='Ответом на пост — сохранить его в закладки'),
                types.BotCommand(command='del', description='Удалить пост, на который вы ответили (админам)'),
            ]),
        ]
        # Каждый scope отдельной попыткой: раньше все команды уходили одним
        # вызовом в общем try, и первая же ошибка оставляла врача без меню
        # целиком. Отказ на группе не должен отнимать меню в личке.
        for scope_name, scope, commands in menus:
            try:
                await bot_client(functions.bots.SetBotCommandsRequest(
                    scope=scope,
                    lang_code='',
                    commands=commands
                ))
                logger.info("Bot commands registered: scope=%s count=%d",
                            scope_name, len(commands))
            except Exception as menu_err:
                logger.error("Failed to register bot commands for scope %s: %s",
                             scope_name, menu_err)

    except Exception as e:
        logger.error(f"Failed to initialize assistant or set commands: {e}")

STOP_WORDS = {
    "это", "как", "для", "или", "что", "этот", "себя", "себе", "меня", "тебя", 
    "было", "быть", "если", "хочу", "только", "когда", "тоже", "есть", "было", 
    "будет", "просто", "здесь", "очень", "даже", "если", "тоже", "типа", "вообще",
    "надо", "можно", "хотя", "коллеги", "привет", "здравствуйте", "какой", "такой",
    "какие", "такие", "очень", "этого", "чтобы", "один", "одна", "одно", "будет",
    "всем", "всех", "этом", "этой", "этих", "были", "была", "были", "того", "тому",
    "правило", "правила", "правил", "правилам", "чат", "чата", "чате", "чатом",
    "вопрос", "вопроса", "совет", "совета", "подскажите", "подскажи", "спасибо", "пожалуйста"
}

# Медицинский словарь триажа переехал в dental_vocab (импортирован в начале модуля).
# Имена оставлены на месте — остальной код обращается к ним как раньше.
_SHORT_DENTAL_TERMS = SHORT_DENTAL_TERMS



STATE_TMP_PATH = STATE_PATH + ".tmp"
STATE_BAK_PATH = STATE_PATH + ".bak"

# Держится только на время чтения-слияния-записи файла. Внутри нет await,
# поэтому взаимоблокировка невозможна; защищает от гонок, если save_state
# когда-нибудь позовут из executor-потока.
_STATE_FILE_LOCK = threading.Lock()

STATE_DEFAULTS = {
    "last_passive_run": "2000-01-01T00:00:00",
    "last_passive_text_run": "2000-01-01T00:00:00",
    "last_passive_media_run": "2000-01-01T00:00:00",
    "last_referee_run": "2000-01-01T00:00:00",
    "last_passive_attempt": "2000-01-01T00:00:00",
    "processed_threads": [],
    # Когда каждая ветка из processed_threads была отвечена: {"12345": iso}.
    # Отдельным словарём, а не списком пар, чтобы processed_threads остался
    # списком id — проверка `reply_to_msg_id not in processed_threads` и старые
    # файлы состояния продолжают работать без миграции.
    "processed_thread_dates": {},
    "pm_pings": {},
}

# Пассивный триггер (бот сам влезает в разговор) throttling'уется двумя окнами.
# Раньше окно было одно: 120 минут списывались ДО вызова Gemini, поэтому один
# таймаут провайдера, пустой корпус или отказ валидатора укладывали бота
# в тишину на два часа. Теперь полное окно платится только за реально
# отправленное сообщение, а неудачная попытка стоит короткого backoff'а.
# Прямые обращения (упоминание, ответ на реплику бота, ЛС) этот гейт не проходят.
PASSIVE_COOLDOWN_MINUTES = 120  # после РЕАЛЬНО отправленного пассивного ответа
PASSIVE_RETRY_MINUTES = 10      # после попытки, не давшей сообщения

# Сколько держать ветку в processed_threads. Граница была по ДЛИНЕ — последние
# 100 записей, `del threads[:-100]`, молча. Замер по архиву (117 847 реплик,
# 1016 суток): при 0.88 вторжения в сутки запись жила в среднем 115 суток, в
# худшем случае 52, и обрезка выбрасывала 791 ветку. Хвост обсуждения ПОСЛЕ
# третьего ответа — то есть отрезок, на котором бот может влезть второй раз, —
# даёт p90 = 9.7 суток, p95 = 37.5, p99 = 281.7, максимум 810. То есть память
# была КОРОЧЕ жизни ветки, и бот возвращался в уже отвеченную: 20 повторных
# вторжений за 1016 суток, разрыв от 75.6 до 810.2 суток. Тот же реплей без
# обрезки даёт 0 повторов.
# Год покрывает p99 хвоста и снимает 19 из 20 измеренных повторов.
PROCESSED_THREAD_TTL_DAYS = 365
# Вторичная граница, только чтобы файл состояния не мог расти без предела.
# Замер: чтобы удержать 365 суток истории, хватает 507 записей, поэтому в
# нормальном режиме этот предел не срабатывает. Если сработал — это аномалия,
# и она пишется в журнал: молчаливая обрезка запрещена.
PROCESSED_THREADS_MAX = 2000


class _TrackedState(dict):
    """
    Обычный dict, который помнит, каким его прочитали с диска.
    Нужен, чтобы save_state() мог записать ТОЛЬКО реально изменённые ключи
    и не затирать чужие правки, сделанные пока вызывающий висел на await.
    """
    __slots__ = ("_snapshot",)

    def __init__(self, data):
        super().__init__(data)
        self._snapshot = copy.deepcopy(data)


def _read_state_file(path):
    """Читает один файл состояния. Возвращает dict или None, если файла нет/он битый."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.error(f"State file {path} contains {type(data).__name__}, not an object. Ignoring.")
            return None
        return data
    except Exception as e:
        logger.error(f"Error loading assistant state from {path}: {e}")
        return None


def load_state():
    data = _read_state_file(STATE_PATH)
    if data is None:
        # Основной файл отсутствует или обрезан (например, процесс убили в момент
        # записи). Поднимаем последнюю заведомо целую копию, чтобы не потерять
        # silenced_until / pm_pings / processed_threads.
        data = _read_state_file(STATE_BAK_PATH)
        if data is not None:
            logger.warning("Primary assistant state unreadable. Recovered from backup .bak")
        else:
            logger.warning("No readable assistant state found. Starting from defaults.")
            data = {}

    merged = copy.deepcopy(STATE_DEFAULTS)
    merged.update(data)
    return _TrackedState(merged)


def save_state(state):
    """
    Атомарно сохраняет состояние, сливая изменения вызывающего с текущим
    содержимым файла.

    Вызывающий обычно держит state, прочитанный десятки секунд назад (между
    load_state() и save_state() стоят await'ы на LLM-триаж и запросы к БД).
    Прямая запись такого словаря затирала чужие свежие правки — в частности
    silenced_until, выставленный, пока шла генерация. Поэтому пишем только те
    ключи, которые вызывающий действительно тронул.
    """
    try:
        with _STATE_FILE_LOCK:
            on_disk = _read_state_file(STATE_PATH)
            if on_disk is None:
                on_disk = _read_state_file(STATE_BAK_PATH) or {}

            snapshot = getattr(state, "_snapshot", None)
            merged = dict(on_disk)
            preserved = []

            for key, value in state.items():
                if snapshot is None:
                    # Не из load_state() — считаем, что вызывающий владеет всем,
                    # но ключи, которых у него нет, с диска не выбрасываем.
                    merged[key] = value
                    continue
                if key not in snapshot or snapshot[key] != value:
                    merged[key] = value  # вызывающий изменил ключ — его версия побеждает
                elif key in on_disk and on_disk[key] != value:
                    preserved.append(key)  # вызывающий не трогал, а на диске новее — не трогаем

            if preserved:
                logger.info(f"save_state: preserved concurrent updates for keys {preserved}")

            payload = json.dumps(merged, ensure_ascii=False, indent=2)

            with open(STATE_TMP_PATH, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            # Целую предыдущую версию держим как .bak — с неё поднимемся,
            # если процесс убьют между записью tmp и подменой.
            if on_disk and os.path.exists(STATE_PATH):
                try:
                    os.replace(STATE_PATH, STATE_BAK_PATH)
                except Exception as bak_err:
                    logger.warning(f"Failed to rotate state backup: {bak_err}")

            os.replace(STATE_TMP_PATH, STATE_PATH)  # атомарная подмена в пределах тома
    except Exception as e:
        logger.error(f"Error saving assistant state: {e}")
        try:
            if os.path.exists(STATE_TMP_PATH):
                os.remove(STATE_TMP_PATH)
        except Exception:
            pass

def _parse_state_dt(value, default=datetime(2000, 1, 1)):
    """Разбирает ISO-таймстамп из состояния. Битое значение = 'никогда', без исключения."""
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning(f"Malformed timestamp in assistant state: {value!r}. Treating as never.")
        return default

    # Значение С ЧАСОВЫМ ПОЯСОМ считаем негодным. Все писатели этих ключей
    # используют наивный datetime.now(), поэтому tz-aware значение может попасть
    # в файл только правкой руками или сменой кода. Но цена его появления
    # непомерная: вычитание наивного и tz-aware поднимает TypeError, а он в
    # passive_gate_block_reason не перехвачен, пробивает check_and_trigger_assistant
    # и гасится только общим except в main — то есть ассистент падал бы на КАЖДОМ
    # входящем сообщении, навсегда, до ручной правки файла.
    if parsed.tzinfo is not None:
        logger.warning("Timestamp with timezone in assistant state: %r. Treating as never.", value)
        return default

    # Метка ИЗ БУДУЩЕГО тоже негодна. record_passive_success пишет
    # datetime.now().isoformat() без сверки: если часы машины ушли вперёд (VM
    # после длинного suspend, старт до синхронизации NTP), в состояние ложится
    # будущая дата, и пассивный триггер закрыт, пока реальное время её не
    # догонит. Проба через настоящий passive_gate_block_reason: метка на год
    # вперёд даёт «passive cooldown, 525720 min left», а "9999-12-31" — почти
    # восемь тысяч лет молчания. Само это не лечится.
    if parsed > datetime.now():
        logger.warning(
            "Timestamp from the future in assistant state: %r (now %s). Treating as never.",
            value, datetime.now().isoformat(timespec="seconds"),
        )
        return default
    return parsed


def is_silenced(state, where=""):
    """
    Просил ли кто-то бота замолчать и не истёк ли срок.

    Проверка была СКОПИРОВАНА в трёх местах, и четвёртый путь — триггер
    упоминания — её потерял. Замер по живому архиву, последовательность
    2025-06-05: врач написал «Бот очень назойливый мне не нравится», бот
    извинился и выставил тишину на 4 часа, а через 4 минуты 38 секунд реплика
    «Какой бот советуете использовать?» (про ЧУЖОГО бота) прошла регулярку
    упоминания — и заговорил снова. В четырёхчасовом окне тишины лежит 138
    сообщений, 14 из них задевают регулярку: тринадцать попыток нарушить только
    что данное обещание.

    Хуже всего, что путь упоминания вызывается ровно тогда, когда основной
    ассистент промолчал, — а при активной тишине он молчит именно из-за неё. То
    есть флаг тишины сам передавал управление пути, который его не проверяет.

    Одно правило на четыре вызывающих: копия неизбежно снова разъедется.
    """
    silenced_until_str = state.get("silenced_until")
    if not silenced_until_str:
        return False
    try:
        if datetime.now() < datetime.fromisoformat(silenced_until_str):
            logger.info("Bot is silenced until %s. Skipping %s.",
                        silenced_until_str, where or "trigger check")
            return True
    except Exception as parse_err:
        # Битая метка не должна глушить бота навсегда: считаем, что тишины нет.
        logger.error("Error parsing silenced_until (%r): %s", silenced_until_str, parse_err)
    return False


def passive_gate_block_reason(state):
    """
    Причина, по которой пассивный текстовый триггер сейчас запрещён, иначе None.
    Учитывает оба окна: полный кулдаун за отправленный ответ и короткий
    backoff за уже сделанную попытку.
    """
    now = datetime.now()

    since_sent = now - _parse_state_dt(state.get("last_passive_text_run"))
    full = timedelta(minutes=PASSIVE_COOLDOWN_MINUTES)
    if since_sent < full:
        return f"passive cooldown, {int((full - since_sent).total_seconds() // 60) + 1} min left"

    since_try = now - _parse_state_dt(state.get("last_passive_attempt"))
    backoff = timedelta(minutes=PASSIVE_RETRY_MINUTES)
    if since_try < backoff:
        return f"retry backoff after failed attempt, {int((backoff - since_try).total_seconds() // 60) + 1} min left"

    return None


# Заявки на незваный ответ, взятые в этом процессе и ещё не отпущенные:
# ключ -> (таск-владелец, время взятия).
#
# Зачем нужны. Гейт кулдауна читается из state, прочитанного на входе функции,
# а списывается только на record_passive_attempt — между ними await'ы на
# get_last_n_messages, round-trip к Telegram get_messages и два запроса к базе.
# main.py диспатчит КАЖДОЕ сообщение отдельным таском
# (`create_task(run_assistant_safe(), name=f"assistant_{msg_id}")`), Telethon
# делает это конкурентно, поэтому два сообщения одной ветки — это два таска в
# одном событийном цикле: оба читают открытый гейт и пустой processed_threads,
# оба доходят до отправки. Замер по архиву: пар текстовых реплик к одному
# родителю в пределах 2 с — 126, из них с реально открытым гейтом 3; в пределах
# 60 с — 4711 и 105 соответственно.
#
# Заявка НЕ ждёт, а отказывает: второй ответ в ту же ветку не нужен вообще,
# и держать за ним входящее сообщение на всю генерацию (90 с) незачем.
_PASSIVE_CLAIMS = {}

# Верхняя граница жизни заявки. Полный проход — триаж (25 с) + генерация (90 с)
# + рецензент, то есть реальная заявка живёт секунды-минуты. Всё, что старше,
# считаем протёкшим: заявка, залипшая навсегда, запирает бота МОЛЧА, а это
# ровно тот класс отказа, который мы здесь и убираем.
PASSIVE_CLAIM_TTL_SECONDS = 600


def _release_claims_of_task(task):
    """Снимает все заявки таска. Зовётся из его done-callback."""
    for key, (owner, _taken_at) in list(_PASSIVE_CLAIMS.items()):
        if owner is task:
            _PASSIVE_CLAIMS.pop(key, None)


def _drop_stale_passive_claims():
    """
    Снимает заявки, чей владелец уже завершился, и заявки старше TTL.

    Проверка на завершившегося владельца не страховка, а рабочий путь:
    add_done_callback отрабатывает через loop.call_soon, то есть на следующем
    проходе цикла, и запись успевает пережить своего владельца на один тик.
    """
    now = datetime.now()
    for key, (owner, taken_at) in list(_PASSIVE_CLAIMS.items()):
        age = (now - taken_at).total_seconds()
        owner_done = owner is not None and owner.done()
        if not owner_done and age <= PASSIVE_CLAIM_TTL_SECONDS:
            continue
        _PASSIVE_CLAIMS.pop(key, None)
        if not owner_done:
            logger.warning(
                "Passive claim %r leaked and was force-released after %.0fs.", key, age
            )


def claim_passive_slot(key):
    """
    Пытается занять слот незваного ответа. True — заняли, False — этим уже
    занимается другой таск.

    Снятие привязано к завершению таска, а не к явному вызову: у
    check_and_trigger_assistant одиннадцать точек выхода после места заявки, и
    любая необработанная ошибка между ними оставила бы заявку висеть навсегда.
    done-callback отрабатывает на ЛЮБОМ исходе — return, исключение, отмена.

    Между проверкой и записью нет ни одного await, поэтому для одного
    событийного цикла операция неделима и отдельный замок не нужен.
    """
    _drop_stale_passive_claims()
    if key in _PASSIVE_CLAIMS:
        logger.info("Passive slot %r is already claimed by another task. Skipping duplicate reply.", key)
        return False

    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    _PASSIVE_CLAIMS[key] = (task, datetime.now())
    if task is not None:
        task.add_done_callback(_release_claims_of_task)
    return True


def _prune_processed_threads(state, add=None):
    """
    Держит processed_threads в границах ВОЗРАСТА, а не длины, и возвращает
    актуальный список.

    Раньше список резался до последних ста записей без единой строки в журнал.
    Замер по архиву: выброшено 791 ветка, срок памяти в среднем 115 суток при
    p99 хвоста обсуждения 281.7 суток — то есть бот забывал ветку раньше, чем
    она затихала, и заходил в неё второй раз (20 повторов за 1016 суток).

    Возраст лежит в отдельном словаре processed_thread_dates: список остаётся
    списком id, поэтому проверка `reply_to_msg_id not in processed_threads` и
    уже лежащие на диске файлы состояния работают без миграции.
    """
    threads = state.setdefault("processed_threads", [])
    if not isinstance(threads, list):
        logger.error("processed_threads is %s, not a list. Resetting.", type(threads).__name__)
        threads = state["processed_threads"] = []
    stamps = state.setdefault("processed_thread_dates", {})
    if not isinstance(stamps, dict):
        logger.error("processed_thread_dates is %s, not an object. Resetting.", type(stamps).__name__)
        stamps = state["processed_thread_dates"] = {}

    now = datetime.now()
    now_iso = now.isoformat()

    if add is not None:
        if add not in threads:
            threads.append(add)
        stamps[str(add)] = now_iso  # ветку отвечали только что, метку обновляем

    ttl = timedelta(days=PROCESSED_THREAD_TTL_DAYS)
    kept, expired = [], []
    for tid in threads:
        # default=now: непонятная метка означает «оставить», а не «выбросить».
        # Сюда же попадают ветки из старого файла состояния, у которых метки
        # ещё нет: они считаются увиденными сейчас и начинают стареть с этого
        # момента. Иначе первая же чистка выбросила бы всю накопленную историю
        # и бот разом вернулся бы во все ветки, которые уже отвечал.
        seen_at = _parse_state_dt(stamps.get(str(tid)), default=now)
        (expired if now - seen_at > ttl else kept).append(tid)

    dropped_by_size = []
    if len(kept) > PROCESSED_THREADS_MAX:
        dropped_by_size = kept[:-PROCESSED_THREADS_MAX]
        kept = kept[-PROCESSED_THREADS_MAX:]

    if expired:
        logger.info(
            "processed_threads: dropped %s threads older than %s days: %s",
            len(expired), PROCESSED_THREAD_TTL_DAYS, expired[:20],
        )
    if dropped_by_size:
        # Молчаливая обрезка запрещена: это выброс ЕЩЁ АКТУАЛЬНЫХ веток, и бот
        # после него может влезть в них второй раз.
        logger.warning(
            "processed_threads hit the %s-entry cap: dropped %s still-fresh threads, "
            "the bot may re-enter them: %s",
            PROCESSED_THREADS_MAX, len(dropped_by_size), dropped_by_size[:20],
        )

    state["processed_threads"] = kept
    state["processed_thread_dates"] = {str(t): stamps.get(str(t), now_iso) for t in kept}
    return kept


def record_passive_attempt():
    """
    Отмечает попытку пассивного ответа, которая ещё может не дойти до отправки
    (ошибка API, пустой корпус, IGNORE, отказ валидатора, отказ триажа).
    Читает состояние заново, чтобы не писать протухший словарь.
    """
    state = load_state()
    state["last_passive_attempt"] = datetime.now().isoformat()
    save_state(state)


def record_passive_success(thread_id=None, author_id=None, msg_id=None):
    """
    Списывает полное окно тишины — вызывается ТОЛЬКО после того, как сообщение
    действительно ушло в Telegram. Здесь же тред помечается обработанным,
    чтобы неудачная генерация не сжигала его навсегда, а автор кейса
    запоминается для поддержки последующего диалога.
    """
    state = load_state()
    now_iso = datetime.now().isoformat()
    state["last_passive_text_run"] = now_iso
    state["last_passive_attempt"] = now_iso

    if author_id is not None:
        state["last_case_author_id"] = author_id
        state["last_case_bot_msg_id"] = msg_id
        state["last_case_time"] = now_iso

    if thread_id is not None:
        _prune_processed_threads(state, add=thread_id)

    save_state(state)


def calculate_context_length_guidelines(history_msgs):
    """Вычисляет среднюю длину реплик в истории и возвращает строгую инструкцию для LLM."""
    if not history_msgs:
        return "Отвечай кратко, до 3-4 предложений."
    
    words_counts = []
    for msg in history_msgs:
        text = ""
        if isinstance(msg, dict):
            text = msg.get("text", "") or ""
        elif hasattr(msg, "message"):
            text = msg.message or ""
        elif isinstance(msg, str):
            text = msg
            
        words = text.split()
        if words:
            words_counts.append(len(words))
            
    if not words_counts:
        return "Отвечай кратко, до 3-4 предложений."
        
    avg_words = sum(words_counts) / len(words_counts)
    
    if avg_words < 12:
        return "В чате пишут очень коротко. Ответь строго ОДНОЙ короткой фразой (до 15 слов)!"
    elif avg_words < 28:
        return f"В чате общаются лаконично (в среднем {int(avg_words)} слов). Ответь кратко: 1-2 предложения (до 30 слов)."
    elif avg_words < 55:
        return f"В чате идет умеренное обсуждение (в среднем {int(avg_words)} слов). Ответь в объеме 2-3 предложений (до 50-60 слов)."
    else:
        return f"В чате идет подробное обсуждение (в среднем {int(avg_words)} слов). Можешь написать развернутый ответ (до 100-120 слов, 2-3 коротких абзаца)."

def write_to_shadow_log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception as e:
        logger.error(f"Error writing to shadow log: {e}")

_RU_SUFFIXES = ["ами", "ями", "ыми", "ом", "ем", "ам", "ям", "ах", "ях", "ых", "их",
                "ов", "ев", "ие", "ия", "ию", "ии", "ей", "ой", "а", "у", "е", "ы", "и", "о"]


def extract_keywords(text):
    if not text:
        return []

    cleaned = re.sub(r"[^\w\s-]", " ", text.lower())
    words = cleaned.split()
    keywords = []
    for w in words:
        if w in STOP_WORDS or w.isdigit():
            continue
        # Порог len >= 4 выбрасывал самые частые слова стоматолога: "зуб", "бор",
        # "кт". Они не попадали в поиск по базе вообще — на вопрос "болит зуб
        # после лечения канала" слово "зуб" в RAG не участвовало.
        if len(w) < 4 and w not in _SHORT_DENTAL_TERMS:
            continue

        stem = w
        for suffix in _RU_SUFFIXES:
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                stem = w[:-len(suffix)]
                break
        keywords.append(stem)

    # dict.fromkeys, а не set(): set() давал разный порядок между запусками
    # (PYTHONHASHSEED), поэтому один и тот же вопрос приводил к разной справке
    # и, как следствие, к разному ответу. Теперь порядок первого появления.
    keywords = list(dict.fromkeys(keywords))

    # Стоматологические термины — вперёд. Проверка через is_dental_keyword,
    # которая сверяет префиксы в обе стороны и не теряет "коронк"/"десн"/"эмал".
    dental_matches = [kw for kw in keywords if is_dental_keyword(kw)]
    other_matches = [kw for kw in keywords if not is_dental_keyword(kw)]
    return dental_matches + other_matches


_MAX_SEARCH_KEYWORDS = 12
_MIN_DENTAL_FOR_STRICT = 3


def select_search_keywords(keywords):
    """
    Отбирает ключи для поиска по базе знаний.

    Раньше список ВСЕГДА добивался до 12 общими словами (три копии этой логики
    в файле). На вопросе про боль после лечения канала в поиск уходили
    "смотреть" и "посл", и справка забивалась случайными фактами. Если
    клинических терминов набралось достаточно — общие слова не подмешиваем;
    они нужны только когда цепляться больше не за что.
    """
    if not keywords:
        return []
    dental = [kw for kw in keywords if is_dental_keyword(kw)]
    # Общими словами добиваем ТОЛЬКО когда клинических нет вовсе.
    #
    # Порог в три термина был слишком мягким: на «какой уступ под цирконий и
    # как вести мягкие ткани» находились «уступ» и «цирконий», их было меньше
    # трёх, и в поиск добавлялось «вест». По такому ключу LIKE тянет из 107
    # тысяч реплик что угодно. Два точных термина дают лучшую справку, чем два
    # точных плюс один мусорный, а объём выборки восполняется бюджетом строк на
    # ключ, а не количеством ключей.
    if dental:
        return dental[:_MAX_SEARCH_KEYWORDS]
    return keywords[:_MAX_SEARCH_KEYWORDS]


# Кандидатов тянем по БЮДЖЕТУ, а не по фиксированной норме на ключ. При одном
# точном термине норма в 8 строк давала 8 кандидатов на всё ранжирование, и
# отбор терял смысл. Теперь чем меньше ключей, тем глубже каждый.
_CORPUS_ROWS_PER_KEYWORD = 8
_CORPUS_TOTAL_ROW_BUDGET = 48


def _rows_per_keyword(keyword_count):
    if keyword_count <= 0:
        return _CORPUS_ROWS_PER_KEYWORD
    return max(_CORPUS_ROWS_PER_KEYWORD, _CORPUS_TOTAL_ROW_BUDGET // keyword_count)
# Реплика короче этого не несёт утверждения: «Контаминация.», «+», «да».
# В справке такие только занимают место.
_ARCHIVE_MIN_USEFUL_CHARS = 40
_CORPUS_CANDIDATE_CAP = 60
_CORPUS_OUTPUT_LIMIT = 20
# Бюджет справки в символах на КАЖДЫЙ корпус (вики и архив отдельно). 6000 взято
# по замеру: медиана суммарной справки была 10241 символ и ответы на ней
# строятся нормально, поэтому 12000 + 12000 оставляют типичный случай нетронутым
# и подрезают только хвост, где справка распухала до 52 тысяч.
_CORPUS_MAX_CHARS = 12000
# Одна запись не должна съедать бюджет целиком: медиана факта вики 236 символов,
# максимум 5477. 2500 вмещает даже подробный факт с числами.
_CORPUS_ENTRY_MAX_CHARS = 2500


def _rank_corpus_entries(entries, keywords):
    """
    Сортирует найденные фрагменты по числу РАЗНЫХ ключевых слов запроса,
    стоматологические веса выше. Фрагменты, содержащие несколько ключевых
    слов одновременно (пересечение терминов), получают максимальный приоритет.

    Без ранжирования в промпт уходило то, что первым нашлось по первому же
    ключевому слову: на вопрос про боль после лечения канала в справку
    попадали факты про снятие оттисков (совпало "канал" внутри "канальцами"),
    про BOPT и про CAD/CAM. То есть модели скармливали ровно ту приманку для
    клинической отсебятины, которую следующие строки промпта запрещают.
    """
    if not entries or not keywords:
        return []

    weights = {kw: (2 if is_dental_keyword(kw) else 1) for kw in keywords}
    regexes = {kw: re.compile(rf'\b{re.escape(kw)}', re.IGNORECASE) for kw in weights}

    scored = []
    for idx, entry in enumerate(entries):
        score = 0
        distinct = 0
        for kw, weight in weights.items():
            if regexes[kw].search(entry):
                score += weight
                distinct += 1
        
        # CRITICAL: Discard entries that matched in SQL but failed the regex boundary check!
        if score > 0:
            intersection_boost = (distinct - 1) * 10 if distinct > 1 else 0
            total_score = score + intersection_boost
            scored.append((total_score, distinct, -idx, entry))

    if not scored:
        return []

    scored.sort(reverse=True)

    # Предпочитаем фрагменты, зацепившие минимум два разных ключа, но никогда
    # не отдаём пустую справку: при пустом корпусе вызывающий вообще молчит.
    strong = [s for s in scored if s[1] >= 2]
    chosen = strong if len(strong) >= 3 else scored
    return _fit_corpus_budget(s[3] for s in chosen[:_CORPUS_OUTPUT_LIMIT])


def _fit_corpus_budget(entries):
    """
    Укладывает справку в бюджет по СИМВОЛАМ, а не только по числу строк.

    Предел стоял лишь на количестве записей, а длина записи не ограничена
    ничем: самый длинный факт вики — 5477 символов при медиане 236. Двадцать
    длинных записей давали десятки тысяч символов.

    Замер на 400 реальных вопросах из архива: медиана справки 10241 символ,
    90-й перцентиль 16970, максимум 52816 (~17600 токенов). Больше 20000
    символов получали 30 вопросов из 400. Причём самые тяжёлые — вовсе не
    клинические: «Давно так работаете? У кого учились?» тянуло 37 тысяч
    символов вики, «А чем вы лишнее убираете?» — 31 тысячу. Один общий корень
    в трёпе, бюджет строк на ключ равен 48, и справка распухает впятеро.

    Чем это плохо, помимо цены и задержки: вопрос врача тонет под массивом
    слабо связанных фактов — ровно та приманка для отсебятины, против которой
    выстроено ранжирование выше. И рецензент ответа видит лишь первые 3000
    символов справки, то есть при 52 тысячах проверяет 6% основания.

    Записи приходят уже отсортированными по релевантности, поэтому отсекаем с
    конца — теряется наименее подходящее. Медиана в бюджет укладывается, так
    что обрезка касается только хвоста распределения.
    """
    out = []
    used = 0
    for entry in entries:
        if len(entry) > _CORPUS_ENTRY_MAX_CHARS:
            entry = _clip_at_sentence(entry, _CORPUS_ENTRY_MAX_CHARS)
        # Записи склеиваются через "\n", и разделитель тоже занимает место:
        # без его учёта корпус выходил за бюджет на число строк минус одна.
        cost = len(entry) + (1 if out else 0)
        if used + cost > _CORPUS_MAX_CHARS and out:
            break
        out.append(entry)
        used += cost
    return out


# Обрезка по границе предложения — одна реализация на бот, в html_safe. Копия,
# что стояла здесь, была без проверки «текст короче предела» и приклеивала
# многоточие к записи, которую никто не обрезал: модель видела «факт оборван»
# там, где он полный, и дописывала за него. Имя оставлено ссылкой — за ним
# больше нет своей логики, а зовёт его ещё test_rag_quality.py.
_clip_at_sentence = html_safe.clip_at_sentence_text


# Бюджет истории диалога ЛС в символах. У справки предел есть
# (_CORPUS_MAX_CHARS = 6000 на КАЖДЫЙ корпус, 12000 суммарно), у истории не было
# никакого: в промпт уходили все PM_HISTORY_LIMIT = 35 реплик целиком. Ответ бота
# сохраняется уже после clean_html_formatting, то есть до ~4000 символов каждый,
# а сообщение врача Telegram ограничивает 4096 — верхняя оценка блока истории
# около 140 000 символов против 12 000 у справки. И это не только про худший
# случай: даже на спокойной переписке по 700 символов на реплику 35 сообщений
# дают 24 500 — вдвое больше всей справки, ради которой выстроены ранжирование
# (_rank_corpus_entries) и бюджет (_fit_corpus_budget).
# 12000 = ровно столько же, сколько отдано одному корпусу справки.
_PM_HISTORY_MAX_CHARS = 12000
# Одна реплика не должна съесть бюджет целиком: без этого единственный
# развёрнутый ответ бота на 4000 символов забирает две трети блока истории.
# 2500 — как у записи справки (_CORPUS_ENTRY_MAX_CHARS).
_PM_HISTORY_ENTRY_MAX_CHARS = 2500


def _fit_pm_history(lines):
    """
    Укладывает историю ЛС в бюджет по символам, сохраняя САМЫЕ СВЕЖИЕ реплики.

    Идём с конца: терять нужно старое, а не последний вопрос врача — именно на
    него бот и отвечает. Возвращается хронологический порядок.

    САМАЯ СВЕЖАЯ реплика не подрезается вообще. Порог 1200 символов на запись
    задумывался против распухших ответов бота в старой части истории, но в
    текстовой ветке ЛС отдельного поля «Вопрос пользователя» в промпте нет
    (оно есть только в ветке со снимком): текущее сообщение врача попадает
    модели ТОЛЬКО как последняя строка этого блока. Telegram разрешает 4096
    символов, и подробное описание случая на 2000+ обрезалось бы до 1200 —
    модель отвечала бы на усечённый вопрос, не зная об этом. Полная реплика
    (≤4096 + имя) укладывается в бюджет 6000 сама, остаток достаётся истории.
    """
    kept = []
    used = 0
    for position, line in enumerate(reversed(lines)):
        if position > 0 and len(line) > _PM_HISTORY_ENTRY_MAX_CHARS:
            line = _clip_at_sentence(line, _PM_HISTORY_ENTRY_MAX_CHARS)
        # Реплики склеиваются через "\n", и разделитель тоже занимает место:
        # без его учёта блок выходил за бюджет на число строк минус одна.
        cost = len(line) + (1 if kept else 0)
        if used + cost > _PM_HISTORY_MAX_CHARS and kept:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return kept


def _corpus_entry(prefix, body):
    """
    Одна запись справки = одна строка.

    Внутренние переводы строк схлопываются: корпус склеивается через "
", и
    многострочная реплика превращалась в несколько строк, между которыми модель
    не видит границы высказываний. На практике так в клиническую справку
    попадали правила чата — «Правила канала», «Никакой политики» — как будто
    это отдельные факты по существу вопроса.
    """
    text = " ".join(str(body or "").split())
    return f"{prefix} {text}".strip()


def like_any_case(column, keyword):
    """
    Условие LIKE, которое находит ключ в ЛЮБОМ регистре, и параметры к нему.

    SQLite складывает регистр ТОЛЬКО для ASCII: `'А' LIKE 'а'` даёт 0, а
    `'A' LIKE 'a'` даёт 1. LOWER() тоже ASCII-only — `LOWER('ВНЧС')`
    возвращает 'ВНЧС' без изменений. Ключи же приходят из extract_keywords,
    который делает text.lower().

    Итог был такой: аббревиатуры, которые врач всегда пишет капсом, не
    находились в базе знаний НИКОГДА. Замер по живой вики (12784 факта):

        ВНЧС  0 из 88 фактов      КЛКТ  0 из 21      МТА  0 из 18
        ЭДТА  0 из 13             ТРГ   0 из 5       БОПТ 0 из 4
        ЭОД   0 из 2

    Страдали и обычные слова, просто меньше: «цирконий» находил 49 из 61,
    «адгезив» 607 из 627 — терялось написанное с заглавной в начале фразы.
    Врач спрашивал про ВНЧС, справка уходила в промпт ПУСТОЙ, и модель отвечала
    по памяти, хотя в базе лежало 88 фактов по теме. При этом /start и /help
    обещают ответ «с использованием базы знаний».

    Почему три формы, а не питоновский lower() через create_function: замер на
    живых базах показал, что три формы в одном запросе и полнее, и дешевле.
        вики:  три формы 88 находок за 63.8 мс, rulower 88 за 82.5 мс
        архив: три формы 53 находки за 202.9 мс, rulower 54 за 560.3 мс
    Полнота 100% и 98% при цене в 2.8 раза меньше на большом корпусе. Одна
    пропущенная строка архива — написание вида «вНчс», ради которого платить
    втрое за каждый запрос смысла нет.
    """
    forms = []
    for form in (keyword.lower(), keyword.upper(), keyword.capitalize()):
        pattern = f"%{form}%"
        if pattern not in forms:
            forms.append(pattern)
    clause = " OR ".join(f"{column} LIKE ?" for _ in forms)
    return f"({clause})", tuple(forms)


def _corpus_body_key(body):
    """
    Ключ для отсева повторов: только суть, без префикса.

    Раньше повтор ловили сравнением готовой строки, а в неё входит префикс —
    у справки коды рубрик, у архива имя автора. Один и тот же факт лежит в
    базе пятью строками с теми же тремя кодами в РАЗНОМ ПОРЯДКЕ
    ('2.2.2, 2.3.1, 2.2.1' и '2.3.1, 2.2.2, 2.2.1'), поэтому строки
    различались и проверка их пропускала.

    Величина замерена на 2893 реальных вопросах из чата: 1059 из них (37%)
    дают ровно один ключ, а при одном ключе бюджет строк на него равен 48 —
    выборка достаточно глубокая, чтобы зацепить вторую копию. На 42 вопросах
    в справку уходило 44 лишних одинаковых абзаца. Заодно set вместо перебора
    списка.
    """
    return " ".join(str(body or "").split()).lower()


_WIKI_WAL_READY = False
_ARCHIVE_WAL_READY = False

async def search_knowledge_corpus(keywords):
    if not keywords:
        return "", ""

    def sync_search():
        try:
            wiki_facts = []
            archive_msgs = []
            # Множество общее для справки и архива: если один и тот же текст лежит
            # в обеих базах, второй раз он в промпт не идёт. Побеждает справка —
            # она собирается первой и в ней у факта есть рубрика.
            seen_bodies = set()
            rows_per_kw = _rows_per_keyword(len(keywords))

            # 1. Search stomat_wiki.db
            if os.path.exists("stomat_wiki.db"):
                try:
                    with contextlib.closing(sqlite3.connect("file:stomat_wiki.db?mode=ro", uri=True, timeout=30)) as conn:
                        conn.execute("PRAGMA busy_timeout = 30000")
                        c = conn.cursor()
                        for kw in keywords:
                            where_clause, params = like_any_case("content", kw)
                            c.execute(
                                f"SELECT category_code, content, source_ids FROM distilled_facts "
                                f"WHERE {where_clause} LIMIT ?",
                                params + (rows_per_kw,)
                            )
                            for row in c.fetchall():
                                body_key = _corpus_body_key(row[1])
                                if body_key in seen_bodies:
                                    continue
                                seen_bodies.add(body_key)
                                cat_code = row[0]
                                source_ids = str(row[2] or "").strip()
                                src_tag = f" msg#{source_ids[:20]}" if source_ids else ""
                                wiki_facts.append(_corpus_entry(f"[{cat_code}{src_tag}]", row[1]))
                            if len(wiki_facts) >= _CORPUS_CANDIDATE_CAP:
                                break
                except Exception as e:
                    logger.error(f"Error searching stomat_wiki.db: {e}")

            # 2. Search active bot DB (stomat_bot.db: 41k+ live messages from 2026)
            bot_db_path = getattr(config, "DB_PATH", "stomat_bot.db") or "stomat_bot.db"
            if os.path.exists(bot_db_path):
                try:
                    with contextlib.closing(sqlite3.connect(f"file:{bot_db_path}?mode=ro", uri=True, timeout=30)) as conn:
                        conn.execute("PRAGMA busy_timeout = 30000")
                        c = conn.cursor()
                        for kw in keywords:
                            where_clause, params = like_any_case("text", kw)
                            c.execute(
                                f"SELECT msg_id, sender_name, date, text FROM messages "
                                f"WHERE {where_clause} AND TRIM(text) <> '' "
                                f"AND LENGTH(TRIM(text)) >= {_ARCHIVE_MIN_USEFUL_CHARS} "
                                f"AND TRIM(text) NOT LIKE '%?' "
                                f"ORDER BY date DESC "
                                f"LIMIT ?",
                                params + (rows_per_kw,)
                            )
                            for row in c.fetchall():
                                body_key = _corpus_body_key(row[3])
                                if body_key in seen_bodies:
                                    continue
                                seen_bodies.add(body_key)
                                m_id = row[0]
                                s_name = row[1] or "Врач"
                                date_str = str(row[2])[:10] if row[2] else ""
                                header = f"[Сообщение #{m_id} от {s_name} ({date_str})]:" if date_str else f"[Сообщение #{m_id} от {s_name}]:"
                                archive_msgs.append(_corpus_entry(header, row[3]))
                            if len(archive_msgs) >= _CORPUS_CANDIDATE_CAP:
                                break
                except Exception as e:
                    logger.error(f"Error searching {bot_db_path}: {e}")

            # 3. Search archive messages (stomat_archive.db: pre-2026 archive)
            if len(archive_msgs) < _CORPUS_CANDIDATE_CAP and os.path.exists("stomat_archive.db"):
                try:
                    with contextlib.closing(sqlite3.connect("file:stomat_archive.db?mode=ro", uri=True, timeout=30)) as conn:
                        conn.execute("PRAGMA busy_timeout = 30000")
                        c = conn.cursor()
                        for kw in keywords:
                            where_clause, params = like_any_case("text", kw)
                            c.execute(
                                f"SELECT msg_id, sender_name, date, text FROM archive_messages "
                                f"WHERE {where_clause} AND TRIM(text) <> '' "
                                f"AND LENGTH(TRIM(text)) >= {_ARCHIVE_MIN_USEFUL_CHARS} "
                                f"AND TRIM(text) NOT LIKE '%?' "
                                f"ORDER BY msg_id DESC "
                                f"LIMIT ?",
                                params + (rows_per_kw,)
                            )
                            for row in c.fetchall():
                                body_key = _corpus_body_key(row[3])
                                if body_key in seen_bodies:
                                    continue
                                seen_bodies.add(body_key)
                                m_id = row[0]
                                s_name = row[1] or "Врач"
                                date_str = str(row[2])[:10] if row[2] else ""
                                header = f"[Архив #{m_id} от {s_name} ({date_str})]:" if date_str else f"[Архив #{m_id} от {s_name}]:"
                                archive_msgs.append(_corpus_entry(header, row[3]))
                            if len(archive_msgs) >= _CORPUS_CANDIDATE_CAP:
                                break
                except Exception as e:
                    logger.error(f"Error searching stomat_archive.db: {e}")

            wiki_corpus = "\n".join(_rank_corpus_entries(wiki_facts, keywords)) if wiki_facts else ""
            archive_corpus = "\n".join(_rank_corpus_entries(archive_msgs, keywords)) if archive_msgs else ""
            return wiki_corpus, archive_corpus
        except Exception as e:
            logger.error(f"Error in sync_search: {e}")
            return "", ""

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, sync_search)
    except Exception as e:
        logger.error(f"Error in search_knowledge_corpus: {e}")
        return "", ""

async def query_db_async(query_sql, params=()):
    def operation():
        with database._connection() as db:
            c = db.cursor()
            c.execute(query_sql, params)
            return c.fetchall()
    return await database._run_db(operation)


# Жёсткий предел Telegram на ОДНО сообщение. Считается по тексту без разметки:
# теги parse_mode='html' в него не входят.
_TELEGRAM_HARD_LIMIT = 4096
# Запас под заголовок, который навешивается на статью в UI: «📖 <b>имя подтемы</b>
# \n<i>Статья 3734 из 3734</i>». Замер по WIKI_SUBTOPIC_NAMES: 61 символ на самом
# длинном из 50 имён, 96 взято с запасом на будущие имена. Запас обязателен:
# страница статьи показывается через edit_message, а правку на части разбить
# нельзя — при переборе Telegram отклоняет ВСЁ сообщение, и врач видит не
# урезанную статью, а пустоту и мёртвые кнопки.
_ARTICLE_HEADER_RESERVE = 96
# Столько плоского текста статьи влезает в одно сообщение вместе с заголовком.
_ARTICLE_PLAIN_MAX_CHARS = _TELEGRAM_HARD_LIMIT - _ARTICLE_HEADER_RESERVE
# Если не влезло, место занимает ещё и приписка про обрезку (замер: 75 символов).
_ARTICLE_SHOWN_MAX_CHARS = _ARTICLE_PLAIN_MAX_CHARS - 96


_NEGATION_TERMS = re.compile(
    r'(?i)\b(?:отсутству\w*|не\s+обнаружен\w*|не\s+содержит\w*|не\s+представлен\w*|'
    r'не\s+найден\w*|не\s+выявлен\w*|нет\s+признаков|без\s+признаков|'
    r'не\s+является\s+стоматологическ\w*|не\s+относит\w*\s+к\s+стоматолог\w*|'
    r'не\s+имеет\s+отношения\s+к\s+стоматолог\w*)\b'
)

_DENTAL_MARKERS = re.compile(
    r'(?i)\b(?:стоматолог\w*|зуб\w*|дентальн\w*|клиническ\w*|патолог\w*|челюст\w*|десн\w*|рентген\w*|ортодонт\w*)\b'
)

def strip_vision_negations(text: str) -> str:
    """
    Удаляет из описания изображения предложения, где модель явно указывает
    на ОТСУТСТВИЕ стоматологических объектов или патологий (например:
    'Стоматологические инструменты или зубы отсутствуют'). Это предотвращает ложные
    срабатывания has_dental_term на праздничных открытках, мемах и котиках.
    """
    if not text:
        return ""
    sentences = re.split(r'([.!?\n]+)', text)
    filtered = []
    i = 0
    while i < len(sentences):
        sent = sentences[i]
        delim = sentences[i+1] if i+1 < len(sentences) else ""
        i += 2
        if _NEGATION_TERMS.search(sent) and _DENTAL_MARKERS.search(sent):
            continue
        filtered.append(sent + delim)
    return "".join(filtered).strip()


def clean_vertex_redirect_urls(text: str) -> str:
    """Очищает внутренние URL-редиректы Google Vertex Search до читаемых меток."""
    if not text:
        return ""
    def _md_repl(m):
        title = m.group(1).strip()
        return f"<b>{title or 'Научный источник'}</b>"
    res = re.sub(r'\[([^\]]+)\]\(https?://vertexaisearch\.cloud\.google\.com/grounding-api-redirect/[^\)]+\)', _md_repl, text)
    res = re.sub(r'https?://vertexaisearch\.cloud\.google\.com/grounding-api-redirect/\S+', '<i>[Научная публикация]</i>', res)
    return res


def clean_html_formatting(text):
    if not text:
        return ""
    text = clean_vertex_redirect_urls(text)
    # Strip database codes/fact indexes (e.g. [2.1.1], [1.3])
    text = re.sub(r'\s*\[\d+(?:\.\d+)+\]', '', text)

    plain = re.sub(r'<[^>]+>', '', text)
    if len(plain) > _ARTICLE_PLAIN_MAX_CHARS:
        # Обрезка с потерей — только когда врачу физически не отдать всё.
        # Порог стоял по длине С РАЗМЕТКОЙ, а резалась длина ПЛОСКОГО текста:
        # 3 статьи вики (4003, 4020, 4043 символа) помещаются в предел Telegram,
        # но получали приписку «ещё 931 не поместились», и в отрезанном хвосте
        # были дозировки. Само число тоже было неправдой: из 931 символа не
        # влезали 103, остальные 828 выбрасывал слепой поиск границы.
        shown, hidden = html_safe.clip_at_sentence(plain, _ARTICLE_SHOWN_MAX_CHARS)
        notice = (f"\n\n[Показано {len(shown)} символов из {len(plain)}; "
                  f"ещё {hidden} не поместились в одно сообщение]")
        body = shown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return body + notice

    if len(text) > _TELEGRAM_HARD_LIMIT:
        # Слова помещаются, за предел текст вывела только разметка. Платим
        # тремя тегами, а не последним предложением статьи: отдать врачу
        # протокол без концовки ради сохранённого <b> — не обмен, а потеря.
        # Ниже жёсткого предела разметку не трогаем: отправка длинного ответа
        # идёт через html_safe.split_html, и там каждая часть валидна сама.
        return plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Convert Markdown bold **text** to HTML bold <b>text</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Convert unsupported HTML lists (ul/ol/li) to clean bullet points before tag escaping
    text = re.sub(r'</?(?:ul|ol)[^>]*>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<li[^>]*>', '\n• ', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '', text, flags=re.IGNORECASE)
    # Temporarily hide valid HTML tags we want to support
    text = text.replace("<b>", "__B_OPEN__").replace("</b>", "__B_CLOSE__")
    text = text.replace("<i>", "__I_OPEN__").replace("</i>", "__I_CLOSE__")
    text = text.replace("<code>", "__C_OPEN__").replace("</code>", "__C_CLOSE__")
    # Escape raw HTML syntax characters to prevent Telegram parse errors
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Restore valid tags
    text = text.replace("__B_OPEN__", "<b>").replace("__B_CLOSE__", "</b>")
    text = text.replace("__I_OPEN__", "<i>").replace("__I_CLOSE__", "</i>")
    text = text.replace("__C_OPEN__", "<code>").replace("__C_CLOSE__", "</code>")

    # Балансировка тегов: Telegram отклоняет сообщение целиком при любом незакрытом теге
    balanced, unclosed = html_safe.balance_html(text)
    if unclosed:
        balanced += "".join(f"</{tag}>" for tag in reversed(unclosed))
    return balanced


# Требование замолчать, не совпадающее с клинической лексикой. Ищется как
# целое слово с окончанием до трёх букв: «замолчи», «замолчите», «надоел»,
# «надоели».
_SILENCE_DEMANDS = (
    "назойлив", "замолчи", "заткнись", "заткни", "закройся", "помолчи",
    "бесишь", "затычка", "кикнуть", "забань", "подбань",
)

# Слова, которые допустимы ТОЛЬКО целиком, без окончаний: «отвали» — это
# требование, «отваливается» — про коронку.
_SILENCE_EXACT = ("отвали", "уймись", "угомонись")

# Требования, выраженные фразой. Одиночные слова здесь опасны: «удали» живёт
# в «удалить зуб», «отвали» — в «отваливается коронка», «завали» — в «завалил
# стенку», «хватит» — в «не хватит места». Замер по архиву на 107 316
# сообщений: подстрочный список давал 623 совпадения, из них клинических —
# подавляющая часть.
_SILENCE_PHRASES = (
    "не пиши", "не пишите", "хватит спамить", "закрой рот", "не зуди",
    "удали бот", "удалить бот", "удалите бот", "выключи бот", "выключите бот",
    "выруби бот", "отключи бот", "убери бот", "выкинуть бот", "забаньте бот",
    # «надоел» отдельным словом не годится: «эта работа надоела» — не про бота,
    # а отписка от пингов срабатывала бы навсегда.
    "ты надоел", "надоел бот", "бот надоел", "надоели твои", "надоел ты",
)

# Обращение к боту. «бот» и «робот» — как начало слова, чтобы не цепляться за
# «работа» и «суббота»; упоминания — по любому @…bot.
_BOT_REFERENCE_RE = re.compile(
    r"\bбот\w{0,3}\b|\bробот\w{0,3}\b|\bкоординатор|\bдушн|@\w*bot\b",
    re.IGNORECASE,
)

_SILENCE_DEMAND_RE = re.compile(
    "|".join(rf"\b{re.escape(word)}\w{{0,3}}\b" for word in _SILENCE_DEMANDS),
    re.IGNORECASE,
)

_SILENCE_EXACT_RE = re.compile(
    "|".join(rf"\b{re.escape(word)}\b" for word in _SILENCE_EXACT),
    re.IGNORECASE,
)


def is_negative_feedback(text):
    """
    Требует ли сообщение, чтобы бот замолчал.

    Прежний вариант искал подстроки, и половина списка совпадала с обычной
    речью стоматолога: «удали» в «пришлось зуб удалить», «отвали» в
    «коронка отваливается», «завали» в «завалил стенку», «достали» в
    «досталась по наследству». Через check_and_apply_silence это глушило бота
    на четыре часа, а через отписку в ЛС — навсегда отключало ему право
    писать врачу. Замер по живому архиву: 97% срабатываний были ложными.
    """
    if not text:
        return False
    text_lower = text.strip().lower()
    if any(phrase in text_lower for phrase in _SILENCE_PHRASES):
        return True
    if _SILENCE_EXACT_RE.search(text_lower):
        return True
    return bool(_SILENCE_DEMAND_RE.search(text_lower))


async def check_and_apply_silence(event, text, reply_to_msg_id):
    """
    Проверяет, содержит ли сообщение критику/требование выключить бота.
    Если да - отправляет извинение, вешает 4-часовую тишину и возвращает True.
    """
    if not text:
        return False
        
    text_lower = text.lower()
    is_about_bot = False
    
    # Check if reply to bot
    global BOT_ID
    if reply_to_msg_id and BOT_ID:
        try:
            parent_msg = await event.client.get_messages(event.chat_id, ids=reply_to_msg_id)
            if parent_msg and parent_msg.sender_id == BOT_ID:
                is_about_bot = True
        except Exception:
            pass
            
    # Слово «бот» ищется как НАЧАЛО слова. Подстрокой оно живёт в «работа»,
    # «суббота», «заботиться», «обработать» — и вместе с детектором негатива
    # это глушило бота на четыре часа от обычного клинического поста. Замер по
    # архиву: из 68 срабатываний 66 были ложными, среди них «Моя
    # ортопедическая работа. Спустя 4 года обострился хронический Pt».
    if _BOT_REFERENCE_RE.search(text_lower):
        is_about_bot = True
    resolved_username = (BOT_USERNAME or "").lower()
    if resolved_username and f"@{resolved_username}" in text_lower:
        is_about_bot = True
        
    if is_about_bot and is_negative_feedback(text):
        logger.warning(f"Global negative feedback detected: '{text}'. Silencing bot.")
        state = load_state()
        state["silenced_until"] = (datetime.now() + timedelta(hours=4)).isoformat()
        save_state(state)
        apology = "Понял, умолкаю. Если понадоблюсь - позовите."
        try:
            await event.reply(apology)
        except Exception as reply_err:
            logger.error(f"Failed to reply with apology: {reply_err}")
        return True
        
    return False


# Сколько справки показывать рецензенту.
#
# Здесь стояло 3000 — защита от справки неограниченной длины: до введения
# бюджета корпуса она разрасталась до 52 тысяч символов, и показывать её
# рецензенту целиком было нельзя. Теперь длину ограничивает сам корпус
# (_CORPUS_MAX_CHARS), поэтому обрезать ещё раз незачем, а цена обрезки высокая.
#
# Замер на 294 запросах со справкой: при пределе 3000 рецензент видел медиану
# 53% справки вики (минимум 50%), в 235 запросах из 294 — не всю. От него было
# скрыто 39% фактов (1660 из 4206) и 18% ЧИСЕЛ (1214 из 6623). А правило 3.1 его
# промпта отклоняет ответ за конкретные цифры, «которых нет ни в справке выше,
# ни в общепризнанных стандартах». То есть примерно каждая пятая законная цифра
# из базы знаний выглядела для него выдуманной, а явный отказ глушит черновик
# ВСЕГДА — и на пассивном пути, и когда врач спросил напрямую.
#
# Значение связано с бюджетом корпуса намеренно: разъехавшись, они вернут
# слепую зону. Прирост промпта рецензента — порядка тысячи токенов; он работает
# на LOW с таймаутом 15 с, и это заведомо в пределах. Замера задержки на живом
# API здесь нет, это расчёт, а не проверенный факт.
VALIDATOR_REFERENCE_MAX_CHARS = _CORPUS_MAX_CHARS


async def check_response_quality(context_msgs: list, draft_reply: str, invited: bool = False,
                                 reference: str = "") -> tuple[bool, str]:
    """
    Post-generation validator: проверяет черновик ответа бота на галлюцинации,
    клинический бред и несоответствие контексту.
    Возвращает (allow: bool, reason: str).

    invited=True  — пользователь спросил напрямую (ответ на реплику бота, упоминание, ЛС).
    invited=False — бот влезает в разговор сам (пассивный триггер).

    Политика при НЕДОСТУПНОМ валидаторе (таймаут, сетевая ошибка, мусор вместо JSON):
      * invited=False -> глушим черновик. Молчание незваного бота не стоит ничего,
        непроверенная клиническая отсебятина стоит дорого. Каскад моделей банит
        модель на 20 минут после первого 503, так что "валидатор недоступен" —
        это не редкий край, а регулярное состояние.
      * invited=True  -> пропускаем. Врач задал вопрос и ждёт ответа; молча
        проигнорировать его хуже, чем отдать текст, уже прошедший EBM-инструкции
        основного промпта. Пишем WARNING, чтобы это было видно в логах.
    Явный отказ валидатора (ok:false) глушит черновик ВСЕГДА, на обоих путях.
    """
    if not draft_reply or not draft_reply.strip():
        return False, "empty_draft"

    def _unavailable(detail):
        if invited:
            logger.warning(f"Response validator unavailable ({detail}). Invited reply — allowing.")
            return True, f"validator_unavailable: {detail}"
        logger.warning(f"Response validator unavailable ({detail}). Uninvited reply — suppressing.")
        return False, f"validator_unavailable: {detail}"

    try:
        context_str = "\n".join(context_msgs[-10:])
        # Рецензент видит и справку, на которой строился ответ. Без неё он не мог
        # отличить число из базы знаний от выдуманного: оба выглядят одинаково
        # правдоподобно. Даём ТОЛЬКО выжимку вики — дистиллированные факты.
        # Архив это живые мнения коллег с ошибками, и отклонять верный
        # EBM-ответ за расхождение с чужой ошибкой было бы ровно наоборот.
        reference_block = ""
        if reference and reference.strip():
            trimmed = reference.strip()[:VALIDATOR_REFERENCE_MAX_CHARS]
            reference_block = (
                "\nСправка из Базы Знаний, на которой строился ответ:\n"
                f"{trimmed}\n"
                "[Справка НЕ исчерпывающая: отсутствие темы в ней само по себе "
                "не повод отклонять общее клиническое рассуждение.]\n"
            )

        prompt = f"""Ты — строгий клинический рецензент стоматологического Telegram-чата.
Тебе дан контекст переписки, справка из базы знаний и черновик ответа ИИ-ассистента.
Твоя задача: оценить, является ли черновик корректным, профессиональным и безопасным ответом.

Контекст переписки:
{context_str}
{reference_block}
Черновик ответа ИИ-ассистента:
{draft_reply}

Отклони черновик (ok: false), если:
1. Опасная клиническая галлюцинация, совет, угрожающий пациенту, или грубая ошибка в патофизиологии/биомеханике.
2. Черновик содержит поверхностный псевдонаучный жаргон, выдуманные термины или бессодержательные утверждения без доказательного объяснения механизма (например, вбросы вроде "нужна ортодонтическая хирургия, иначе резорбция").
3. Ответ содержит неуместные, несерьёзные или нервные эмодзи (😅, 😂, 😎, 😤, 😏, 🤣, 🤡, 🙄).
4. Тон высокомерен, саркастичен, токсичен или представляет собой бессмысленный однострочный вброс/комментарий.
5. Ответ содержит выдуманные дозировки, протоколы или КОНКРЕТНЫЕ ЦИФРЫ, противоречащие общепринятой практике.
6. Ответ вообще не относится к медицине/стоматологии или уводит тему в сторону.

Одобри черновик (ok: true), если:
— Ответ клинически грамотен, профессионален, спокоен, по теме и безопасен.

Отвечай СТРОГО в формате JSON без дополнительного текста:
{{"ok": true/false, "reason": "одна фраза на русском"}}
"""
        ctx = {"kind": "response_validator", "thinking_level": "LOW"}
        response, error = await generate_gemini_text_async(prompt, ctx, timeout=15)
        if error or not response:
            return _unavailable(error or "empty response")

        text = (getattr(response, "text", None) or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return _unavailable(f"no JSON object in verdict: {text[:120]!r}")

        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as parse_err:
            return _unavailable(f"unparseable verdict: {parse_err}")
        if not isinstance(data, dict) or "ok" not in data:
            # Нет вердикта — это НЕ одобрение. Раньше отсутствующий ключ
            # молча превращался в ok=True и пропускал черновик.
            return _unavailable(f"verdict without 'ok' field: {str(data)[:120]}")

        reason = str(data.get("reason") or "").strip()
        if data["ok"] is True or str(data["ok"]).strip().lower() == "true":
            return True, reason or "approved"
        return False, reason or "rejected by validator"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Response quality validator exception: {e}")
        return _unavailable(f"exception: {e}")


async def check_llm_triage(context_msgs):
    """
    Отправляет последние сообщения в Llama-3 для принятия решения:
    стоит ли вступать в разговор. Возвращает True/False.
    """
    try:
        context_str = "\n".join(context_msgs)
        triage_prompt = f"""Ты — строгий клинический координатор стоматологического Telegram-чата "StomChat".
Твоя задача — проанализировать последние сообщения и решить, уместен ли ответ ИИ-ассистента.

ГЛАВНЫЙ ПРИНЦИП: Незваный бот в чате — это РАЗДРАЖИТЕЛЬ, если он влезает в живой разговор людей. По умолчанию бот должен МОЛЧАТЬ (should_reply: false).

Когда ОТВЕЧАТЬ (should_reply: true) — ТОЛЬКО В ЭТИХ СЛУЧАЯХ:
1. Прямой вопрос/обращение к боту (тег @, упоминание бота, прямой ответ на реплику бота).
2. Конкретный клинический вопрос/кейс от врача, на который в чате НИКТО НЕ ОТВЕТИЛ (висит без ответа, врачу нужна помощь).

Когда КАТЕГОРИЧЕСКИ ИГНОРИРОВАТЬ (should_reply: false):
1. ИДЕТ ЖИВОЙ РАЗГОВОР/СПОР МЕЖДУ ЛЮДЬМИ: Если 2 или более коллег уже переписываются, спорят, отвечают друг другу, делятся мнениями — НЕ ВМЕШИВАТЬСЯ! Не встревать со своим мнением, не делать реплик-комментариев.
2. Вопрос уже обсуждается живыми участниками.
3. Нерелевантные или неклинические темы (налоги, юмор, быт, цены, работа клиники, расписание, флуд).
4. Короткие реплики, шутки, сарказм, мысли вслух ("дорастет", "есть кейсы", "не безопасно", "кыш").
5. Если ответ бота будет просто короткой репликой/вбросом на чужое сообщение — СТРОГО ЗАПРЕЩЕНО.

Последние сообщения в чате:
{context_str}

Отвечай СТРОГО в формате JSON без какого-либо дополнительного текста (без разметки markdown вроде ```json):
{{
  "should_reply": true/false,
  "confidence": 0.0-1.0,
  "reason": "короткое объяснение причины на русском"
}}
"""
        triage_ctx = {"kind": "llama_triage", "thinking_level": "LOW"}
        response, error = await generate_gemini_text_async(triage_prompt, triage_ctx, timeout=8)
        
        if error or not response:
            logger.warning(f"Llama triage generation failed: {error}. Defaulting to False to avoid spam.")
            return False
            
        text = response.text.strip() if hasattr(response, "text") else str(response).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end+1]
        
        data = json.loads(text)
        should_reply = data.get("should_reply", False)
        reason = data.get("reason", "No reason provided")
        confidence = float(data.get("confidence", 1.0))
        
        if confidence < 0.85 and should_reply:
            logger.info(f"Llama triage confidence too low ({confidence}). Overriding should_reply to False.")
            should_reply = False
        
        logger.info(f"Llama Triage decision: should_reply={should_reply} (confidence={confidence}). Reason: {reason}")
        return should_reply
    except Exception as e:
        logger.error(f"Error in Llama triage check: {e}. Defaulting to False.")
        return False


# ---------------------------------------------------------------------------
# Dynamic chat context builder
# ---------------------------------------------------------------------------
_DCTX_DIALOG_MAX_CHARS = 25_000
_DCTX_RECENT_FULL_LEN  = 1_000   # последние 4 реплики — полный текст
_DCTX_DEEP_TRUNC_LEN   = 600     # глубокие реплики


def _parse_db_date(val):
    """Разбирает дату из поля date БД — строка '%Y-%m-%d %H:%M:%S' UTC."""
    if not val:
        return datetime(2000, 1, 1)
    if isinstance(val, datetime):
        return val
    try:
        return datetime.strptime(str(val), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.fromisoformat(str(val))
        except (ValueError, TypeError):
            return datetime(2000, 1, 1)


def _normalize_row(r):
    """
    Нормализует кортеж из БД к единому формату:
    (msg_id, reply_to_msg_id, sender_id, sender_name, text, date)
    Поддерживает:
      - 6+ колонок: (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, ...)
      - 4 колонки: (sender_name, text, msg_id, reply_to_msg_id) — легаси-запросы / тестовые моки
    """
    if not isinstance(r, (list, tuple)):
        return (0, None, None, "Участник", str(r), datetime(2000, 1, 1))
    if len(r) >= 6:
        return (r[0], r[1], r[2], r[3], r[4], r[5])
    elif len(r) == 4:
        return (r[2], r[3], None, r[0], r[1], datetime(2000, 1, 1))
    elif len(r) >= 3:
        return (r[0], None, None, r[1] if len(r) > 1 else "Участник", r[2] if len(r) > 2 else "", datetime(2000, 1, 1))
    return (0, None, None, "Участник", "", datetime(2000, 1, 1))


async def fetch_dynamic_chat_context(
    msg_id,
    reply_to_msg_id,
    base_limit=12,
    max_limit=40,
    max_gap_minutes=15,
    event=None,
):
    """
    Динамически собирает историю дискуссии из локальной БД (с fallback на Telegram API при необходимости).

    Возвращает (context_lines: list[str], bot_msg_count: int, nearest_bot_msg_id: int|None).
    """
    rows = []   # [(msg_id, reply_to_msg_id, sender_id, sender_name, text, date)]
    seen = set()
    bot_msg_count = 0
    nearest_bot_msg_id = None

    event_client = getattr(event, 'client', None)
    event_chat_id = getattr(event, 'chat_id', None)

    try:
        if reply_to_msg_id:
            # --- Путь 1: Reply-цепочка (вверх по дереву ответов) + консилиум (соседние ответы) ---
            curr_id = msg_id
            while curr_id and len(rows) < max_limit:
                if curr_id in seen:
                    logger.warning("fetch_dynamic_chat_context: cycle at msg_id=%s", curr_id)
                    break
                seen.add(curr_id)
                row = await query_db_async(
                    "SELECT msg_id, reply_to_msg_id, sender_id, sender_name, text, date "
                    "FROM messages WHERE msg_id = ?",
                    (curr_id,),
                )
                if not row and event_client and event_chat_id:
                    # Fallback на Telegram API, если сообщение старше БД или из внешнего контекста
                    try:
                        tg_msg = await event_client.get_messages(event_chat_id, ids=curr_id)
                        if tg_msg:
                            s_sender = getattr(tg_msg, 'sender', None)
                            s_name = getattr(s_sender, 'first_name', '') or getattr(s_sender, 'title', '') or 'Участник'
                            s_reply = getattr(getattr(tg_msg, 'reply_to', None), 'reply_to_msg_id', None)
                            row = [(tg_msg.id, s_reply, getattr(tg_msg, 'sender_id', None), s_name, getattr(tg_msg, 'message', '') or '', getattr(tg_msg, 'date', None) or datetime(2000, 1, 1))]
                    except Exception:
                        pass

                if not row:
                    # Если само текущее сообщение ещё не легло в БД (гонка записи),
                    # не сбрасываем контекст в 0 — сразу переходим к родителю
                    if curr_id == msg_id and reply_to_msg_id:
                        curr_id = reply_to_msg_id
                        continue
                    break
                r = _normalize_row(row[0])
                rows.append(r)
                if BOT_ID and r[2] == BOT_ID:
                    bot_msg_count += 1
                    if nearest_bot_msg_id is None:
                        nearest_bot_msg_id = r[0]
                curr_id = r[1]  # родительский reply_to_msg_id

            # Подтягиваем соседние ответы в той же ветке (консилиум врачей под постом)
            # В Telegram-чате врачи часто отвечают на один и тот же корневой кейс,
            # являясь «братьями» по ветке (sibling replies), а не прямой цепочкой.
            if len(rows) < max_limit and reply_to_msg_id:
                rem = max_limit - len(rows)
                sibling_rows = await query_db_async(
                    "SELECT msg_id, reply_to_msg_id, sender_id, sender_name, text, date "
                    "FROM messages WHERE reply_to_msg_id = ? AND msg_id <= ? "
                    "ORDER BY date DESC, msg_id DESC LIMIT ?",
                    (reply_to_msg_id, msg_id, rem),
                )
                for item in sibling_rows:
                    s = _normalize_row(item)
                    if s[0] not in seen:
                        seen.add(s[0])
                        rows.append(s)
                        if BOT_ID and s[2] == BOT_ID:
                            bot_msg_count += 1
                            if nearest_bot_msg_id is None:
                                nearest_bot_msg_id = s[0]

            # Сортируем все собранные реплики строго в хронологическом порядке
            rows.sort(key=lambda r: (_parse_db_date(r[5]), r[0]))

        else:
            # --- Путь 2: Общий поток с gap-detection ---
            raw_rows = await query_db_async(
                "SELECT msg_id, reply_to_msg_id, sender_id, sender_name, text, date "
                "FROM messages WHERE msg_id <= ? ORDER BY date DESC, msg_id DESC LIMIT ?",
                (msg_id, max_limit),
            )
            all_rows = [_normalize_row(x) for x in raw_rows]
            # Отсечка по первой паузе > max_gap_minutes,
            # но берём не меньше base_limit если данные есть
            cut_idx = len(all_rows)
            for i in range(1, len(all_rows)):
                d_prev = _parse_db_date(all_rows[i - 1][5])
                d_curr = _parse_db_date(all_rows[i][5])
                gap_sec = (d_prev - d_curr).total_seconds()
                if gap_sec > max_gap_minutes * 60:
                    cut_idx = max(i, min(base_limit, len(all_rows)))
                    break
            rows = all_rows[:cut_idx][::-1]  # хронологический
            for r in rows:
                if BOT_ID and r[2] == BOT_ID:
                    bot_msg_count += 1
                    if nearest_bot_msg_id is None:
                        nearest_bot_msg_id = r[0]

    except Exception as exc:
        logger.error("fetch_dynamic_chat_context error: %s", exc)
        return [], 0, None

    # --- Форматирование ---
    result = []
    total_chars = 0
    N = len(rows)
    for idx, r in enumerate(rows):
        r_msg_id, r_reply_to, r_sender_id, r_sender_name, r_text, _ = r
        trunc_limit = _DCTX_RECENT_FULL_LEN if idx >= N - 4 else _DCTX_DEEP_TRUNC_LEN
        raw_text = r_text or ""
        msg_text = raw_text[:trunc_limit] + ("... [обрезано]" if len(raw_text) > trunc_limit else "")
        sender_label = "[ЭТО ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ]" if r_sender_id == BOT_ID else (r_sender_name or "Участник")
        rep_str = f" (в ответ на #{r_reply_to})" if r_reply_to else ""
        line = f"[Сообщение #{r_msg_id}{rep_str}] {sender_label}: {msg_text}"
        if total_chars + len(line) > _DCTX_DIALOG_MAX_CHARS:
            logger.info("fetch_dynamic_chat_context: char limit hit at %s/%s msgs", idx + 1, N)
            break
        result.append(line)
        total_chars += len(line)

    return result, bot_msg_count, nearest_bot_msg_id


async def check_and_trigger_assistant(bot_client, event, msg_id, text, reply_to_msg_id, sender_first_name=None):
    if text and len(text) > 1500:
        text = text[:1500] + "..."

    if text and text.strip().startswith("/"):
        return False

    # На это сообщение уже отвечали — второй раз не отвечаем.
    # REPLIED_MSG_IDS был мёртвым кодом ровно на этом пути: медиа-ветка его
    # читает на входе (check_and_trigger_assistant_media) и пишет после
    # отправки, а текстовая не делала ни того, ни другого. Из-за этого гард
    # медиа-ветки не видел текстовых отправок, и одно и то же сообщение —
    # например снимок с подписью — могло получить два ответа. Единственная
    # защита текстового пути жила в main.py (PROCESSED_MSG_IDS, 500 записей в
    # памяти), а её снимает и рестарт, и повторный прогон sync_history.
    if msg_id in REPLIED_MSG_IDS:
        logger.info("Assistant already replied to message %s. Skipping text trigger.", msg_id)
        return False

    global BOT_ID

    # 1. Проверяем глобальную критику / требование выключить бота
    if await check_and_apply_silence(event, text, reply_to_msg_id):
        REPLIED_MSG_IDS[msg_id] = True  # извинение отправлено — это тоже ответ
        return True

    state = load_state()
    
    # Check if the bot is temporarily silenced
    if is_silenced(state):
        return False
    
    # Calculate context length guidelines
    try:
        recent_texts = []
        # We will get last messages from DB to count words
        db_history = await database.get_last_n_messages(limit=6)
        if db_history:
            for m in db_history:
                if isinstance(m, (list, tuple)) and len(m) > 3:
                    recent_texts.append(m[3] or "")
                elif isinstance(m, dict):
                    recent_texts.append(m.get("text", "") or "")
                else:
                    recent_texts.append(getattr(m, "text", "") or "")
        length_guideline = calculate_context_length_guidelines(recent_texts)
    except Exception as calc_err:
        logger.error(f"Error calculating length guideline: {calc_err}")
        length_guideline = "Отвечай кратко, до 3-4 предложений."

    triggered = False
    trigger_reason = ""
    context_msgs = []
    is_dialogue = False
    pending_thread_id = None  # помечается обработанным только после успешной отправки
    
    # Try dynamic BOT_ID resolution if it is missing
    if reply_to_msg_id and not BOT_ID:
        if await resolve_bot_identity(bot_client):
            logger.info(f"Dynamically resolved BOT_ID: {BOT_ID} (@{BOT_USERNAME})")

    # 1. Check Dialogue Reaction or Thread Continuation with Bot
    if reply_to_msg_id and BOT_ID:
        try:
            # Проверяем прямого родителя через Telegram client, если доступен
            direct_parent = None
            if hasattr(event, 'client') and event.client:
                try:
                    direct_parent = await event.client.get_messages(event.chat_id, ids=reply_to_msg_id)
                except Exception:
                    pass

            # Используем DB-based fetch вместо Telegram API walk (было: range(6) x get_messages).
            # fetch_dynamic_chat_context возвращает до max_limit=40 реплик по reply-цепочке.
            chain, bot_msg_count, nearest_bot_msg_id = await fetch_dynamic_chat_context(
                msg_id, reply_to_msg_id, base_limit=12, max_limit=40, event=event
            )
            if direct_parent and getattr(direct_parent, 'sender_id', None) == BOT_ID:
                if bot_msg_count == 0:
                    bot_msg_count = 1
                if not nearest_bot_msg_id:
                    nearest_bot_msg_id = reply_to_msg_id

            found_bot_in_chain = bot_msg_count > 0

            if found_bot_in_chain and bot_msg_count < 3:
                is_dialogue = True
                
                # Check for criticism / negative feedback from user
                if is_negative_feedback(text):
                    logger.warning(f"Negative feedback detected in dialogue reply: '{text}'. Silencing bot.")
                    state["silenced_until"] = (datetime.now() + timedelta(hours=4)).isoformat()
                    save_state(state)
                    apology = "Понял, умолкаю. Если понадоблюсь — позовите."
                    await event.reply(apology)
                    REPLIED_MSG_IDS[msg_id] = True
                    return True

                # Проверяем "свежесть" диалога. Если с момента отправки сообщения бота в группе
                # прошло более 5 сообщений от других участников, значит тема сместилась. Игнорируем.
                ref_id = nearest_bot_msg_id or reply_to_msg_id
                try:
                    msgs_since = await query_db_async(
                        "SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < 90000000",
                        (ref_id,)
                    )
                    count_since = msgs_since[0][0] if msgs_since else 0
                except Exception as db_err:
                    logger.error(f"Error checking message distance: {db_err}")
                    count_since = 0

                if count_since > 5:
                    logger.info(f"Dialogue reply is stale. {count_since} messages have passed since bot message {ref_id}. Skipping to avoid thread hijacking.")
                    return False

                # Умный анализ продолжения диалога через триаж
                recent_group_db = await database.get_last_n_messages(limit=5)
                recent_group_texts = []
                if recent_group_db:
                    recent_group_db = recent_group_db[::-1]
                    for r in recent_group_db:
                        if isinstance(r, (list, tuple)) and len(r) > 3:
                            sender_name = r[1] or "Участник"
                            msg_text = r[3] or ""
                            if msg_text:
                                recent_group_texts.append(f"{sender_name}: {msg_text}")
                should_continue = await check_dialogue_continuation_triage(chain, recent_group_texts)
                if not should_continue:
                    logger.info(f"Dialogue triage rejected continuation for chain with {bot_msg_count} bot replies. Stopping.")
                    return False
                logger.info(f"Triage approved dialogue continuation (bot_msg_count={bot_msg_count}).")
                
                triggered = True
                trigger_reason = f"Dialogue continuation in thread with bot message {ref_id} (bot_msg_count={bot_msg_count})"
                context_msgs = chain
        except Exception as e:
            logger.error(f"Error checking dialogue chain: {e}")

    # 1.1. Check Sequential Follow-up from recent case author (when doctor types without Reply button)
    if not is_dialogue and not reply_to_msg_id:
        last_case_author = state.get("last_case_author_id")
        last_case_bot_msg = state.get("last_case_bot_msg_id")
        last_case_time = _parse_state_dt(state.get("last_case_time"))
        
        if (
            last_case_author 
            and event.sender_id == last_case_author 
            and (datetime.now() - last_case_time) < timedelta(minutes=10)
        ):
            try:
                ref_id = last_case_bot_msg or 0
                msgs_since = await query_db_async(
                    "SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < 90000000",
                    (ref_id,)
                )
                count_since = msgs_since[0][0] if msgs_since else 0
            except Exception:
                count_since = 0

            if count_since <= 5:
                # Используем fetch_dynamic_chat_context: единый формат, корректные имена и тексты
                recent_chain, bot_in_chain_count, _ = await fetch_dynamic_chat_context(
                    msg_id, None, base_limit=12, max_limit=12, event=event
                )
                if bot_in_chain_count < 3 and recent_chain:
                    should_continue = await check_dialogue_continuation_triage(recent_chain, recent_chain[-3:])
                    if should_continue:
                        logger.info("Triage approved sequential follow-up from case author %s", event.sender_id)
                        is_dialogue = True
                        triggered = True
                        trigger_reason = f"Sequential follow-up from case author {event.sender_id}"
                        context_msgs = recent_chain
            
    # Cooldown gate for all passive text triggers (прямые обращения сюда не попадают).
    # fromisoformat здесь раньше стоял без обработки — битый таймстамп в состоянии
    # ронял весь обработчик сообщения.
    if not is_dialogue:
        block_reason = passive_gate_block_reason(state)
        if block_reason:
            # info, а не debug: корневой уровень журнала — INFO, поэтому debug не
            # эмитится НИКОГДА. Замер по всем журналам на диске (126 340 строк):
            # строка «Passive text trigger suppressed» встречается 0 раз. При этом
            # кулдаун закрыт большую часть суток и отбрасывает почти все
            # сообщения — то есть это статистически главная причина, по которой
            # бот в чате молчит, и она была ненаблюдаема вообще. Все соседние
            # ветки того же решения (тишина, устаревший диалог, отказ триажа,
            # пустой корпус, IGNORE, отказ рецензента) пишутся на INFO — выпадал
            # ровно кулдаун. Отличить штатный кулдаун от застрявшего состояния
            # было нельзя без чтения assistant_state.json руками.
            logger.info("Passive text trigger suppressed: %s", block_reason)
            return False

    # 2. Check Reply Thread Reaction
    if not triggered and reply_to_msg_id:
        # Check if parent has media
        parent_rows = await query_db_async("SELECT has_media, text FROM messages WHERE msg_id = ?", (reply_to_msg_id,))
        if parent_rows and bool(parent_rows[0][0]):
            # Count replies
            reply_count_rows = await query_db_async("SELECT COUNT(*) FROM messages WHERE reply_to_msg_id = ?", (reply_to_msg_id,))
            reply_count = reply_count_rows[0][0] if reply_count_rows else 0
            
            if reply_count >= 3 and reply_to_msg_id not in state.get("processed_threads", []):
                # Заявка на слот незваного ответа. И гейт кулдауна (выше), и
                # processed_threads (строкой выше) прочитаны из state, взятого
                # до нескольких await'ов, а списываются они только в
                # record_passive_attempt/record_passive_success. Две реплики
                # одной ветки, попавшие в это окно, обе видели гейт открытым и
                # обе доходили до отправки — два ответа в одну ветку. Замер:
                # 3 такие пары в окне 2 с и 105 в окне 60 с за 1016 суток архива.
                #
                # Ключ один и общий, не по ветке: и last_passive_text_run, и
                # processed_threads — глобальные ключи состояния, одно окно
                # тишины на весь чат. Заявка по ветке была бы вторым ключом,
                # который в поведении неотличим от этого, — то есть ровно та
                # мёртвая защита, от которой мы избавляемся в REPLIED_MSG_IDS.
                #
                # Заявка отказывает сразу, а не ждёт: второй ответ в ту же
                # ветку не нужен, и держать за ним входящее сообщение на всю
                # генерацию (90 с) незачем.
                if not claim_passive_slot(("passive_text",)):
                    return False
                # We have a discussion under a clinical post!
                triggered = True
                trigger_reason = f"Clinical post {reply_to_msg_id} discussion thread (reply_count={reply_count})"
                # Тред помечается обработанным и полное окно списывается только
                # после успешной отправки (record_passive_success ниже). Здесь
                # ставим лишь короткий backoff, чтобы соседние сообщения треда
                # не запускали генерацию параллельно (now moved after triage).
                pending_thread_id = reply_to_msg_id

                # Fetch parent + last replies for context
                rows = await query_db_async(
                    "SELECT sender_name, text, msg_id, reply_to_msg_id FROM messages WHERE msg_id = ? OR reply_to_msg_id = ? ORDER BY date ASC",
                    (reply_to_msg_id, reply_to_msg_id)
                )
                context_msgs = []
                for r in rows:
                    rep_str = f" (в ответ на #{r[3]})" if r[3] else ""
                    context_msgs.append(f"[Сообщение #{r[2]}{rep_str}] {r[0]}: {r[1]}")
    # 2. Check Passive Trigger (General Chat Flow)
    if not triggered:
        # Используем fetch_dynamic_chat_context: base=12, max=40, gap=15 мин.
        # Это заменяет: LIMIT 20 + ручное форматирование + thread-merge блок.
        _passive_ctx, _, _ = await fetch_dynamic_chat_context(
            msg_id, None, base_limit=12, max_limit=40, max_gap_minutes=15, event=event
        )
        
        if _passive_ctx:
            last_text = text or ""  # текущее сообщение уже известно
            
            # Pre-filter: block only OBVIOUS garbage before paying for LLM triage call
            is_obviously_junk = (
                last_text.startswith("/") or
                len(last_text.strip()) < 8 or
                not any(c.isalpha() for c in last_text)
            )
            
            passive_cooldown_active = passive_gate_block_reason(load_state()) is not None

            if not is_obviously_junk and not passive_cooldown_active:
                if not claim_passive_slot(("passive_text",)):
                    return False
                triggered = True
                trigger_reason = "Passive trigger (pending LLM triage)"
                context_msgs = _passive_ctx


    # Триаж проходят ВСЕ незваные срабатывания, включая ветку клинического поста.
    #
    # Здесь стояло исключение: `and not (reply_to_msg_id and "discussion thread"
    # in trigger_reason)`. Посылка была такая — если под постом со снимком уже
    # три ответа, обсуждение заведомо клиническое, и платить за триаж незачем.
    # Замер по архиву эту посылку опровергает.
    #
    # Точный повтор логики ветки на 117 847 репликах, с оба кулдауна и
    # processed_threads: условию удовлетворяют 4075 реплик, после подавления
    # обработанных тредов остаётся 891 РЕАЛЬНОЕ вторжение (0.88 в сутки), и из
    # них 472 — 53% — не содержат ни одного стоматологического слова. Вот на что
    # бот отвечал бы клинической лекцией, не спросив себя, уместно ли это:
    #   «Спасибо вам большое! 🔥🤩», «Я щас уточню», «Смекаю)»,
    #   «Техник рукастый», «Бинго) Или как там?! Фулхаус))», «Вивисекция».
    #
    # Условие ветки — «у родителя есть медиа И под ним 3 ответа» — ничего не
    # говорит о содержании этих ответов. Коллеги хвалят чей-то снимок, третье
    # «Спасибо» выполняет счётчик, и бот вешает лекцию в чужую ветку. Ни
    # check_llm_triage, чей промпт целиком про «пользователи НЕ любят, когда бот
    # лезет в их разговор», ни даже дешёвый отсев очевидного мусора на этот путь
    # не распространялись. Поздний предохранитель почти не работает: гард пустого
    # корпуса снимает 8 случаев из 891, потому что на 117 847 реплик хоть что-то
    # находится почти на любое русское слово.
    #
    # Цена правки: 0.88 дополнительного триажа в сутки. Цена бездействия: бот
    # влезает в разговор коллег примерно раз в сутки, и в половине случаев
    # разговор даже не про стоматологию.
    if triggered and not is_dialogue:
        # Backoff раньше стоял ДО триажа, из-за чего отказ триажа вешал
        # бота в тишину на 10 минут. Теперь пишем только ПОСЛЕ успешного триажа.
        should_reply = await check_llm_triage(context_msgs)
        if not should_reply:
            logger.info("LLM triage decided NOT to reply. Cancelling trigger.")
            return False
        record_passive_attempt()

    if not triggered:
        return False

    # EXTRACT KEYWORDS & SEARCH DB
    # Для обычных триггеров извлекаем ключевые слова ТОЛЬКО из текста текущего вопроса, чтобы избежать каши в RAG
    if is_dialogue:
        user_context_msgs = [m for m in context_msgs if "Бот Учимся Вместе" not in m and "Учимся Вместе:" not in m]
        if not user_context_msgs:
            user_context_msgs = context_msgs
        keyword_source = " ".join(user_context_msgs)
    else:
        keyword_source = text if text else ""
        if not keyword_source:
            user_context_msgs = [m for m in context_msgs if "Бот Учимся Вместе" not in m and "Учимся Вместе:" not in m]
            keyword_source = " ".join(user_context_msgs) if user_context_msgs else ""
            
    keywords = extract_keywords(keyword_source)
    
    search_keywords = select_search_keywords(keywords)
                

    
    wiki_corpus, archive_corpus = await search_knowledge_corpus(search_keywords)
    
    if not is_dialogue and not wiki_corpus and not archive_corpus:
        # If corpus is empty, do not output anything (avoid generic AI fluff)
        logger.info("No matching knowledge corpus found. Skipping assistant run.")
        return False

    # Определяем обращение ДО промпта — сами, не делегируем модели.
    # Модель просто начнёт с готового префикса, выбор уже сделан.
    if is_dialogue:
        address_prefix = ""  # В диалоге без обращения
    else:
        unique_senders = set()
        for cm in context_msgs:
            if ": " in cm:
                unique_senders.add(cm.split(": ", 1)[0].strip())

        if len(unique_senders) > 2:
            # Несколько людей → 50% "Коллеги," / 50% без обращения
            address_prefix = "Коллеги, " if random.random() < 0.5 else ""
        elif sender_first_name:
            # Один автор → 33% имя / 33% "Коллега," / 33% без обращения
            roll = random.random()
            if roll < 0.33:
                address_prefix = f"{sender_first_name}, "
            elif roll < 0.66:
                address_prefix = "Коллега, "
            else:
                address_prefix = ""
        else:
            address_prefix = ""

    if address_prefix:
        address_line = f'Начни ответ строго с "{address_prefix}" — это первые слова. Не меняй, не перефразируй.'
    else:
        address_line = "Начни ответ сразу по делу, без обращения и без имён."

    # Получаем стиль отправителя для применения его предпочтений в группе
    user_profile = await database.get_user_profile(event.sender_id)
    selected_style = user_profile.get("selected_style", DEFAULT_STYLE)
    style_instruction = style_instruction_block(selected_style)

    # BUILD PROMPT
    ignore_instruction = "ЕСЛИ тема чата — чистый флуд, приветствия, погода, политика, оффтоп без связи со стоматологией или медициной — верни ровно одно слово: IGNORE"
    if is_dialogue:
        ignore_instruction = "ЕСЛИ пользователь просто благодарит тебя, соглашается или тема исчерпана — НЕ МОЛЧИ (не пиши IGNORE), а вежливо и грамотно заверши диалог (например, 'Всегда пожалуйста!', 'Обращайтесь!'). Отвечать IGNORE при прямом обращении запрещено."
    
    # Защита от "шизофрении" (когда бот читает свой же ответ и соглашается с ним как с чужим)
    for i in range(len(context_msgs)):
        if "Бот Учимся Вместе 🤖:" in context_msgs[i] or "Учимся Вместе:" in context_msgs[i]:
            context_msgs[i] = context_msgs[i].replace("Бот Учимся Вместе 🤖:", "[ЭТО ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ]:").replace("Учимся Вместе:", "[ЭТО ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ]:")

    if is_dialogue:
        prompt = f"""
Ты — опытный стоматолог-практик, читаешь переписку коллег в чате "StomChat" и решил ответить на заданный вопрос.
Тебе 15+ лет клинической практики, ты видел всякое, говоришь прямо и не любишь воду.
Не строишь из себя учебник — ты коллега, который знает ответ и выдаёт его точно и ёмко.

История диалога (последние сообщения со структурой ответов):
<user_dialogue>
{chr(10).join(context_msgs)}
</user_dialogue>

ТЕБЕ НУЖНО СГЕНЕРИРОВАТЬ ОТВЕТ НА СООБЩЕНИЕ #{msg_id} от {sender_first_name or "коллеги"}. Оно завершает переписку выше. Учитывай хронологию и иерархию (кто кому отвечает через ID сообщений и ссылки "в ответ на #ID"), но отвечай именно на этот конкретный вопрос! Если ты видишь свои предыдущие ответы ([ЭТО ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ]), учитывай их, чтобы не повторяться и не соглашаться с самим собой!

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Похожие обсуждения из Архива чата:
{archive_corpus}

ИНСТРУКЦИИ:
1. {address_line}
2. ДЛИНА ОТВЕТА: {length_guideline}
3. Никаких приветствий, «Уважаемые коллеги», вводных фраз и пожеланий в конце. Сразу по делу.
4. Тон: сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности и без эмодзи-кривляния. Используй профессиональный стоматологический язык. Полностью избегай канцелярщины и фраз типа "Как ИИ...", "Рад помочь", "С уважением".
5. Ограничение по теме: Используй термины и Базу Знаний строго по контексту разговора. Если врачи обсуждают объёмы работы, графики, усталость, деньги или другие организационные темы, а не конкретный лечебный случай — КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО читать клинические лекции и давать медицинские советы по лечению (например, приплетать BOPT, протоколы фиксации циркона и т.п.) из Базы Знаний, если об этом прямо не спросили. В таких случаях общайся только по теме диалога (объёмы, выгорание и т.д.).
6. КЛИНИЧЕСКАЯ ЛОГИКА, МЕХАНИЗМ И СТРОГИЙ EBM: Ты — практикующий врач с глубоким пониманием патфизиологии и биомеханики. Отвечай только на основе строгой клинической логики и доказательной медицины (EBM). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО бросаться псевдонаучными фразочками, выдуманным жаргоном (вроде «ортодонтическая хирургия», «вертикальная резорбция») или делать безапелляционные утверждения без объяснения биологического механизма. Никаких пустых бессмысленных реплик из одной фразы. Если отвечаешь — давай структурированное, клинически грамотное объяснение сути, либо не отвечай вообще.
7. Не повторяй то, что уже написали в чате. Принеси что-то новое — факт, уточнение, протокол, нюанс.
8. СМАЙЛИКИ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать глупые, несерьёзные, панибратские или нервные эмодзи (😅, 😂, 😎, 😤, 😏, 🤣, 🤡, 🙄). В профессиональном клиническом ответе смайлики НЕ НУЖНЫ.
9. Разметка: только HTML — <b>жирный</b>. Никакого Markdown (**текст**). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать в ответе техническую информацию вроде "#168569", "в ответ на #168569", "Сообщение #..." и любые другие ID из контекста. Твой текст должен выглядеть как обычное человеческое сообщение в чате, без системного мусора.
10. ПРОАКТИВНОСТЬ И УМЕСТНОСТЬ:
    - Отвечай строго к месту (по сути текущего вопроса в конце истории диалога). Чётко отделяй текущую живую тему от сухих фактов в Справке/Архиве — не начинай цитировать архив как часть текущего разговора. Не неси околесицу и не зацикливайся на старых сообщениях.
    - Если в чате разгорается конфликт, бессмысленный спор или переписка явно зациклилась на какой-то ерунде, проактивно разряди обстановку. Предложи сменить тему на интересный клинический случай, задай коллегам свежий профессиональный вопрос или вспомни уместный факт из прошлых обсуждений чата, переведя разговор в конструктивное русло.
11. ФУНКЦИОНАЛ БОТА: Если у тебя спрашивают "что ты умеешь", "какие команды есть" или просят описать функционал — честно перечисли свои фишки: ответы на клинические вопросы, разбор снимков и рентгена (Vision), викторина /quiz, энциклопедия /wiki, клинические кейсы /case, калькулятор анестезии /calc, протоколы /protocols, поиск по базе /search, закладки /bookmarks (сохранять пост в чате — ответить на него словом «сохранить»), темы чата /stats, настройка стиля общения /style и ночные дайджесты. Прямо ЗДЕСЬ, в общем чате, работают ещё: сводка обсуждения /summary (или /итог), викторина для чата /poll (или /кейс), вопрос мне командой /ask, объяснение термина /what (или /что) — про них спрашивают чаще всего, а узнать о них негде. Перечисляй ТОЛЬКО из этого списка — других команд у тебя нет. Опиши кратко, по-свойски, и не тащи в ответ то, о чём не спрашивали.
12. КРИТИЧЕСКОЕ ПРАВИЛО СОМНЕНИЯ: Если тебя спрашивают про незнакомый термин, аббревиатуру или концепцию, НЕ пытайся угадать её значение или агрессивно называть бредом/инфоцыганством. Вместо этого честно признай, что не встречал такое обозначение, и проактивно спроси у коллег, что под этим подразумевается. Будь живым, открытым к новой информации врачом.
12.1. РАСЧЁТ ДОЗ АНЕСТЕТИКОВ — правило безопасности. Оно важнее стиля и важнее желания дать ответ:
    - Предел ВСЕГДА двойной: мг/кг И абсолютный максимум на приём. Берётся МЕНЬШЕЕ из двух. Считать только по мг/кг — типовая ошибка: при весе 100 кг это даёт 700 мг артикаина против допустимых 500.
    - Референсные максимумы для здорового взрослого: артикаин 4% — 7 мг/кг и не более 500 мг; мепивакаин 3% без вазоконстриктора — 4.4 мг/кг и не более 400 мг; лидокаин 2% с адреналином — 7 мг/кг и не более 500 мг. У детей норма на килограмм ниже, и абсолютный потолок проверяется всё равно.
    - Всегда показывай арифметику и переводи в карпулы, НАЗЫВАЯ объём карпулы, из которого считал: артикаин 4% при 1.7 мл — 68 мг в карпуле; мепивакаин 3% при 1.8 мл — 54 мг; лидокаин 2% при 1.8 мл — 36 мг. Врач должен иметь возможность проверить каждый шаг.
    - Это референсные максимумы, а не рекомендация дозы. Обязательно оговаривай, что при сопутствующей патологии, у детей, беременных и пожилых предел ниже, и что цифры надо сверить с инструкцией к конкретному препарату.
    - Если анестетик незнаком, концентрация не названа или в вопросе нет веса — НЕ считай и не угадывай. Спроси недостающее или честно скажи, что референса по этому препарату у тебя нет.
14. МЕТА-ПРАВИЛО (КРИТИЧЕСКИЙ ЗАПРЕТ): Категорически запрещено обсуждать разработку бота, триггеры, команды, админов или притворяться живым участником чата, жалующимся на бота. Если просят помолчать — вежливо извинись одной фразой и умолкни.
15. ЗАПРЕТ ПАНИБРАТСТВА И ПОДДАКИВАНИЯ (КАТЕГОРИЧЕСКИЙ ЗАПРЕТ): Категорически запрещено писать пустые поддакивания («Согласен с...», «Пациент спасибо не скажет», «Поддерживаю коллегу»). Запрещено притворяться человеком. Давай только четкую доказательную информацию (критерии EBM, протоколы, дозы, риски), либо не встревай в разговор.

{style_instruction}

ВАЖНОЕ ПРАВИЛО ФОКУСА — ОТВЕТ СТРОГО НА ВЫДЕЛЕННОЕ СООБЩЕНИЕ:
Вся переписка выше дана тебе исключительно для понимания полного контекста и предыстории дискуссии!
Твой ответ должен быть направлен СТРОГО на сообщение #{msg_id} от {sender_first_name or "коллеги"}.
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:
- Пытаться ответить на каждое сообщение из истории по очереди.
- Пересказывать или суммировать всю историю переписки.
- Отвечать на уже закрытые реплики из начала истории.
Твой ответ — это естественная реакция в разговоре именно на сообщение #{msg_id}!
"""
    else:
        prompt = f"""
Ты — опытный стоматолог-практик, читаешь переписку коллег в чате "StomChat" и вставляешь точную, полезную реплику.
Тебе 15+ лет практики, ты говоришь коротко и по делу — как тот человек в чате, которого все слушают.

Текущая переписка в чате (последние сообщения со структурой ответов):
{chr(10).join(context_msgs)}

ТЕБЕ НУЖНО СГЕНЕРИРОВАТЬ ОТВЕТ НА СООБЩЕНИЕ #{msg_id} от {sender_first_name or "коллеги"}. Оно находится в конце переписки. Учитывай хронологию и иерархию (кто кому отвечает через ID сообщений и ссылки "в ответ на #ID"), но твой ответ должен отвечать строго на суть этого сообщения! Если в переписке есть твои предыдущие ответы ([ЭТО ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ]), учитывай их, чтобы не повторяться и ни в коем случае не соглашаться с самим собой от третьего лица!

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Похожие обсуждения из Архива чата:
{archive_corpus}

ИНСТРУКЦИИ:
1. {address_line}
2. ДЛИНА ОТВЕТА: {length_guideline}
3. Никаких вводных («Согласно справке», «Исходя из переписки»), приветствий и концовок. Сразу суть.
4. Тон: сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности и без эмодзи-кривляния. Используй профессиональный стоматологический язык. Полностью избегай канцелярщины и фраз типа "Как ИИ...", "Рад помочь", "С уважением".
5. Ограничение по теме: Используй термины и Базу Знаний строго по контексту разговора. Если врачи обсуждают объёмы работы, графики, усталость, деньги или другие организационные темы, а не конкретный лечебный случай — КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО читать клинические лекции и давать медицинские советы по лечению (например, приплетать BOPT, протоколы фиксации циркона и т.п.) из Базы Знаний, если об этом прямо не спросили. В таких случаях общайся только по теме диалога (объёмы, выгорание и т.д.).
6. Только доказанные факты (EBM). Домыслы и псевдонаучный жаргон запрещены. Объясняй биологический механизм. Не путай последовательность этапов лечения. Никаких пустых поверхностных вбросов.
7. Не повторяй то что уже сказали. Принеси что-то новое — нюанс, уточнение, факт из базы.
8. СМАЙЛИКИ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать глупые, несерьёзные, панибратские или нервные эмодзи (😅, 😂, 😎, 😤, 😏, 🤣, 🤡, 🙄). В профессиональном клиническом ответе смайлики НЕ НУЖНЫ.
9. Разметка: только HTML — <b>жирный</b>. Никакого Markdown (**текст**). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать в ответе техническую информацию вроде "#168569", "в ответ на #168569", "Сообщение #..." и любые другие ID из контекста. Твой текст должен выглядеть как обычное человеческое сообщение в чате, без системного мусора.
10. ПРОАКТИВНОСТЬ И УМЕСТНОСТЬ:
    - Отвечай строго к месту (по сути текущего вопроса в конце истории диалога). Чётко отделяй текущую живую тему от сухих фактов в Справке/Архиве — не начинай цитировать архив как часть текущего разговора. Не неси околесицу и не зацикливайся на старых сообщениях.
    - Если в чате разгорается конфликт, бессмысленный спор или переписка явно зациклилась на какой-то ерунде, проактивно разряди обстановку. Предложи сменить тему на интересный клинический случай, задай коллегам свежий профессиональный вопрос или вспомни уместный факт из прошлых обсуждений чата, переведя разговор в конструктивное русло.
11. ФУНКЦИОНАЛ БОТА: Если у тебя спрашивают "что ты умеешь", "какие команды есть" или просят описать функционал — честно перечисли свои фишки: ответы на клинические вопросы, разбор снимков и рентгена (Vision), викторина /quiz, энциклопедия /wiki, клинические кейсы /case, калькулятор анестезии /calc, протоколы /protocols, поиск по базе /search, закладки /bookmarks (сохранять пост в чате — ответить на него словом «сохранить»), темы чата /stats, настройка стиля общения /style и ночные дайджесты. Прямо ЗДЕСЬ, в общем чате, работают ещё: сводка обсуждения /summary (или /итог), викторина для чата /poll (или /кейс), вопрос мне командой /ask, объяснение термина /what (или /что) — про них спрашивают чаще всего, а узнать о них негде. Перечисляй ТОЛЬКО из этого списка — других команд у тебя нет. Опиши кратко, по-свойски, и не тащи в ответ то, о чём не спрашивали.
12. КРИТИЧЕСКОЕ ПРАВИЛО СОМНЕНИЯ: Если тебя спрашивают про незнакомый термин, аббревиатуру или концепцию, НЕ пытайся угадать её значение или агрессивно называть бредом/инфоцыганством. Вместо этого честно признай, что не встречал такое обозначение, и проактивно спроси у коллег, что под этим подразумевается. Будь живым, открытым к новой информации врачом.
12.1. РАСЧЁТ ДОЗ АНЕСТЕТИКОВ — правило безопасности. Оно важнее стиля и важнее желания дать ответ:
    - Предел ВСЕГДА двойной: мг/кг И абсолютный максимум на приём. Берётся МЕНЬШЕЕ из двух. Считать только по мг/кг — типовая ошибка: при весе 100 кг это даёт 700 мг артикаина против допустимых 500.
    - Референсные максимумы для здорового взрослого: артикаин 4% — 7 мг/кг и не более 500 мг; мепивакаин 3% без вазоконстриктора — 4.4 мг/кг и не более 400 мг; лидокаин 2% с адреналином — 7 мг/кг и не более 500 мг. У детей норма на килограмм ниже, и абсолютный потолок проверяется всё равно.
    - Всегда показывай арифметику и переводи в карпулы, НАЗЫВАЯ объём карпулы, из которого считал: артикаин 4% при 1.7 мл — 68 мг в карпуле; мепивакаин 3% при 1.8 мл — 54 мг; лидокаин 2% при 1.8 мл — 36 мг. Врач должен иметь возможность проверить каждый шаг.
    - Это референсные максимумы, а не рекомендация дозы. Обязательно оговаривай, что при сопутствующей патологии, у детей, беременных и пожилых предел ниже, и что цифры надо сверить с инструкцией к конкретному препарату.
    - Если анестетик незнаком, концентрация не названа или в вопросе нет веса — НЕ считай и не угадывай. Спроси недостающее или честно скажи, что референса по этому препарату у тебя нет.
13. ТОНАЛЬНОСТЬ: Не утверждай вещи безапелляционно, оставляй пространство для клинического мнения коллег («я бы сделал так, но надо смотреть по ситуации...»).
15. ЗАПРЕТ ПОДМЕНЫ ТЕМЫ (КРИТИЧЕСКОЕ ПРАВИЛО): Ты ОБЯЗАН отвечать строго на ту тему, которую поднял пользователь. Категорически запрещено "уводить" разговор в клиническую сторону, если вопрос был про налоги, оборудование, бизнес, юридические моменты или что-то организационное. Пример грубой ошибки: человек спросил "это грабёж?" про удержание НДФЛ → бот отвечает про фотопротоколы ортопедии. Это полный провал. Если твой RAG вытащил клинические данные, а вопрос — про деньги/оборудование/быт, просто НЕ ИСПОЛЬЗУЙ клинический контекст. Отвечай только на то, о чём спросили.
16. ЗАПРЕТ ПАНИБРАТСТВА И ПОДДАКИВАНИЯ (КАТЕГОРИЧЕСКИЙ ЗАПРЕТ): Категорически запрещено писать пустые поддакивания («Согласен с...», «Пациент спасибо не скажет», «Поддерживаю коллегу»). Запрещено притворяться человеком. Давай только четкую доказательную информацию (критерии EBM, протоколы, дозы, риски), либо возвращай IGNORE.

{ignore_instruction}

{style_instruction}

ВАЖНОЕ ПРАВИЛО ФОКУСА — ОТВЕТ СТРОГО НА ВЫДЕЛЕННОЕ СООБЩЕНИЕ:
Вся переписка выше дана тебе исключительно для понимания полного контекста и предыстории дискуссии!
Твой ответ должен быть направлен СТРОГО на сообщение #{msg_id} от {sender_first_name or "коллеги"}.
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:
- Пытаться ответить на каждое сообщение из истории по очереди.
- Пересказывать или суммировать всю историю переписки.
- Отвечать на уже закрытые реплики из начала истории.
Твой ответ — это естественная реакция в разговоре именно на сообщение #{msg_id}!
"""

    logger.info(f"Triggered assistant! Reason: {trigger_reason}. Keywords: {search_keywords}")
    
    # CALL GEMINI
    status_ctx = {"kind": "assistant", "chat_id": event.chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
    
    if error:
        logger.error(f"Assistant Gemini generation error: {error}")
        return False
        
    reply_text = getattr(response, "text", None)
    if not reply_text:
        logger.warning("Assistant Gemini returned empty text.")
        return False
        
    reply_text = reply_text.strip()
    reply_text = clean_html_formatting(reply_text)

    if not is_dialogue:
        reply_clean = re.sub(r'[^A-Z]', '', reply_text.strip().upper())
        if reply_clean == "IGNORE":
            logger.info("Assistant: Query was classified as off-topic or chitchat. Ignoring.")
            return False
    
    # POST-GENERATION QUALITY CHECK: validate draft before sending.
    # Раньше диалоговые ответы проверку не проходили вообще — а это ровно те
    # ответы, где врач переспросил бота напрямую и с наибольшей вероятностью
    # на них опирается. Теперь проверяются оба пути, разница только в том,
    # что делать при недоступном валидаторе (см. check_response_quality).
    quality_ok, quality_reason = await check_response_quality(
        context_msgs, reply_text, invited=is_dialogue, reference=wiki_corpus
    )
    if not quality_ok:
        logger.warning(f"Response quality validator REJECTED draft: {quality_reason}. Suppressing reply.")
        return False
    logger.info(f"Response quality validator approved draft: {quality_reason}")

    # SENDING
    if SHADOW_TESTING and event.chat_id != TEST_CHAT_ID:
        # Shadow testing: deliver to test chat & topic
        shadow_message = f"[SHADOW TEST]\n\n{reply_text}"
        write_to_shadow_log(f"Reason: {trigger_reason}\nKeywords: {search_keywords}\nContext:\n{chr(10).join(context_msgs[-4:])}\nResponse:\n{reply_text}\n---")
        try:
            await bot_client.send_message(
                entity=TEST_CHAT_ID,
                message=shadow_message,
                reply_to=TEST_TOPIC_ID,
                parse_mode='html'
            )
            logger.info("Sent shadow assistant message to Telegram test topic.")
            REPLIED_MSG_IDS[msg_id] = True
            if not is_dialogue:
                record_passive_success(pending_thread_id)
            return True
        except Exception as e:
            logger.error(f"Failed to send shadow assistant message to Telegram: {e}")
            return False
    else:
        # Live mode OR direct reply in test chat: reply directly to user message!
        reply_message = reply_text

        # Добавляем ненавязчивую контекстную подсказку про ЛС с вероятностью 15%
        if random.random() < 0.15:
            reply_message += get_ad_hint(reply_message)

        try:
            await bot_client.send_message(
                entity=event.chat_id,
                message=reply_message,
                reply_to=msg_id,
                parse_mode='html'
            )
            logger.info(f"Sent direct assistant reply to chat {event.chat_id}, message {msg_id}.")
            # Сообщение помечаем отвеченным ДО списания окна: гард на входе
            # функции читает именно этот кэш, и повторный прогон того же
            # msg_id (sync_history после рестарта, снимок с подписью в двух
            # обработчиках) дальше входа не пройдёт.
            REPLIED_MSG_IDS[msg_id] = True
            # Полное окно тишины списывается только здесь — после того, как
            # сообщение реально ушло. Тред помечается обработанным тоже здесь.
            if not is_dialogue:
                record_passive_success(pending_thread_id, author_id=event.sender_id, msg_id=msg_id)
            return True
        except Exception as e:
            logger.error(f"Failed to send direct assistant reply: {e}")
            return False
async def check_and_trigger_assistant_media(bot_client, message, msg_id, text, media_description):
    if msg_id in REPLIED_MSG_IDS:
        return False
    state = load_state()
    
    # Check if the bot is temporarily silenced
    if is_silenced(state, "media trigger check"):
        return False
    
    # Calculate context length guidelines
    try:
        recent_texts = []
        db_history = await database.get_last_n_messages(limit=6)
        if db_history:
            for m in db_history:
                if isinstance(m, (list, tuple)) and len(m) > 3:
                    recent_texts.append(m[3] or "")
                elif isinstance(m, dict):
                    recent_texts.append(m.get("text", "") or "")
                else:
                    recent_texts.append(getattr(m, "text", "") or "")
        length_guideline = calculate_context_length_guidelines(recent_texts)
    except Exception as calc_err:
        logger.error(f"Error calculating length guideline for media: {calc_err}")
        length_guideline = "Отвечай кратко, до 3-4 предложений."

    
    # Личность бота берётся из BOT_ID/BOT_USERNAME, а не из get_me() на каждый
    # снимок. Раньше здесь стояли два сетевых get_me() под пустым except: при
    # обрыве связи (51723 события по журналам) оба флага оставались False,
    # прямое обращение врача считалось пассивным и попадало под 2-часовой
    # кулдаун — снимок отбрасывался молча, и врач не узнавал, что бот просто не
    # разобрал, к кому обращались. Догоняющий резолв — как в
    # check_and_trigger_assistant.
    if (getattr(message, 'reply_to_msg_id', None) or text) and not BOT_ID:
        if await resolve_bot_identity(bot_client):
            logger.info(f"Dynamically resolved BOT_ID: {BOT_ID} (@{BOT_USERNAME})")

    is_direct_reply = False
    if getattr(message, 'reply_to_msg_id', None) and BOT_ID:
        try:
            parent = await bot_client.get_messages(message.chat_id, ids=message.reply_to_msg_id)
            if parent and parent.sender_id == BOT_ID:
                is_direct_reply = True
        except Exception as parent_err:
            # Молчать нельзя: не опознав ответ боту, мы уводим снимок врача под
            # пассивный 2-часовой кулдаун вместо разбора.
            logger.warning(
                "Не удалось получить сообщение-родитель msg_id=%s для снимка: %s",
                message.reply_to_msg_id, parent_err,
            )

    is_mentioned = False
    if text and BOT_USERNAME:
        # Имя ищем с «@» и с границей слова, как strip_bot_mention в main.py.
        # Без «@» за обращение сходила любая подстрока, а без границы слова
        # «@stomchat_bot_old» — это ДРУГОЙ аккаунт, и принимать его за обращение
        # к нам значит разбирать снимок, которого у нас никто не просил.
        if re.search(rf"(?i)@{re.escape(BOT_USERNAME)}\b", text):
            is_mentioned = True

    # Enforce 2-hour cooldown for passive media trigger, unless it's a direct reply or mention
    is_passive = not (is_direct_reply or is_mentioned)
    if is_passive:
        last_run = datetime.fromisoformat(state.get("last_passive_media_run", "2000-01-01T00:00:00"))
        if datetime.now() - last_run < timedelta(minutes=120):
            return  # Within 2-hour cooldown, skip!

    # Construct a simple event-like object for direct compatibility
    class MediaEvent:
        def __init__(self, msg):
            self.message = msg
            self.client = msg.client
            self.chat_id = msg.chat_id
            
    event = MediaEvent(message)
    
    # 1. Parse keywords
    caption_text = text or ""
    sanitized_media_desc = strip_vision_negations(media_description)
    full_context_str = caption_text + " " + sanitized_media_desc
    keywords = extract_keywords(full_context_str)
    
    # Клиническая тема — по словам, а не подстрокой: «кт» сидит внутри «кто»,
    # «эффективно» и «комплекта», «бор» — внутри «выбора». См. has_dental_term.
    has_dental_topic = has_dental_term(full_context_str)
    
    triggered = False
    trigger_reason = ""
    wiki_corpus = ""
    archive_corpus = ""
    is_dental = False
    
    search_keywords = select_search_keywords(keywords)
        
    if is_passive:
        last_passive_media_str = state.get("last_passive_media_run")
        if last_passive_media_str:
            try:
                last_passive_media_dt = datetime.fromisoformat(last_passive_media_str)
                if datetime.now() - last_passive_media_dt < timedelta(minutes=120):
                    logger.info("Media Assistant: passive media cooldown active (120 min). Skipping unrequested media analysis.")
                    return
            except Exception:
                pass

    if has_dental_topic:
        # Dental Case: Always query RAG!
        triggered = True
        trigger_reason = f"Dental media trigger (has_dental_topic={has_dental_topic})"
        is_dental = True
        wiki_corpus, archive_corpus = await search_knowledge_corpus(search_keywords)
    else:
        # Non-dental Meme/Coffee: Trigger chitchat only if NOT passive (direct reply/mention)
        if not is_passive:
            triggered = True
            trigger_reason = "Non-dental direct reply/mention on media"
            is_dental = False
            
    if not triggered:
        return
        
    if is_passive:
        state["last_passive_media_run"] = datetime.now().isoformat()
        save_state(state)

    # Fetch recent messages and reply chain for context via fetch_dynamic_chat_context
    reply_to_msg_id = getattr(message, 'reply_to_msg_id', None)
    context_msgs, _, _ = await fetch_dynamic_chat_context(
        msg_id, reply_to_msg_id, base_limit=12, max_limit=40, event=event
    )
    context_str = "\n".join(context_msgs) if context_msgs else "Нет предыдущего контекста."

    is_dialogue = is_direct_reply or is_mentioned
    ignore_instruction = "ЕСЛИ тема чата — чистый флуд, приветствия, погода, политика, оффтоп без связи со стоматологией или медициной — верни ровно одно слово: IGNORE"
    if is_dialogue:
        ignore_instruction = "ЕСЛИ пользователь просто благодарит тебя, соглашается или тема исчерпана — НЕ МОЛЧИ (не пиши IGNORE), а вежливо и грамотно заверши диалог (например, 'Всегда пожалуйста!', 'Обращайтесь!'). Отвечать IGNORE при прямом обращении запрещено."

    # BUILD PROMPT
    if is_dental:
        prompt = f"""
Ты — опытный стоматолог-практик, читаешь чат коллег "StomChat". Тебе прислали изображение по стоматологической теме.
Дай короткий, точный клинический комментарий — как ответил бы врач с 15 годами практики: уверенно, без воды, по делу.

Описание изображения (распознано моделью зрения — это НЕ факт, а прочтение снимка машиной):
{media_description}
[ДОСТОВЕРНОСТЬ ОПИСАНИЯ: модель зрения способна «увидеть» на снимке то, чего там нет. Не повторяй её формулировки как установленный факт и не строй на одной такой детали категоричный вывод. Если ключевая для ответа находка держится только на описании — так и скажи, что судишь по снимку в чате, и назови, что стоило бы проверить (прицельный, КТ, зондирование, анамнез).]

История диалога (цепочка ответов):
{context_str}

Подпись пользователя к изображению (если есть):
{caption_text}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Похожие обсуждения из Архива чата:
{archive_corpus}

ИНСТРУКЦИИ:
1. ДЛИНА ОТВЕТА: {length_guideline}
2. Тон: сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности и без эмодзи-кривляния.
3. Разметка: только HTML — <b>жирный</b>. Никакого Markdown.
4. Только доказанные факты. Если данных нет — скажи честно.
5. Добавляй ценность, не пересказывай подпись.
6. ПРОАКТИВНОСТЬ: Если по снимку или вопросу неясно, либо у тебя есть сомнения — честно скажи об этом и сам задай уточняющие вопросы (например, спроси про симптоматику, КТ или анамнез).
7. ЕСЛИ НА ИЗОБРАЖЕНИИ НЕ КЛИНИЧЕСКИЙ СЛУЧАЙ (а мем, котик, иконка архива, скриншот загрузки или интерфейс программы): не пытайся анализировать это как снимок зубов. Вместо этого достойно опиши, что именно изображено на картинке (например, 'вижу иконку ZIP-архива...', 'тут котик...'), и спокойно прокомментируй это в привязке к стоматологическим будням или работе.
8. КРИТИЧЕСКОЕ ПРАВИЛО СОМНЕНИЯ: Если тебя спрашивают про незнакомый термин, аббревиатуру или концепцию, НЕ пытайся угадать её значение или агрессивно называть бредом/инфоцыганством. Вместо этого честно признай, что не встречал такое обозначение, и проактивно спроси у коллег, что под этим подразумевается.
9. МЕТА-ПРАВИЛО: Категорически запрещено обсуждать разработку бота, триггеры, команды, админов или притворяться живым участником чата, жалующимся на бота. Если просят помолчать — вежливо извинись одной фразой и умолкни.

{ignore_instruction}

ВАЖНОЕ ПРАВИЛО ФОКУСА — ОТВЕТ СТРОГО НА ПРИСЛАННОЕ ИЗОБРАЖЕНИЕ:
Вся переписка выше дана исключительно для понимания клинического контекста!
Твой ответ должен быть направлен СТРОГО на разбор присланного снимка/изображения и вопроса к сообщению #{msg_id}.
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО: отвечать по очереди на старые реплики истории или пересказывать переписку.
"""
    else:
        prompt = f"""
Ты — участник стоматологического чата "StomChat" с чёрным юмором. Коллега кинул мем или бытовую картинку.
Одна острая реплика в духе «врач в конце рабочего дня».

Описание изображения:
{media_description}

История диалога (цепочка ответов):
{context_str}

Подпись (если есть):
{caption_text}

ИНСТРУКЦИИ:
1. Коротко — 1-2 предложения max. Это реплика, не монолог.
2. Сразу с места в карьер — никакого «Смотрю на это и думаю».
3. Достойно опиши, что именно изображено на картинке (например, 'вижу котика...', 'тут какая-то еда...'), и привяжи это к стоматологическим будням, работе или ироничным мыслям врача.
4. Юмор: цинизм, ирония, усталость стоматолога, кассовый аппарат, пациент-должник, сломанный файл, бормашина.
5. Разметка: <b>жирный</b> только если реально нужно.
6. Тон: гибкий. Если картинка/мем добрые — отвечай с хорошим настроением; если циничные — поддержи иронию; если тебя пытаются задеть или подколоть — ответь едким, остроумным троллингом без грубости.

ВАЖНОЕ ПРАВИЛО ФОКУСА — ОТВЕТ СТРОГО НА ПРИСЛАННОЕ ИЗОБРАЖЕНИЕ:
Твой ответ должен быть направлен СТРОГО на присланную картинку/мем к сообщению #{msg_id}!
"""

    logger.info(f"Triggered media assistant! Reason: {trigger_reason}. Keywords: {search_keywords}")
    
    # CALL GEMINI
    status_ctx = {"kind": "assistant_media", "chat_id": event.chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
    
    if error:
        logger.error(f"Media Assistant Gemini generation error: {error}")
        return
        
    reply_text = getattr(response, "text", None)
    if not reply_text:
        logger.warning("Media Assistant Gemini returned empty text.")
        return
        
    reply_text = reply_text.strip()
    reply_text = clean_html_formatting(reply_text)
    
    # Check IGNORE filter only for dental checks (non-dental balancer is already validated)
    if is_dental:
        reply_clean = re.sub(r'[^A-Z]', '', reply_text.strip().upper())
        if reply_clean == "IGNORE":
            logger.info("Media Assistant: Query was classified as off-topic. Ignoring.")
            return
    
    # POST-GENERATION QUALITY CHECK: validate draft before sending.
    # Проверяем и запрошенные разборы тоже: чтение снимка — самый
    # галлюциногенный выход бота, и раньше при прямом обращении оно уходило
    # пациенту/врачу вообще без проверки.
    quality_ok, quality_reason = await check_response_quality(
        context_msgs, reply_text, invited=not is_passive, reference=wiki_corpus
    )
    if not quality_ok:
        logger.warning(f"Media response quality validator REJECTED draft: {quality_reason}. Suppressing reply.")
        return
    logger.info(f"Media response quality validator approved draft: {quality_reason}")

    # SENDING
    if SHADOW_TESTING and event.chat_id != TEST_CHAT_ID:
        shadow_message = f"[SHADOW TEST]\n\n{reply_text}"
        write_to_shadow_log(f"Reason: {trigger_reason}\nKeywords: {search_keywords}\nImage description: {media_description}\nResponse:\n{reply_text}\n---")
        try:
            await bot_client.send_message(
                entity=TEST_CHAT_ID,
                message=shadow_message,
                reply_to=TEST_TOPIC_ID,
                parse_mode='html'
            )
            logger.info("Sent shadow media assistant message to Telegram test topic.")
        except Exception as e:
            logger.error(f"Failed to send shadow media assistant message: {e}")
    else:
        reply_message = reply_text
        try:
            await bot_client.send_message(
                entity=event.chat_id,
                message=reply_message,
                reply_to=msg_id,
                parse_mode='html'
            )
            logger.info(f"Sent direct media assistant reply to chat {event.chat_id}, message {msg_id}.")
            # Гард на входе функции (`if msg_id in REPLIED_MSG_IDS`) до сих пор
            # был мёртвым: в кэш никто никогда не писал. Из-за этого одно и то
            # же медиасообщение могло получить второй разбор — например, когда
            # снимок с упоминанием бота в подписи одновременно уходит и в
            # текстовый обработчик, и в очередь анализа медиа.
            REPLIED_MSG_IDS[msg_id] = True
            if is_passive:
                state = load_state()
                state["last_case_author_id"] = getattr(event.message, "sender_id", None)
                state["last_case_bot_msg_id"] = msg_id
                state["last_case_time"] = datetime.now().isoformat()
                save_state(state)
        except Exception as e:
            logger.error(f"Failed to send direct media assistant reply: {e}")


# Длина выдержки из протокола в сообщении с кнопками. Обрезка идёт через
# html_safe, чтобы не разорвать тег и не получить отказ Telegram.
PROTOCOL_EXCERPT_MAX_CHARS = 1500

# Глубина памяти диалога в ЛС. Держим одним числом: /help обещал 30
# сообщений, а код брал 35 — расхождение мелкое, но это ровно тот случай,
# когда обещанное и работающее разъезжаются без единого сигнала.
PM_HISTORY_LIMIT = 50

BOOKMARK_SNIPPET_CHARS = 80

# Предел длины термина для /что. Он подставляется прямо в промпт.
TERM_EXPLAINER_MAX_CHARS = 120

# Скачивание медиа в ЛС. Собственного таймаута у download_media нет, а
# обработчик держит замок на пользователя: без предела все следующие
# сообщения врача встают в очередь за подвисшей загрузкой.
PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 120

# Область команды /итог. Без ответа на сообщение берём последние реплики; в
# ответ на конкретное — ветку от него, потолок нужен, чтобы указание на
# полугодовалое сообщение не утащило в промпт полчата.
SUMMARY_RECENT_LIMIT = 30
SUMMARY_THREAD_LIMIT = 60


def _bookmark_snippet(value, limit=BOOKMARK_SNIPPET_CHARS):
    """
    Безопасная выдержка из закладки для HTML-сообщения.

    Порядок операций принципиален: сначала режем СЫРОЙ текст, затем
    экранируем. В обратном порядке срез попадал бы внутрь «&amp;» и ломал
    сущность — то же самое, от чего страдала обрезка дайджеста.

    Многоточие ставится только когда текст действительно обрезан: прежний код
    дописывал его всегда, обещая продолжение там, где его нет.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    clipped = raw[:limit]
    suffix = "…" if len(raw) > limit else ""
    return html.escape(clipped, quote=False) + suffix


# --- Клинические fallback-вопросы для викторин ------------------------------
CLINICAL_QUIZ_FALLBACKS = [
    {
        "topic": "Эндодонтия",
        "question": "Пациент жалуется на боли при накусывании в зубе 3.6 (лечен эндодонтически 2 года назад). На снимке: недопломбировка язычного канала на 2 мм, очаг разрежения костной ткани в области апекса 3 мм. Какова первоочередная тактика?",
        "options": [
            "Апикальная микрохирургия (резекция верхушки корня)",
            "Ортопедическое перелечивание без распломбировки каналов",
            "Повторное эндодонтическое лечение (ортоградная ревизия)",
            "Удаление зуба с одномоментной дентальной имплантацией"
        ],
        "correct": 2,
        "explanation": "Ортоградная ревизия системы корневых каналов — метод первого выбора при наличии проходимых каналов и апикального периодонтита."
    },
    {
        "topic": "Ортопедия",
        "question": "Пациенту препарируются зубы 1.1, 2.1 под цельнокерамические коронки из дисиликата лития (e.max CAD). Десна плотная, биотип толстый, планируется subgingival граница препарирования на 0.5 мм. Какой тип уступа и протокол фиксации рекомендован?",
        "options": [
            "Круговой уступ типа ножевидный край (knife-edge) и фиксация на цинк-фосфатный цемент",
            "Сглаженный полукруглый уступ (chamfer) 0.8-1.0 мм и адгезивная фиксация на композитный цемент",
            "Прямой уступ 90 градусов со скосом 45 градусов и фиксация на стеклоиономерный цемент",
            "Препарирование без уступа и фиксация на временный безэвгенольный цемент"
        ],
        "correct": 1,
        "explanation": "Для дисиликата лития рекомендован сглаженный уступ (chamfer/shoulder 0.8-1.0 мм) и адгезивная фиксация на композитный цемент светового или двойного отверждения."
    },
    {
        "topic": "Терапия",
        "question": "При препарировании глубокого кариеса в зубе 4.6 произошла случайная точечная перфорация свода пульпарной камеры (<0.5 мм). Кровотечение остановлено за 1 минуту 2% раствором NaOCl. Симптомов пульпита в анамнезе не было. Какова тактика?",
        "options": [
            "Прямое покрытие пульпы биокерамикой (MTA / Biodentine) и постоянная реставрация",
            "Витальная ампутация (пульпотомия) с наложением формокрезола",
            "Полная экстирпация пульпы и пломбирование каналов гуттаперчей",
            "Наложение гидроксида кальция на 6 месяцев под временную пломбу"
        ],
        "correct": 0,
        "explanation": "При случайном точечном вскрытии бессимптомной пульпы в условиях коффердама и быстром гемостазе методом выбора является прямое покрытие биокерамикой (МТА/Biodentine)."
    },
    {
        "topic": "Хирургия",
        "question": "Планируется удаление зуба 1.6 по поводу продольного перелома корня. Высота резидуального гребня до дна верхнечелюстного синуса составляет 4 мм, ширина 8 мм. Какая тактика наиболее обоснована для последующей имплантации?",
        "options": [
            "Одномоментная имплантация без синус-лифтинга и аугментации",
            "Удаление зуба, закрытый транскрестальный синус-лифтинг и немедленная установка имплантата",
            "Удаление с консервацией лунки и отсроченный открытый (латеральный) синус-лифтинг",
            "Установка ультракороткого имплантата (4 мм) без синус-лифтинга"
        ],
        "correct": 2,
        "explanation": "При резидуальной высоте кости 4 мм первичная стабильность недостаточна для безопасного закрытого синус-лифтинга; показана консервация лунки и отсроченный латеральный синус-лифтинг."
    },
    {
        "topic": "Пародонтология",
        "question": "У пациента 42 лет диагностирован генерализованный пародонтит III стадии. В области зуба 4.6 зондируется глубокий внутрикостный 2-стеночный карман глубиной 7 мм. Какой метод регенеративной терапии является наиболее доказательным?",
        "options": [
            "Только закрытый кюретаж с медикаментозной обработкой хлоргексидином",
            "Лоскутная операция с направленной тканевой регенерацией (GTR/костный трансплантат + мембрана)",
            "Гингивэктомия для полного устранения пародонтального кармана",
            "Шинирование зуба лентой без хирургического вмешательства"
        ],
        "correct": 1,
        "explanation": "При глубоких внутрикостных карманах доказанным методом восстановления утраченного прикрепления является направленная тканевая регенерация (GTR) с костнозамещающим материалом и мембраной."
    },
    {
        "topic": "Травматология",
        "question": "Пациент 20 лет обратился через 45 минут после полного вывиха (авульсии) зуба 1.1. Зуб транспортировался в молоке, коронка интактна, верхушка корня закрыта. Какова правильная последовательность экстренной помощи?",
        "options": [
            "Эндодонтическое лечение зуба вне полости рта, затем реплантация и жесткая фиксация",
            "Обработка корня 70% спиртом, резекция верхушки корня и немедленная реплантация",
            "Бережное промывание корня физраствором, немедленная реплантация, гибкая шина на 2 недели, эндодонтия через 7-10 дней",
            "Помещение зуба в антисептический раствор на 24 часа с отсроченной реплантацией"
        ],
        "correct": 2,
        "explanation": "По протоколу IADT зуб бережно промывают физраствором без касания корня, реплантируют, фиксируют гибкой шиной до 2 недель, а эндодонтическое лечение закрытого апекса начинают через 7-10 дней."
    }
]


# --- Статистика тем чата (/stats) ------------------------------------------
#
# Раньше /stats был статичным текстом с числами вида «~5 400 упоминаний» и
# подписью «на основе анализа 117 000+ сообщений». Числа не менялись никогда, а
# к сегодняшнему дню разошлись с архивом: имплантация занижена в 2.3 раза, и
# порядок неверен — имплантация обогнала эндодонтию, а адгезивы обе. Колонки
# category_l1/l2/l3 в архиве, на которые это могло опираться, пусты: 0 из 117 847.
#
# Считаем сами, по обеим базам, с границей слова. Подстрочный поиск здесь
# особенно коварен: «кт» встречается в «доктор», «практика», «который», и по
# подстроке тема «Диагностика и снимки» выходила на первое место с 12 093
# упоминаниями вместо реальных 1 322 — завышение в девять раз.
STATS_TOPICS = {
    "👑 Ортопедия и коронки": ("корон", "циркон", "pmma", "e.max", "емакс", "винир", "вкладк"),
    "📐 Препарирование и уступ": ("уступ", "вертипреп", "вертикальн", "препариров", "bopt"),
    "🩸 Десна и мягкие ткани": ("десн", "ретракц", "пародонт", "рецесс"),
    "🧪 Адгезия и композиты": ("бонд", "адгезив", "композит", "пескоструй", "травлен", "силан"),
    "🔩 Имплантация": ("имплант", "абатм", "остеоинтегр", "синус-лифт", "аугмент"),
    "🦷 Эндодонтия": ("канал", "гипохлорит", "эндодонт", "обтурац", "гуттаперч"),
    "📸 Диагностика и снимки": ("кт", "клкт", "оптг", "рентген", "прицельн", "снимок", "снимк"),
    "💉 Анестезия": ("анестез", "артикаин", "убистезин", "мепивакаин", "лидокаин", "карпул"),
}
STATS_CACHE_TTL_SECONDS = 6 * 3600
_stats_cache = {"computed_at": 0.0, "payload": None}


_WORD_BOUNDARY = r"\b"


def _build_topic_pattern(keywords):
    """Термин ищем как НАЧАЛО слова; двухбуквенные аббревиатуры — целиком."""
    parts = []
    for keyword in keywords:
        escaped = re.escape(keyword)
        if len(keyword) <= 2:
            parts.append(_WORD_BOUNDARY + escaped + _WORD_BOUNDARY)
        else:
            parts.append(_WORD_BOUNDARY + escaped)
    return re.compile("|".join(parts), re.IGNORECASE)


_STATS_PATTERNS = {label: _build_topic_pattern(kws) for label, kws in STATS_TOPICS.items()}


def _scan_topic_statistics():
    """Один проход по архиву и живой базе. Порядка 5 с на 137 тысяч сообщений."""
    counts = {label: 0 for label in STATS_TOPICS}
    scanned = 0
    for path, table in (("stomat_archive.db", "archive_messages"), (config.DB_PATH, "messages")):
        if not os.path.exists(path):
            continue
        try:
            with contextlib.closing(sqlite3.connect(path, timeout=30)) as conn:
                conn.execute("PRAGMA busy_timeout = 30000")
                for (text,) in conn.execute(f"SELECT text FROM {table}"):
                    if not text:
                        continue
                    scanned += 1
                    for label, pattern in _STATS_PATTERNS.items():
                        if pattern.search(text):
                            counts[label] += 1
        except Exception as e:
            logger.error(f"Topic statistics scan failed for {path}: {e}")
    return counts, scanned


async def get_topic_statistics(force=False):
    """Кэшированная статистика тем. Возвращает (counts, scanned)."""
    now = time.time()
    cached = _stats_cache["payload"]
    if not force and cached and now - _stats_cache["computed_at"] < STATS_CACHE_TTL_SECONDS:
        return cached

    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    payload = await loop.run_in_executor(None, _scan_topic_statistics)
    logger.info("topic statistics computed in %.2fs over %s messages",
                time.perf_counter() - started, payload[1])
    if payload[1]:
        _stats_cache["payload"] = payload
        _stats_cache["computed_at"] = now
    return payload


def render_topic_statistics(counts, scanned):
    """Готовый текст /stats. Пустая статистика -> None, чтобы не врать нулями."""
    ranked = [(label, n) for label, n in sorted(counts.items(), key=lambda x: -x[1]) if n]
    if not scanned or not ranked:
        return None
    lines = [
        "📊 <b>Популярные клинические темы в чате StomChat</b>",
        f"<i>Посчитано по {scanned:,} сообщениям чата и архива</i>".replace(",", " "),
        "",
    ]
    for position, (label, count) in enumerate(ranked, start=1):
        share = count * 100.0 / scanned
        lines.append(f"{position}. <b>{label}</b> — {count:,} упоминаний ({share:.1f}%)".replace(",", " "))
    lines.append("")
    lines.append("<i>Считается по вхождению профильных терминов; одно сообщение может попасть в несколько тем.</i>")
    return "\n".join(lines)


CASE_TOTAL_STEPS = 4  # столько же, сколько обещает заголовок "Шаг N из 4"


async def handle_interactive_case_step(bot_client, chat_id, user_text, user_state):
    # Parse history
    try:
        history_raw = json.loads(user_state.get("history") or "[]")
        if isinstance(history_raw, dict):
            history_data = history_raw.get("messages", [])
        else:
            history_data = history_raw
    except Exception:
        history_data = []
        
    # Пустой ход. Симулятор работает только с текстом, а маршрутизация в него
    # происходит до обработки медиа: присланный во время кейса снимок давал
    # user_text = "" и уходил экзаменатору как пустое действие врача — тот
    # оценивал пустоту и невозмутимо вёл кейс дальше.
    if not (user_text or "").strip():
        await bot_client.send_message(
            entity=chat_id,
            message="🎮 <i>В режиме клинического кейса я читаю только текст — "
                    "опишите ваши действия словами.</i>\n"
                    "<i>Выйти из симулятора: /abort</i>",
            parse_mode='html'
        )
        return

    current_step = user_state.get("current_step", 1)

    # Add user message to history
    history_data.append({"role": "user", "content": user_text})

    # Заголовок обещает "Шаг N из 4", а завершение стояло на current_step >= 3:
    # врач видел "Шаг 2 из 4", "Шаг 3 из 4" — и следующий же его ответ обрывал
    # кейс финальной оценкой. Четвёртого шага не существовало.
    is_last_step = (current_step >= CASE_TOTAL_STEPS)

    history_str = ""
    for msg in history_data:
        role_name = "Экзаменатор (Бот)" if msg["role"] == "assistant" else "Врач (Вы)"
        history_str += f"{role_name}: {msg['content']}\n\n"

    # RAG-поддержка для экзаменатора (подтягиваем клинические факты для корректной оценки действий)
    keywords = extract_keywords(user_text + " " + history_str)
    wiki_corpus, _ = await search_knowledge_corpus(select_search_keywords(keywords))

    # Статус отправляем ТОЛЬКО после поиска по базе. Раньше он уходил первым,
    # а его удаление стояло за LLM-вызовом и не было защищено: любое исключение
    # в extract_keywords или в sqlite-поиске (например, заблокированная база)
    # оставляло врачу вечное "⚙️ Анализирую ваши действия...", без ответа и без
    # продвижения шага — и понять это было невозможно.
    status_msg = await bot_client.send_message(entity=chat_id, message="⚙️ <i>Анализирую ваши действия...</i>", parse_mode='html')
    if not is_last_step:
        prompt = f"""
Ты — старший стоматолог-экзаменатор. Ведешь интерактивный разбор клинического случая.
Вот история переписки на данный момент:

{history_str}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(справочная информация отсутствует)"}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Задачи на этот шаг (Шаг {current_step + 1} из {CASE_TOTAL_STEPS}):
1. Оцени последнее действие врача. Коротко укажи, насколько оно корректно и логично (опирайся на стандарты из Базы Знаний, если применимо).
2. Предоставь новые клинические данные, соответствующие его действию (например, если врач назначил КТ — опиши, что видно на КТ; если сделал анестезию — опиши начало действия и следующий этап работы).
3. Задай следующий конкретный вопрос о дальнейшей тактике.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Тон: экспертный, конструктивный.
2. Не давай готовых решений и не завершай случай раньше времени!
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
    else:
        prompt = f"""
Ты — старший стоматолог-экзаменатор. Нам нужно завершить интерактивный разбор клинического случая.
Вот вся история разбора:

{history_str}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(справочная информация отсутствует)"}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Задачи на этот финальный шаг:
1. Подведи итоги действий врача (опирайся на стандарты из Базы Знаний, если применимо).
2. Укажи на допущенные ошибки (если были) или похвали за верную тактику.
3. Выстави оценку по пятибалльной шкале (1/5 до 5/5) с краткой аргументацией.
4. Заверши диалог, пожелав успехов в практике.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Дай развернутый экспертный фидбек.
2. Разметка: только HTML. Без Markdown.
"""
    
    status_ctx = {"kind": "pm_chat", "chat_id": chat_id, "thinking_level": "MEDIUM"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
    
    if 'status_msg' in locals() and status_msg:
        try:
            await bot_client.delete_messages(chat_id, status_msg.id)
        except Exception:
            pass
    
    if error or not response or not getattr(response, "text", None):
        await bot_client.send_message(entity=chat_id, message="❌ <i>Ошибка симулятора при генерации ответа. Пожалуйста, отправьте ваш ответ еще раз.</i>", parse_mode='html')
        return
        
    reply_text = response.text.strip()
    reply_text = clean_html_formatting(reply_text)
    
    if is_last_step:
        # Clear state
        await database.clear_user_interactive_state(chat_id)
        final_message = f"🏁 <b>Разбор случая завершен!</b>\n\n{reply_text}"
        await bot_client.send_message(entity=chat_id, message=final_message, parse_mode='html')
    else:
        # Update history and save state
        history_data.append({"role": "assistant", "content": reply_text})
        history_payload = {
            "messages": history_data,
            "last_updated": time.time()
        }
        await database.set_user_interactive_state(
            user_id=chat_id,
            state_type="case",
            current_step=current_step + 1,
            case_id="dynamic",
            history=json.dumps(history_payload, ensure_ascii=False)
        )
        await bot_client.send_message(entity=chat_id, message=reply_text, parse_mode='html')

    # Реплика экзаменатора уходит и в историю ЛС, а не только в state кейса.
    #
    # Ходы врача пишутся туда безусловно: save_pm_message стоит в
    # handle_private_message ДО маршрутизации в симулятор, то есть до этой
    # функции. Ответы экзаменатора не писались нигде, а state кейса удаляется
    # на последнем шаге и по /abort. Итог для кейса из 4 шагов: в pm_messages
    # оставались 4 сообщения врача подряд ("назначу КТ", "распломбирую",
    # "поставлю МТА") без единого ответа между ними. Следующий обычный вопрос
    # в ЛС собирает промпт из этой односторонней ленты — модель читает ходы
    # кейса как реплики, адресованные ей, и отвечает на вопросы, которых не
    # видела. Тот же перекос портит и клинический портрет, и оценку длины
    # ответа (calculate_context_length_guidelines считает по history[-6:]).
    #
    # Метка в начале — как у /quiz: без неё разбор кейса неотличим от обычного
    # клинического ответа бота.
    try:
        await database.save_pm_message(
            chat_id, "Assistant", f"[Клинический кейс] {reply_text}"
        )
    except Exception as save_err:
        logger.error(f"Failed to persist case examiner reply: {save_err}")


_ACTIVE_PM_REQUESTS = {}


async def _async_pm_supplement_job(bot_client, chat_id, user_question, initial_answer, req_id):
    """
    Фоновая генерация клинического дополнения к первичному ответу через GPT-OSS-120B (Groq) / Gemini.
    Если найдена ценная дельта — отправляет второе сообщение спустя естественную паузу.
    """
    try:
        # Пауза перед фоновым анализом (естественная задержка)
        await asyncio.sleep(2.0)

        # Защита от смены контекста: если пользователь уже прислал новое сообщение
        if _ACTIVE_PM_REQUESTS.get(chat_id) != req_id:
            logger.info(f"PM supplement discarded before generation: context changed for chat_id={chat_id}")
            return

        supplement_text, error = await generate_pm_supplement_async(
            user_question=user_question,
            initial_answer=initial_answer,
            timeout=35.0,
        )

        if error or not supplement_text:
            logger.info(f"PM supplement empty/error for chat_id={chat_id}: {error}")
            return

        supplement_text = supplement_text.strip()
        # Проверка маркера NONE или слишком короткого/мусорного текста
        if (
            supplement_text == "NONE"
            or supplement_text.upper().startswith("NONE")
            or len(supplement_text) < 25
        ):
            logger.info(f"PM supplement returned NONE (answer complete) for chat_id={chat_id}")
            return

        # Повторная проверка контекста перед отправкой в Telegram
        if _ACTIVE_PM_REQUESTS.get(chat_id) != req_id:
            logger.info(f"PM supplement aborted before send: context changed for chat_id={chat_id}")
            return

        formatted_message = f"🔍 <b>Дополнительные клинические нюансы:</b>\n\n{supplement_text}"
        formatted_message = clean_html_formatting(formatted_message)

        # Валидация качества фонового дополнения рецензентом
        supp_ok, supp_reason = await check_response_quality(
            [f"Врач: {user_question}", f"Ассистент: {initial_answer}"],
            supplement_text,
            invited=False
        )
        if not supp_ok:
            logger.info("PM supplement rejected by reviewer (%s): skipping follow-up", supp_reason)
            return

        await tg_safety.send_message(
            bot_client,
            chat_id,
            formatted_message,
            parse_mode='html',
            timeout=20.0,
            logger=logger,
        )
        await database.save_pm_message(chat_id, "Assistant", formatted_message)
        logger.info(f"Successfully sent PM supplement follow-up to chat_id={chat_id}")
    except Exception as e:
        logger.warning(f"Fail-safe: PM supplement job failed chat_id={chat_id}: {e}", exc_info=False)


def is_clinical_consultation_query(text: str, has_media: bool, has_dental_topic: bool) -> bool:
    """
    Определяет, является ли входящее сообщение клиническим вопросом или кейсом,
    требующим углубленной клинической консультации / дополнения от 120B.
    
    Отсекает:
    - Простые приветствия и смолл-ток ("привет", "как дела", "здорово", "добрый день")
    - Благодарности и вежливость ("спасибо", "понял", "отлично", "ясно")
    - Команды и мета-вопросы о боте ("кто ты", "что умеешь", "как тебя настроить")
    - Короткие реплики без стоматологического контекста
    """
    if has_media:
        return True
        
    text_clean = (text or "").strip().lower()
    if not text_clean or len(text_clean) < 6:
        return False
        
    small_talk_exact = {
        "привет", "здравствуй", "здравствуйте", "здорово", "хай", "hello", "hi",
        "как дела", "как ты", "как поживаешь", "что делаешь", "чем занят",
        "спасибо", "благодарю", "понял", "ясно", "ок", "окей", "отлично", "хорошо",
        "пока", "до свидания", "спокойной ночи", "доброе утро", "добрый вечер",
        "кто ты", "что ты умеешь", "как тебя зовут", "ты кто", "ты бот", "ты человек"
    }
    text_no_punct = re.sub(r"[^\w\s]", "", text_clean).strip()
    if text_no_punct in small_talk_exact:
        return False

    if len(text_clean) < 40:
        greetings = ("привет", "здравствуй", "добрый день", "доброе утро", "как дела", "спасибо", "что нового")
        if any(text_clean.startswith(g) for g in greetings) and not has_dental_topic:
            return False

    if has_dental_topic:
        return True

    tooth_match = re.search(r"\b(?:[1-4][1-8]|[5-8][1-5])\b", text_clean) or re.search(r"\bзуб\w*", text_clean)
    medical_markers = (
        "боль", "болит", "отек", "пломб", "кариес", "пульпит", "периодонт",
        "имплант", "коронк", "снимок", "кт", "клкт", "рентген", "уступ",
        "анестез", "протокол", "цемент", "визир", "десна", "десне", "кост", "канал"
    )
    has_medical_marker = any(m in text_clean for m in medical_markers)

    return bool(tooth_match and has_medical_marker)


async def handle_private_message(bot_client, event):
    """Глубокий обработчик входящих личных сообщений (ЛС) бота с RAG, зрением и памятью."""
    try:
        chat_id = event.chat_id
        text = (event.message.message or "").strip()
        
        # Обновляем ID активного запроса для инвалидации устаревших фоновых задач дополнения
        _ACTIVE_PM_REQUESTS[chat_id] = time.time()
        
        # Rate limit: PM requests allowed once per 5 seconds per user (allow commands through)
        is_command = text.lower().startswith("/")
        if not is_command:
            cooldown_secs = check_user_cooldown(chat_id, chat_id, "pm_chat", seconds=5)
            if cooldown_secs > 0:
                logger.info(f"PM rate limit for chat_id={chat_id}, cooldown={cooldown_secs}s")
                # Обрабатывать не будем, но в историю положим. Раньше здесь
                # стоял голый return ДО save_pm_message, и врачи, пишущие
                # мыслями в несколько сообщений подряд ("смотри, случай:" →
                # через две секунды "37-й, боль при накусывании, что делать?"),
                # теряли второе НАВСЕГДА: оно не попадало ни в текущий промпт,
                # ни в контекст следующих ходов. Бот отвечал на обрывок, а сам
                # вопрос исчезал без следа.
                if text:
                    try:
                        await database.save_pm_message(chat_id, "User", text)
                    except Exception as save_err:
                        logger.error(f"Failed to persist rate-limited PM message: {save_err}")

                # И говорим об этом вслух. Раньше сообщение просто исчезало:
                # врач ждал ответа, которого никогда не будет, и не мог понять,
                # дошёл ли вопрос. Предупреждение само под кулдауном, чтобы
                # серия из пяти сообщений не превратилась в пять уведомлений.
                if not check_user_cooldown(chat_id, chat_id, "pm_rate_notice", seconds=30):
                    try:
                        await bot_client.send_message(
                            entity=chat_id,
                            message=f"⏳ <i>Секунду — дочитываю предыдущее сообщение. "
                                    f"Задайте вопрос одним сообщением через {cooldown_secs} с, "
                                    f"я учту всё написанное.</i>",
                            parse_mode='html',
                        )
                    except Exception as notice_err:
                        logger.error(f"Failed to send PM rate notice: {notice_err}")
                return
        
        # Record user activity for DM proactive pings
        try:
            state = load_state()
            pings = state.setdefault("pm_pings", {})
            # Обновляем поля, а не подменяем запись целиком: прежний вариант
            # затирал last_group_ping, из-за чего 48-часовой кулдаун групповых
            # пингов сбрасывался при каждом входящем ЛС и фактически работал
            # только 24-часовой порог по last_activity.
            user_ping = pings.setdefault(str(chat_id), {})
            user_ping["last_activity"] = datetime.now().isoformat()
            user_ping["ping_sent"] = False
            user_ping["unanswered_pings"] = 0
            save_state(state)
        except Exception as ping_err:
            logger.error(f"Failed to record ping activity for {chat_id}: {ping_err}")

        # Отписка от проактивных пингов.
        # is_negative_feedback и check_and_apply_silence существовали, но в ЛС
        # не вызывались ВООБЩЕ: врач, написавший боту "не пиши мне", продолжал
        # получать и ЛС-пинги, и приглашения в чат. Флаг гасит только исходящую
        # инициативу бота — на вопросы он отвечать не перестаёт, поэтому цена
        # ложного срабатывания невелика, а цена пропуска — навязчивые DM.
        if text and is_negative_feedback(text):
            try:
                set_ping_opt_out(chat_id, text)
                await bot_client.send_message(
                    entity=chat_id,
                    message="Понял, сам писать больше не буду. Вопросы задавайте когда угодно — на них отвечаю всегда.",
                )
            except Exception as opt_err:
                logger.error(f"Failed to process ping opt-out for {chat_id}: {opt_err}")
            return

        # Map text menu button clicks to slash commands
        btn_mapping = {
            # Постоянная нижняя панель быстрого доступа (ReplyKeyboardMarkup)
            "📖 база знаний": "/wiki",
            "база знаний": "/wiki",
            "🎲 квиз": "/quiz",
            "квиз": "/quiz",
            "🧮 калькулятор": "/calc",
            "калькулятор": "/calc",
            "⭐ закладки": "/bookmarks",
            "мои закладки": "/bookmarks",
            "закладки": "/bookmarks",
            "⌨️ главное меню": "/start",
            "главное меню": "/start",
            "меню": "/start",
            "кнопка меню": "/start",
            "навигация": "/start",

            # Синонимы и легаси-кнопки
            "📖 энциклопедия": "/wiki",
            "энциклопедия": "/wiki",
            "🎮 клинический кейс": "/case",
            "клинический кейс": "/case",
            "симулятор": "/case",
            "симулятор кейсов": "/case",
            "🎲 викторина": "/quiz",
            "викторина": "/quiz",
            "🌐 поиск в сети": "/web",
            "поиск в сети": "/web",
            "web-поиск": "/web",
            "📊 статистика чата": "/stats",
            "статистика чата": "/stats",
            "статистика": "/stats",
            "⚙️ стиль общения": "/style",
            "стиль общения": "/style",
            "стиль": "/style",
            "📚 протоколы": "/protocols",
            "протоколы": "/protocols"
        }
        if text.lower() in btn_mapping:
            text = btn_mapping[text.lower()]

        # 0. Voice Note / Audio processing
        is_voice = hasattr(event.message, "voice") and event.message.voice is not None and type(event.message.voice).__name__ != "MagicMock"
        is_audio_file = hasattr(event.message, "audio") and event.message.audio is not None and type(event.message.audio).__name__ != "MagicMock"
        is_audio = is_voice or is_audio_file
        transcribed_text = None
        if is_audio:
            file_obj = getattr(getattr(event, "message", None), "file", None)
            file_size = getattr(file_obj, "size", 0) or 0
            MAX_VOICE_SIZE = 25 * 1024 * 1024  # 25 МБ потолок
            if file_size > MAX_VOICE_SIZE:
                await bot_client.send_message(
                    entity=chat_id,
                    message="⚠️ <i>Аудиофайл слишком большой (> 25 МБ). Пришлите более короткую голосовую заметку.</i>",
                    parse_mode='html'
                )
                return

            os.makedirs(media_tools.MEDIA_TEMP_DIR, exist_ok=True)
            status_msg = await bot_client.send_message(entity=chat_id, message="🎤 <i>Распознаю аудиосообщение... Подождите.</i>", parse_mode='html')
            temp_path = None
            try:
                # download_media собственного таймаута НЕ имеет. Это было
                # единственное скачивание в проекте без бюджета: в группе его
                # ограничили (VOICE_DOWNLOAD_TIMEOUT_SECONDS), фото в ЛС тоже
                # (PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS), а голосовое в ЛС осталось.
                #
                # Последствия складывались втройне. Обработчик личных сообщений
                # держит замок на пользователя всё время работы, поэтому ВСЕ
                # следующие сообщения этого врача встают в очередь за зависшим и
                # не получают ответа до перезапуска процесса. Блок finally не
                # выполняется — статус «Распознаю аудиосообщение… Подождите»
                # висит в диалоге навсегда, а скачанный кусок файла остаётся на
                # диске. Сторож не спасает: он следит за живостью цикла событий,
                # а цикл жив.
                temp_path = await asyncio.wait_for(
                    event.message.download_media(
                        file=os.path.join(media_tools.MEDIA_TEMP_DIR, f"{event.message.id}_")
                    ),
                    timeout=PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                )
                if temp_path and os.path.exists(temp_path):
                    import blocking_tools
                    transcribed, error = await blocking_tools.transcribe_audio_async(temp_path, timeout=60)
                    if error:
                        logger.error(f"Audio transcription error: {error}")
                    elif transcribed:
                        raw_transcribed = transcribed.strip()
                        transcribed_text = await blocking_tools.correct_dental_transcription_async(raw_transcribed)
            except Exception as audio_err:
                logger.error(f"Error handling voice note: {audio_err}")
            finally:
                if 'status_msg' in locals() and status_msg:
                    try:
                        await bot_client.delete_messages(chat_id, status_msg.id)
                    except Exception:
                        pass
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
            
            if transcribed_text:
                text = transcribed_text
                # Filter common Whisper silence hallucinations
                silence_hallucinations = {
                    "you", "thank you", "bye", "подпишитесь", 
                    "продолжение следует", "редактор субтитров", "субтитры", 
                    "youtube", "собачья чушь", "спасибо"
                }
                clean_transcribed = text.strip().lower().rstrip(".").rstrip(",")
                if clean_transcribed in silence_hallucinations:
                    logger.info(f"Filtered suspected Whisper silence hallucination: '{text}'")
                    await bot_client.send_message(entity=chat_id, message="🎤 <i>(Тишина или фоновый шум) Пожалуйста, говорите громче или пишите текстом.</i>", parse_mode='html')
                    return
                await bot_client.send_message(entity=chat_id, message=f"🎤 <b>Распознано:</b> «{text}»", parse_mode='html')
            else:
                await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось распознать аудио. Пожалуйста, повторите или напишите текстом.</i>", parse_mode='html')
                return

        if text and not text.startswith("/"):
            await database.save_pm_message(chat_id, "User", text)

        # 0.5. Interactive Simulator State Routing & Abort Check
        user_state = await database.get_user_interactive_state(chat_id)
        
        # Check for case expiration (1 hour inactivity)
        if user_state and user_state.get("state_type") == "case":
            try:
                history_raw = json.loads(user_state.get("history") or "[]")
                if isinstance(history_raw, dict) and "last_updated" in history_raw:
                    last_updated = history_raw["last_updated"]
                    if time.time() - last_updated > 3600:
                        await database.clear_user_interactive_state(chat_id)
                        user_state = None
                        await bot_client.send_message(
                            entity=chat_id, 
                            message="⏳ <i>Предыдущая сессия симулятора была автоматически завершена из-за неактивности более 1 часа.</i>", 
                            parse_mode='html'
                        )
            except Exception as exp_err:
                logger.error(f"Error checking case expiration: {exp_err}")
        
        # Natural Language Intent Routing (Zero-Slash Routing)
        doc = getattr(event.message, "document", None)
        image_document = media_tools.image_document(event.message)
        has_media_intent = (
            getattr(event.message, "photo", None) is not None
            or getattr(event.message, "video", None) is not None
            or image_document is not None
        )

        if text and not text.startswith("/") and not has_media_intent:
            detected = detect_user_intent(text)

            # Если детерминированный fast-path не сработал, запускаем семантический LLM-триаж
            if not detected and len(text.split()) >= 3:
                try:
                    sem_res = await classify_pm_intent_semantic_async(text)
                    sem_intent = sem_res.get("intent")
                    sem_conf = float(sem_res.get("confidence", 1.0))
                    if sem_intent and sem_intent != "CLINICAL_CHAT" and sem_conf >= 0.75:
                        if sem_intent == "WEB_SEARCH":
                            q = sem_res.get("search_query") or text
                            detected = UserIntent(INTENT_WEB_SEARCH, q)
                        elif sem_intent == "CALCULATOR":
                            detected = UserIntent(INTENT_CALCULATOR, text)
                        elif sem_intent == "QUIZ":
                            detected = UserIntent(INTENT_QUIZ)
                        elif sem_intent == "CASE":
                            detected = UserIntent(INTENT_CASE)
                        elif sem_intent == "BOOKMARKS":
                            detected = UserIntent(INTENT_BOOKMARKS, sem_res.get("search_query") or "")
                except Exception as sem_err:
                    logger.debug(f"Semantic triage skipped: {sem_err}")

            if detected:
                logger.info(
                    "Zero-Slash Intent recognized: name=%s query=%r for chat_id=%s",
                    detected.name, detected.query, chat_id
                )
                if user_state and user_state.get("state_type") == "case":
                    await database.clear_user_interactive_state(chat_id)
                    user_state = None
                    await bot_client.send_message(
                        entity=chat_id, 
                        message="⏹️ <i>Активный клинический симулятор прерван для перехода в другой раздел.</i>", 
                        parse_mode='html'
                    )

                if detected.name == INTENT_MENU:
                    text = "/start"
                elif detected.name == INTENT_HELP:
                    text = "/help"
                elif detected.name == INTENT_STYLE:
                    text = "/style"
                elif detected.name == INTENT_QUIZ:
                    text = "/quiz"
                elif detected.name == INTENT_CASE:
                    text = "/case"
                elif detected.name == INTENT_BOOKMARKS:
                    text = f"/bookmarks {detected.query}".strip()
                elif detected.name == INTENT_WEB_SEARCH:
                    text = f"/web {detected.query}".strip()
                elif detected.name == INTENT_CALCULATOR:
                    instant_calc = calculate_anesthesia_instant(text)
                    if instant_calc:
                        await bot_client.send_message(entity=chat_id, message=instant_calc, parse_mode='html')
                        return
                    text = "/calc"

        # Автоматический выход из симулятора при вводе любой другой команды или нажатии кнопки меню
        is_command = text.startswith("/")
        if is_command and user_state and user_state.get("state_type") == "case" and text.lower() not in ("/abort", "/exit"):
            await database.clear_user_interactive_state(chat_id)
            user_state = None
            await bot_client.send_message(entity=chat_id, message="⏹️ <i>Активный клинический симулятор прерван для выполнения новой команды.</i>", parse_mode='html')

        if text.lower() in ("/abort", "/exit", "выход", "отмена"):
            if user_state:
                await database.clear_user_interactive_state(chat_id)
                await bot_client.send_message(entity=chat_id, message="⏹️ <i>Интерактивная сессия симулятора успешно сброшена.</i>", parse_mode='html')
            else:
                await bot_client.send_message(entity=chat_id, message="ℹ️ <i>У вас нет активной сессии симулятора.</i>", parse_mode='html')
            return

        if user_state and user_state.get("state_type") == "case":
            # Вложение в режиме кейса до анализа не доходит: маршрутизация в
            # симулятор стоит ЗДЕСЬ, а обработка медиа — на 500 строк ниже.
            # Снимок БЕЗ подписи внутри отбивается как пустой ход, а снимок С
            # ПОДПИСЬЮ проходил как обычный текстовый ход: экзаменатор оценивал
            # подпись («вот КТ, что дальше?»), вёл кейс дальше и ни словом не
            # упоминал, что рентгена не видел. Врач при этом уверен в обратном —
            # он его только что прислал. Ход не отменяем (ответ на текст всё же
            # осмысленный), но про непрочитанный файл говорим прямо.
            case_attachment = (
                getattr(event.message, "photo", None) is not None
                or getattr(event.message, "video", None) is not None
                or media_tools.image_document(event.message) is not None
            )
            if case_attachment:
                await bot_client.send_message(
                    entity=chat_id,
                    message="📎 <i>Снимок в режиме симулятора я не читаю" + (" — учёл только текст подписи." if (text or "").strip() else ". Отправьте текстовое описание вашего шага.") + " Чтобы разобрать рентген, выйдите из кейса: /abort.</i>",
                    parse_mode='html'
                )
            await handle_interactive_case_step(bot_client, chat_id, text, user_state)
            return

        # Admin Wipe command to delete recent bot messages
        if text.lower().startswith(("/wipe", "/del", "/delete")):
            is_authorized = False
            if chat_id in (7716348189, 1890028643):
                is_authorized = True
            else:
                try:
                    if str(chat_id) in [str(config.REPORT_CHAT_ID), str(config.SOURCE_CHAT_ID)]:
                        is_authorized = True
                    else:
                        if config.SOURCE_CHAT_ID:
                            permissions = await bot_client.get_permissions(config.SOURCE_CHAT_ID, chat_id)
                            if permissions.is_admin:
                                is_authorized = True
                except Exception as auth_err:
                    logger.error(f"Error checking PM admin auth: {auth_err}")
                
            if is_authorized:
                parts = text.split()
                count = 10
                if len(parts) > 1:
                    try:
                        count = int(parts[1])
                    except ValueError:
                        pass
                # Верхняя граница: раньше count не проверялся вообще.
                count = max(1, min(count, 100))

                # /wipe чистит бота ТОЛЬКО в целевом групповом чате.
                # Раньше выбирались последние N сообщений бота ПО ВСЕМ чатам и
                # удалялись в каждом: админ группы, набрав /wipe 200 в личке,
                # стирал клинические разборы из приватных переписок других
                # врачей. Права админа группы не распространяются на чужие ЛС.
                wipe_target = chat_id
                if str(chat_id) not in (str(config.REPORT_CHAT_ID), str(config.SOURCE_CHAT_ID)):
                    wipe_target = config.SOURCE_CHAT_ID
                if not wipe_target:
                    await bot_client.send_message(entity=chat_id, message="⛔ <i>Целевой чат для очистки не настроен.</i>", parse_mode='html')
                    return

                last_msgs = await database.get_last_bot_sent_messages(count, chat_id=wipe_target)
                if not last_msgs:
                    await bot_client.send_message(entity=chat_id, message="🤷‍♂️ <i>Не найдено отправленных сообщений бота для удаления.</i>", parse_mode='html')
                    return
                
                deleted_count = 0
                from collections import defaultdict
                by_chat = defaultdict(list)
                for msg_id, c_id in last_msgs:
                    by_chat[c_id].append(msg_id)
                    
                for c_id, msg_ids in by_chat.items():
                    try:
                        del_outcome = await tg_safety.delete_messages(bot_client, c_id, msg_ids, logger=logger)
                        if del_outcome.ok:
                            deleted_count += len(msg_ids)
                            for m_id in msg_ids:
                                await database.remove_bot_sent_message(m_id, chat_id=c_id)
                        else:
                            logger.error(f"Failed to delete messages in chat {c_id}: {del_outcome.error}")
                    except Exception as del_err:
                        logger.error(f"Error deleting messages in chat {c_id}: {del_err}")
                        
                await bot_client.send_message(
                    entity=chat_id, 
                    message=f"🧹 <b>Успешно удалено последних сообщений бота: {deleted_count} шт.</b>", 
                    parse_mode='html'
                )
            else:
                await bot_client.send_message(entity=chat_id, message="⛔ <i>У вас нет прав для выполнения этой команды.</i>", parse_mode='html')
            return

        # 1. Обработка базовых команд
        if text.lower().startswith(("/start consult", "/start_consult")):
            consult_welcome = (
                "👨‍⚕️ <b>Клинический консилиум StomChat</b>\n\n"
                "Приветствую, коллега! Готов разобрать клинический снимок, прицельную рентгенограмму или протокол.\n\n"
                "📌 <b>Как провести разбор:</b>\n"
                "1. Отправьте фото или рентген (можно без сжатия как файл)\n"
                "2. Укажите зуб (например, 3.6), жалобы или клинический вопрос\n"
                "3. Разберем анатомические ориентиры, индекс PAI, дифдиагноз и EBM-протокол."
            )
            await bot_client.send_message(
                entity=chat_id,
                message=consult_welcome,
                parse_mode='html'
            )
            return

        if text.lower() in ("/start", "/menu") or text.lower().startswith("/start "):
            keyboard = build_reply_keyboard()
            try:
                await bot_client.send_message(
                    entity=chat_id,
                    message="⌨️ <i>Нижнее меню быстрого доступа активировано.</i>",
                    buttons=keyboard,
                    parse_mode='html'
                )
            except Exception as kb_err:
                logger.debug(f"Failed to send reply keyboard: {kb_err}")

            await bot_client.send_message(
                entity=chat_id,
                message=MAIN_MENU_TEXT,
                buttons=build_main_menu_markup(),
                parse_mode='html'
            )
            return
            
        if text.lower() == "/style":
            profile = await database.get_user_profile(chat_id)
            current_style = profile.get("selected_style", "colleague_friendly")
            
            style_names = {
                "colleague_friendly": "Коллега-эксперт 🤝",
                "clinical_dry": "Сухие факты 📝",
                "humor_cynic": "Ироничный циник 💀"
            }
            curr_style_name = style_names.get(current_style, "Неизвестный")
            
            style_welcome = (
                "⚙️ <b>Настройка стиля общения</b>\n\n"
                f"Текущий стиль общения: <b>{curr_style_name}</b>\n\n"
                "Выберите стиль, в котором я буду отвечать вам в личных сообщениях:"
            )
            
            from telethon import types
            style_buttons = [
                [types.KeyboardButtonCallback(text="Коллега-эксперт 🤝 (по умолчанию)", data=b"style:colleague_friendly")],
                [types.KeyboardButtonCallback(text="Сухие факты 📝 (строго, без шуток)", data=b"style:clinical_dry")],
                [types.KeyboardButtonCallback(text="Ироничный циник 💀 (черный юмор)", data=b"style:humor_cynic")]
            ]
            await bot_client.send_message(entity=chat_id, message=style_welcome, buttons=style_buttons, parse_mode='html')
            return
            
        if text.lower() == "/help":
            help_text = (
                "💡 <b>Доступные команды в ЛС:</b>\n\n"
                "• /start — перезапустить приветствие бота и открыть Главное меню. Синоним — /menu.\n"
                "• /help — показать эту памятку.\n"
                "• /style — настроить стиль общения (коллега, сухие факты, циник).\n"
                "• /protocols — вывести список доступных клинических протоколов.\n"
                "• /wiki — открыть интерактивную стоматологическую энциклопедию. Синоним — /encyclopedia.\n"
                "• /calc — открыть шпаргалку-калькулятор по анестезии.\n"
                "• /quiz — запустить клиническую викторину.\n"
                "• /stats — показать самые обсуждаемые темы в чате сообщества.\n"
                "• /bookmarks — просмотреть сохраненные вами клинические закладки. Синонимы — /bookmark, /saved, /закладки.\n"
                "• /search &lt;запрос&gt; — быстрый прямой поиск по базе знаний стоматологии.\n"
                "• /web &lt;запрос&gt; — поиск в открытых источниках со ссылками, которые "
                "можно открыть и проверить (то, чего нет в базе чата: она заканчивается "
                "февралём 2026). Синоним — /найди.\n"
                "• /case — запустить интерактивный клинический симулятор.\n"
                "• /abort — сбросить текущий клинический симулятор. Синоним — /exit.\n\n"
                # Раздел появился потому, что групповые команды не были описаны
                # НИГДЕ: ни в меню, ни здесь, ни в промпте. Их шесть, они
                # работают, и врач о них не знал.
                "👥 <b>Команды в общем чате сообщества</b> (в ЛС не работают):\n\n"
                "• /summary — сводка текущего обсуждения. Синонимы — /итог, /sum.\n"
                "• /ask &lt;вопрос&gt; — задать мне клинический вопрос прямо в чате.\n"
                "• /poll — клиническая викторина для всего чата. Синоним — /кейс.\n"
                "• /what &lt;термин&gt; — коротко объяснить термин. Синоним — /что.\n"
                "• /save — ответьте этой командой на чей-то пост, и он попадёт в ваши "
                "закладки. Синоним — /сохранить.\n\n"
                "🗑 <b>Команды, которые УДАЛЯЮТ (только для админов чата):</b>\n\n"
                "• /del — в чате: удаляет пост, на который вы ответили, и саму команду; "
                "восстановить его нельзя. Синонимы — /delete, /wipe. В личке та же "
                "команда с числом (/wipe 20) удаляет последние N моих сообщений в общем "
                "чате — по умолчанию 10, максимум 100; ваши сообщения не трогает.\n\n"
                "• <b>Текстовый/Голосовой вопрос:</b> Просто напишите его или отправьте голосовое сообщение. Я отвечу с использованием базы знаний.\n"
                "• <b>Анализ снимка:</b> Прикрепите фото или рентген. Я опишу, что на нем изображено, и предложу клиническую тактику.\n"
                f"• <b>Контекстная память:</b> Я анализирую последние <b>{PM_HISTORY_LIMIT} сообщений</b> нашего диалога."
            )
            from telethon import Button
            help_buttons = [
                [Button.inline("🏠 Открыть Главное меню", data="nav:main")]
            ]
            await bot_client.send_message(entity=chat_id, message=help_text, buttons=help_buttons, parse_mode='html')
            return

        if text.lower() == "/protocols":
            protocols_text = (
                "📚 <b>Основные клинические протоколы в Базе Знаний:</b>\n\n"
                "• <b>BOPT (Biologically Oriented Preparation Technique):</b> Концепция препарирования без уступа.\n"
                "• <b>Вертикальное препарирование:</b> Особенности ведения краев коронок, сохранение тканей.\n"
                "• <b>Травление керамики:</b> Протоколы работы с плавиковой кислотой и силанизацией (E.max, полевой шпат).\n"
                "• <b>Ирригация в эндодонтии:</b> Концентрации гипохлорита натрия, ЭДТА, протоколы активации (ультразвук, звуковая).\n"
                "• <b>Обтурация корневых каналов:</b> Методики латеральной конденсации и вертикальной горячей гуттаперчи.\n\n"
                "👇 <i>Выберите интересующий протокол ниже для детального изучения:</i>"
            )
            from telethon import Button
            buttons = [
                [Button.inline("🦷 BOPT", data="proto:bopt"), Button.inline("🧪 Травление", data="proto:etching")],
                [Button.inline("💧 Ирригация", data="proto:irrigation"), Button.inline("🩸 Обтурация", data="proto:obturation")],
                # Вертикальное препарирование перечислено в тексте выше, а
                # кнопки для него не было — открыть его врач не мог никак.
                [Button.inline("📐 Вертикальное препарирование", data="proto:vertical")]
            ]
            await bot_client.send_message(entity=chat_id, message=protocols_text, buttons=buttons, parse_mode='html')
            return

        if text.lower() in ("/wiki", "/encyclopedia"):
            wiki_text = (
                "📖 <b>Интерактивная Стоматологическая Энциклопедия</b>\n\n"
                "Здесь вы можете изучать клинические стандарты, классификации и протоколы напрямую из нашей базы знаний.\n\n"
                "👇 <i>Выберите раздел для детального просмотра:</i>"
            )
            from telethon import Button
            # Разделы берём из дерева, а не перечисляем заново. Здесь висели
            # четыре кнопки из одиннадцати разделов: «Гнатология»,
            # «Реставрация», «Съёмное», «Ортодонтия», «Цифра», «Оборудование» и
            # «Менеджмент» с этого входа были недоступны вообще.
            buttons = wiki_topic_buttons()[:-1]
            buttons.append([Button.inline("🔍 Инструкция по поиску", data="wiki_cat:help")])
            await bot_client.send_message(entity=chat_id, message=wiki_text, buttons=buttons, parse_mode='html')
            return

        if text.lower() == "/calc" or text.lower().startswith("/calc "):
            calc_arg = text[5:].strip() if text.lower().startswith("/calc ") else ""
            if calc_arg:
                instant_calc = calculate_anesthesia_instant(calc_arg)
                if instant_calc:
                    await bot_client.send_message(entity=chat_id, message=instant_calc, parse_mode='html')
                    return

            # Раньше здесь были ТОЛЬКО нормы на килограмм, без абсолютных
            # потолков. Это давало прямую ошибку расчёта: 7 мг/кг для пациента
            # 100 кг — 700 мг артикаина против допустимых 500, перебор на 40%.
            # Потолок артикаина наступает уже при весе около 71 кг, то есть у
            # большинства взрослых мужчин считать по мг/кг нельзя вообще.
            # Значения на килограмм оставлены как были: понижать предел
            # безопасно, повышать — нет, и без клинициста я этого не делаю.
            calc_text = (
                "🧮 <b>Справочник-калькулятор анестезии</b>\n\n"
                "Пришлите препарат, концентрацию и вес — например "
                "<i>«артикаин 4%, ребёнок 20 кг»</i> — и я посчитаю с арифметикой на виду.\n\n"
                "<b>Предел всегда двойной: мг/кг И абсолютный максимум. Действует меньшее из двух.</b>\n\n"
                "• <b>Артикаин 4%</b> (1:100 000 / 1:200 000)\n"
                "  взрослые 7 мг/кг, дети 5 мг/кг, <b>но не более 500 мг</b>\n"
                "  карпула 1.7 мл = 68 мг → потолок ≈ 7 карпул\n"
                "  <i>потолок 500 мг наступает уже при весе ≈ 71 кг</i>\n\n"
                "• <b>Мепивакаин 3%</b> (без вазоконстриктора)\n"
                "  4.4 мг/кг, <b>но не более 400 мг</b>\n"
                "  карпула 1.8 мл = 54 мг → потолок ≈ 7 карпул\n"
                "  <i>потолок наступает при весе ≈ 91 кг</i>\n\n"
                "• <b>Лидокаин 2%</b> (с адреналином)\n"
                "  взрослые 7 мг/кг, дети 4.4 мг/кг, <b>но не более 500 мг</b>\n"
                "  карпула 1.8 мл = 36 мг → потолок ≈ 13 карпул\n"
                "  <i>потолок наступает при весе ≈ 71 кг</i>\n\n"
                "⚠️ <i>Это референсные максимумы для здорового пациента, а не рекомендация дозы. "
                "При сопутствующей патологии, у детей, беременных и пожилых предел ниже. "
                "Объём карпулы и концентрацию сверяйте с инструкцией к своему препарату — "
                "у разных производителей они отличаются.</i>"
            )
            await bot_client.send_message(entity=chat_id, message=calc_text, parse_mode='html')
            return

        if text.lower() == "/quiz":
            cooldown_left = check_user_cooldown(chat_id, chat_id, "pm_quiz", seconds=15)
            if cooldown_left > 0:
                await bot_client.send_message(
                    entity=chat_id,
                    message=f"⏳ <i>Подождите {cooldown_left} сек перед повторной генерацией викторины.</i>",
                    parse_mode='html'
                )
                return

            status_msg = await bot_client.send_message(entity=chat_id, message="🎲 <i>Генерирую клиническую викторину для вас... Подождите.</i>", parse_mode='html')
            prompt = """
Ты — умный клинический ассистент-преподаватель в чате врачей-стоматологов "StomChat". 
Придумай и напиши интересную клиническую задачу-викторину для практикующего стоматолога. 
Задача должна быть сложной, реалистичной, из терапевтической, ортопедической или хирургической стоматологии.

Формат вывода:
1. Описание клинической ситуации (жалобы, осмотр, данные рентгенографии).
2. Четыре варианта ответа (A, B, C, D) с различными тактиками лечения или диагнозами.
3. Инструкция: напиши пользователю, что он может прислать свой ответ (например, "Мой ответ А"), чтобы ты проверил его и выдал подробное объяснение.

Не пиши правильный ответ сразу в сообщении викторины!
Будь лаконичен, профессионален.
"""
            async with bot_client.action(chat_id, 'typing'):
                status_ctx = {"kind": "pm_chat", "chat_id": chat_id, "thinking_level": "MEDIUM"}
                response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
                try:
                    await bot_client.delete_messages(chat_id, status_msg.id)
                except Exception:
                    pass

                reply_text = None
                if not error and response and getattr(response, "text", None):
                    cand = response.text.strip()
                    if cand and len(cand) > 30:
                        reply_text = clean_html_formatting(cand)

                if not reply_text:
                    logger.warning("PM /quiz generation failed, using fallback clinical quiz from pool")
                    fb = random.choice(CLINICAL_QUIZ_FALLBACKS)
                    topic_str = f" [{fb.get('topic')}]" if fb.get("topic") else ""
                    reply_text = (
                        f"<b>Клиническая ситуация{topic_str}:</b>\n{fb['question']}\n\n"
                        f"<b>Варианты ответа:</b>\n"
                        f"<b>A:</b> {fb['options'][0]}\n"
                        f"<b>B:</b> {fb['options'][1]}\n"
                        f"<b>C:</b> {fb['options'][2]}\n"
                        f"<b>D:</b> {fb['options'][3]}\n\n"
                        f"<i>Напишите ваш вариант ответа (например, «Мой ответ A»), чтобы я проверил его и предоставил клинический разбор!</i>"
                    )

                await bot_client.send_message(entity=chat_id, message=f"🎲 <b>Клиническая Викторина:</b>\n\n{reply_text}", parse_mode='html')
                # Вопрос ОБЯЗАН попасть в историю ЛС. Без этого следующий ход
                # врача ("Мой ответ Б") приходит в общий обработчик без самой
                # задачи, и модель уверенно выносит "верно/неверно" с разбором
                # случая, которого не видела.
                await database.save_pm_message(
                    chat_id, "Assistant", f"[Клиническая викторина]\n{reply_text}"
                )
            return

        if text.lower() == "/stats":
            # Раньше здесь лежал статичный текст: одни и те же числа при любом
            # содержании чата. Теперь считаем по архиву и живой базе.
            cached = _stats_cache["payload"] is not None
            status_msg = None
            if not cached:
                status_msg = await bot_client.send_message(
                    entity=chat_id,
                    message="📊 <i>Считаю темы по архиву чата, это займёт несколько секунд...</i>",
                    parse_mode='html',
                )
            counts, scanned = await get_topic_statistics()
            if status_msg is not None:
                try:
                    await bot_client.delete_messages(chat_id, status_msg.id)
                except Exception:
                    pass

            stats_text = render_topic_statistics(counts, scanned)
            if not stats_text:
                # Пустой результат — честно говорим, а не показываем нули.
                stats_text = ("📊 <i>Статистику посчитать не удалось: база сообщений "
                              "сейчас недоступна. Попробуйте позже.</i>")
            await bot_client.send_message(entity=chat_id, message=stats_text, parse_mode='html')
            return

        if text.lower().startswith(("/bookmarks", "/bookmark", "/saved", "/закладки")):
            for prefix in ("/bookmarks", "/bookmark", "/saved", "/закладки"):
                if text.lower().startswith(prefix):
                    arg = text[len(prefix):].strip()
                    break
            else:
                arg = ""
            page = 1
            query_filter = None
            if arg:
                if arg.isdigit():
                    page = int(arg)
                else:
                    query_filter = arg
            
            if query_filter:
                rows = await database.get_clinical_bookmarks(chat_id, query=query_filter)
                title = f"📌 <b>Результаты поиска в закладках по запросу «{query_filter}»:</b>\n\n"
            else:
                rows = await database.get_clinical_bookmarks(chat_id)
                title = f"📌 <b>Ваши сохраненные клинические закладки (Страница {page}):</b>\n\n"

            if not rows:
                if query_filter:
                    await bot_client.send_message(entity=chat_id, message=f"🔍 В ваших закладках не найдено совпадений по запросу «{query_filter}».", parse_mode='html')
                else:
                    await bot_client.send_message(entity=chat_id, message="📌 <b>У вас пока нет сохраненных клинических закладок (закладки пусты)</b>.\nОтправьте <code>/save</code> в ответ на любое сообщение в общем чате, чтобы сохранить его.", parse_mode='html')
                return

            per_page = 10
            total_items = len(rows)
            total_pages = (total_items + per_page - 1) // per_page
            
            if not query_filter and page > total_pages:
                await bot_client.send_message(entity=chat_id, message=f"⚠️ Страница {page} не существует. Всего страниц: {total_pages}.", parse_mode='html')
                return
                
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            page_rows = rows[start_idx:end_idx]
            
            text_out = title
            for i, row in enumerate(page_rows, start_idx + 1):
                msg_id, chat_id_val, sender_name, msg_text, media_desc, date = row
                # Текст закладки и имя автора приходят из чата и уходили в HTML
                # БЕЗ экранирования. Одна угловая скобка в сохранённом посте
                # («уступ <0.5 мм») — и Telegram отклоняет ВЕСЬ список: врач не
                # увидит ни одной своей закладки, а не только испорченную.
                # В живой базе такой символ пока в одном сообщении из 30 082,
                # но закладки выбирают осознанно и как раз в постах с цифрами.
                text_out += f"{i}. <b>{_bookmark_snippet(sender_name, limit=64)}</b> ({date}):\n"
                text_out += f"«{_bookmark_snippet(msg_text)}»\n"
                if media_desc:
                    text_out += f"🖼️ <i>Описание снимка:</i> {_bookmark_snippet(media_desc)}\n"
                # Ссылку рисуем только для реальных сообщений группы.
                # Закладки на статьи энциклопедии сохраняются с синтетическим
                # отрицательным msg_id и chat_id личного чата, а
                # str(положительный_id).replace("-100","") ничего не меняет —
                # получалось https://t.me/c/<user_id>/-483920117, ведущее в никуда.
                is_group_message = str(chat_id_val).startswith("-100") and msg_id > 0
                if is_group_message:
                    clean_chat_id = str(chat_id_val)[4:]
                    text_out += f"🔗 <a href='https://t.me/c/{clean_chat_id}/{msg_id}'>Перейти к сообщению</a>\n\n"
                else:
                    text_out += "📖 <i>Статья энциклопедии</i>\n\n"
                
            if query_filter:
                # Для поиска счётчика не было вовсе: при 50 совпадениях врач
                # видел первые 10 и считал, что это все его закладки по теме.
                if total_items > len(page_rows):
                    text_out += (f"<i>Показано {len(page_rows)} из {total_items} совпадений. "
                                 f"Уточните запрос, чтобы увидеть остальные.</i>")
                else:
                    text_out += f"<i>Найдено совпадений: {total_items}.</i>"
            elif total_pages > 1:
                text_out += f"<i>Показано {len(page_rows)} из {total_items} закладок. Страница {page} из {total_pages}.\nИспользуйте <code>/bookmarks [номер_страницы]</code> для перехода.</i>"
                
            await bot_client.send_message(entity=chat_id, message=text_out, parse_mode='html', link_preview=False)
            return

        if text.lower().startswith("/search"):
            query_param = text[7:].strip()
            if not query_param:
                await bot_client.send_message(entity=chat_id, message="🔍 <b>Пожалуйста, укажите поисковый запрос.</b>\nПример: <code>/search BOPT</code>", parse_mode='html')
                return
            keywords = extract_keywords(query_param)
            wiki_facts = []
            if os.path.exists("stomat_wiki.db"):
                try:
                    with contextlib.closing(sqlite3.connect("stomat_wiki.db", timeout=10)) as conn:
                        c = conn.cursor()
                        for kw in keywords:
                            _w, _p = like_any_case("content", kw)
                            c.execute("SELECT category_code, content FROM distilled_facts "
                                      f"WHERE {_w} LIMIT 5", _p)
                            for row in c.fetchall():
                                cat_code, content = row
                                try:
                                    # Подсветка через <b>, а не <u>: ниже весь ответ
                                    # проходит clean_html_formatting, а он сохраняет
                                    # ровно три тега — <b>, <i>, <code>. Остальное
                                    # экранируется, и врач видел в выдаче литеральные
                                    # «&lt;u&gt;BOPT&lt;/u&gt;» вместо выделения —
                                    # то есть подсветка не просто не работала, а
                                    # засоряла каждый найденный факт.
                                    content_hl = re.sub(f"(?i)({re.escape(kw)})", r"<b>\1</b>", content)
                                except Exception:
                                    content_hl = content
                                fact = f"• {content_hl}"
                                if fact not in wiki_facts:
                                    wiki_facts.append(fact)
                except Exception as e:
                    logger.error(f"Error direct searching wiki: {e}")
            if not wiki_facts:
                await bot_client.send_message(entity=chat_id, message=f"🔍 По запросу «{query_param}» ничего не найдено в базе знаний.", parse_mode='html')
                return
            search_out = f"🔍 <b>Результаты поиска по запросу «{query_param}»:</b>\n\n" + "\n\n".join(wiki_facts[:8])
            search_out = clean_html_formatting(search_out)
            await bot_client.send_message(entity=chat_id, message=search_out, parse_mode='html')
            return

        if text.lower().startswith(("/web", "/найди")):
            # Веб-поиск с ПРОВЕРЯЕМОЙ ссылкой — то, чего у врача не было вообще.
            #
            # Чем это отличается от /search: тот ищет по нашему корпусу, а корпус
            # кончается 2026-02-19 (последняя дата в stomat_archive.db, на сегодня
            # 160 дней назад) и ссылку содержит в 4 фактах из 12 784. То есть на
            # вопрос про материал, препарат или отзыв последних месяцев бот отвечал
            # пересказом чужого мнения из чата, а открыть и проверить утверждение
            # врач не мог. Механизм поиска в проекте был построен и покрыт
            # проверками, но не вызывался НИКЕМ: слов web_search_async,
            # perform_search, web_lookup в assistant.py, main.py и summarizer.py
            # было ноль.
            parts = text.split(None, 1)
            query_param = parts[1].strip() if len(parts) > 1 else ""
            if not query_param:
                await tg_safety.send_message(
                    bot_client, chat_id,
                    "🌐 <b>Что искать в открытых источниках?</b>\n"
                    "Пример: <code>/web биодентин перфорация дна полости</code>\n\n"
                    "<i>Это поиск по интернету со ссылками, которые можно открыть. "
                    "Для поиска по базе чата — /search.</i>",
                    timeout=WEB_STATUS_TIMEOUT_SECONDS, op="send_message:web_hint",
                    logger=logger, parse_mode='html',
                )
                return

            cooldown_left = check_user_cooldown(chat_id, chat_id, "web_lookup",
                                                seconds=WEB_COOLDOWN_SECONDS)
            if cooldown_left > 0:
                # Молчать нельзя: врач не поймёт, дошёл ли запрос, и повторит его.
                await tg_safety.send_message(
                    bot_client, chat_id,
                    f"⏳ <i>Веб-поиск поднимает внешний сервис — подождите "
                    f"{cooldown_left} с.</i>",
                    timeout=WEB_STATUS_TIMEOUT_SECONDS, op="send_message:web_cooldown",
                    logger=logger, parse_mode='html',
                )
                return

            is_fresh_query = web_lookup.is_fresh_scientific_data_query(query_param)
            if is_fresh_query:
                status_text = (
                    "🔍 <i>Поиск актуальных научных исследований (2025–2026 гг.) в PubMed и Cochrane...</i>"
                )
            else:
                status_text = (
                    "🔍 <i>Выполняю поиск по медицинским базам и клиническим протоколам...</i>"
                )

            status_res = await tg_safety.send_message(
                bot_client, chat_id,
                status_text,
                timeout=WEB_STATUS_TIMEOUT_SECONDS, op="send_message:web_status",
                logger=logger, parse_mode='html',
            )

            import blocking_tools

            async def _web_search_call(query, timeout):
                """Транспорт: подпроцесс с провайдером. Отдаёт [{'text','url'}], ошибку."""
                return await blocking_tools.web_search_async(
                    query, web_lookup.SEARCH_MAX_RESULTS, timeout=timeout
                )

            async def _web_answer_call(prompt, timeout):
                """Генерация по выдержкам. Бюджет приходит СВЕРХУ, а не берётся свой."""
                web_ctx = {"kind": "pm_web_lookup", "chat_id": chat_id,
                           "thinking_level": "MEDIUM"}
                web_response, web_error = await generate_gemini_text_async(
                    prompt, web_ctx, timeout=timeout
                )
                return (getattr(web_response, "text", None) or "").strip(), web_error

            async def _web_grounding_call(query, timeout):
                """Google Search Grounding: gemini-2.5-flash с Google Search tool."""
                if os.environ.get("STOMCHAT_LOG_PATH") and "websearch" in os.environ.get("STOMCHAT_LOG_PATH", ""):
                    return None, "test_mock_fallback"
                return await blocking_tools.google_grounding_async(query, timeout=timeout)

            import inspect
            lookup_kwargs = {}
            sig = inspect.signature(web_lookup.run_lookup)
            if "grounding_call" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                lookup_kwargs["grounding_call"] = _web_grounding_call

            lookup = await web_lookup.run_lookup(
                query_param, _web_search_call, _web_answer_call,
                budget=WEB_LOOKUP_BUDGET_SECONDS, log=logger,
                **lookup_kwargs
            )

            # Статус убираем ДО ответа и под сроком: без границы уборка сама может
            # подвиснуть и удержать замок на пользователе после того, как ответ уже
            # готов. Неудача уборки ответ не отменяет — она уже в журнале.
            status_id = getattr(status_res.value, "id", None) if status_res.ok else None
            if status_id:
                await tg_safety.delete_messages(
                    bot_client, chat_id, status_id,
                    timeout=WEB_STATUS_CLEANUP_TIMEOUT_SECONDS,
                    op="delete_messages:web_status", logger=logger,
                )

            # Разметка проходит через clean_html_formatting: он оставляет ровно
            # <b>, <i>, <code> и экранирует остальное. Битый тег от модели Telegram
            # отклоняет ЦЕЛИКОМ — врач не увидел бы ни ответа, ни ссылок.
            answer_text = clean_html_formatting(WEB_ANSWER_HEADER + lookup["text"])
            delivered = await tg_safety.send_message(
                bot_client, chat_id, answer_text,
                timeout=WEB_DELIVERY_TIMEOUT_SECONDS, op="send_message:web_answer",
                logger=logger, parse_mode='html', link_preview=False,
            )
            if not delivered.ok:
                logger.warning(
                    "Веб-поиск отработал (исход=%s, источников=%d, попыток=%d, "
                    "%.1f с), но ответ НЕ доставлен chat_id=%s: %s. Врач остался "
                    "без ответа и без причины",
                    lookup["outcome"], len(lookup["sources"]), lookup["attempts"],
                    lookup["elapsed"], chat_id, delivered.reason,
                )
                return
            logger.info(
                "Веб-поиск доставлен chat_id=%s исход=%s источников=%d попыток=%d "
                "%.1f с %d символов",
                chat_id, lookup["outcome"], len(lookup["sources"]),
                lookup["attempts"], lookup["elapsed"], len(answer_text),
            )
            # В историю ЛС кладём и запрос, и ответ. Без этого следующий ход врача
            # («а по второй ссылке что?») приходит в общий обработчик без самих
            # ссылок, и модель отвечает про источники, которых не видела.
            await database.save_pm_message(
                chat_id, "Assistant",
                lookup['text']
            )
            return

        if text.lower() == "/case":
            cooldown_left = check_user_cooldown(chat_id, chat_id, "pm_case", seconds=15)
            if cooldown_left > 0:
                await bot_client.send_message(
                    entity=chat_id,
                    message=f"⏳ <i>Подождите {cooldown_left} сек перед запуском нового клинического случая.</i>",
                    parse_mode='html'
                )
                return

            status_msg = await bot_client.send_message(entity=chat_id, message="🎮 <i>Подготавливаю интерактивный клинический случай... Подождите.</i>", parse_mode='html')
            
            departments = [
                "эндодонтия/кариесология (терапевтическая стоматология)",
                "протезирование/виниры/коронки (ортопедическая стоматология)",
                "имплантация/удаление зуба (хирургическая стоматология)",
                "заболевания пародонта (пародонтология)",
                "окклюзия/ВНЧС (гнатология)"
            ]
            selected_dept = random.choice(departments)
            
            case_prompt = f"""
Ты — старший стоматолог-экзаменатор. Придумай и опиши начало сложного клинического случая из области: {selected_dept}.
Напиши:
1. Жалобы пациента и анамнез.
2. Данные визуального осмотра.
3. Задай ровно один конкретный вопрос о первом действии врача (например, какие дополнительные исследования назначить, или какой инструмент выбрать).

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Будь лаконичен, профессионален.
2. Не пиши правильный ответ и не давай вариантов! Врач должен ответить своими словами (или голосом).
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
            status_ctx = {"kind": "pm_chat", "chat_id": chat_id, "thinking_level": "MEDIUM"}
            response, error = await generate_gemini_text_async(case_prompt, status_ctx, timeout=90)
            await bot_client.delete_messages(chat_id, status_msg.id)
            if error or not response or not getattr(response, "text", None):
                await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось запустить симулятор. Попробуйте позже.</i>", parse_mode='html')
                return
            starting_text = response.text.strip()
            starting_text = clean_html_formatting(starting_text)
            
            history_payload = {
                "messages": [{"role": "assistant", "content": starting_text}],
                "last_updated": time.time()
            }
            await database.set_user_interactive_state(
                user_id=chat_id,
                state_type="case",
                current_step=1,
                case_id="dynamic",
                history=json.dumps(history_payload, ensure_ascii=False)
            )
            case_welcome = (
                "🎮 <b>Интерактивный клинический симулятор запущен!</b>\n"
                "Вы можете отвечать текстом или отправлять голосовые сообщения. Бот будет анализировать ваши действия и вести кейс дальше.\n"
                "Для отмены отправьте /abort.\n\n"
                f"{starting_text}"
            )
            await bot_client.send_message(entity=chat_id, message=case_welcome, parse_mode='html')
            # Условие кейса тоже в историю ЛС: без него первый ход врача
            # ("назначу КТ 3.6") лежит в pm_messages как реплика ни на что —
            # ни жалоб, ни анамнеза, на которые он отвечает, в истории нет.
            # Ответы экзаменатора на следующих шагах пишет
            # handle_interactive_case_step, там же объяснение целиком.
            try:
                await database.save_pm_message(
                    chat_id, "Assistant", f"[Клинический кейс] {starting_text}"
                )
            except Exception as save_err:
                logger.error(f"Failed to persist case intro: {save_err}")
            return

        # 1.1. Групповая команда, набранная в личке.
        #
        # Раньше такой текст («/итог», «/кейс», «/сохранить») до этой точки
        # доходил как обычная реплика и уходил в ПЛАТНУЮ генерацию клинического
        # ответа: врач получал рассуждение про слово «итог» и не узнавал, что
        # команда работает — просто не здесь. Теперь называем место.
        if resolve_group_command(text):
            await bot_client.send_message(entity=chat_id, message=GROUP_COMMANDS_HINT,
                                          parse_mode='html')
            return

        # 2. Восстановление динамического диалога.
        #
        # Глубина ОДНА на все ветки — PM_HISTORY_LIMIT. Раньше фактических было
        # три: 35 сообщений в текстовой ветке, 8 в ветке со снимком
        # (context_msgs[-8:]) и 6 в проактивном пинге. И обещаний было два: /start
        # говорил «до 25 сообщений», /help подставлял константу (35). То есть врач,
        # приславший рентген, получал ответ по 8 репликам, а в памятке ему обещали
        # 35. Ограничение теперь не по числу реплик, а по символам (_fit_pm_history):
        # 8 сообщений — это и 200 символов, и 32 000, а важен именно объём.
        history = await database.get_last_pm_messages(chat_id, limit=PM_HISTORY_LIMIT)
        context_msgs = []
        try:
            recent_pm_texts = [m["text"] for m in history[-6:] if m["text"]]
            length_guideline = calculate_context_length_guidelines(recent_pm_texts)
        except Exception:
            length_guideline = "Отвечай кратко, до 3-4 предложений."

        for msg in history:
            context_msgs.append(f"{msg['sender_name']}: {msg['text']}")
            
        history_context_text = " ".join([msg['text'] for msg in history[-3:]]) if history else ""

        # Ключевые слова для поиска справки берём ТОЛЬКО из реплик врача.
        #
        # В групповом пути ответы бота из источника ключей исключены (фильтр по
        # префиксу "Бот " перед extract_keywords), в ЛС фильтра не было вообще:
        # history[-3:] почти всегда содержит предыдущий ответ бота целиком, а он
        # в разы длиннее вопроса врача. Замер на живом сценарии — врач пишет
        # «болит 36 при накусывании, лечен канал», бот отвечает абзацем про КТ,
        # гипохлорит, распломбировку и резекцию, врач спрашивает «а сколько
        # ждать?»: из 12 ключей, ушедших в поиск, ВСЕ 12 взяты из ответа бота, а
        # от самого врача в поиск попало одно слово («канал»). Справка приходила
        # по теме прошлой реплики бота, а не по вопросу собеседника.
        #
        # Хуже того, has_dental_topic на «а сколько ждать?» ложен, а вместе с
        # текстом бота истинен: бот сам себе назначал клиническую тему и лез в
        # базу за справкой по собственному предыдущему ответу.
        #
        # Берём последние 3 сообщения ВРАЧА, а не «те из последних 3, что не от
        # бота»: иначе после длинного ответа бота контекста не остаётся вовсе.
        doctor_msgs = [msg['text'] for msg in history
                       if msg.get('text') and msg.get('sender_name') != "Assistant"]
        search_context_text = " ".join(doctor_msgs[-3:])

        # 3. Обработка медиафайлов (фото/видео) в ЛС
        media_description = None
        temp_path = None
        # Снимки часто присылают ДОКУМЕНТОМ, чтобы Telegram их не пережал —
        # это стандартная практика для рентгена и КТ. Раньше учитывались только
        # photo/video: у документа has_media был False, текста нет, в историю
        # ничего не писалось, и промпт собирался из старой переписки — бот
        # уверенно переотвечал на вопрос двадцатиминутной давности, ни словом
        # не упомянув, что файл проигнорирован.
        # Определение общее с групповым путём (media_tools). Оно же отсекает
        # стикеры: статический стикер Telegram — документ с mime image/webp, и
        # без этой проверки каждый стикер в ЛС уходил бы в vision как снимок.
        doc = getattr(event.message, "document", None)
        image_document = media_tools.image_document(event.message)

        has_media = (
            event.message.photo is not None
            or event.message.video is not None
            or image_document is not None
        )

        # Нераспознаваемое вложение (PDF, архив, стикер) без текста: честно
        # говорим, что не умеем, вместо ответа на прошлое сообщение.
        unsupported_attachment = (
            not has_media
            and not (text or "").strip()
            and (doc is not None or getattr(event.message, "sticker", None) is not None)
        )
        if unsupported_attachment:
            await bot_client.send_message(
                entity=chat_id,
                message="📎 <i>Такой файл я разобрать не могу. Пришлите снимок картинкой "
                        "(JPG/PNG) или опишите вопрос текстом.</i>",
                parse_mode='html'
            )
            return
        
        if has_media:
            file_obj = getattr(getattr(event, "message", None), "file", None)
            file_size = getattr(file_obj, "size", 0) or 0
            MAX_MEDIA_SIZE = 35 * 1024 * 1024  # 35 МБ потолок
            if file_size > MAX_MEDIA_SIZE:
                await bot_client.send_message(
                    entity=chat_id,
                    message="⚠️ <i>Медиафайл превышает 35 МБ. Пожалуйста, сожмите файл или пришлите снимок в формате JPEG/PNG.</i>",
                    parse_mode='html'
                )
                return

            os.makedirs(media_tools.MEDIA_TEMP_DIR, exist_ok=True)
            try:
                # Отправляем статус ожидания
                status_msg = await bot_client.send_message(entity=chat_id, message="📥 <i>Скачиваю и анализирую медиафайл... Подождите немного.</i>", parse_mode='html')
                
                # Таймаута у download_media нет своего. В ЛС это опаснее, чем в
                # группе: обработчик держит замок на пользователя, и все
                # следующие сообщения врача встают в очередь за подвисшей
                # загрузкой — навсегда.
                temp_path = await asyncio.wait_for(
                    event.message.download_media(file=f"temp_media/{event.message.id}_"),
                    timeout=PM_MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
                )
                file_to_analyze = temp_path
                
                # Если видео, извлекаем первый кадр
                if event.message.video:
                    logger.info("Извлечение первого кадр из видео в ЛС...")
                    from media_tools import extract_first_frame_async
                    file_to_analyze = await extract_first_frame_async(temp_path, timeout=60)
                    
                if file_to_analyze:
                    # ПЕРЕДАЕМ ИСТОРИЮ ЧАТА В ВИЖН-МОДЕЛЬ ДЛЯ КОНТЕКСТА
                    vision_caption = f"Caption: {text or ''}\nContext: {history_context_text[:1000]}"
                    media_description = await vision.describe_image(file_to_analyze, caption=vision_caption, is_passive=False)
                    
                # Удаляем статусное сообщение
                await bot_client.delete_messages(chat_id, status_msg.id)
            except Exception as e:
                logger.error(f"Error analyzing media in PM: {e}")
                media_error_shown = True
                if 'status_msg' in locals():
                    # Единственная строка, которой врач узнаёт, что снимок не
                    # открылся. Без границы по времени она сама висла: врач сидел
                    # перед «Скачиваю и анализирую…» до перезапуска процесса, замок
                    # на пользователя не отпускался, следующие его вопросы не
                    # обрабатывались вообще — и в журнале об этом ни строки,
                    # зависание не исключение и except его не ловит. tg_safety сам
                    # пишет WARNING с причиной и потраченным временем.
                    notified = await tg_safety.edit_message(
                        bot_client, chat_id, status_msg.id,
                        "❌ <i>Не удалось обработать файл. Попробуйте еще раз.</i>",
                        timeout=PM_STATUS_EDIT_TIMEOUT_SECONDS,
                        op="edit_message:pm_media_failed", logger=logger,
                        parse_mode='html',
                    )
                    if not notified.ok:
                        # Отметку снимаем: врач НЕ получил отказа, и ниже его обязан
                        # догнать обычный send_message, иначе файл провалится молча.
                        media_error_shown = False
                        logger.warning(
                            "Отказ разбора файла не доставлен chat_id=%s: %s — "
                            "врач не знает, что снимок не открылся",
                            chat_id, notified.reason,
                        )
            finally:
                # Очистка временных файлов.
                #
                # Здесь было os.path.exists(file_to_analyze) без проверки на
                # None, а extract_first_frame_async именно None и возвращает,
                # когда кадр вытащить не удалось. os.path.exists(None) бросает
                # TypeError — прямо из finally, поверх любой обработки. Итог:
                # видео в ЛС с неудачным извлечением кадра оставляло висеть
                # статус «Скачиваю и анализирую...» навсегда, и ответа не было.
                for path in {temp_path, locals().get('file_to_analyze')}:
                    if not path:
                        continue
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError as cleanup_err:
                        logger.warning("PM temp cleanup failed path=%s: %s", path, cleanup_err)

        # Анализ снимка мог не состояться: упало скачивание, не извлёкся кадр,
        # vision вернул пусто. Раньше здесь не было ни return, ни отметки об
        # этом: управление уходило в ТЕКСТОВУЮ ветку промпта, которая про
        # изображение ничего не знает. Врач получал "❌ не удалось обработать
        # файл", а следом — уверенный клинический ответ, который читается как
        # интерпретация присланного снимка. Молча подменять чтение рентгена
        # догадкой по тексту недопустимо.
        media_failed = has_media and not media_description
        if media_failed:
            logger.warning(
                f"Media analysis produced nothing for chat_id={chat_id}. "
                f"has_text={bool((text or '').strip())}"
            )
            if not (text or "").strip():
                # Отвечать не на что: снимка нет, вопроса тоже.
                if not locals().get("media_error_shown"):
                    await bot_client.send_message(
                        entity=chat_id,
                        message="❌ <i>Не смог открыть присланный файл — снимок не проанализирован. "
                                "Пришлите его ещё раз или опишите вопрос текстом.</i>",
                        parse_mode='html'
                    )
                return

        # Получаем стиль и долговременную клиническую память пользователя из БД (до 64 КБ)
        user_profile = await database.get_user_profile(chat_id)
        selected_style = user_profile.get("selected_style", "colleague_friendly")
        style_prompt_text = STYLE_PROMPTS.get(selected_style, STYLE_PROMPTS["colleague_friendly"])
        
        clinician_mem = await user_memory.get_clinician_memory(chat_id)
        portrait = user_memory.format_clinician_memory_prompt(chat_id, clinician_mem)
        
        # Если портрета и клинической памяти еще нет, запускаем первичную генерацию в фоне
        if not clinician_mem.get("clinical_summary") and not user_profile.get("profile_portrait"):
            async def _bg_portrait():
                try:
                    p_text = await generate_user_portrait(chat_id)
                    last_msg_id = await database.get_last_msg_id()
                    await database.set_user_portrait(chat_id, p_text, last_msg_id)
                    await database.save_user_memory(chat_id, clinical_summary=p_text, message_count=1)
                    logger.info(f"Generated and saved initial portrait/memory for user {chat_id}: {p_text}")
                except Exception as p_err:
                    logger.error(f"Error in bg portrait gen: {p_err}")
            
            import runtime_guard
            runtime_guard.create_task(_bg_portrait(), name=f"portrait_gen_{chat_id}")
            portrait = "Клинический профиль доктора формируется."

        # Получаем недавние сообщения пользователя из группы
        user_group_messages = await database.get_user_recent_group_messages(chat_id, limit=15)
        group_msgs_str = "\n".join([f"- {m}" for m in user_group_messages]) if user_group_messages else "(нет сообщений в группе)"
            
        # 4. RAG-поиск по стоматологической базе знаний с учетом контекста переписки
        # Собираем текст текущего запроса и последних 3 сообщений ВРАЧА для детекции
        # клинической темы. search_context_text (без реплик бота) определён выше —
        # почему именно так, написано там же.
        full_context_str = (text or "") + " " + (media_description or "") + " " + search_context_text
        full_context_str_lower = full_context_str.lower()
        
        # Клиническая тема — по словам, а не подстрокой (см. has_dental_term):
        # подстрочная проверка на 20 000 живых сообщений давала 1291 лишнее
        # срабатывание, из них 901 из-за «кт» внутри «кто» и «эффективно».
        has_dental_topic = has_dental_term(full_context_str)
        
        # Запрос ссылок, архива, опыта коллег или поиска в чате
        has_search_or_link_intent = any(k in full_context_str_lower for k in ["ссылк", "чат", "где писали", "кто говорил", "поиск", "найти", "источник"])
        
        # Извлекаем ключевые слова из всего контекста (текущий запрос + медиа + история), чтобы искать статьи
        keywords = extract_keywords(full_context_str)
                    
        wiki_corpus, archive_corpus = "", ""
        if has_dental_topic or has_media or has_search_or_link_intent:
            # Ищем совпадения в стоматологической базе
            search_keywords = select_search_keywords(keywords)
            wiki_corpus, archive_corpus = await search_knowledge_corpus(search_keywords)

        raw_chat_id = str(getattr(config, "SOURCE_CHAT_ID", "") or "")
        clean_chat_id = raw_chat_id[4:] if raw_chat_id.startswith("-100") else raw_chat_id.lstrip("-")

        # 5. Сборка индивидуального глубокого промпта
        if media_description:
            prompt = f"""
Ты — старший врач-консультант, ведущий эксперт клинического консилиума сообщества "StomChat".
Твой собеседник — ДИПЛОМИРОВАННЫЙ ВРАЧ-СТОМАТОЛОГ. Пациентов в диалоге нет. Общение строго на равных («Врач — Врачу») на академическом профессиональном языке (биологическая ширина, BOPT, IDS, феррул, торк, торсионная усталость файлов, PAI, гипохлоритная авария, адгезивные протоколы).
{style_prompt_text}

Клинический портрет собеседника:
{portrait}

Недавние сообщения собеседника в общем чате (поможет понять его клинический фокус):
{group_msgs_str}

История вашего диалога (контекст):
{chr(10).join(_fit_pm_history(context_msgs)) if context_msgs else "(история пуста)"}

Описание изображения (распознано Vision-моделью):
{media_description}

Вопрос или подпись пользователя:
{text or "(без подписи)"}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии!]

Похожие обсуждения из Архива и чата:
{archive_corpus}

КРИТИЧЕСКИЕ ИНСТРУКЦИИ ДЛЯ КОНСИЛИУМА:
1. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ПАЦИЕНТСКИХ ДИСКЛЕЙМЕРОВ:
   - Полностью исключи любые фразы вида: «обратитесь к врачу», «необходим очный осмотр», «я всего лишь ИИ», «диагноз ставит только очный врач».
   - Запрещено отмахиваться от снимков фразами «по 2D снимку сказать нельзя, сделайте КТ/КЛКТ». Выжимай максимум клинической информации из предоставленного изображения! Ограничения проекции описывай профессионально в дифдиагнозе (наложение корней, проекционные искажения).
2. СТРУКТУРА КЛИНИЧЕСКОГО РАЗБОРА:
   - Анатомический и рентгенологический статус: уровень костной ткани (резорбция, кортикальная пластинка), периодонтальная щель, состояние апикального периодонта (PAI), плотность и рабочая длина обтурации, нависающие края пломб/коронок, прилегание уступов.
   - Дифференциальный диагноз: 2–3 наиболее вероятные патологии с анатомическим обоснованием.
   - EBM-тактика и пошаговый протокол лечения: конкретные препараты, концентрации (NaOCl 3-5% с УЗ-активацией, Ca(OH)2), силеры, материалы и последовательность действий.
   - Ссылки на опыт чата: если в блоке обсуждений есть опыт коллег, сошлись на него. Если уместно или коллега спрашивает ссылку — давай прямую ссылку в формате: https://t.me/c/{clean_chat_id}/<msg_id> или упоминай сообщение #<msg_id>.
3. АКТИВНОЕ ВЕДЕНИЕ КОНСИЛИУМА (MULTI-TURN RETENTION):
   - В конце клинического разбора задай 1–2 точечных профессиональных вопроса коллеге для выбора окончательной тактики (сохранность феррула по высоте/толщине, глубина периодонтального зондирования, реакция на перкуссию/термометрию, проходимость апикальной дельты, биотип десны).
   - Запрещены банальные пустые фразы вроде «чем еще помочь?». Вопрос должен быть строго клиническим, как на реальном врачебном консилиуме.
4. ФОРМАТ И ТОН: Тон: сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности, без пустых вводных слов и без эмодзи-кривляния.
5. ДЛИНА ОТВЕТА: {length_guideline}
6. РАЗМЕТКА: Только HTML-теги — <b>жирный</b>. Никакого Markdown (**текст**, ## заголовки).
7. СМАЙЛИКИ: Никаких смайликов и эмодзи.
8. ЕСЛИ НА ИЗОБРАЖЕНИИ НЕ КЛИНИЧЕСКИЙ СЛУЧАЙ (а мем, котик, иконка архива, скриншот загрузки или интерфейс): не пытайся анализировать это как зубы, а спокойно и достойно прокомментируй по факту.
9. МЕТА-ПРАВИЛО: Категорически запрещено обсуждать разработку бота, триггеры, команды, админов или притворяться живым участником чата.
"""
        else:
            # Определяем тип запроса: это клинический вопрос или свободная тема
            has_clinical_topic = has_dental_topic or bool(wiki_corpus)
            if has_clinical_topic:
                system_role = f"""Ты — старший врач-консультант, ведущий эксперт клинического консилиума сообщества "StomChat". Общаешься с дипломированным врачом-стоматологом в личных сообщениях.
{style_prompt_text}

Клинический портрет собеседника:
{portrait}

Недавние сообщения собеседника в общем чате:
{group_msgs_str}
"""
                instructions = f"""КРИТИЧЕСКИЕ ИНСТРУКЦИИ ДЛЯ КОНСИЛИУМА:
1. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ПАЦИЕНТСКИХ ДИСКЛЕЙМЕРОВ: Никаких «обратитесь к очному врачу», «нужен очный осмотр», «как ИИ...». Твой собеседник — врач-стоматолог. Диалог ведется строго на уровне высококвалифицированного консилиума «Врач — Врачу».
2. ГЛУБИНА И СУТЬ: Если задан клинический вопрос, дай четкий, научно обоснованный (EBM) ответ: патогенез, пошаговый протокол действий, возможные ошибки и нюансы. Если реплика короткая — ответь коротко и по делу.
3. ССЫЛКИ НА СООБЩЕНИЯ ИЗ ЧАТА: В блоке обсуждений указаны сообщения чата вида [Сообщение #ID от Имя]: текст. Если коллега спрашивает ссылку, где это обсуждалось, или кто делился опытом — давай прямую ссылку: https://t.me/c/{clean_chat_id}/<msg_id> или ссылайся на пост #<msg_id>.
4. ДЛИНА ОТВЕТА: {length_guideline}
5. ФОРМАТ И ТОН: Никаких приветствий, вводных ("Отличный вопрос!"), концовок ("Успехов!") и канцеляризмов. Тон: сдержанный, академичный, уважительный старший коллега-эксперт.
6. РАЗМЕТКА: Только HTML-теги — <b>жирный</b>. Никакого Markdown.
7. СМАЙЛИКИ: Никаких смайликов и эмодзи.
8. НАУЧНАЯ ТОЧНОСТЬ: Опирайся на золотые стандарты стоматологии и доказательную медицину.
9. КОНТЕКСТ: Учитывай всю историю диалога.
10. АКТИВНОЕ ВЕДЕНИЕ КОНСИЛИУМА (MULTI-TURN RETENTION):
    - В конце разбора сложного клинического протокола задай 1 точечный профессиональный вопрос коллеге по применяемым материалам или этапам (например, марка цемента, дизайн уступа, толщина композитной стенки).
11. МЕТА-ПРАВИЛО: Категорически запрещено обсуждать разработку бота, триггеры, команды, админов или притворяться живым участником чата."""
            else:
                system_role = f"""Ты — врач-стоматолог из чата "StomChat", ведёшь диалог с коллегой в личных сообщениях.
{style_prompt_text}

Клинический портрет собеседника:
{portrait}

Недавние сообщения собеседника в общем чате:
{group_msgs_str}
"""
                instructions = f"""КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Тон: сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности и без эмодзи-кривляния. Отвечай строго к месту, без цитирования лишней теории и без зацикливания.
2. ДЛИНА ОТВЕТА: {length_guideline}
3. Никаких вводных, фраз "Как ИИ...", "С уважением" and концовок. Начинай сразу с сути.
4. РАЗМЕТКА: Только HTML-теги — <b>жирный</b>. Никакого Markdown.
5. СМАЙЛИКИ: Никаких смайликов и эмодзи.
6. КРИТИЧЕСКОЕ ПРАВИЛО СОМНЕНИЯ: Если тебя спрашивают про незнакомый термин, аббревиатуру или концепцию, НЕ пытайся угадать её значение или агрессивно называть бредом/инфоцыганством. Вместо этого честно признай, что не встречал такое обозначение, и проактивно спроси у коллег, что под этим подразумевается.
7. МЕТА-ПРАВИЛО: Категорически запрещено обсуждать разработку бота, триггеры, команды, админов или притворяться живым участником чата, жалующимся на бота. Если просят помолчать — вежливо извинись одной фразой и умолкни.
8. Если тебя спрашивают о твоих возможностях, подробно и доброжелательно расскажи о следующем функционале:
   • <b>Общение в ЛС</b>:
     - 📚 Клинические вопросы — ищу ответы в базе данных 118 000+ сообщений врачей чата.
     - 📸 Анализ снимков — пришли рентген или фото, разберу через компьютерное зрение (Vision).
     - 🎤 Голосовые сообщения — можешь наговорить вопрос голосом, я его расшифрую и отвечу.
     - 💬 Память контекста — помню до {PM_HISTORY_LIMIT} последних сообщений, можно уточнять детали.
   • <b>Интерактивные функции (кнопки внизу или команды)</b>:
     - 📖 <b>Энциклопедия</b> (/wiki) — поиск статей по базе знаний стоматологии.
     - 🎮 <b>Клинический кейс</b> (/case) — интерактивная игра, где нужно вести диагностику пациента.
     - 🎲 <b>Викторина</b> (/quiz) — случайные профессиональные вопросы для проверки знаний.
     - 🧮 <b>Калькулятор</b> (/calc) — расчет доз анестетиков в карпулах.
     - ⭐ <b>Закладки</b> (/bookmarks) — сохраненные тобой полезные сообщения из чата.
     - 📊 <b>Статистика</b> (/stats) — аналитика по чату StomChat.
   • <b>Работа в общем чате StomChat</b>:
     - Реагирую на стоматологические вопросы, если в диалоге есть ключевые слова.
     - Отвечаю на обращения к "боту" в сдержанном профессиональном тоне.
     - Каждую ночь генерирую подробный дайджест со всеми важными обсуждениями."""

            prompt = f"""
{system_role}

История вашего диалога (последние сообщения):
{chr(10).join(_fit_pm_history(context_msgs))}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(не найдено — свободная беседа)"}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Похожие обсуждения из Архива чата:
{archive_corpus or ""}

{instructions}
"""

        if media_failed:
            # Текст есть — на него ответим, но модель обязана знать, что снимка
            # она не видела, и не имеет права его описывать или трактовать.
            prompt += """

[КРИТИЧЕСКОЕ ОГРАНИЧЕНИЕ: пользователь прислал изображение, но проанализировать его НЕ УДАЛОСЬ. Ты его НЕ ВИДЕЛ.
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО описывать содержимое снимка, интерпретировать его, ставить по нему диагноз или делать любые выводы о том, что на нём изображено.
Начни ответ с честного признания, что снимок не открылся, и попроси прислать его повторно. Затем, если в тексте есть отдельный вопрос, ответь только на него.]
"""

        logger.info(
            f"Processing deep PM query from chat_id={chat_id}. "
            f"Has media={has_media}. Media analyzed={bool(media_description)}."
        )
        
        # 6. Отправка статуса "печатает" и генерация с циклом рецензирования (до 2 ретраев)
        max_retries = 2
        reply_text = None
        current_prompt = prompt
        current_context_msgs = list(context_msgs)
        last_pm_reason = ""

        async with bot_client.action(chat_id, 'typing'):
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    logger.info(
                        "PM retry attempt %s/%s for chat_id=%s. Last rejection: %s",
                        attempt, max_retries, chat_id, last_pm_reason
                    )
                    # Изоляция контекста: старая история переписки часто путает LLM при смене темы.
                    # Оставляем только текущий вопрос и последнюю реплику.
                    if len(current_context_msgs) > 2:
                        current_context_msgs = current_context_msgs[-2:]
                    clean_history_str = chr(10).join(_fit_pm_history(current_context_msgs))
                    
                    current_prompt = f"""{prompt}

[КРИТИЧЕСКОЕ ЗАМЕЧАНИЕ РЕЦЕНЗЕНТА К ПРЕДЫДУЩЕМУ ЧЕРНОВИКУ (ПОПЫТКА {attempt})]:
Предыдущий вариант ответа отклонён рецензентом: "{last_pm_reason}".
НЕ ДОПУСКАЙ ЭТОЙ ОШИБКИ! Отвечай строго по текущему клиническому вопросу врача. Не смешивай темы, не используй неподтвержденные утверждения, не выдумывай несуществующие методики и не упоминай посторонние системы/материалы.
Актуальный контекст текущего вопроса:
{clean_history_str}
"""

                status_ctx = {"kind": "pm_chat", "chat_id": chat_id, "thinking_level": "MEDIUM"}
                response, error = await generate_gemini_text_async(current_prompt, status_ctx, timeout=90)
                
                if error:
                    logger.error("PM Gemini generation error on attempt %s: %s", attempt, error)
                    if attempt == max_retries:
                        await bot_client.send_message(
                            entity=chat_id,
                            message="❌ <i>Ошибка генерации ответа нейросетью. Пожалуйста, повторите запрос позже.</i>",
                            parse_mode='html'
                        )
                        return
                    continue
                    
                candidate_text = getattr(response, "text", None)
                if not candidate_text or not candidate_text.strip():
                    logger.warning("PM Gemini returned empty text on attempt %s.", attempt)
                    if attempt == max_retries:
                        return
                    continue
                    
                candidate_text = candidate_text.strip()

                # Проверка качества ответа рецензентом
                pm_ok, pm_reason = await check_response_quality(
                    current_context_msgs, candidate_text, invited=True, reference=wiki_corpus
                )
                last_pm_reason = pm_reason or ""

                if not pm_ok:
                    reason_lower = last_pm_reason.lower()
                    # Если отказ вызван исключительно эмодзи или поверхностной стилистикой, санируем черновик
                    if any(w in reason_lower for w in ("эмодз", "emoji", "смайл", "несерьез", "нервн")):
                        logger.info("PM response validator rejected draft due to emoji/tone (%s). Sanitizing and allowing.", pm_reason)
                        candidate_text = re.sub(r"[😅😂😎😤😏🤣🤡🙄]+", "", candidate_text).strip()
                        pm_ok = True
                    else:
                        logger.warning(
                            "PM response validator REJECTED draft chat_id=%s (attempt %s/%s): %s",
                            chat_id, attempt, max_retries, pm_reason
                        )

                if pm_ok:
                    reply_text = candidate_text
                    logger.info("PM response validator approved chat_id=%s on attempt %s: %s", chat_id, attempt, pm_reason)
                    break

            if not reply_text:
                # Все попытки исчерпаны, отправляем клинический fallback и обязательно сохраняем в БД
                fallback_msg = (
                    "👨‍⚕️ <i>Коллега, в данном клиническом вопросе недостаточно "
                    "вводных данных для однозначного и безопасного протокола. "
                    "Уточните детали (снимок/КЛКТ, точную локализацию, анамнез "
                    "или статус зуба), чтобы я мог дать выверенную рекомендацию.</i>"
                )
                await bot_client.send_message(
                    entity=chat_id,
                    message=fallback_msg,
                    parse_mode='html',
                )
                try:
                    await database.save_pm_message(chat_id, "Assistant", fallback_msg)
                except Exception as db_save_err:
                    logger.error("Failed to persist PM fallback reply in DB: %s", db_save_err)
                return

            reply_text = clean_html_formatting(reply_text)

            # Отправка развернутого ответа (Этап 1: Быстрый ответ)
            await send_message_chunks_async(
                bot_client,
                chat_id,
                reply_text,
                parse_mode='html'
            )
            await database.save_pm_message(chat_id, "Assistant", reply_text)
            logger.info(f"Successfully sent deep PM response to chat_id={chat_id}")

            # Асинхронное динамическое обновление клинической памяти о враче в БД (до 64 КБ)
            try:
                sender = getattr(event, "sender", None)
                sender_username = getattr(sender, "username", "") or ""
                sender_first_name = getattr(sender, "first_name", "") or ""
                user_msg_summary = text or (f"Снимок: {media_description}" if media_description else "")
                import runtime_guard
                runtime_guard.create_task(
                    user_memory.update_clinician_memory_async(
                        user_id=chat_id,
                        user_message=user_msg_summary,
                        bot_response=reply_text,
                        username=sender_username,
                        first_name=sender_first_name,
                    ),
                    name=f"update_clinician_memory_{chat_id}"
                )
            except Exception as mem_err:
                logger.debug(f"Could not spawn clinician memory update: {mem_err}")

            # Запуск асинхронного фонового аудита и дополнения (Этап 2: Только для клинических консультаций)
            if is_clinical_consultation_query(text, has_media, has_dental_topic) and not is_command:
                req_id = time.time()
                _ACTIVE_PM_REQUESTS[chat_id] = req_id
                user_q = text or (f"Клинический снимок: {media_description}" if media_description else "Клинический вопрос")
                asyncio.create_task(
                    _async_pm_supplement_job(
                        bot_client=bot_client,
                        chat_id=chat_id,
                        user_question=user_q,
                        initial_answer=reply_text,
                        req_id=req_id,
                    )
                )
            
    except Exception as e:
        logger.exception(f"Unexpected error in handle_private_message: {e}")


async def check_bot_mention_trigger(bot_client, event, msg_id, text, sender_first_name=None):
    if text and len(text) > 1500:
        text = text[:1500] + "..."

    """
    Срабатывает когда кто-то пишет 'бот' в чате.
    Этап 1: отправляет контекст в LLM с вопросом — стоит ли отвечать?
    Этап 2: если YES — генерирует живой ответ и отправляет (shadow mode пока не промотировано).
    """
    # 1. Проверяем глобальную критику / требование выключить бота
    if await check_and_apply_silence(event, text, getattr(event.message, 'reply_to_msg_id', None)):
        return True

    # Тишина проверяется и ЗДЕСЬ. Этот путь был четвёртым и единственным без
    # проверки, и вызывается он ровно тогда, когда основной ассистент промолчал,
    # — а при активной тишине тот молчит именно из-за неё. То есть флаг тишины
    # сам передавал управление пути, который его не смотрел.
    #
    # Замер по живому архиву, последовательность 2025-06-05: врач написал «Бот
    # очень назойливый мне не нравится», бот извинился и умолк на 4 часа, а через
    # 4 минуты 38 секунд реплика про ЧУЖОГО бота прошла регулярку упоминания. В
    # окне тишины 138 сообщений, 14 задевают регулярку — тринадцать попыток
    # нарушить только что данное обещание.
    if is_silenced(load_state(), "bot mention trigger"):
        return False

    BOT_MENTION_SHADOW_MODE = False  # Выкачено в боевой

    text_lower = (text or "").lower()
    # Триггер: упомянули "бот" во всех возможных падежах и числах (бот, бота, боту, ботом, боте, боты, ботов, ботам, ботами, ботах)
    bot_words = ["бот", "бота", "боту", "ботом", "боте", "боты", "ботов", "ботам", "ботами", "ботах"]
    if not any(w in text_lower.split() or text_lower == w for w in bot_words):
        # Ищем substring с границами слов и возможными окончаниями
        if not re.search(r'\bбот(а|у|ом|е|ы|ов|ам|ами|ах)?\b', text_lower):
            return False

    chat_id = event.chat_id

    try:
        # Используем fetch_dynamic_chat_context: base=12, max=40, gap=15 мин.
        # Было: LIMIT 6, плоский формат без структуры ответов.
        _bot_ctx, _, _ = await fetch_dynamic_chat_context(
            msg_id, reply_to_msg_id, base_limit=12, max_limit=40, max_gap_minutes=15
        )
        context_rows = await query_db_async(
            "SELECT sender_id, sender_name, text FROM messages WHERE msg_id <= ? ORDER BY msg_id DESC LIMIT 12",
            (msg_id,)
        )
        context_rows = context_rows[::-1]  # хронологический порядок
        # Используем богатый формат из fetch для prompts, плоский context_rows только для sender_ids
        context_str = "\n".join(_bot_ctx) if _bot_ctx else "\n".join(f"{r[1]}: {r[2]}" for r in context_rows if r[2])

        # ЭТАП 1: Спросить LLM — стоит ли отвечать?
        triage_prompt = f"""Ты — ИИ-ассистент в стоматологическом Telegram-чате StomChat.
Кто-то написал слово "бот" в переписке. Вот контекст:

{context_str}

Реши: стоит ли боту вступить в разговор с живым ответом?

Отвечай строго одним словом:
YES — если человек обращается к боту, задаёт вопрос, хочет чем-то помочь, или ждёт реакции.
NO — если это случайное упоминание, обсуждение другого бота, ругательство, или контекст никак не требует реакции бота.
"""
        triage_ctx = {"kind": "bot_mention_triage", "chat_id": chat_id, "thinking_level": "LOW"}
        triage_resp, triage_err = await generate_gemini_text_async(triage_prompt, triage_ctx, timeout=8)

        if triage_err or not triage_resp:
            logger.warning(f"Bot mention triage failed: {triage_err}")
            return False

        decision = (getattr(triage_resp, "text", "") or "").strip().upper()
        logger.info(f"Bot mention triage decision: {decision!r} for msg_id={msg_id}")

        if not decision.startswith("YES"):
            return False

        # Calculate context-based length guidelines
        recent_texts = [r[2] for r in context_rows if r[2]]
        length_guideline = calculate_context_length_guidelines(recent_texts)

        # Подгружаем накопленную память беседы об участниках диалога
        sender_ids = [r[0] for r in context_rows if r[0]]
        users_chunk_context = await user_memory.format_users_chunk_context(sender_ids)

        # RAG lookup for bot mention (so bot has knowledge when answering clinical questions)
        mention_keywords = extract_keywords(text or "")
        mention_wiki, mention_archive = await search_knowledge_corpus(mention_keywords[:12]) if mention_keywords else ("", "")

        # ЭТАП 2: Сгенерировать живой ответ
        reply_prompt = f"""Ты — опытный стоматолог-практик и живой участник чата StomChat. 
Тебя только что позвали или упомянули в чате. Вот контекст переписки:

{context_str}

{users_chunk_context}

Справка из Базы Знаний (stomat_wiki):
{mention_wiki or "(нет данных)"}
[КРИТИЧЕСКОЕ ПРАВИЛО: Игнорируй факты из справки, не относящиеся к вопросу. Фильтруй через EBM!]

Задачи:
1. Ответь сдержанно, коротко и по делу.
2. Тон: сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности и без эмодзи-кривляния. Без канцелярщины.
3. Длина: {length_guideline}
4. Разметка: только HTML <b>жирный</b>.
5. КРИТИЧЕСКОЕ ПРАВИЛО СОМНЕНИЯ: Если тебя спрашивают про незнакомый термин, аббревиатуру или концепцию, НЕ пытайся угадать её значение или агрессивно называть бредом/инфоцыганством. Вместо этого честно признай, что не встречал такое обозначение, и проактивно спроси у коллег, что под этим подразумевается.
6. ПРОАКТИВНОСТЬ: Если непонятно чего хотят, не хватает данных или ты сомневаешься — честно признай это и переспрашивай.
7. ЕСЛИ ТЕБЯ СПРАШИВАЮТ "что ты умеешь", коротко перечисли функционал (анализ снимков, энциклопедия, кейсы) и позови в ЛС.
8. [КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка содержит живые чаты, где могут быть ошибки. Фильтруй через EBM и здравый смысл!]
9. МЕТА-ПРАВИЛО: Категорически запрещено обсуждать разработку бота, триггеры, команды, админов или притворяться живым участником чата, жалующимся на бота. Если просят помолчать — вежливо извинись одной фразой и умолкни.

ВАЖНОЕ ПРАВИЛО ФОКУСА — ОТВЕТ СТРОГО НА ВЫДЕЛЕННОЕ СООБЩЕНИЕ:
Вся переписка выше дана тебе исключительно для понимания контекста!
Твой ответ должен быть направлен СТРОГО на сообщение #{msg_id} от {sender_first_name or "коллеги"}.
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО отвечать по очереди на все реплики истории или пересказывать переписку.
Твой ответ — это естественная реакция именно на сообщение #{msg_id}!
"""
        reply_ctx = {"kind": "bot_mention_reply", "chat_id": chat_id, "thinking_level": "HIGH"}
        reply_resp, reply_err = await generate_gemini_text_async(reply_prompt, reply_ctx, timeout=60)

        if reply_err or not reply_resp:
            logger.warning(f"Bot mention reply generation failed: {reply_err}")
            return False

        reply_text = (getattr(reply_resp, "text", "") or "").strip()
        if not reply_text:
            return False

        # Прямое обращение к боту в ОБЩЕМ чате: ответ читают все 749 коллег, и
        # рецензента здесь не было. invited=True — врач позвал сам и ждёт
        # ответа, поэтому при недоступном рецензенте пропускаем с
        # предупреждением; явный отказ глушит черновик.
        # Контекст здесь собран строкой (context_str), списка реплик в этой
        # функции нет — отдаём строку одним элементом.
        mention_ok, mention_reason = await check_response_quality(
            [context_str] if context_str else [],
            reply_text, invited=True, reference=mention_wiki,
        )
        if not mention_ok:
            logger.warning(
                "Bot mention validator REJECTED draft msg_id=%s: %s", msg_id, mention_reason
            )
            return False
        logger.info("Bot mention validator approved msg_id=%s: %s", msg_id, mention_reason)

        reply_text = clean_html_formatting(reply_text)

        if BOT_MENTION_SHADOW_MODE:
            write_to_shadow_log(
                f"[BOT_MENTION] msg_id={msg_id} sender={sender_first_name}\n"
                f"Context:\n{context_str}\n"
                f"Triage: {decision}\nReply:\n{reply_text}\n---"
            )
            logger.info(f"[SHADOW] Bot mention reply logged (not sent): {reply_text[:80]}")
            return False
        else:
            try:
                await bot_client.send_message(
                    entity=chat_id,
                    message=reply_text,
                    reply_to=msg_id,
                    parse_mode='html'
                )
                logger.info(f"Bot mention reply sent to chat {chat_id}, msg_id={msg_id}")
                return True
            except Exception as send_err:
                logger.error(f"Failed to send bot mention reply: {send_err}")
                return False

    except Exception as e:
        logger.exception(f"Unexpected error in check_bot_mention_trigger: {e}")


async def handle_group_summary(bot_client, event, reply_to_msg_id):

    """Сборка саммари обсуждения в группе по запросу."""
    chat_id = event.chat_id
    msg_id = event.message.id
    
    cooldown = check_user_cooldown(chat_id, event.sender_id, "summary", seconds=30)
    if cooldown > 0:
        await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста, подождите {cooldown} сек перед использованием команды.", reply_to=msg_id)
        return
        
    status_msg = await bot_client.send_message(entity=chat_id, message="📝 <i>Собираю и анализирую историю обсуждения... Подождите.</i>", reply_to=msg_id, parse_mode='html')
    
    try:
        # Область сводки. Параметр reply_to_msg_id передавался вызывающей
        # стороной и НЕ ИСПОЛЬЗОВАЛСЯ: врач отвечал «/итог» на конкретный спор,
        # а получал выжимку последних тридцати сообщений чата — часто про
        # совсем другое. Указанное сообщение задаёт начало ветки.
        if reply_to_msg_id:
            rows = await database.get_messages_from(reply_to_msg_id, limit=SUMMARY_THREAD_LIMIT)
            scope_note = "с указанного сообщения"
        else:
            rows = await database.get_last_n_messages(limit=SUMMARY_RECENT_LIMIT)
            scope_note = f"последние {SUMMARY_RECENT_LIMIT} сообщений"

        chat_rows = [r for r in rows if r[3] and r[3].strip()]

        # Ответ на сообщение, которого нет в базе (старше бота), не должен
        # оставлять врача без сводки — откатываемся к последним репликам.
        if not chat_rows and reply_to_msg_id:
            rows = await database.get_last_n_messages(limit=SUMMARY_RECENT_LIMIT)
            chat_rows = [r for r in rows if r[3] and r[3].strip()]
            scope_note = f"последние {SUMMARY_RECENT_LIMIT} сообщений"

        if not chat_rows:
            # И на отказе граница нужна: без неё задача висит навсегда на
            # попытке сказать врачу, что сводки не будет.
            await tg_safety.edit_message(
                bot_client, chat_id, status_msg.id,
                "❌ <i>Не удалось найти сообщения для саммари.</i>",
                timeout=SUMMARY_DELIVERY_TIMEOUT_SECONDS,
                op="edit_message:group_summary_empty", logger=logger,
                parse_mode='html',
            )
            return
            
        history_msgs = []
        for r in chat_rows:
            history_msgs.append(f"{r[1] or 'Врач'}: {r[3]}")
            
        history_str = "\n".join(history_msgs)
        
        prompt = f"""
Ты — старший научный редактор и эксперт-клиницист стоматологического сообщества "StomChat".
Проанализируй следующую дискуссию врачей-стоматологов и сделай краткую, профессиональную выжимку.

История переписки:
{history_str}

Задачи:
1. Суть спора или обсуждаемого клинического вопроса (1-2 предложения).
2. Выдели основные точки зрения/аргументы участников (кратко, тезисно).
3. Клиническая рекомендация на основе доказательной стоматологии (каков золотой стандарт решения этого вопроса).

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
- Никакой воды, приветствий и концовок. Начинай сразу со структуры.
- Разметка: только HTML (<b>жирный</b>, <i>курсив</i>). Никакого Markdown.
- Будь краток: вся сводка должна занимать не более 800 символов.
- КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: История переписки может содержать ошибки и галлюцинации участников. Клиническую рекомендацию формулируй ТОЛЬКО на основе EBM и золотых стандартов стоматологии, не копируй сомнительные утверждения из чата.
"""
        status_ctx = {"kind": "group_summary", "chat_id": chat_id, "thinking_level": "HIGH"}
        response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
        
        if error or not response or not getattr(response, "text", None):
            await tg_safety.edit_message(
                bot_client, chat_id, status_msg.id,
                "❌ <i>Ошибка генерации саммари. Пожалуйста, попробуйте позже.</i>",
                timeout=SUMMARY_DELIVERY_TIMEOUT_SECONDS,
                op="edit_message:group_summary_genfail", logger=logger,
                parse_mode='html',
            )
            return
            
        summary_text = response.text.strip()
        summary_text = clean_html_formatting(summary_text)
        
        # Область анализа называем прямо: иначе непонятно, что именно разобрано.
        final_text = (
            f"📋 <b>Результаты клинического анализа дискуссии</b>\n"
            f"<i>Разобрано: {scope_note} ({len(chat_rows)} реплик)</i>\n\n{summary_text}"
        )
        # Единственный путь, которым готовая сводка попадает врачу. Без границы
        # по времени зависший Telegram означал: сводка собрана и потеряна, врач
        # сидит перед «Собираю и анализирую... Подождите» до бесконечности, и в
        # журнале об этом ни строки — зависание не исключение, except ниже его
        # не ловит. tg_safety сам пишет WARNING с причиной и потраченным временем.
        delivered = await tg_safety.edit_message(
            bot_client, chat_id, status_msg.id, final_text,
            timeout=SUMMARY_DELIVERY_TIMEOUT_SECONDS,
            op="edit_message:group_summary", logger=logger, parse_mode='html',
        )
        if not delivered.ok:
            logger.warning(
                "Сводка собрана (%d реплик, %d символов), но НЕ доставлена в "
                "chat_id=%s: %s. Врач остался с сообщением «Подождите» и не "
                "узнает, что ответа не будет",
                len(chat_rows), len(final_text), chat_id, delivered.reason,
            )
            return
        logger.info(f"Successfully posted group summary for chat_id={chat_id}")
    except Exception as e:
        logger.error(f"Error generating group summary: {e}")
        # Последнее слово врачу — тоже под сроком: иначе задача повисает уже
        # внутри обработчика ошибки, и врач не получает даже отказа. Наружу
        # отсюда не пускаем ничего: иначе вызывающий (main.py:2084) сочтёт
        # команду необработанной и следом отвечать полезет пассивный ассистент.
        try:
            await tg_safety.edit_message(
                bot_client, chat_id, status_msg.id,
                "❌ <i>Произошла неожиданная ошибка при составлении сводки.</i>",
                timeout=SUMMARY_DELIVERY_TIMEOUT_SECONDS,
                op="edit_message:group_summary_error", logger=logger,
                parse_mode='html',
            )
        except Exception as notify_err:
            logger.error(
                "Не удалось сообщить врачу об ошибке сводки chat_id=%s: %s",
                chat_id, notify_err,
            )


async def handle_group_direct_ask(bot_client, event, question):
    """Ответ на прямой клинический вопрос пользователя в группе."""
    chat_id = event.chat_id
    msg_id = event.message.id
    
    cooldown = check_user_cooldown(chat_id, event.sender_id, "direct_ask", seconds=30)
    if cooldown > 0:
        await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста, подождите {cooldown} сек перед использованием команды.", reply_to=msg_id)
        return
        
    # Получаем стиль отправителя для применения его предпочтений в группе
    user_profile = await database.get_user_profile(event.sender_id)
    selected_style = user_profile.get("selected_style", DEFAULT_STYLE)
    style_instruction = style_instruction_block(selected_style)

    async with bot_client.action(chat_id, 'typing'):
        keywords = extract_keywords(question)
        wiki_corpus, archive_corpus = await search_knowledge_corpus(keywords[:12])
        
        prompt = f"""
Ты - опытный стоматолог-практик с 15-летней клинической историей, отвечаешь коллеге на вопрос в группе "StomChat".
Ответь кратко, экспертно и строго по существу.

Вопрос коллеги:
{question}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Похожие обсуждения из Архива чата:
{archive_corpus}

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Максимально 600 символов. Никаких приветствий, обращений и пожеланий. Сразу ответ.
2. Тон: сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности и без эмодзи-кривляния. Полностью исключи высокомерие, поучения, сарказм и подколки.
3. СМАЙЛИКИ: Никаких смайликов и эмодзи.
4. Разметка: только HTML (<b>жирный</b>). Никакого Markdown.
5. Только проверенные клинические факты. Не выдумывай упрощенные практические советы (например, "бери любые штифты", "главное бренд X"), если они научно не доказаны. Если в базе нет точных данных по брендам или протоколам, напиши: "В нашей базе знаний нет точных сведений о Х, а клинические рекомендации советуют ориентироваться на...", без отсебятины.
6. ПРОАКТИВНОСТЬ: Если суть вопроса неясна, не хватает данных или ты сомневаешься - честно признай это и сам задай уточняющие вопросы (попроси КТ, снимок, симптоматику).
7. ДЛИНА ИЗ КОНТЕКСТА: адаптируйся под вопрос. Если можно ответить одной фразой - отвечай коротко. Не растягивай текст.
8. МЕТА-ПРАВИЛО: Категорически запрещено обсуждать разработку бота, триггеры, команды, админов или притворяться живым участником чата, жалующимся на бота. Если просят помолчать — вежливо извинись одной фразой и умолкни.

{style_instruction}
"""
        status_ctx = {"kind": "group_ask", "chat_id": chat_id, "thinking_level": "HIGH"}
        response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
        
        if error or not response or not getattr(response, "text", None):
            logger.warning("group direct ask generation failed chat=%s: %s", chat_id, error)
            await bot_client.send_message(
                entity=chat_id,
                message="⚠️ <i>Сейчас не получилось собрать ответ — модели недоступны. "
                    "Повторите вопрос через пару минут.</i>",
                reply_to=msg_id,
                parse_mode='html',
            )
            return
            
        reply_text = response.text.strip()

        # Публичный клинический ответ в общем чате: рецензента здесь не было.
        # invited=True — вопрос задан боту прямо.
        # Своего контекста у этой функции нет: рецензенту отдаём сам вопрос
        # врача — по нему и проверяется, относится ли ответ к делу.
        ask_ok, ask_reason = await check_response_quality(
            [f"Врач: {question}"] if question else [], reply_text,
            invited=True, reference=wiki_corpus
        )
        if not ask_ok:
            logger.warning("Group ask validator REJECTED draft msg_id=%s: %s", msg_id, ask_reason)
            await bot_client.send_message(
                entity=chat_id,
                message=("🤔 <i>Ответ собрался, но не прошёл мою же проверку на клиническую "
                         "обоснованность. Уточните вопрос или добавьте деталей.</i>"),
                reply_to=msg_id,
                parse_mode='html',
            )
            return
        logger.info("Group ask validator approved msg_id=%s: %s", msg_id, ask_reason)

        reply_text = clean_html_formatting(reply_text)

        # Добавляем ненавязчивую контекстную подсказку про ЛС с вероятностью 15%
        if random.random() < 0.15:
            reply_text += get_ad_hint(reply_text)

        try:
            await bot_client.send_message(
                entity=chat_id,
                message=reply_text,
                reply_to=msg_id,
                parse_mode='html'
            )
            logger.info(f"Sent group direct ask reply to msg_id={msg_id}")
        except Exception as e:
            logger.error(f"Failed to send group direct ask reply: {e}")


# Последний выданный ключ состояния викторины (по модулю, без знака).
_LAST_QUIZ_STATE_ID = 0


def _next_quiz_state_id():
    """
    Ключ состояния викторины, который НЕ МОЖЕТ повториться.

    Ключ был random.randint(100000, 999999) — 900 000 значений, при том что
    строки викторин из user_interactive_states не удаляются никогда. Задача о
    днях рождения: при 200 проведённых викторинах вероятность хотя бы одного
    совпадения 2.2%, при 500 — 12.9%, при 1000 — 42.6%, при 2000 — 89.2%.
    А совпадение — это не «редкая мелочь»: set_user_interactive_state делает
    INSERT OR REPLACE, поэтому новая викторина затирает строку старой. Сообщение
    старой викторины в чате живёт вечно и кнопки в нём остаются рабочими: клик по
    нему читает состояние НОВОЙ викторины и выдаёт врачу разбор чужого случая
    (explanation лежит в case_id той же строки), голос уходит в чужую
    статистику, а «вы уже проголосовали» срабатывает на тех, кто в новой
    викторине не голосовал.

    Микросекунды эпохи вместо случайного числа: значение монотонно, поэтому
    повтор невозможен и после перезапуска процесса. Счётчик нужен потому, что
    часы Windows идут крупными шагами — две викторины внутри одного тика получили
    бы одинаковое время; +1 гарантирует строгий рост и в этом случае.
    Знак минус сохранён по причине выше, величина ~1.8e15 на три порядка больше
    id супергрупп (~1.0e12) и свободно укладывается в 64-битный INTEGER sqlite.
    """
    global _LAST_QUIZ_STATE_ID
    candidate = int(time.time() * 1_000_000)
    if candidate <= _LAST_QUIZ_STATE_ID:
        candidate = _LAST_QUIZ_STATE_ID + 1
    _LAST_QUIZ_STATE_ID = candidate
    return -candidate


async def handle_group_quiz(bot_client, event):
    """Генерация и отправка клинической викторины с инлайн-кнопками в группу."""
    chat_id = event.chat_id
    msg_id = event.message.id
    
    cooldown = check_user_cooldown(chat_id, event.sender_id, "quiz", seconds=60)
    if cooldown > 0:
        await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста, подождите {cooldown} сек перед генерацией новой викторины.", reply_to=msg_id)
        return
        
    status_msg = await bot_client.send_message(entity=chat_id, message="🎲 <i>Конструирую клиническую задачу... Подождите.</i>", reply_to=msg_id, parse_mode='html')
    
    prompt = """
Ты — старший стоматолог-экзаменатор. Твоя задача — сгенерировать сложную клиническую задачу-викторину для группы врачей.
Выдай строго в формате JSON:
{
  "question": "Описание клинического случая и вопрос (до 300 символов)...",
  "options": ["Вариант A", "Вариант B", "Вариант C", "Вариант D"],
  "correct": 0,
  "explanation": "Объяснение правильного ответа (до 150 символов)..."
}
Ответ должен быть валидным JSON, без markdown разметки и без ```json.
"""
    status_ctx = {"kind": "group_quiz_gen", "chat_id": chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
    try:
        await bot_client.delete_messages(chat_id, status_msg.id)
    except Exception:
        pass
    
    question = None
    options = None
    correct = None
    explanation = None

    if not error and response and getattr(response, "text", None):
        try:
            raw_text = response.text.strip()
            start = raw_text.find("{")
            end = raw_text.rfind("}")
            if start != -1 and end != -1:
                raw_text = raw_text[start:end+1]
                
            data = json.loads(raw_text)
            q_candidate = str(data.get("question", "")).strip()
            opts_candidate = [str(option).strip() for option in data.get("options", [])]
            corr_candidate = int(data.get("correct", -1))
            expl_candidate = str(data.get("explanation", "")).strip()

            if len(opts_candidate) >= 4 and all(opts_candidate[:4]) and q_candidate and (0 <= corr_candidate <= 3):
                question = q_candidate
                options = opts_candidate[:4]
                correct = corr_candidate
                explanation = expl_candidate or "Правильный ответ!"
            else:
                logger.warning(f"Invalid quiz payload structure: {data}")
        except Exception as parse_err:
            logger.warning(f"Failed to parse quiz JSON: {parse_err}. Raw: {getattr(response, 'text', '')}")

    if not question or not options or correct is None or not explanation:
        logger.info("Selecting random clinical quiz from fallback pool for group quiz")
        fb = random.choice(CLINICAL_QUIZ_FALLBACKS)
        question = fb["question"]
        options = list(fb["options"])
        correct = fb["correct"]
        explanation = fb["explanation"]

    # Состояние викторины хранится в user_interactive_states, где ключ —
    # user_id. Диапазон 100000..999999 пересекается с id старых аккаунтов
    # Telegram: совпадение затёрло бы врачу его активный /case, а его /abort
    # убил бы живую викторину в группе. Отрицательные значения id пользователя
    # не бывают никогда.
    #
    # Само значение теперь строго возрастает по времени (_next_quiz_state_id), а
    # не выпадает из random.randint(100000, 999999).
    quiz_id = str(_next_quiz_state_id())
    init_votes = {"votes": [0, 0, 0, 0], "voters": {}}
    await database.set_user_interactive_state(
        user_id=int(quiz_id),
        state_type="quiz_config",
        current_step=correct,
        case_id=explanation[:200],
        history=json.dumps(init_votes)
    )
    
    from telethon import Button
    
    buttons = [
        [
            Button.inline(f"A: {options[0][:30]}", data=f"qa:{correct}:0:{quiz_id}"),
            Button.inline(f"B: {options[1][:30]}", data=f"qa:{correct}:1:{quiz_id}")
        ],
        [
            Button.inline(f"C: {options[2][:30]}", data=f"qa:{correct}:2:{quiz_id}"),
            Button.inline(f"D: {options[3][:30]}", data=f"qa:{correct}:3:{quiz_id}")
        ]
    ]
    
    message_text = (
        "🎲 <b>КЛИНИЧЕСКИЙ КЕЙС-ВИКТОРИНА</b>\n\n"
        f"{question}\n\n"
        f"<b>A:</b> {options[0]}\n"
        f"<b>B:</b> {options[1]}\n"
        f"<b>C:</b> {options[2]}\n"
        f"<b>D:</b> {options[3]}\n\n"
        "<i>Нажмите на кнопку с вашим вариантом ответа, чтобы проверить себя!</i>"
    )
    message_text = clean_html_formatting(message_text)
    
    await bot_client.send_message(
        entity=chat_id,
        message=message_text,
        buttons=buttons,
        parse_mode='html'
    )


# Сколько строк тянуть при запасном поиске по ключевым словам. Основной путь
# теперь листает базу постранично и в этот предел не упирается.
WIKI_FALLBACK_ROWS_PER_CODE = 15

# Энциклопедия: ГРУППИРОВКА кодов по кнопкам. Раздел -> (заголовок, подтемы),
# подтема -> (id, заголовок, коды рубрик).
#
# Что здесь своё, а что берётся из taxonomy. Своя — только группировка и
# надписи кнопок (эмодзи плюс короткая форма: на кнопке «🌉 Мосты», а в дереве
# «Мостовидные протезы и консоли»). НАБОР кодов своим быть перестал: он
# сверяется с taxonomy.LEAF_CODES, а листья, которых здесь не разложили руками,
# добираются автоматически ниже — с именами ИЗ taxonomy, чтобы никто не
# придумывал название клинического раздела.
#
# Зачем: этот список был ЧЕТВЁРТОЙ копией таксономии и разошёлся с выгрузкой в
# обе стороны (замер mode=ro 2026-07-29 по 12 784 фактам): код 6.1.2 (82 факта)
# был в кнопке, но не в дереве, а под кодами 8.1.1, 9.1.1, 10.1.1 не было ни
# одной кнопки — то есть первый же факт детской стоматологии или
# материаловедения стал бы для врача несуществующим. 51 факт не открывается ни
# одной кнопкой до сих пор, причина — в отчёте _fix_reachable.md.
#
# Зачем структура вместо трёх словарей. Раньше карта кодов лежала в файле
# дважды, карта названий подтем — тоже дважды, а списки кнопок были расписаны
# руками по разделам. Копии уже разъезжались: в одной осталось
# "gnat_joint": ["2.3.1", "2.3.2"], то есть «Окклюзия» была надмножеством
# «Сплинтов» — из 505 статей по сплинтам 461 показывалась в соседней кнопке.
#
# Что это закрывает по существу: в меню было 19 кодов из 52, и 3569 фактов
# (27.9% базы) не открывались НИ ОДНОЙ кнопкой. Целые темы с тысячами статей —
# фиксация и цементы, техника уступа, адгезия и IDS, фотопротокол, оптика,
# фармакология — существовали только для поиска, пролистать их было нельзя.
#
# Идентификатор подтемы ОБЯЗАН начинаться с идентификатора раздела: кнопка
# «Назад к подтемам» вычисляет раздел как subtopic_id.split("_")[0].
WIKI_TREE = {
    "endo": ("💧 Эндодонтия", [
        ("endo_access", "🔎 Доступ и поиск каналов", ["1.1.1"]),
        ("endo_files", "🔬 Инструментация и файлы", ["1.1.2"]),
        ("endo_irr", "💧 Ирригация каналов", ["1.1.3"]),
        ("endo_obt", "🩸 Обтурация каналов", ["1.1.4"]),
        ("endo_retreat", "🔁 Перелечивание каналов", ["1.1.5"]),
        ("endo_diag", "🩺 Диагностика в эндодонтии", ["1.1.6"]),
    ]),
    "rest": ("🧱 Реставрация", [
        ("rest_adh", "🧪 Адгезия и IDS", ["1.2.1"]),
        ("rest_alcohol", "💧 Спиртовой протокол", ["1.2.2"]),
        ("rest_morph", "🎨 Морфология и анатомия", ["1.2.3"]),
        ("rest_matrix", "📎 Матрицы и контактный пункт", ["1.2.4"]),
        ("rest_buildup", "🧷 Билдап и штифты", ["1.2.5"]),
        ("rest_polish", "✨ Полировка", ["1.2.6"]),
    ]),
    "perio": ("🩹 Пародонтология и гигиена", [
        ("perio_clean", "🪥 Профгигиена и GBT", ["1.3.1"]),
        ("perio_dis", "🩹 Болезни пародонта и SRP", ["1.3.2"]),
        ("perio_white", "🦷 Отбеливание", ["1.3.3"]),
        ("perio_plast", "🥩 Пластика десны и ССТ", ["3.3.1"]),
    ]),
    "ortho": ("👑 Ортопедия", [
        ("ortho_vin", "💎 Виниры", ["2.1.1"]),
        ("ortho_crown", "👑 Коронки", ["2.1.2"]),
        ("ortho_bridge", "🌉 Мосты", ["2.1.3"]),
        ("ortho_micro", "🔹 Микропротезирование", ["2.1.4"]),
        ("ortho_bopt", "🦷 BOPT и вертипреп", ["2.2.1"]),
        ("ortho_shoulder", "📐 Техника уступа", ["2.2.2"]),
        ("ortho_impr", "🥄 Оттиски", ["2.2.3"]),
        ("ortho_retr", "🧵 Ретракция десны", ["2.2.4"]),
        ("ortho_temp", "⏳ Временные конструкции", ["2.2.5"]),
        ("ortho_cement", "🧴 Фиксация и цементы", ["2.2.6"]),
    ]),
    "gnat": ("📐 Гнатология", [
        ("gnat_joint", "📐 Окклюзия", ["2.3.1"]),
        ("gnat_splint", "🦷 ВНЧС, сплинты и шины", ["2.3.2"]),
        ("gnat_artic", "⚙️ Артикуляторы", ["2.3.3"]),
    ]),
    "remov": ("🦷 Съёмное протезирование", [
        ("remov_full", "🦷 Полные протезы", ["2.4.1"]),
        ("remov_clasp", "🔗 Бюгельные протезы", ["2.4.2"]),
        ("remov_reline", "🔧 Перебазировка", ["2.4.3"]),
    ]),
    "surg": ("🔩 Хирургия и имплантация", [
        ("surg_rem", "🩸 Удаление зубов", ["3.1.1"]),
        ("surg_apic", "🔺 Апикальная хирургия", ["3.1.2"]),
        ("surg_save", "🛡 Зубосохраняющие операции", ["3.1.3"]),
        ("surg_impl", "🔩 Имплантация: планирование и системы", ["3.2.1", "3.2.2", "3.2.3"]),
        ("surg_compl", "⚠️ Осложнения имплантации", ["3.2.4"]),
        ("surg_bone", "🦴 Костная пластика и синус-лифтинг", ["3.3.2"]),
    ]),
    "odont": ("😬 Ортодонтия", [
        ("odont_brack", "🪢 Брекеты", ["4.1.1"]),
        ("odont_align", "🦷 Элайнеры", ["4.1.2"]),
        ("odont_diag", "🩺 Ортодонтическая диагностика", ["4.1.3"]),
    ]),
    "dig": ("🖥 Цифровая стоматология", [
        ("dig_scan", "📷 Сканеры", ["5.1.1"]),
        ("dig_exocad", "🖥 Exocad и моделирование", ["5.2.1"]),
        ("dig_print", "🖨 3D-печать", ["5.3.1"]),
    ]),
    "com": ("🔬 Оборудование и фармакология", [
        # 6.1.2 здесь НЕ перечислен: его добавляет ниже taxonomy.NAVIGATION_ALIASES,
        # потому что кода 6.1.2 в дереве знаний нет, а 82 факта под ним есть.
        ("com_optic", "🔬 Оптика и оборудование", ["6.1.1"]),
        ("com_pharm", "💊 Фармакология", ["6.2.1"]),
        ("com_photo", "📸 Фотопротокол", ["6.3.1"]),
    ]),
    "man": ("💼 Менеджмент клиники", [
        ("man_econ", "💰 Экономика и цены", ["7.1.1"]),
        ("man_legal", "⚖️ Юридические вопросы", ["7.2.1"]),
        ("man_psy", "🗣 Психология и общение с пациентом", ["7.3.1"]),
    ]),
}

# Эмодзи автоматически добранного раздела. Нейтральное намеренно: клинический
# смысл разделу приписывать нечего, имя берётся из taxonomy как есть.
_WIKI_AUTO_SECTION_EMOJI = "📂"


def _wiki_code_order(code):
    """Числовой порядок кода: иначе «10.1.1» встаёт перед «8.1.1» как строка."""
    return tuple(int(part) for part in code.split("."))


def _wiki_add_missing_leaves(tree):
    """Досыпает в дерево кнопок листья taxonomy, которых в нём не разложили.

    Кнопка обязана быть у КАЖДОГО кода таксономии. Иначе повторяется уже
    случившееся: коды 8.1.1 (детская стоматология), 9.1.1 (материаловедение) и
    10.1.1 (прочее) есть и в дереве, и в выгрузке, а кнопки под них не было — то
    есть первый же факт по детскому приёму лёг бы в базу и остался для врача
    несуществующим. Раздел и подтема называются ИМЕНАМИ ИЗ taxonomy: придумывать
    название клинического раздела нельзя, по нему врач сделает вывод о лечении.

    Возвращает id добавленных подтем — их перечисляет отчёт и проверяет тест.
    """
    covered = {code
               for _title, subs in tree.values()
               for _sub_id, _sub_title, codes in subs
               for code in codes}
    added = []
    for code in sorted(taxonomy.LEAF_CODES, key=_wiki_code_order):
        if code in covered:
            continue
        section = code.split(".")[0]
        cat_id = f"sec{section}"
        if cat_id not in tree:
            section_name = taxonomy.SECTION_NAMES.get(section) or f"раздел {section}"
            tree[cat_id] = (f"{_WIKI_AUTO_SECTION_EMOJI} {section_name.capitalize()}", [])
        # Подчёркивание в id подтемы значащее: «Назад к подтемам» берёт раздел
        # как split("_")[0], поэтому точки кода заменяются на подчёркивания
        # ПОСЛЕ префикса раздела и сам префикс их не содержит.
        sub_id = f"{cat_id}_{code.replace('.', '_')}"
        tree[cat_id][1].append((sub_id, taxonomy.LEAF_NAMES.get(code) or taxonomy.describe(code), [code]))
        added.append(sub_id)
    return added


WIKI_AUTO_SUBTOPICS = tuple(_wiki_add_missing_leaves(WIKI_TREE))

# Ниже — производные представления. Руками их не заполнять: любое расхождение
# с WIKI_TREE и есть тот дефект, из-за которого статьи двоились в кнопках.
WIKI_SUBTOPIC_CODES = {
    sub_id: codes
    for _title, subs in WIKI_TREE.values()
    for sub_id, _sub_title, codes in subs
}

# Коды вне дерева, под которыми в боевой вике лежат живые факты, показываются
# вместе с листом-родителем. Список объявлен в taxonomy
# (taxonomy.NAVIGATION_ALIASES), а не здесь: иначе это снова была бы своя карта
# кодов. Без этого 82 факта под кодом 6.1.2 не открывались бы ни одной кнопкой —
# ровно та потеря, из-за которой навигацию и свели с таксономией.
for _alias in taxonomy.NAVIGATION_ALIASES:
    _leaf = taxonomy.alias_leaf(_alias)
    for _sub_id, _codes in WIKI_SUBTOPIC_CODES.items():
        if _leaf in _codes and _alias not in _codes:
            _codes.append(_alias)
            break

WIKI_SUBTOPIC_NAMES = {
    sub_id: sub_title
    for _title, subs in WIKI_TREE.values()
    for sub_id, sub_title, _codes in subs
}

WIKI_CATEGORY_NAMES = {cat_id: title for cat_id, (title, _subs) in WIKI_TREE.items()}


def wiki_tree_errors():
    """Расхождения навигации с таксономией. Пусто = врач дойдёт до любого кода.

    Вызывается тестом, а не при импорте: расхождение рубрикатора — повод уронить
    проверку, а не бота. Уронив бота, мы отнимем у врача и те разделы, которые
    в порядке.
    """
    problems = []
    seen = {}
    for cat_id, (_title, subs) in WIKI_TREE.items():
        for sub_id, sub_title, codes in subs:
            if not sub_id.startswith(cat_id + "_"):
                problems.append(f"{sub_id}: id подтемы не начинается с «{cat_id}_» — "
                                f"кнопка «Назад к подтемам» уведёт в пустоту")
            if not sub_title.strip():
                problems.append(f"{sub_id}: подтема без надписи")
            for code in codes:
                if code in seen:
                    problems.append(f"{code}: код в двух кнопках ({seen[code]} и "
                                    f"{sub_id}) — одна статья живёт в двух разделах")
                seen[code] = sub_id
                if code in taxonomy.LEAF_CODES:
                    continue
                if code in taxonomy.NAVIGATION_ALIASES:
                    continue
                problems.append(f"{code}: кода нет ни в дереве знаний, ни в "
                                f"taxonomy.NAVIGATION_ALIASES — кнопка «{sub_title}» "
                                f"будет молча пустой")
    missing = sorted(set(taxonomy.LEAF_CODES) - set(seen), key=_wiki_code_order)
    if missing:
        problems.append(f"нет кнопки у кодов таксономии: {missing} — факты под ними "
                        f"врач не откроет никак")
    for alias in taxonomy.NAVIGATION_ALIASES:
        if alias not in seen:
            problems.append(f"{alias}: живой код вне дерева не попал ни в одну кнопку")
    return problems


def wiki_category_buttons(cat_id, counts=None):
    """
    Кнопки подтем раздела. Собираются из дерева, а не расписаны руками.

    counts — сколько статей в каждой подтеме. Число выносится на кнопку, потому
    что разброс огромный: «Отбеливание» это 18 статей, «Коронки» — 3734. Без
    числа врач выбирает наугад и не понимает, куда он попал: в подборку из двух
    десятков заметок или в раздел, который за вечер не пролистать.
    """
    from telethon import Button
    entry = WIKI_TREE.get(cat_id)
    if not entry:
        return [[Button.inline("⬅️ Назад к разделам", data="wiki_cat:topics")]]
    buttons = []
    for sub_id, sub_title, _codes in entry[1]:
        label = sub_title
        if counts:
            total = counts.get(sub_id)
            if total:
                label = f"{sub_title} · {total}"
        buttons.append([Button.inline(label, data=f"wiki_page:{sub_id}:0")])
    buttons.append([
        Button.inline("⬅️ Назад к разделам", data="wiki_cat:topics"),
        Button.inline("⬅️ Назад в меню", data="nav:main")
    ])
    return buttons


_WIKI_COUNT_CACHE = {}


async def wiki_subtopic_counts(cat_id):
    """
    Число статей по подтемам раздела — одним запросом на подтему.

    Считает SQL, в память ничего не тянется. Если базы нет или запрос упал,
    возвращаем пустой словарь: кнопки просто останутся без чисел, раздел
    открыться должен всё равно.

    Результат кэшируется на время жизни процесса: вики статична, её пересобирает
    отдельный дистиллятор в офлайне. Без кэша каждое нажатие кнопки раздела
    стоило 170-350 мс на пересчёт одних и тех же чисел.
    """
    if cat_id in _WIKI_COUNT_CACHE:
        return _WIKI_COUNT_CACHE[cat_id]

    entry = WIKI_TREE.get(cat_id)
    if not entry or not os.path.exists("stomat_wiki.db"):
        return {}

    def sync_count():
        result = {}
        with contextlib.closing(sqlite3.connect("stomat_wiki.db", timeout=10)) as conn:
            conn.execute("PRAGMA busy_timeout = 10000")
            for sub_id, _title, _codes in entry[1]:
                where, params = _wiki_code_filter(sub_id)
                if not where:
                    continue
                row = conn.execute(
                    f"SELECT COUNT(*) FROM distilled_facts WHERE {where} "
                    "AND content IS NOT NULL AND TRIM(content) <> ''",
                    params,
                ).fetchone()
                result[sub_id] = row[0] if row else 0
        return result

    try:
        counts = await asyncio.get_running_loop().run_in_executor(None, sync_count)
    except Exception as exc:
        logger.warning("wiki counts failed cat=%s: %s", cat_id, exc)
        return {}
    # Пустой результат не кэшируем: значит база была недоступна, и при следующем
    # нажатии стоит попробовать снова.
    if counts:
        _WIKI_COUNT_CACHE[cat_id] = counts
    return counts


def wiki_topic_buttons():
    """Кнопки разделов рубрикатора, по два в ряд."""
    from telethon import Button
    items = [Button.inline(title, data=f"wiki_cat:{cat_id}")
             for cat_id, (title, _subs) in WIKI_TREE.items()]
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    rows.append([
        Button.inline("⬅️ В энциклопедию", data="wiki_cat:back"),
        Button.inline("⬅️ Назад в меню", data="nav:main")
    ])
    return rows


def _wiki_code_filter(subtopic_id):
    """SQL-условие по кодам подтемы и параметры к нему.

    Отбор идёт ПО ГРАНИЦЕ ТОКЕНА (taxonomy.token_sql), а не подстрокой. Здесь
    стояло `category_code LIKE '%2.1.2%'`, и на сегодняшнем наборе кодов это не
    врало (замер mode=ro по всем 53 кодам кнопок: подстрока и токен дают
    ОДИНАКОВОЕ число фактов, расхождений 0) — но ловушка была заряжена. В базе
    99.1 % записей хранят СПИСОК кодов через запятую, а до реклассификации в ней
    жили коды глубже L3 (`1.3.10`, `2.2.3.1`, `11.1.1`). Замер на живой вике для
    L2-кода: `1.1` по границе токена — 1 118 фактов, подстрокой — 5 428, то есть
    4 310 ЧУЖИХ. Врач читает чужой раздел как свой, и это хуже пропажи: пропажу
    хотя бы видно.
    """
    codes = WIKI_SUBTOPIC_CODES.get(subtopic_id, [])
    if not codes:
        return None, []
    condition = taxonomy.token_sql("category_code")
    clause, params = [], []
    for code in codes:
        if not taxonomy.code_is_valid(code):
            # Кривой код молча набрал бы в кнопку чужие факты: '_' и '%' в
            # шаблоне LIKE — подстановочные знаки.
            logger.warning("wiki: код рубрики отброшен как недопустимый sub=%s code=%r",
                           subtopic_id, code)
            continue
        clause.append(condition)
        params.extend(taxonomy.token_patterns(code))
    if not clause:
        return None, []
    return f"({' OR '.join(clause)})", params


async def query_wiki_fact_page(subtopic_id, page_idx):
    """
    Одна статья подтемы и общее их число.

    Раньше подтема грузилась целиком через LIMIT 15 на код, и энциклопедия
    показывала 284 статьи из 12 784 — 2.2% базы. В разделе «Коронки и мосты»
    доступно 4149 статей, врач видел 29. Листание в SQL стоит 4 мс на самом
    крупном разделе, поэтому предела больше нет: пагинация ходит за одной
    строкой по OFFSET, а не тянет раздел в память на каждое нажатие кнопки.
    """
    where, params = _wiki_code_filter(subtopic_id)
    if not where or not os.path.exists("stomat_wiki.db"):
        return None, 0

    async def keyword_fallback():
        """Запасной поиск по словам, как было раньше: если по кодам пусто."""
        facts = await query_wiki_subtopic(subtopic_id)
        if not facts:
            return None, 0
        return facts[page_idx % len(facts)], len(facts)

    def sync_query():
        with contextlib.closing(sqlite3.connect("stomat_wiki.db", timeout=10)) as conn:
            conn.execute("PRAGMA busy_timeout = 10000")
            base = (f"FROM distilled_facts WHERE {where} "
                    f"AND content IS NOT NULL AND TRIM(content) <> '' GROUP BY content")
            total = conn.execute(f"SELECT COUNT(*) FROM (SELECT 1 {base})", params).fetchone()[0]
            if not total:
                return None, 0
            offset = page_idx % total
            row = conn.execute(
                f"SELECT content, MIN(id) AS ord {base} ORDER BY ord LIMIT 1 OFFSET ?",
                params + [offset],
            ).fetchone()
            return (row[0].strip() if row else None), total

    try:
        fact, total = await asyncio.get_running_loop().run_in_executor(None, sync_query)
    except Exception as e:
        logger.error(f"Error paging wiki subtopic {subtopic_id}: {e}")
        return await keyword_fallback()

    if not total:
        return await keyword_fallback()
    return fact, total


async def query_wiki_subtopic(subtopic_id):
    # Коды берём из WIKI_SUBTOPIC_CODES: второй копии здесь больше нет.
    #
    # Копия была, и она УЖЕ разъехалась. В ней осталось
    # "gnat_joint": ["2.3.1", "2.3.2"] — ровно то значение, которое модульный
    # словарь описывает как ИСПРАВЛЕННЫЙ дефект: «Окклюзия» была надмножеством
    # «Сплинтов», и из 505 статей по сплинтам 461 показывалась в соседней
    # кнопке. Правку внесли в один словарь из двух.
    #
    # Проявиться не успело: сюда попадают только когда поиск по кодам не дал
    # ничего, а сейчас факты есть у всех 14 подтем (проверено на живой вики).
    # То есть ловушка была заряжена на первый же случай, когда подтема опустеет.
    codes_map = WIKI_SUBTOPIC_CODES

    facts = []
    if os.path.exists("stomat_wiki.db"):
        try:
            conn = sqlite3.connect("stomat_wiki.db", timeout=10)
            c = conn.cursor()

            # 1. Try category code search
            codes = codes_map.get(subtopic_id, [])
            for code in codes:
                if not taxonomy.code_is_valid(code):
                    continue
                # Та же граница токена, что и в основном пути: запасной поиск не
                # имеет права показывать врачу другой набор статей.
                params = taxonomy.token_patterns(code) + (WIKI_FALLBACK_ROWS_PER_CODE,)
                c.execute("SELECT content FROM distilled_facts WHERE "
                          f"{taxonomy.token_sql('category_code')} LIMIT ?", params)
                for row in c.fetchall():
                    fact = row[0].strip()
                    if fact not in facts:
                        facts.append(fact)

            # 2. Fallback to keyword search if category code yields no results
            if not facts:
                keywords_map = {
                    "ortho_bopt": ["bopt", "уступ", "преп"],
                    "ortho_vin": ["винил", "вкладк", "накладк"],
                    "ortho_crown": ["коронка", "коронок", "мост", "протез"],
                    "endo_irr": ["гипохлорит", "хлоргексидин", "эдта", "ирригац"],
                    "endo_obt": ["гуттаперч", "силер", "обтурац"],
                    "endo_files": ["файл", "реципрок", "протейпер", "мту"],
                    "perio_dis": ["гингивит", "пародонт", "пародонтоз"],
                    "perio_clean": ["кюрет", "скалер", "чистк", "налет", "камень"],
                    "perio_plast": ["десна", "десны", "сст", "трансплантат"],
                    "surg_impl": ["имплант", "абатм", "формировател", "заглушк"],
                    "surg_rem": ["удален", "экстракц", "лунк"],
                    "surg_bone": ["синус", "остеот", "мембран", "биоосс", "аугмент"],
                    "gnat_joint": ["окклюз", "сустав", "внчс"],
                    "gnat_splint": ["сплинт", "капп", "шина"]
                }
                kws = keywords_map.get(subtopic_id, ["дентин"])
                for kw in kws:
                    _w, _p = like_any_case("content", kw)
                    c.execute("SELECT content FROM distilled_facts "
                              f"WHERE {_w} LIMIT 10", _p)
                    for row in c.fetchall():
                        fact = row[0].strip()
                        if fact not in facts:
                            facts.append(fact)
            conn.close()
        except Exception as e:
            logger.error(f"Error querying wiki subtopic: {e}")
    return facts


async def query_random_wiki_fact():
    fact = None
    if os.path.exists("stomat_wiki.db"):
        try:
            with contextlib.closing(sqlite3.connect("stomat_wiki.db", timeout=10)) as conn:
                c = conn.cursor()
                c.execute("SELECT content FROM distilled_facts ORDER BY RANDOM() LIMIT 1")
                row = c.fetchone()
                if row:
                    fact = row[0].strip()
        except Exception as e:
            logger.error(f"Error querying random wiki fact: {e}")
    return fact


async def edit_callback_message(bot_client, event, text, op, **kwargs):
    """
    Правка сообщения по нажатию инлайн-кнопки — обязательно под сроком.

    Спиннер на кнопке снимает event.answer(), а он стоит строкой НИЖЕ правки.
    Пока правка была без границы, зависший Telegram означал: врач смотрит на
    крутящуюся кнопку до 500 с (timeout=30 x request_retries=10, main.py:900-910),
    решает, что бот считает, и жмёт снова. Страховка в main.py:2315 тут не
    помогает: она ловит исключение, а зависание не исключение — await просто не
    возвращается, и finally не наступает.

    Исключение наружу не летит (tg_safety отдаёт TgOutcome), поэтому
    event.answer() после вызова выполняется в любом случае и кнопка гаснет.
    """
    if hasattr(event, 'edit') and callable(getattr(event, 'edit', None)):
        try:
            await event.edit(text, **kwargs)
        except TypeError:
            try:
                await event.edit(text)
            except Exception:
                pass
        except Exception:
            pass
    return await tg_safety.edit_message(
        bot_client, getattr(event, 'chat_id', None), getattr(event, 'message_id', None), text,
        timeout=CALLBACK_EDIT_TIMEOUT_SECONDS, op=op, logger=logger, **kwargs,
    )


async def handle_quiz_callback(bot_client, event):
    """
    Централизованный диспетчер навигационных колбэков и инлайн-кнопок.
    
    Маршруты:
      • nav:* / menu:* — переключение основных разделов бота (Главная, База Знаний, Квиз, Калькулятор, Закладки, Стиль, Протоколы, Статистика, Справка)
      • quiz:* — запуск, генерация и интерактивный ответ в викторинах
      • case:* — старт, управление и сброс клинического симулятора
      • calc:* — общий калькулятор анестезии и детальные карточки препаратов (Артикаин, Мепивакаин, Лидокаин)
      • style:* — меню выбора и сохранение стиля общения ассистента
      • bm:* — постраничная навигация по клиническим закладкам
      • proto:* — перечень и статьи клинических протоколов
      • wiki_cat:* / wiki_page:* / wiki_save:* — рубрикатор, статьи и сохранение в Энциклопедии
      • qa:* — ответы на групповые/опросные клинические кейс-викторины
    """
    data_bytes = getattr(event, "data", b"")
    if isinstance(data_bytes, str):
        data_str = data_bytes
    elif isinstance(data_bytes, bytes):
        data_str = data_bytes.decode('utf-8', errors='ignore')
    else:
        data_str = str(data_bytes or "")

    from telethon import Button

    # 1. ОБЩИЙ НАВИГАЦИОННЫЙ ДИСПЕТЧЕР nav:* И menu:*
    if data_str.startswith("nav:") or data_str.startswith("menu:"):
        nav_target = data_str.split(":", 1)[1]
        
        if nav_target in ("main", "home"):
            await edit_callback_message(
                bot_client, event, MAIN_MENU_TEXT,
                "edit_message:main_menu", buttons=build_main_menu_markup(),
                parse_mode='html', link_preview=False
            )
            await event.answer()
            return
            
        elif nav_target in ("wiki", "encyclopedia"):
            wiki_text = (
                "📖 <b>Интерактивная Стоматологическая Энциклопедия</b>\n\n"
                "Добро пожаловать в базу клинических знаний и протоколов StomChat. Здесь собраны проверенные стандарты доказательной стоматологии (12 000+ статей и фактов).\n\n"
                "👇 <i>Выберите интересующее действие:</i>"
            )
            buttons = [
                [Button.inline("📚 Обзор по разделам", data="wiki_cat:topics")],
                [Button.inline("🎲 Случайный факт", data="wiki_cat:random"), Button.inline("🔍 Поиск по базе", data="wiki_cat:search_info")],
                [Button.inline("📚 Клинические протоколы", data="nav:proto")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, wiki_text,
                                       "edit_message:wiki_menu", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif nav_target in ("web", "search_web"):
            web_info = (
                "🌐 <b>Поиск в сети и PubMed</b>\n\n"
                "Поиск актуальных зарубежных исследований, метаанализов и гайдлайнов в открытых научных источниках с проверкой доказательности.\n\n"
                "💡 <b>Как пользоваться:</b>\n"
                "Отправьте команду поиска со своим запросом, например:\n"
                "• <code>/web BOPT preparation technique success rate</code>\n"
                "• <code>/web vital pulp therapy MTA vs Biodentine</code>\n"
                "• <code>/web peri-implantitis treatment protocol 2025</code>\n"
                "• <code>погугли протокол вертипрепа</code>"
            )
            buttons = [[Button.inline("⬅️ Назад в меню", data="nav:main")]]
            await edit_callback_message(bot_client, event, web_info,
                                       "edit_message:nav_web", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif nav_target in ("calc", "anesthesia"):
            calc_msg = (
                "🧮 <b>Справочник-калькулятор анестезии</b>\n\n"
                "Пришлите препарат, концентрацию и вес — например "
                "<i>«артикаин 4%, ребёнок 20 кг»</i> — и я посчитаю с арифметикой на виду.\n\n"
                "<b>Предел всегда двойной: мг/кг И абсолютный максимум. Действует меньшее из двух.</b>\n\n"
                "• <b>Артикаин 4%</b> (1:100 000 / 1:200 000)\n"
                "  взрослые 7 мг/кг, дети 5 мг/кг, <b>но не более 500 мг</b>\n"
                "  карпула 1.7 мл = 68 мг → потолок ≈ 7 карпул\n"
                "  <i>потолок 500 мг наступает уже при весе ≈ 71 кг</i>\n\n"
                "• <b>Мепивакаин 3%</b> (без вазоконстриктора)\n"
                "  4.4 мг/кг, <b>но не более 400 мг</b>\n"
                "  карпула 1.8 мл = 54 мг → потолок ≈ 7 карпул\n"
                "  <i>потолок наступает при весе ≈ 91 кг</i>\n\n"
                "• <b>Лидокаин 2%</b> (с адреналином)\n"
                "  взрослые 7 мг/кг, дети 4.4 мг/кг, <b>но не более 500 мг</b>\n"
                "  карпула 1.8 мл = 36 мг → потолок ≈ 13 карпул\n"
                "  <i>потолок наступает при весе ≈ 71 кг</i>\n\n"
                "⚠️ <i>Это референсные максимумы для здорового пациента, а не рекомендация дозы. "
                "При сопутствующей патологии, у детей, беременных и пожилых предел ниже.</i>"
            )
            buttons = [
                [Button.inline("🦷 Артикаин 4%", data="calc:articaine"), Button.inline("💉 Мепивакаин 3%", data="calc:mepivacaine")],
                [Button.inline("🩸 Лидокаин 2%", data="calc:lidocaine")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, calc_msg,
                                       "edit_message:nav_calc", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif nav_target in ("proto", "protocols"):
            protocols_text = (
                "📚 <b>Основные клинические протоколы в Базе Знаний:</b>\n\n"
                "• <b>BOPT (Biologically Oriented Preparation Technique):</b> Концепция препарирования без уступа.\n"
                "• <b>Вертикальное препарирование:</b> Особенности ведения краев коронок, сохранение тканей.\n"
                "• <b>Травление керамики:</b> Протоколы работы с плавиковой кислотой и силанизацией (E.max, полевой шпат).\n"
                "• <b>Ирригация в эндодонтии:</b> Концентрации гипохлорита натрия, ЭДТА, протоколы активации (ультразвук, звуковая).\n"
                "• <b>Обтурация корневых каналов:</b> Методики латеральной конденсации и вертикальной горячей гуттаперчи.\n\n"
                "👇 <i>Выберите интересующий протокол ниже для детального изучения:</i>"
            )
            buttons = [
                [Button.inline("🦷 BOPT", data="proto:bopt"), Button.inline("🧪 Травление", data="proto:etching")],
                [Button.inline("💧 Ирригация", data="proto:irrigation"), Button.inline("🩸 Обтурация", data="proto:obturation")],
                [Button.inline("📐 Вертикальное препарирование", data="proto:vertical")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, protocols_text,
                                       "edit_message:proto_list", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif nav_target == "style":
            profile = await database.get_user_profile(event.sender_id)
            current_style = profile.get("selected_style", "colleague_friendly")
            style_names = {
                "colleague_friendly": "Коллега-эксперт 🤝",
                "clinical_dry": "Сухие факты 📝",
                "humor_cynic": "Ироничный циник 💀"
            }
            curr_style_name = style_names.get(current_style, "Неизвестный")
            style_welcome = (
                "⚙️ <b>Настройка стиля общения</b>\n\n"
                f"Текущий стиль общения: <b>{curr_style_name}</b>\n\n"
                "Выберите стиль, в котором я буду отвечать вам в личных сообщениях:"
            )
            style_buttons = [
                [Button.inline("Коллега-эксперт 🤝 (по умолчанию)", data="style:colleague_friendly")],
                [Button.inline("Сухие факты 📝 (строго, без шуток)", data="style:clinical_dry")],
                [Button.inline("Ироничный циник 💀 (черный юмор)", data="style:humor_cynic")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, style_welcome,
                                       "edit_message:nav_style", buttons=style_buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif nav_target in ("bookmarks", "saved"):
            total_items = await database.count_clinical_bookmarks(event.sender_id)
            if not total_items:
                bm_text = (
                    "⭐ <b>Ваши клинические закладки</b>\n\n"
                    "У вас пока нет сохраненных записей.\n\n"
                    "💡 <i>Чтобы добавить запись в закладки:</i>\n"
                    "• В общем чате: ответьте на полезное клиническое сообщение командой <code>/save</code>\n"
                    "• В энциклопедии: нажмите кнопку <b>«⭐ В закладки»</b> при чтении статьи.\n\n"
                    "Для просмотра списка закладок используйте команду <code>/bookmarks</code>."
                )
                buttons = [
                    [Button.inline("📖 В Базу Знаний", data="nav:wiki")],
                    [Button.inline("⬅️ Назад в меню", data="nav:main")]
                ]
                await edit_callback_message(bot_client, event, bm_text,
                                           "edit_message:nav_bookmarks_empty", buttons=buttons,
                                           parse_mode='html')
                await event.answer()
                return

            per_page = 5
            total_pages = max(1, (total_items + per_page - 1) // per_page)
            rows = await database.get_clinical_bookmarks(event.sender_id, limit=per_page, offset=0)
            
            bm_text = f"⭐ <b>Ваши клинические закладки (Страница 1/{total_pages}):</b>\n\n"
            for idx, row in enumerate(rows, 1):
                msg_id, chat_id_val, sender_name, msg_text, media_desc, date = row
                snip = _bookmark_snippet(msg_text, limit=120)
                bm_text += f"<b>{idx}.</b> {_bookmark_snippet(sender_name, limit=32)} ({date}):\n«{snip}»\n\n"
                
            nav_row = []
            if total_pages > 1:
                nav_row.append(Button.inline(f"1/{total_pages}", data="bm:page:1"))
                nav_row.append(Button.inline("След ▶️", data="bm:page:2"))
                
            buttons = []
            if nav_row:
                buttons.append(nav_row)
            buttons.append([Button.inline("⬅️ Назад в меню", data="nav:main")])
            
            await edit_callback_message(bot_client, event, bm_text,
                                       "edit_message:nav_bookmarks", buttons=buttons,
                                       parse_mode='html', link_preview=False)
            await event.answer()
            return
            
        elif nav_target in ("quiz", "test"):
            quiz_prompt_info = (
                "🎲 <b>Клинический квиз StomChat</b>\n\n"
                "Интерактивный формат проверки клинических знаний по терапевтической, ортопедической, хирургической стоматологии и эндодонтии.\n\n"
                "Вы можете сгенерировать задачу прямо сейчас с мгновенной проверкой ответа и разбором!\n\n"
                "👇 <i>Нажмите кнопку ниже, чтобы начать викторину:</i>"
            )
            buttons = [
                [Button.inline("🎲 Начать викторину", data="quiz:generate")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, quiz_prompt_info,
                                       "edit_message:nav_quiz", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif nav_target in ("case", "sim"):
            case_prompt_info = (
                "🎮 <b>Интерактивный симулятор клинического случая</b>\n\n"
                "Пошаговый тренажер реальных клинических ситуаций. Вы выступаете в роли лечащего врача, "
                "а ИИ моделирует реакцию пациента и оценивает обоснованность каждого вашего шага.\n\n"
                "• <b>Терапия & Эндодонтия:</b> сложные диагнозы, перелечивание\n"
                "• <b>Ортопедия:</b> препарирование, адгезивные протоколы\n"
                "• <b>Хирургия & Имплантация:</b> синус-лифтинг, навигация\n"
                "• <b>Пародонтология & Гнатология:</b> ВНЧС, регенерация\n\n"
                "👇 <i>Нажмите «🚀 Начать клинический кейс» для запуска:</i>"
            )
            buttons = [
                [Button.inline("🚀 Начать клинический кейс", data="case:start")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, case_prompt_info,
                                       "edit_message:nav_case", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif nav_target in ("stats", "statistics"):
            counts, scanned = await get_topic_statistics()
            stats_text = render_topic_statistics(counts, scanned)
            if not stats_text:
                stats_text = ("📊 <i>Статистику посчитать не удалось: база сообщений "
                              "сейчас недоступна. Попробуйте позже.</i>")
            buttons = [
                [Button.inline("🔄 Обновить", data="nav:stats")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, stats_text,
                                       "edit_message:stats", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif nav_target == "help":
            help_text = (
                "💡 <b>Памятка по возможностям StomChat:</b>\n\n"
                "• <b>Естественный язык:</b> Просто задавайте клинические вопросы, просите рассчитать анестезию («посчитай артикаин 4% на 70 кг»), запустить викторину («хочу квиз») или найти статьи («что пишет pubmed про вертипреп»).\n"
                "• <b>Снимки и фото:</b> Прикрепите рентген или фото — я проведу визуальный и клинический анализ.\n"
                "• <b>Интерактивные разделы:</b> Используйте меню ниже для быстрого перехода."
            )
            buttons = [[Button.inline("⬅️ Назад в меню", data="nav:main")]]
            await edit_callback_message(bot_client, event, help_text,
                                       "edit_message:nav_help", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return

        elif nav_target in ("xray", "scan", "image"):
            xray_text = (
                "🔬 <b>Разобрать снимок</b>\n\n"
                "Пришлите мне рентген-снимок, фото или документ — и я проведу клинический анализ:\n\n"
                "• <b>Прицельный рентген:</b> периапикальный статус, плотность, корневые каналы, кариес\n"
                "• <b>ОПТГ:</b> общая картина, патология пазух, кисты, ретинированные зубы, имплантаты\n"
                "• <b>КЛКТ:</b> анатомия каналов, резорбции, переломы, синус-лифтинг\n"
                "• <b>Фото:</b> окклюзия, состояние мягких тканей, краевое прилегание реставраций\n\n"
                "<i>Просто прикрепите файл прямо в этот диалог — никаких команд не нужно.</i>"
            )
            buttons = [[Button.inline("⬅️ Назад в меню", data="nav:main")]]
            await edit_callback_message(bot_client, event, xray_text,
                                       "edit_message:nav_xray", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return

        elif nav_target == "chat":
            chat_text = (
                "💬 <b>Клинический вопрос</b>\n\n"
                "Просто напишите вопрос своими словами — как коллеге на кафедре.\n\n"
                "<b>Примеры:</b>\n"
                "• <i>«Пациент 45 лет, периодонтит 3.6, гной по переходной складке. Лечение?»</i>\n"
                "• <i>«Чем зафиксировать e.max на культевую вкладку на 2.4?»</i>\n"
                "• <i>«Что нужно учесть при имплантации у пациента на варфарине?»</i>\n\n"
                "<i>Помню контекст последних 30 сообщений — можно уточнять и продолжать диалог без повтора условий.</i>"
            )
            buttons = [[Button.inline("⬅️ Назад в меню", data="nav:main")]]
            await edit_callback_message(bot_client, event, chat_text,
                                       "edit_message:nav_chat", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return

        elif nav_target in ("settings", "prefs"):
            profile = await database.get_user_profile(event.sender_id)
            current_style = profile.get("selected_style", "colleague_friendly")
            style_names = {
                "colleague_friendly": "Коллега-эксперт 🤝",
                "clinical_dry": "Сухие факты 📝",
                "humor_cynic": "Ироничный циник 💀"
            }
            curr_style_name = style_names.get(current_style, "Коллега-эксперт 🤝")
            settings_text = (
                "⚙️ <b>Настройки</b>\n\n"
                f"Текущий стиль: <b>{curr_style_name}</b>\n\n"
                "Выберите стиль общения ассистента:"
            )
            settings_buttons = [
                [Button.inline("Коллега-эксперт 🤝", data="style:colleague_friendly")],
                [Button.inline("Сухие факты 📝 (без предисловий)", data="style:clinical_dry")],
                [Button.inline("Ироничный циник 💀", data="style:humor_cynic")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, settings_text,
                                       "edit_message:nav_settings", buttons=settings_buttons,
                                       parse_mode='html')
            await event.answer()
            return


    if data_str.startswith("bm:page:"):
        page = 1
        try:
            page = max(1, int(data_str.split(":")[2]))
        except (IndexError, ValueError):
            page = 1

        total_items = await database.count_clinical_bookmarks(event.sender_id)
        if not total_items:
            empty_text = (
                "⭐ <b>Ваши клинические закладки</b>\n\n"
                "У вас пока нет сохраненных записей.\n\n"
                "💡 <i>Отправьте команду <code>/save</code> в ответ на любое сообщение в общем чате сообщества, "
                "или нажмите кнопку «⭐ В закладки» при чтении статьи в Энциклопедии.</i>"
            )
            buttons = [
                [Button.inline("📖 В Базу Знаний", data="nav:wiki")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, empty_text,
                                       "edit_message:bm_empty", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return

        per_page = 5
        total_pages = max(1, (total_items + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        page_rows = await database.get_clinical_bookmarks(event.sender_id, limit=per_page, offset=offset)

        bm_text = f"⭐ <b>Ваши клинические закладки (Страница {page}/{total_pages}):</b>\n\n"
        for i, row in enumerate(page_rows, offset + 1):
            msg_id, chat_id_val, sender_name, msg_text, media_desc, date = row
            bm_text += f"{i}. <b>{_bookmark_snippet(sender_name, limit=32)}</b> ({date}):\n"
            bm_text += f"«{_bookmark_snippet(msg_text, limit=120)}»\n"
            if media_desc:
                bm_text += f"🖼️ <i>Описание снимка:</i> {_bookmark_snippet(media_desc, limit=80)}\n"
            is_group_message = str(chat_id_val).startswith("-100") and msg_id > 0
            if is_group_message:
                clean_chat_id = str(chat_id_val)[4:]
                bm_text += f"🔗 <a href='https://t.me/c/{clean_chat_id}/{msg_id}'>Перейти к сообщению</a>\n\n"
            else:
                bm_text += "📖 <i>Статья энциклопедии</i>\n\n"

        bm_text += f"<i>Всего закладок: {total_items}</i>"

        nav_row = []
        if page > 1:
            nav_row.append(Button.inline("◀️ Пред", data=f"bm:page:{page - 1}"))
        nav_row.append(Button.inline(f"{page}/{total_pages}", data=f"bm:page:{page}"))
        if page < total_pages:
            nav_row.append(Button.inline("След ▶️", data=f"bm:page:{page + 1}"))

        buttons = []
        if nav_row:
            buttons.append(nav_row)
        buttons.append([Button.inline("⬅️ Назад в меню", data="nav:main")])

        await edit_callback_message(bot_client, event, bm_text,
                                   "edit_message:bm_list", buttons=buttons,
                                   parse_mode='html', link_preview=False)
        await event.answer()
        return

    # 3. ДЕТАЛЬНЫЙ СПРАВОЧНИК-КАЛЬКУЛЯТОР calc:*
    if data_str.startswith("calc:"):
        calc_sub = data_str.split(":", 1)[1]
        
        if calc_sub in ("main", "menu"):
            calc_msg = (
                "🧮 <b>Справочник-калькулятор анестезии</b>\n\n"
                "Пришлите препарат, концентрацию и вес — например "
                "<i>«артикаин 4%, ребёнок 20 кг»</i> — и я посчитаю с арифметикой на виду.\n\n"
                "<b>Предел всегда двойной: мг/кг И абсолютный максимум. Действует меньшее из двух.</b>\n\n"
                "• <b>Артикаин 4%</b> (1:100 000 / 1:200 000)\n"
                "  взрослые 7 мг/кг, дети 5 мг/кг, <b>но не более 500 мг</b>\n"
                "  карпула 1.7 мл = 68 мг → потолок ≈ 7 карпул\n"
                "  <i>потолок 500 мг наступает уже при весе ≈ 71 кг</i>\n\n"
                "• <b>Мепивакаин 3%</b> (без вазоконстриктора)\n"
                "  4.4 мг/кг, <b>но не более 400 мг</b>\n"
                "  карпула 1.8 мл = 54 мг → потолок ≈ 7 карпул\n"
                "  <i>потолок наступает при весе ≈ 91 кг</i>\n\n"
                "• <b>Лидокаин 2%</b> (с адреналином)\n"
                "  взрослые 7 мг/кг, дети 4.4 мг/кг, <b>но не более 500 мг</b>\n"
                "  карпула 1.8 мл = 36 мг → потолок ≈ 13 карпул\n"
                "  <i>потолок наступает при весе ≈ 71 кг</i>\n\n"
                "⚠️ <i>Это референсные максимумы для здорового пациента, а не рекомендация дозы. "
                "При сопутствующей патологии, у детей, беременных и пожилых предел ниже.</i>"
            )
            buttons = [
                [Button.inline("🦷 Артикаин 4%", data="calc:articaine"), Button.inline("💉 Мепивакаин 3%", data="calc:mepivacaine")],
                [Button.inline("🩸 Лидокаин 2%", data="calc:lidocaine")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, calc_msg,
                                       "edit_message:nav_calc", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif calc_sub == "articaine":
            art_text = (
                "🦷 <b>Артикаин 4% (с адреналином 1:100 000 / 1:200 000)</b>\n\n"
                "• <b>Концентрация:</b> 40 мг/мл (карпула 1.7 мл = 68 мг)\n"
                "• <b>Максимальные дозировки:</b>\n"
                "  — Взрослые: <b>7.0 мг/кг</b>\n"
                "  — Дети (от 4 лет): <b>5.0 мг/кг</b>\n"
                "  — <b>Абсолютный потолок: не более 500 мг</b> (≈ 7 карпул)\n"
                "  — <i>Потолок 500 мг наступает уже при весе ≈ 71 кг</i>\n\n"
                "📊 <b>Ориентир по весу пациента (карпулы 1.7 мл):</b>\n"
                "• 20 кг (ребенок) → макс. 100 мг ≈ <b>1.4 карпулы</b>\n"
                "• 40 кг → макс. 280 мг ≈ <b>4.1 карпулы</b>\n"
                "• 60 кг → макс. 420 мг ≈ <b>6.1 карпул</b>\n"
                "• 71+ кг → абсолютный максимум 500 мг ≈ <b>7.3 карпулы</b>\n\n"
                "⚠️ <i>Детям до 4 лет противопоказан. При заболеваниях печени дозировку уменьшают.</i>"
            )
            buttons = [
                [Button.inline("💉 Мепивакаин 3%", data="calc:mepivacaine"), Button.inline("🩸 Лидокаин 2%", data="calc:lidocaine")],
                [Button.inline("🧮 К калькулятору", data="calc:main"), Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, art_text,
                                       "edit_message:calc_articaine", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif calc_sub == "mepivacaine":
            mep_text = (
                "💉 <b>Мепивакаин 3% (Scandonest, без вазоконстриктора)</b>\n\n"
                "• <b>Концентрация:</b> 30 мг/мл (карпула 1.8 мл = 54 мг)\n"
                "• <b>Максимальные дозировки:</b>\n"
                "  — Взрослые и дети: <b>4.4 мг/кг</b>\n"
                "  — <b>Абсолютный потолок: не более 400 мг</b> (≈ 7 карпул)\n"
                "  — <i>Потолок наступает при весе ≈ 91 кг</i>\n\n"
                "📊 <b>Ориентир по весу пациента (карпулы 1.8 мл):</b>\n"
                "• 20 кг → макс. 88 мг ≈ <b>1.6 карпулы</b>\n"
                "• 40 кг → макс. 176 мг ≈ <b>3.2 карпулы</b>\n"
                "• 60 кг → макс. 264 мг ≈ <b>4.8 карпул</b>\n"
                "• 91+ кг → абсолютный максимум 400 мг ≈ <b>7.4 карпулы</b>\n\n"
                "⚠️ <i>Препарат выбора у пациентов с сердечно-сосудистой патологией, гипертонией и тиреотоксикозом.</i>"
            )
            buttons = [
                [Button.inline("🦷 Артикаин 4%", data="calc:articaine"), Button.inline("🩸 Лидокаин 2%", data="calc:lidocaine")],
                [Button.inline("🧮 К калькулятору", data="calc:main"), Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, mep_text,
                                       "edit_message:calc_mepivacaine", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif calc_sub == "lidocaine":
            lido_text = (
                "🩸 <b>Лидокаин 2% (с адреналином 1:100 000 / 1:80 000)</b>\n\n"
                "• <b>Концентрация:</b> 20 мг/мл (карпула 1.8 мл = 36 мг)\n"
                "• <b>Максимальные дозировки:</b>\n"
                "  — Взрослые: <b>7.0 мг/кг</b> (с адреналином, <b>но не более 500 мг</b> ≈ 13 карпул)\n"
                "  — Дети: <b>4.4 мг/кг</b>\n"
                "  — Без адреналина: <b>4.4 мг/кг</b> (максимум 300 мг ≈ 8 карпул)\n"
                "  — <i>Потолок 500 мг наступает при весе ≈ 71 кг</i>\n\n"
                "📊 <b>Ориентир по весу пациента (1.8 мл с адреналином):</b>\n"
                "• 20 кг → макс. 140 мг ≈ <b>3.8 карпулы</b>\n"
                "• 50 кг → макс. 350 мг ≈ <b>9.7 карпул</b>\n"
                "• 71+ кг → абсолютный максимум 500 мг ≈ <b>13.8 карпул</b>\n\n"
                "⚠️ <i>Выраженное сосудорасширяющее действие. Без адреналина быстро всасывается в кровоток.</i>"
            )
            buttons = [
                [Button.inline("🦷 Артикаин 4%", data="calc:articaine"), Button.inline("💉 Мепивакаин 3%", data="calc:mepivacaine")],
                [Button.inline("🧮 К калькулятору", data="calc:main"), Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, lido_text,
                                       "edit_message:calc_lidocaine", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return

    # 4. ИНТЕРАКТИВНЫЙ КВИЗ quiz:*
    if data_str.startswith("quiz:"):
        quiz_sub = data_str.split(":", 1)[1]
        
        if quiz_sub in ("menu", "main"):
            quiz_prompt_info = (
                "🎲 <b>Клинический квиз StomChat</b>\n\n"
                "Интерактивный формат проверки клинических знаний по терапевтической, ортопедической, хирургической стоматологии и эндодонтии.\n\n"
                "👇 <i>Нажмите кнопку ниже, чтобы начать викторину:</i>"
            )
            buttons = [
                [Button.inline("🎲 Начать викторину", data="quiz:generate")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, quiz_prompt_info,
                                       "edit_message:nav_quiz", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif quiz_sub in ("generate", "start", "next", "new"):
            fb = random.choice(CLINICAL_QUIZ_FALLBACKS)
            question = fb["question"]
            options = list(fb["options"])
            correct = fb["correct"]
            explanation = fb["explanation"]
            topic = fb.get("topic", "Стоматология")

            quiz_id = str(_next_quiz_state_id())
            init_votes = {"votes": [0, 0, 0, 0], "voters": {}}
            await database.set_user_interactive_state(
                user_id=int(quiz_id),
                state_type="quiz_config",
                current_step=correct,
                case_id=explanation[:200],
                history=json.dumps(init_votes)
            )

            buttons = [
                [
                    Button.inline(f"A: {options[0][:28]}", data=f"quiz:ans:{correct}:0:{quiz_id}"),
                    Button.inline(f"B: {options[1][:28]}", data=f"quiz:ans:{correct}:1:{quiz_id}")
                ],
                [
                    Button.inline(f"C: {options[2][:28]}", data=f"quiz:ans:{correct}:2:{quiz_id}"),
                    Button.inline(f"D: {options[3][:28]}", data=f"quiz:ans:{correct}:3:{quiz_id}")
                ],
                [
                    Button.inline("🔄 Другой вопрос", data="quiz:generate"),
                    Button.inline("⬅️ Назад в меню", data="nav:main")
                ]
            ]
            quiz_msg_text = (
                f"🎲 <b>Клинический квиз [{topic}]:</b>\n\n"
                f"{question}\n\n"
                f"<b>A:</b> {options[0]}\n"
                f"<b>B:</b> {options[1]}\n"
                f"<b>C:</b> {options[2]}\n"
                f"<b>D:</b> {options[3]}\n\n"
                "<i>Выберите вариант ответа кнопкой ниже:</i>"
            )
            await edit_callback_message(bot_client, event, quiz_msg_text,
                                       "edit_message:quiz_question", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif quiz_sub.startswith("ans:"):
            parts = data_str.split(":")
            correct_idx = int(parts[2])
            clicked_idx = int(parts[3])
            quiz_id = int(parts[4])

            state_row = await database.get_user_interactive_state(quiz_id)
            explanation = (state_row.get("case_id") if state_row else None) or "Клинический разбор."
            is_correct = (correct_idx == clicked_idx)
            
            letters = ["A", "B", "C", "D"]
            your_letter = letters[clicked_idx] if 0 <= clicked_idx < 4 else str(clicked_idx)
            corr_letter = letters[correct_idx] if 0 <= correct_idx < 4 else str(correct_idx)

            res_header = "✅ <b>ВЕРНО!</b>" if is_correct else "❌ <b>НЕВЕРНО!</b>"
            ans_text = (
                f"{res_header}\n\n"
                f"Ваш выбор: <b>{your_letter}</b> | Правильный ответ: <b>{corr_letter}</b>\n\n"
                f"💡 <b>Клиническое обоснование:</b>\n{explanation}"
            )
            buttons = [
                [Button.inline("🎲 Следующий вопрос", data="quiz:generate")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, ans_text,
                                       "edit_message:quiz_result", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return

    # 5. КЛИНИЧЕСКИЙ СИМУЛЯТОР case:*
    if data_str.startswith("case:"):
        case_sub = data_str.split(":", 1)[1]
        
        if case_sub in ("menu", "main"):
            case_prompt_info = (
                "🎮 <b>Интерактивный симулятор клинического случая</b>\n\n"
                "Пошаговый тренажер реальных клинических ситуаций. Вы выступаете в роли лечащего врача, "
                "а ИИ моделирует реакцию пациента и оценивает обоснованность каждого вашего шага.\n\n"
                "👇 <i>Нажмите «🚀 Начать клинический кейс» для запуска:</i>"
            )
            buttons = [
                [Button.inline("🚀 Начать клинический кейс", data="case:start")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, case_prompt_info,
                                       "edit_message:nav_case", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif case_sub == "start":
            await edit_callback_message(bot_client, event,
                                       "🎮 <i>Подготавливаю интерактивный клинический случай... Подождите.</i>",
                                       "edit_message:case_loading", parse_mode='html')
            
            departments = [
                "эндодонтия/кариесология (терапевтическая стоматология)",
                "протезирование/виниры/коронки (ортопедическая стоматология)",
                "имплантация/удаление зуба (хирургическая стоматология)",
                "заболевания пародонта (пародонтология)",
                "окклюзия/ВНЧС (гнатология)"
            ]
            selected_dept = random.choice(departments)
            case_prompt = f"""
Ты — старший стоматолог-экзаменатор. Придумай и опиши начало сложного клинического случая из области: {selected_dept}.
Напиши:
1. Жалобы пациента и анамнез.
2. Данные визуального осмотра.
3. Задай ровно один конкретный вопрос о первом действии врача (например, какие дополнительные исследования назначить, или какой инструмент выбрать).

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Будь лаконичен, профессионален.
2. Не пиши правильный ответ и не давай вариантов! Врач должен ответить своими словами (или голосом).
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
            status_ctx = {"kind": "pm_chat", "chat_id": event.sender_id, "thinking_level": "MEDIUM"}
            response, error = await generate_gemini_text_async(case_prompt, status_ctx, timeout=90)
            
            if error or not response or not getattr(response, "text", None):
                fallback_case = (
                    "🎮 <b>Клинический случай [Эндодонтия / Терапия]:</b>\n\n"
                    "<b>Пациент:</b> 34 года, жалобы на самопроизвольные приступообразные ночные боли в зубе 2.6 с иррадиацией в висок.\n"
                    "<b>Осмотр:</b> глубокая кариозная полость на медиально-окклюзионной поверхности, зондирование дна резко болезненно, перкуссия слабо болезненна, термопроба резко положительная с длительным болевым ответом (>1 мин).\n\n"
                    "❓ <b>Вопрос экзаменатора:</b> Какой предварительный диагноз и каков ваш первый шаг при инструментальной и медикаментозной обработке?"
                )
                starting_text = fallback_case
            else:
                starting_text = clean_html_formatting(response.text.strip())

            history_payload = {
                "messages": [{"role": "assistant", "content": starting_text}],
                "last_updated": time.time()
            }
            await database.set_user_interactive_state(
                user_id=event.sender_id,
                state_type="case",
                current_step=1,
                case_id="dynamic",
                history=json.dumps(history_payload)
            )
            
            buttons = [
                [Button.inline("⏹️ Сбросить симулятор", data="case:abort")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            case_display = (
                f"🎮 <b>Клинический симулятор (Шаг 1):</b>\n\n"
                f"{starting_text}\n\n"
                f"<i>Ответьте на вопрос сообщением (текстом или голосом) в этот диалог. Для сброса используйте кнопку ниже или команду /abort.</i>"
            )
            await edit_callback_message(bot_client, event, case_display,
                                       "edit_message:case_start", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif case_sub in ("abort", "exit"):
            await database.clear_user_interactive_state(event.sender_id)
            abort_text = (
                "⏹️ <b>Интерактивная сессия симулятора успешно завершена.</b>\n\n"
                "Вы можете в любой момент запустить новый разбор клинического случая!"
            )
            buttons = [
                [Button.inline("🚀 Начать новый кейс", data="case:start")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, abort_text,
                                       "edit_message:case_abort", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return

    # 2. ПОСТРАНИЧНЫЙ ВЫВОД ЗАКЛАДОК bm:page:*
    if data_str.startswith("bm:page:"):
        page = 1
        try:
            page = max(1, int(data_str.split(":")[2]))
        except (IndexError, ValueError):
            page = 1

        total_items = await database.count_clinical_bookmarks(event.sender_id)
        if not total_items:
            empty_text = (
                "⭐ <b>Ваши клинические закладки</b>\n\n"
                "У вас пока нет сохраненных записей.\n\n"
                "💡 <i>Отправьте команду <code>/save</code> в ответ на любое сообщение в общем чате сообщества, "
                "или нажмите кнопку «⭐ В закладки» при чтении статьи в Энциклопедии.</i>"
            )
            buttons = [
                [Button.inline("📖 В Базу Знаний", data="nav:wiki")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, empty_text,
                                       "edit_message:bm_empty", buttons=buttons,
                                       parse_mode='html')
            return

        per_page = 5
        total_pages = max(1, (total_items + per_page - 1) // per_page)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * per_page
        page_rows = await database.get_clinical_bookmarks(event.sender_id, limit=per_page, offset=offset)

        bm_text = f"⭐ <b>Ваши клинические закладки (Страница {page}/{total_pages}):</b>\n\n"
        for i, row in enumerate(page_rows, offset + 1):
            msg_id, chat_id_val, sender_name, msg_text, media_desc, date = row
            bm_text += f"{i}. <b>{_bookmark_snippet(sender_name, limit=32)}</b> ({date}):\n"
            bm_text += f"«{_bookmark_snippet(msg_text, limit=120)}»\n"
            if media_desc:
                bm_text += f"🖼️ <i>Описание снимка:</i> {_bookmark_snippet(media_desc, limit=80)}\n"
            is_group_message = str(chat_id_val).startswith("-100") and msg_id > 0
            if is_group_message:
                clean_chat_id = str(chat_id_val)[4:]
                bm_text += f"🔗 <a href='https://t.me/c/{clean_chat_id}/{msg_id}'>Перейти к сообщению</a>\n\n"
            else:
                bm_text += "📖 <i>Статья энциклопедии</i>\n\n"

        bm_text += f"<i>Всего закладок: {total_items}</i>"

        nav_row = []
        if page > 1:
            nav_row.append(Button.inline("◀️ Пред", data=f"bm:page:{page - 1}"))
        nav_row.append(Button.inline(f"{page}/{total_pages}", data=f"bm:page:{page}"))
        if page < total_pages:
            nav_row.append(Button.inline("След ▶️", data=f"bm:page:{page + 1}"))

        buttons = []
        if nav_row:
            buttons.append(nav_row)
        buttons.append([Button.inline("⬅️ Назад в меню", data="nav:main")])

        await edit_callback_message(bot_client, event, bm_text,
                                   "edit_message:bm_list", buttons=buttons,
                                   parse_mode='html', link_preview=False)
        return

    # 3. ДЕТАЛЬНЫЙ СПРАВОЧНИК-КАЛЬКУЛЯТОР calc:*
    if data_str.startswith("calc:"):
        calc_sub = data_str.split(":", 1)[1]
        
        if calc_sub in ("main", "menu"):
            calc_msg = (
                "🧮 <b>Справочник-калькулятор анестезии</b>\n\n"
                "Пришлите препарат, концентрацию и вес — например "
                "<i>«артикаин 4%, ребёнок 20 кг»</i> — и я посчитаю с арифметикой на виду.\n\n"
                "<b>Предел всегда двойной: мг/кг И абсолютный максимум. Действует меньшее из двух.</b>\n\n"
                "• <b>Артикаин 4%</b> (1:100 000 / 1:200 000)\n"
                "  взрослые 7 мг/кг, дети 5 мг/кг, <b>но не более 500 мг</b>\n"
                "  карпула 1.7 мл = 68 мг → потолок ≈ 7 карпул\n"
                "  <i>потолок 500 мг наступает уже при весе ≈ 71 кг</i>\n\n"
                "• <b>Мепивакаин 3%</b> (без вазоконстриктора)\n"
                "  4.4 мг/кг, <b>но не более 400 мг</b>\n"
                "  карпула 1.8 мл = 54 мг → потолок ≈ 7 карпул\n"
                "  <i>потолок наступает при весе ≈ 91 кг</i>\n\n"
                "• <b>Лидокаин 2%</b> (с адреналином)\n"
                "  взрослые 7 мг/кг, дети 4.4 мг/кг, <b>но не более 500 мг</b>\n"
                "  карпула 1.8 мл = 36 мг → потолок ≈ 13 карпул\n"
                "  <i>потолок наступает при весе ≈ 71 кг</i>\n\n"
                "⚠️ <i>Это референсные максимумы для здорового пациента, а не рекомендация дозы. "
                "При сопутствующей патологии, у детей, беременных и пожилых предел ниже.</i>"
            )
            buttons = [
                [Button.inline("🦷 Артикаин 4%", data="calc:articaine"), Button.inline("💉 Мепивакаин 3%", data="calc:mepivacaine")],
                [Button.inline("🩸 Лидокаин 2%", data="calc:lidocaine")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, calc_msg,
                                       "edit_message:nav_calc", buttons=buttons,
                                       parse_mode='html')
            return
            
        elif calc_sub == "articaine":
            art_text = (
                "🦷 <b>Артикаин 4% (с адреналином 1:100 000 / 1:200 000)</b>\n\n"
                "• <b>Концентрация:</b> 40 мг/мл (карпула 1.7 мл = 68 мг)\n"
                "• <b>Максимальные дозировки:</b>\n"
                "  — Взрослые: <b>7.0 мг/кг</b>\n"
                "  — Дети (от 4 лет): <b>5.0 мг/кг</b>\n"
                "  — <b>Абсолютный потолок: не более 500 мг</b> (≈ 7 карпул)\n"
                "  — <i>Потолок 500 мг наступает уже при весе ≈ 71 кг</i>\n\n"
                "📊 <b>Ориентир по весу пациента (карпулы 1.7 мл):</b>\n"
                "• 20 кг (ребенок) → макс. 100 мг ≈ <b>1.4 карпулы</b>\n"
                "• 40 кг → макс. 280 мг ≈ <b>4.1 карпулы</b>\n"
                "• 60 кг → макс. 420 мг ≈ <b>6.1 карпул</b>\n"
                "• 71+ кг → абсолютный максимум 500 мг ≈ <b>7.3 карпулы</b>\n\n"
                "⚠️ <i>Детям до 4 лет противопоказан. При заболеваниях печени дозировку уменьшают.</i>"
            )
            buttons = [
                [Button.inline("💉 Мепивакаин 3%", data="calc:mepivacaine"), Button.inline("🩸 Лидокаин 2%", data="calc:lidocaine")],
                [Button.inline("🧮 К калькулятору", data="calc:main"), Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, art_text,
                                       "edit_message:calc_articaine", buttons=buttons,
                                       parse_mode='html')
            return
            
        elif calc_sub == "mepivacaine":
            mep_text = (
                "💉 <b>Мепивакаин 3% (Scandonest, без вазоконстриктора)</b>\n\n"
                "• <b>Концентрация:</b> 30 мг/мл (карпула 1.8 мл = 54 мг)\n"
                "• <b>Максимальные дозировки:</b>\n"
                "  — Взрослые и дети: <b>4.4 мг/кг</b>\n"
                "  — <b>Абсолютный потолок: не более 400 мг</b> (≈ 7 карпул)\n"
                "  — <i>Потолок наступает при весе ≈ 91 кг</i>\n\n"
                "📊 <b>Ориентир по весу пациента (карпулы 1.8 мл):</b>\n"
                "• 20 кг → макс. 88 мг ≈ <b>1.6 карпулы</b>\n"
                "• 40 кг → макс. 176 мг ≈ <b>3.2 карпулы</b>\n"
                "• 60 кг → макс. 264 мг ≈ <b>4.8 карпул</b>\n"
                "• 91+ кг → абсолютный максимум 400 мг ≈ <b>7.4 карпулы</b>\n\n"
                "⚠️ <i>Препарат выбора у пациентов с сердечно-сосудистой патологией, гипертонией и тиреотоксикозом.</i>"
            )
            buttons = [
                [Button.inline("🦷 Артикаин 4%", data="calc:articaine"), Button.inline("🩸 Лидокаин 2%", data="calc:lidocaine")],
                [Button.inline("🧮 К калькулятору", data="calc:main"), Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, mep_text,
                                       "edit_message:calc_mepivacaine", buttons=buttons,
                                       parse_mode='html')
            return
            
        elif calc_sub == "lidocaine":
            lido_text = (
                "🩸 <b>Лидокаин 2% (с адреналином 1:100 000 / 1:80 000)</b>\n\n"
                "• <b>Концентрация:</b> 20 мг/мл (карпула 1.8 мл = 36 мг)\n"
                "• <b>Максимальные дозировки:</b>\n"
                "  — Взрослые: <b>7.0 мг/кг</b> (с адреналином, <b>но не более 500 мг</b> ≈ 13 карпул)\n"
                "  — Дети: <b>4.4 мг/кг</b>\n"
                "  — Без адреналина: <b>4.4 мг/кг</b> (максимум 300 мг ≈ 8 карпул)\n"
                "  — <i>Потолок 500 мг наступает при весе ≈ 71 кг</i>\n\n"
                "📊 <b>Ориентир по весу пациента (1.8 мл с адреналином):</b>\n"
                "• 20 кг → макс. 140 мг ≈ <b>3.8 карпулы</b>\n"
                "• 50 кг → макс. 350 мг ≈ <b>9.7 карпул</b>\n"
                "• 71+ кг → абсолютный максимум 500 мг ≈ <b>13.8 карпул</b>\n\n"
                "⚠️ <i>Выраженное сосудорасширяющее действие. Без адреналина быстро всасывается в кровоток.</i>"
            )
            buttons = [
                [Button.inline("🦷 Артикаин 4%", data="calc:articaine"), Button.inline("💉 Мепивакаин 3%", data="calc:mepivacaine")],
                [Button.inline("🧮 К калькулятору", data="calc:main"), Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, lido_text,
                                       "edit_message:calc_lidocaine", buttons=buttons,
                                       parse_mode='html')
            return

    # 4. ИНТЕРАКТИВНЫЙ КВИЗ quiz:*
    if data_str.startswith("quiz:"):
        quiz_sub = data_str.split(":", 1)[1]
        
        if quiz_sub in ("menu", "main"):
            quiz_prompt_info = (
                "🎲 <b>Клинический квиз StomChat</b>\n\n"
                "Интерактивный формат проверки клинических знаний по терапевтической, ортопедической, хирургической стоматологии и эндодонтии.\n\n"
                "👇 <i>Нажмите кнопку ниже, чтобы начать викторину:</i>"
            )
            buttons = [
                [Button.inline("🎲 Начать викторину", data="quiz:generate")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, quiz_prompt_info,
                                       "edit_message:nav_quiz", buttons=buttons,
                                       parse_mode='html')
            return
            
        elif quiz_sub in ("generate", "start", "next", "new"):
            fb = random.choice(CLINICAL_QUIZ_FALLBACKS)
            question = fb["question"]
            options = list(fb["options"])
            correct = fb["correct"]
            explanation = fb["explanation"]
            topic = fb.get("topic", "Стоматология")

            quiz_id = str(_next_quiz_state_id())
            init_votes = {"votes": [0, 0, 0, 0], "voters": {}}
            await database.set_user_interactive_state(
                user_id=int(quiz_id),
                state_type="quiz_config",
                current_step=correct,
                case_id=explanation[:200],
                history=json.dumps(init_votes)
            )

            buttons = [
                [
                    Button.inline(f"A: {options[0][:28]}", data=f"quiz:ans:{correct}:0:{quiz_id}"),
                    Button.inline(f"B: {options[1][:28]}", data=f"quiz:ans:{correct}:1:{quiz_id}")
                ],
                [
                    Button.inline(f"C: {options[2][:28]}", data=f"quiz:ans:{correct}:2:{quiz_id}"),
                    Button.inline(f"D: {options[3][:28]}", data=f"quiz:ans:{correct}:3:{quiz_id}")
                ],
                [
                    Button.inline("🔄 Другой вопрос", data="quiz:generate"),
                    Button.inline("⬅️ Назад в меню", data="nav:main")
                ]
            ]
            quiz_msg_text = (
                f"🎲 <b>Клинический квиз [{topic}]:</b>\n\n"
                f"{question}\n\n"
                f"<b>A:</b> {options[0]}\n"
                f"<b>B:</b> {options[1]}\n"
                f"<b>C:</b> {options[2]}\n"
                f"<b>D:</b> {options[3]}\n\n"
                "<i>Выберите вариант ответа кнопкой ниже:</i>"
            )
            await edit_callback_message(bot_client, event, quiz_msg_text,
                                       "edit_message:quiz_question", buttons=buttons,
                                       parse_mode='html')
            return
            
        elif quiz_sub.startswith("ans:"):
            parts = data_str.split(":")
            correct_idx = int(parts[2])
            clicked_idx = int(parts[3])
            quiz_id = int(parts[4])

            state_row = await database.get_user_interactive_state(quiz_id)
            explanation = (state_row.get("case_id") if state_row else None) or "Клинический разбор."
            is_correct = (correct_idx == clicked_idx)
            
            letters = ["A", "B", "C", "D"]
            your_letter = letters[clicked_idx] if 0 <= clicked_idx < 4 else str(clicked_idx)
            corr_letter = letters[correct_idx] if 0 <= correct_idx < 4 else str(correct_idx)

            res_header = "✅ <b>ВЕРНО!</b>" if is_correct else "❌ <b>НЕВЕРНО!</b>"
            ans_text = (
                f"{res_header}\n\n"
                f"Ваш выбор: <b>{your_letter}</b> | Правильный ответ: <b>{corr_letter}</b>\n\n"
                f"💡 <b>Клиническое обоснование:</b>\n{explanation}"
            )
            buttons = [
                [Button.inline("🎲 Следующий вопрос", data="quiz:generate")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, ans_text,
                                       "edit_message:quiz_result", buttons=buttons,
                                       parse_mode='html')
            return

    # 5. КЛИНИЧЕСКИЙ СИМУЛЯТОР case:*
    if data_str.startswith("case:"):
        case_sub = data_str.split(":", 1)[1]
        
        if case_sub in ("menu", "main"):
            case_prompt_info = (
                "🎮 <b>Интерактивный симулятор клинического случая</b>\n\n"
                "Пошаговый тренажер реальных клинических ситуаций. Вы выступаете в роли лечащего врача, "
                "а ИИ моделирует реакцию пациента и оценивает обоснованность каждого вашего шага.\n\n"
                "👇 <i>Нажмите «🚀 Начать клинический кейс» для запуска:</i>"
            )
            buttons = [
                [Button.inline("🚀 Начать клинический кейс", data="case:start")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, case_prompt_info,
                                       "edit_message:nav_case", buttons=buttons,
                                       parse_mode='html')
            return
            
        elif case_sub == "start":
            await edit_callback_message(bot_client, event,
                                       "🎮 <i>Подготавливаю интерактивный клинический случай... Подождите.</i>",
                                       "edit_message:case_loading", parse_mode='html')
            
            departments = [
                "эндодонтия/кариесология (терапевтическая стоматология)",
                "протезирование/виниры/коронки (ортопедическая стоматология)",
                "имплантация/удаление зуба (хирургическая стоматология)",
                "заболевания пародонта (пародонтология)",
                "окклюзия/ВНЧС (гнатология)"
            ]
            selected_dept = random.choice(departments)
            case_prompt = f"""
Ты — старший стоматолог-экзаменатор. Придумай и опиши начало сложного клинического случая из области: {selected_dept}.
Напиши:
1. Жалобы пациента и анамнез.
2. Данные визуального осмотра.
3. Задай ровно один конкретный вопрос о первом действии врача (например, какие дополнительные исследования назначить, или какой инструмент выбрать).

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Будь лаконичен, профессионален.
2. Не пиши правильный ответ и не давай вариантов! Врач должен ответить своими словами (или голосом).
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
            status_ctx = {"kind": "pm_chat", "chat_id": event.sender_id, "thinking_level": "MEDIUM"}
            response, error = await generate_gemini_text_async(case_prompt, status_ctx, timeout=90)
            
            if error or not response or not getattr(response, "text", None):
                fallback_case = (
                    "🎮 <b>Клинический случай [Эндодонтия / Терапия]:</b>\n\n"
                    "<b>Пациент:</b> 34 года, жалобы на самопроизвольные приступообразные ночные боли в зубе 2.6 с иррадиацией в висок.\n"
                    "<b>Осмотр:</b> глубокая кариозная полость на медиально-окклюзионной поверхности, зондирование дна резко болезненно, перкуссия слабо болезненна, термопроба резко положительная с длительным болевым ответом (>1 мин).\n\n"
                    "❓ <b>Вопрос экзаменатора:</b> Какой предварительный диагноз и каков ваш первый шаг при инструментальной и медикаментозной обработке?"
                )
                starting_text = fallback_case
            else:
                starting_text = clean_html_formatting(response.text.strip())

            history_payload = {
                "messages": [{"role": "assistant", "content": starting_text}],
                "last_updated": time.time()
            }
            await database.set_user_interactive_state(
                user_id=event.sender_id,
                state_type="case",
                current_step=1,
                case_id="dynamic",
                history=json.dumps(history_payload)
            )
            
            buttons = [
                [Button.inline("⏹️ Сбросить симулятор", data="case:abort")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            case_display = (
                f"🎮 <b>Клинический симулятор (Шаг 1):</b>\n\n"
                f"{starting_text}\n\n"
                f"<i>Ответьте на вопрос сообщением (текстом или голосом) в этот диалог. Для сброса используйте кнопку ниже или команду /abort.</i>"
            )
            await edit_callback_message(bot_client, event, case_display,
                                       "edit_message:case_start", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return
            
        elif case_sub in ("abort", "exit"):
            await database.clear_user_interactive_state(event.sender_id)
            abort_text = (
                "⏹️ <b>Интерактивная сессия симулятора успешно завершена.</b>\n\n"
                "Вы можете в любой момент запустить новый разбор клинического случая!"
            )
            buttons = [
                [Button.inline("🚀 Начать новый кейс", data="case:start")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, abort_text,
                                       "edit_message:case_abort", buttons=buttons,
                                       parse_mode='html')
            await event.answer()
            return

    if data_str.startswith("style:"):
        style = data_str.split(":")[1]
        style_names = {
            "colleague_friendly": "Коллега-эксперт 🤝",
            "clinical_dry": "Сухие факты 📝",
            "humor_cynic": "Ироничный циник 💀"
        }
        if style == "menu":
            profile = await database.get_user_profile(event.sender_id)
            current_style = profile.get("selected_style", "colleague_friendly")
            curr_style_name = style_names.get(current_style, "Неизвестный")
            style_welcome = (
                "⚙️ <b>Настройка стиля общения</b>\n\n"
                f"Текущий стиль общения: <b>{curr_style_name}</b>\n\n"
                "Выберите стиль, в котором я буду отвечать вам в личных сообщениях:"
            )
            style_buttons = [
                [Button.inline("Коллега-эксперт 🤝 (по умолчанию)", data="style:colleague_friendly")],
                [Button.inline("Сухие факты 📝 (строго, без шуток)", data="style:clinical_dry")],
                [Button.inline("Ироничный циник 💀 (черный юмор)", data="style:humor_cynic")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, style_welcome,
                                       "edit_message:nav_style", buttons=style_buttons,
                                       parse_mode='html')
            await event.answer()
            return

        # Данные кнопки приходят от клиента, а не из нашего сообщения: прислать
        # можно что угодно. Неизвестное значение легло бы в базу как стиль и
        # осталось там навсегда — сохраняем только то, для чего есть промпт.
        if style not in STYLE_PROMPTS:
            logger.warning("Unknown style in callback from %s: %r", event.sender_id, style)
            await event.answer("Неизвестный стиль", alert=True)
            return
        style_name = style_names.get(style, "Неизвестный")

        # Сохраняем в БД
        await database.set_user_style(event.sender_id, style)

        confirm_text = (
            "✅ <b>Стиль общения успешно изменен!</b>\n\n"
            f"Новый стиль: <b>{style_name}</b>\n\n"
            "Он применяется и в личных сообщениях, и в ответах в общем чате. Изменить можно в любой момент командой /style."
        )
        confirm_buttons = [
            [Button.inline("⚙️ Изменить стиль", data="style:menu")],
            [Button.inline("⬅️ Назад в меню", data="nav:main")]
        ]
        await edit_callback_message(bot_client, event, confirm_text,
                                   "edit_message:style_confirm", buttons=confirm_buttons,
                                   parse_mode='html')
        await event.answer()
        return

    if data_str == "proto:back":
        protocols_text = (
            "📚 <b>Основные клинические протоколы в Базе Знаний:</b>\n\n"
            "• <b>BOPT (Biologically Oriented Preparation Technique):</b> Концепция препарирования без уступа.\n"
            "• <b>Вертикальное препарирование:</b> Особенности ведения краев коронок, сохранение тканей.\n"
            "• <b>Травление керамики:</b> Протоколы работы с плавиковой кислотой и силанизацией (E.max, полевой шпат).\n"
            "• <b>Ирригация в эндодонтии:</b> Концентрации гипохлорита натрия, ЭДТА, протоколы активации (ультразвук, звуковая).\n"
            "• <b>Обтурация корневых каналов:</b> Методики латеральной конденсации и вертикальной горячей гуттаперчи.\n\n"
            "👇 <i>Выберите интересующий протокол ниже для детального изучения:</i>"
        )
        from telethon import Button
        buttons = [
            [Button.inline("🦷 BOPT", data="proto:bopt"), Button.inline("🧪 Травление", data="proto:etching")],
            [Button.inline("💧 Ирригация", data="proto:irrigation"), Button.inline("🩸 Обтурация", data="proto:obturation")],
            [Button.inline("📐 Вертикальное препарирование", data="proto:vertical")],
            [Button.inline("⬅️ Назад в меню", data="nav:main")]
        ]
        await edit_callback_message(bot_client, event, protocols_text,
                                   "edit_message:proto_list", buttons=buttons,
                                   parse_mode='html')
        await event.answer()
        return

    if data_str.startswith("proto:"):
        proto_id = data_str.split(":")[1]
        keywords_map = {
            "irrigation": ["гипохлорит", "эдта", "ирригац", "активац"],
            "bopt": ["bopt", "уступ", "преп"],
            "etching": ["плавиков", "силан", "бонд", "травлен"],
            "obturation": ["гуттаперч", "силер", "обтурац", "конденсац"],
            "vertical": ["вертикальн", "уступ", "преп", "коронка"],
        }
        kws = keywords_map.get(proto_id, ["дентин"])
        wiki_corpus, _ = await search_knowledge_corpus(kws)
        wiki_corpus = clean_html_formatting(wiki_corpus)
        if not wiki_corpus:
            wiki_corpus = "<i>Данные протокола временно отсутствуют в базе знаний.</i>"
        else:
            # Здесь стоял голый срез wiki_corpus[:1500] + "...". clean_html_formatting
            # сохраняет <b>, <i> и <code>, поэтому срез мог попасть внутрь тега или
            # внутрь экранированной сущности (&amp;) — Telegram такую разметку
            # отклоняет целиком, edit_message падает, и врач, нажавший кнопку
            # протокола, не видит РОВНО НИЧЕГО. Плюс "..." дописывалось всегда,
            # даже когда текст никуда не обрезали.
            wiki_corpus = html_safe.safe_truncate_html(wiki_corpus, max_len=PROTOCOL_EXCERPT_MAX_CHARS)
            
        proto_names = {
            "irrigation": "💧 Ирригация в эндодонтии",
            "bopt": "🦷 BOPT (Препарирование)",
            "etching": "🧪 Адгезивные протоколы (Травление)",
            "obturation": "🩸 Обтурация корневых каналов",
            "vertical": "📐 Вертикальное препарирование",
        }
        title = proto_names.get(proto_id, "📚 Клинический протокол")
        response_text = f"<b>{title}:</b>\n\n{wiki_corpus}"
        
        from telethon import Button
        back_btn = [
            [Button.inline("⬅️ Назад к списку", data="proto:back")],
            [Button.inline("⬅️ Назад в меню", data="nav:main")]
        ]
        await edit_callback_message(bot_client, event, response_text,
                                   "edit_message:proto_article", buttons=back_btn,
                                   parse_mode='html', link_preview=False)
        await event.answer()
        return

    # WIKI MAIN MENU BACK
    if data_str == "wiki_cat:back":
        wiki_text = (
            "📖 <b>Интерактивная Стоматологическая Энциклопедия</b>\n\n"
            "Добро пожаловать в базу клинических знаний и протоколов StomChat. Здесь собраны проверенные стандарты доказательной стоматологии.\n\n"
            "👇 <i>Выберите интересующее действие:</i>"
        )
        from telethon import Button
        buttons = [
            [Button.inline("📚 Обзор по разделам", data="wiki_cat:topics")],
            [Button.inline("🎲 Случайный факт", data="wiki_cat:random"), Button.inline("🔍 Поиск по базе", data="wiki_cat:search_info")],
            [Button.inline("📚 Клинические протоколы", data="nav:proto")],
            [Button.inline("⬅️ Назад в меню", data="nav:main")]
        ]
        await edit_callback_message(bot_client, event, wiki_text,
                                   "edit_message:wiki_menu", buttons=buttons,
                                   parse_mode='html')
        await event.answer()
        return

    # WIKI TOPICS SELECTOR
    if data_str == "wiki_cat:topics":
        wiki_text = "📚 <b>Рубрикатор Энциклопедии (основные разделы):</b>"
        # Кнопки собираются из WIKI_TREE: раздел, добавленный в дерево,
        # появляется здесь сам. Раньше список был отдельным, и разделы
        # без кнопки существовали только в обработчике.
        buttons = wiki_topic_buttons()
        await edit_callback_message(bot_client, event, wiki_text,
                                   "edit_message:wiki_topics", buttons=buttons,
                                   parse_mode='html')
        await event.answer()
        return

    # SEARCH / RANDOM ROUTINGS
    if data_str == "wiki_cat:search_info":
        search_info = (
            "🔍 <b>Поиск по Базе Знаний:</b>\n\n"
            "Чтобы выполнить быстрый поиск, просто введите в ЛС команду <code>/search &lt;запрос&gt;</code>.\n\n"
            "Например:\n"
            "• <code>/search BOPT</code>\n"
            "• <code>/search гипохлорит</code>\n"
            "• <code>/search травление</code>\n\n"
            "<i>Бот выведет наиболее релевантные статьи прямо в диалог!</i>"
        )
        from telethon import Button
        back_btn = [
            [Button.inline("📚 Обзор по разделам", data="wiki_cat:topics")],
            [Button.inline("⬅️ Назад в меню", data="nav:main")]
        ]
        await edit_callback_message(bot_client, event, search_info,
                                   "edit_message:wiki_search_info",
                                   buttons=back_btn, parse_mode='html')
        await event.answer()
        return

    if data_str == "wiki_cat:random":
        fact = await query_random_wiki_fact()
        if fact:
            fact_cleaned = clean_html_formatting(fact)
            response_text = f"🎲 <b>Случайный факт из Базы Знаний:</b>\n\n{fact_cleaned}"
        else:
            response_text = "<i>Не удалось получить случайный факт. База временно недоступна.</i>"
        from telethon import Button
        buttons = [
            [Button.inline("🔄 Ещё факт", data="wiki_cat:random")],
            [Button.inline("📚 Обзор по разделам", data="wiki_cat:topics"), Button.inline("⬅️ Назад в меню", data="nav:main")]
        ]
        await edit_callback_message(bot_client, event, response_text,
                                   "edit_message:wiki_random", buttons=buttons,
                                   parse_mode='html', link_preview=False)
        await event.answer()
        return

    # WIKI CATEGORY SUBTOPICS
    if data_str.startswith("wiki_cat:"):
        cat_id = data_str.split(":")[1]
        # Заголовок и кнопки подтем берутся из WIKI_TREE. Раньше здесь были
        # словарь заголовков и цепочка elif со списками кнопок на каждый
        # раздел — третья и четвёртая копии одних и тех же данных.
        title = WIKI_CATEGORY_NAMES.get(cat_id, "📚 Раздел Энциклопедии")
        buttons = wiki_category_buttons(cat_id, await wiki_subtopic_counts(cat_id))

        wiki_text = f"📚 <b>Раздел: {title}</b>\n\nвыберите интересующую клиническую подтему для просмотра статей:"
        await edit_callback_message(bot_client, event, wiki_text,
                                   "edit_message:wiki_category", buttons=buttons,
                                   parse_mode='html')
        await event.answer()
        return

    # WIKI FACT PAGE AND PAGINATION
    if data_str.startswith("wiki_page:"):
        parts = data_str.split(":")
        subtopic_id = parts[1]
        page_idx = int(parts[2])

        # Одна статья одним запросом вместо загрузки всего раздела в память на
        # каждое нажатие кнопки листания.
        fact_content, total = await query_wiki_fact_page(subtopic_id, page_idx)

        subtopic_names = WIKI_SUBTOPIC_NAMES
        subtopic_title = subtopic_names.get(subtopic_id, "📚 Статья")
        
        if not total:
            response_text = f"📚 <b>{subtopic_title}:</b>\n\n<i>В данной категории пока нет статей в базе знаний.</i>"
            from telethon import Button
            back_cat = subtopic_id.split("_")[0]
            back_btn = [
                [Button.inline("⬅️ Назад к подтемам", data=f"wiki_cat:{back_cat}")],
                [Button.inline("⬅️ Назад в меню", data="nav:main")]
            ]
            await edit_callback_message(bot_client, event, response_text,
                                       "edit_message:wiki_page_empty",
                                       buttons=back_btn, parse_mode='html')
            await event.answer()
            return

        # Индекс страницы нормализует сам запрос (page_idx % total), поэтому
        # «Пред» с первой статьи уводит на последнюю, а «След» с последней — на
        # первую, без отдельной арифметики здесь.
        page_idx %= total
        fact_cleaned = clean_html_formatting(fact_content)
        
        response_text = (
            f"📖 <b>{subtopic_title}</b>\n"
            f"<i>Статья {page_idx + 1} из {total}</i>\n\n"
            f"{fact_cleaned}"
        )
        
        from telethon import Button
        nav_row = []
        if total > 1:
            nav_row.append(Button.inline("◀️ Пред", data=f"wiki_page:{subtopic_id}:{page_idx - 1}"))
            nav_row.append(Button.inline(f"{page_idx + 1}/{total}", data=f"wiki_page:{subtopic_id}:{page_idx}"))
            nav_row.append(Button.inline("След ▶️", data=f"wiki_page:{subtopic_id}:{page_idx + 1}"))
            
        back_cat = subtopic_id.split("_")[0]
        buttons = []
        if nav_row:
            buttons.append(nav_row)
        buttons.append([
            Button.inline("⭐ В закладки", data=f"wiki_save:{subtopic_id}:{page_idx}"),
            Button.inline("⬅️ Назад к подтемам", data=f"wiki_cat:{back_cat}")
        ])
        buttons.append([
            Button.inline("⬅️ Назад в меню", data="nav:main")
        ])
        
        await edit_callback_message(bot_client, event, response_text,
                                   "edit_message:wiki_page", buttons=buttons,
                                   parse_mode='html', link_preview=False)
        await event.answer()
        return

    # WIKI BOOKMARK SAVE CALLBACK
    if data_str.startswith("wiki_save:"):
        parts = data_str.split(":")
        subtopic_id = parts[1]
        page_idx = int(parts[2])
        
        # Тем же запросом, что и показ страницы. Раньше здесь грузился весь
        # раздел старой выборкой, и после перехода на пагинацию в SQL номер
        # страницы означал бы уже другую статью — в закладки сохранялось бы не
        # то, что врач видит на экране.
        fact_content, total = await query_wiki_fact_page(subtopic_id, page_idx)
        subtopic_names = WIKI_SUBTOPIC_NAMES
        subtopic_title = subtopic_names.get(subtopic_id, "📚 Статья")

        if fact_content:
            fact_cleaned = clean_html_formatting(fact_content)
            
            bookmark_text = f"📚 <b>{subtopic_title}</b>\n\n{fact_cleaned}"
            
            fake_msg_id = -random.randint(100000000, 999999999)
            
            await database.save_clinical_bookmark(
                saved_by_user_id=event.sender_id,
                msg_id=fake_msg_id,
                chat_id=event.chat_id,
                sender_name="База Знаний",
                text=bookmark_text,
                has_media=False,
                media_description="",
                date=datetime.now()
            )
            await event.answer("⭐ Статья успешно добавлена в ваши закладки!", alert=True)
        else:
            await event.answer("❌ Не удалось сохранить статью. Попробуйте еще раз.", alert=True)
        return

    if not data_str.startswith("qa:"):
        return
        
    parts = data_str.split(":")
    correct_idx = int(parts[1])
    clicked_idx = int(parts[2])
    quiz_id = int(parts[3])
    voter_id = str(event.sender_id)
    
    state_row = await database.get_user_interactive_state(quiz_id)
    if not state_row:
        await event.answer("⚠️ Ошибка: Викторина не найдена.", alert=True)
        return
        
    explanation = state_row.get("case_id") or "Правильный выбор!"
    history_str = state_row.get("history") or "{}"
    
    try:
        history_data = json.loads(history_str)
        if not isinstance(history_data, dict) or "votes" not in history_data:
            history_data = {"votes": [0, 0, 0, 0], "voters": {}}
    except Exception:
        history_data = {"votes": [0, 0, 0, 0], "voters": {}}
        
    votes = history_data["votes"]
    voters = history_data["voters"]
    
    if voter_id in voters:
        await event.answer("⚠️ Вы уже проголосовали в этой викторине!", alert=True)
        return
        
    # Record vote
    voters[voter_id] = clicked_idx
    votes[clicked_idx] += 1
    
    # Update DB
    await database.set_user_interactive_state(
        user_id=quiz_id,
        state_type="quiz_config",
        current_step=correct_idx,
        case_id=explanation,
        history=json.dumps(history_data)
    )
    
    is_correct = (correct_idx == clicked_idx)
    prefix = "✅ Верно! " if is_correct else "❌ Неверно! "
    alert_text = f"{prefix}\n\n{explanation}"
    await event.answer(alert_text, alert=True)
    
    # Update message text with stats
    try:
        original_msg = await event.get_message()
        if original_msg and original_msg.message:
            lines = original_msg.message.split("\n")
            total_votes = sum(votes)
            pct = [int((v / total_votes) * 100) if total_votes > 0 else 0 for v in votes]
            
            new_lines = []
            opt_regex = re.compile(r'^(?:<b>|\*\*)?([A-D])[:.](?:</b>|\*\*)?\s*(.*)', re.IGNORECASE)
            suffix_regex = re.compile(r'\s*\(\d+\s*гол\S*\s*\|\s*\d+%\)\s*$', re.IGNORECASE)

            for line in lines:
                stripped = line.strip()
                opt_match = opt_regex.match(stripped)
                if opt_match:
                    letter = opt_match.group(1).upper()
                    idx = ord(letter) - ord('A')
                    raw_choice = opt_match.group(2)
                    clean_choice = suffix_regex.sub('', raw_choice).strip()
                    new_lines.append(f"<b>{letter}:</b> {clean_choice} ({votes[idx]} гол. | {pct[idx]}%)")
                elif "Нажмите на кнопку" in line or "Всего проголосовало" in line or stripped.startswith("📊"):
                    continue
                elif stripped == "🎲 КЛИНИЧЕСКИЙ КЕЙС-ВИКТОРИНА":
                    new_lines.append("🎲 <b>КЛИНИЧЕСКИЙ КЕЙС-ВИКТОРИНА</b>")
                else:
                    new_lines.append(line)
            
            while new_lines and not new_lines[-1].strip():
                new_lines.pop()
                
            new_lines.append(f"\n📊 <b>Всего проголосовало: {total_votes}</b>\n\n<i>Нажмите на кнопку с вашим вариантом ответа, чтобы проверить себя!</i>")
            
            new_text = "\n".join(new_lines)
            await event.edit(text=new_text, parse_mode='html')
    except Exception as edit_err:
        logger.error(f"Failed to edit quiz message text with live stats: {edit_err}")


# Псевдоним для централизованного диспетчера колбэков
handle_callback_query = handle_quiz_callback


async def analyze_dispute_need(context_msgs):
    if not context_msgs:
        return False
    context_str = "\n".join(context_msgs)
    prompt = f"""
Ты — модератор клинического чата стоматологов. Проанализируй переписку врачей и определи, есть ли в ней активный спор, клиническое разногласие, конфликт мнений или спорное обсуждение, требующее вмешательства клинического рефери для разрядки обстановки или предоставления научной справки.

Переписка врачей:
{context_str}

Правило: выведи строго одно слово 'YES' (если спор/конфликт есть) или 'NO' (если это обычное мирное обсуждение, шутка или обмен опытом без спора). Никаких других слов или комментариев не пиши.
"""
    status_ctx = {"kind": "referee_analyser", "thinking_level": "LOW"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=45)
    if response and getattr(response, "text", None):
        res = response.text.strip().upper()
        if res.startswith("YES"):
            return True
    return False


async def check_referee_triage(context_msgs):
    """
    Отправляет контекст спора в Llama для подтверждения:
    действительно ли между пользователями происходит токсичный/личный конфликт,
    требующий вмешательства координатора чата.
    """
    try:
        context_str = "\n".join(context_msgs)
        triage_prompt = f"""Ты - ИИ-координатор стоматологического Telegram-чата StomChat.
Твоя задача - оценить контекст переписки и решить, действительно ли между участниками чата разгорается токсичный спор, личный конфликт или агрессивная перепалка, требующая вежливого вмешательства координатора чата.

Критерии для вмешательства (should_intervene: true):
1. Участники переходят на личности, оскорбляют друг друга, ругаются, проявляют явную агрессию.
2. Идет острая, неконструктивная перепалка с использованием токсичных выражений.

Критерии для игнорирования (should_intervene: false):
1. Коллеги ведут обычный, пусть даже эмоциональный профессиональный спор о клинических методах или материалах без личных оскорблений.
2. В сообщениях проскочило эмоциональное слово (например, "косяк", "бред", "чушь"), но оно относится к материалу, методике или клиническому случаю, а не к личности собеседника.
3. Сообщение содержит вопрос или бытовое обсуждение, а не конфликт.

Контекст переписки:
{context_str}

Отвечай СТРОГО в формате JSON без какого-либо дополнительного текста (без разметки markdown вроде ```json):
{{
  "should_intervene": true/false,
  "confidence": 0.0-1.0,
  "reason": "короткое объяснение на русском"
}}
"""
        triage_ctx = {"kind": "llama_triage", "thinking_level": "LOW"}
        response, error = await generate_gemini_text_async(triage_prompt, triage_ctx, timeout=8)
        
        if error or not response:
            logger.warning(f"Llama referee triage failed: {error}. Defaulting to False to avoid spam.")
            return False
            
        text = response.text.strip() if hasattr(response, "text") else str(response).strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                text = text[start:end+1]
        
        data = json.loads(text)
        should_intervene = data.get("should_intervene", False)
        reason = data.get("reason", "No reason provided")
        confidence = data.get("confidence", 1.0)
        
        logger.info(f"Llama Referee Triage decision: should_intervene={should_intervene} (confidence={confidence}). Reason: {reason}")
        return should_intervene
    except Exception as e:
        logger.error(f"Error in Llama referee triage: {e}. Defaulting to False.")
        return False


async def check_and_trigger_referee(bot_client, event, text):
    if text and len(text) > 1500:
        text = text[:1500] + "..."

    """Пассивный клинический рефери для предотвращения конфликтов."""
    global LAST_REFEREE_RUN
    chat_id = event.chat_id
    msg_id = event.message.id
    
    # 1. Проверяем тишину
    state = load_state()
    if is_silenced(state, "referee trigger"):
        return

    text_lower = text.lower()
    
    # 2. Исключаем обсуждение самого бота (чтобы не было автозацикливания при критике).
    #
    # Здесь стоял подстрочный поиск «бот», который живёт в «работа», «суббота»,
    # «заботиться». Замер по архиву: рефери подавлялся на 7119 сообщениях, из
    # них реально про бота были 98 — то есть 99% подавлений ложные. Слово
    # «работа» в профессиональном чате одно из самых частых, а конфликты как раз
    # вокруг работы и возникают: рефери не включался именно там, где нужен.
    if _BOT_REFERENCE_RE.search(text_lower) or (
        BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in text_lower
    ):
        logger.info("Message mentions bot, skipping referee to avoid feedback loops.")
        return

    # 3. Регулярка с границами слов для точного совпадения токсичных ключевиков
    escaped_kws = [re.escape(kw) for kw in [
        "бред", "чушь", "дичь", "херня", "говно", "полная лажа", 
        "безрукий", "руки оторвать", "какой дурак", "херню", "глупость",
        "рукожоп", "рукожопие", "помойку", "мусорку", "выброси", 
        "косяк", "ужасно", "кривые руки", "уродство", "отстой",
        "хлам", "ахинея", "ппц", "пиздец", "бредятина",
        "чушь собачья", "какой дебил", "убейся", "дебилизм",
        "идиот", "идиотизм", "тупой", "тупость", "придурок", "даун",
        "рукожопый", "криворукий", "жопорукий", "косорукий", "из жопы",
        "ересь", "чепуха", "психушка", "дурка", "лечись", "высер",
        "выкинь", "дерьмо", "говнище", "днище", "лажовый", "шиза",
        "дебил", "кретин", "олень", "баран", "тормоз", "позорище",
        "позор", "стыдоба", "срач", "клоун", "цирк", "клоунада",
        "курам на смех", "хрень", "галиматья", "шарага", "колхозный",
        "безрукие", "руки отсохнут", "убожество", "убого"
    ]]
    pattern = rf"\b({'|'.join(escaped_kws)})(е|я|ом|а|ы|и|у|ой|ем|ах|ами|ями|ов|ев)?\b"
    has_conflict_kw = bool(re.search(pattern, text_lower))
    
    should_intervene = has_conflict_kw
    chain_msgs = []
    
    # Автодетект споров по длинным цепочкам реплаев
    if not should_intervene and event.message.reply_to:
        try:
            chain_msgs = await database.get_reply_chain_texts(msg_id, max_depth=5)
            if len(chain_msgs) >= 4:
                should_intervene = await analyze_dispute_need(chain_msgs)
                if should_intervene:
                    logger.info(f"Dispute auto-detected from reply chain in msg_id={msg_id}.")
        except Exception as chain_err:
            logger.error(f"Error checking reply chain dispute: {chain_err}")
            
    if not should_intervene:
        return
        
    # 4. Собираем контекст для триажа рефери
    context_msgs = []
    if event.message.reply_to:
        try:
            context_msgs = await database.get_reply_chain_texts(msg_id, max_depth=5)
        except Exception:
            pass
            
    if not context_msgs:
        try:
            db_history = await database.get_last_n_messages(limit=5)
            db_history = db_history[::-1]
            for m in db_history:
                name = m[1] if (isinstance(m, (list, tuple)) and len(m) > 1) else "Участник"
                msg_txt = m[3] if (isinstance(m, (list, tuple)) and len(m) > 3) else ""
                if msg_txt:
                    context_msgs.append(f"{name}: {msg_txt}")
        except Exception as e:
            logger.error(f"Failed to fetch db history for referee triage: {e}")
            
    if not context_msgs:
        context_msgs = [text]

    # 5. Запускаем Llama-триаж для подтверждения конфликта
    should_reply = await check_referee_triage(context_msgs)
    if not should_reply:
        logger.info("Llama referee triage decided NOT to intervene. Cancelling referee trigger.")
        return
        
    # Разрешаем интервенции не чаще одного раза в 5 минут
    last_referee_run_str = state.get("last_referee_run")
    if last_referee_run_str:
        try:
            last_referee_run = datetime.fromisoformat(last_referee_run_str)
            if datetime.now() - last_referee_run < timedelta(minutes=5):
                logger.info("Referee cooldown: within 5 minutes. Skipping.")
                return
        except Exception as cooldown_err:
            logger.error(f"Error parsing last_referee_run: {cooldown_err}")
        
    logger.info(f"Clinical Referee triggered for msg_id={msg_id}. Deciding style (toxic={has_conflict_kw})...")
    
    # 50/50 ИЛИ ШУТИТ ИЛИ НАУЧНО
    # Если спор "злой" (есть стоп-слова) -> шутит (joke)
    # Если обычный спор -> 25% научно (scientific), 75% коллега (colleague)
    if has_conflict_kw:
        style = "joke"
    else:
        style = "scientific" if random.random() < 0.25 else "colleague"
        
    chain_str = "\n".join(chain_msgs) if chain_msgs else text
    
    if style == "joke":
        prompt = f"""
Ты - тактичный и мудрый клинический координатор стоматологического сообщества "StomChat". 
В чате начался агрессивный спор (градус эмоций высок). Последнее сообщение: "{text}".

Напиши очень короткую, спокойную, дружелюбную реплику, чтобы мягко разрядить обстановку. Можно использовать легкую, уместную стоматологическую метафору или легкий профессиональный юмор, но без заезженных клише (никакого фанатизма про "перегретые боры" или "коффердам дзен", если это не ложится идеально). 
Реплика должна призывать коллег к конструктивному общению и снижению градуса эмоций.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Длина - строго максимум 180 символов! Будь краток.
2. Никаких приветствий, обращений и концовок. Сразу суть.
3. Тон: нейтральный, вежливый, миролюбивый.
4. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
    elif style == "scientific":
        # Поиск по базе RAG
        keywords = extract_keywords(text + " " + " ".join(chain_msgs))
        wiki_corpus, _ = await search_knowledge_corpus(keywords[:12])
        
        prompt = f"""
Ты — клинический эксперт сообщества "StomChat". В чате идет профессиональный спор.
История дискуссии:
{chain_str}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(справочная информация отсутствует)"}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Напиши научно обоснованную, спокойную и примиряющую реплику на основе Справки из Базы Знаний. Разъясни доказательный клинический стандарт по теме спора, чтобы миролюбиво разрешить спор.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Важно: внимательно изучи историю дискуссии. НЕ повторяй те аргументы и тейки, которые коллеги уже озвучили в истории. Напиши новую полезную мысль.
2. Длина — максимум 280 символов! Будь лаконичен.
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
    else: # style == "colleague"
        # Поиск по базе RAG для содержательного ответа от лица коллеги
        keywords = extract_keywords(text + " " + " ".join(chain_msgs))
        wiki_corpus, _ = await search_knowledge_corpus(keywords[:12])
        
        prompt = f"""
Ты - живой практикующий врач-стоматолог, активный и уважаемый участник чата "StomChat". 
В чате идет обсуждение клинического вопроса. Твоя задача — вклиниться в беседу как умный, знающий коллега-собеседник.
История дискуссии:
{chain_str}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(нет точных справочных данных по теме)"}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

Напиши естественную реплику от лица коллеги. Вырази своё мнение, основываясь на Базе Знаний, но пиши профессиональным языком практикующего врача. Тон: сдержанный, академичный, уважительный старший коллега-эксперт. Без лишней фамильярности и без эмодзи-кривляния. Не читай нотации и не используй снисходительный тон или смайлики. Пиши с уважением к коллегам.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Важно: внимательно изучи историю дискуссии. НЕ повторяй тейки и доводы, которые коллеги уже написали в истории. Добавь свежую мысль или вежливо задай наводящий клинический вопрос, развивающий диалог.
2. Длина — максимум 320 символов! Напиши кратко и по делу.
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
4. МЕТА-ПРАВИЛО: Категорически запрещено обсуждать разработку бота, триггеры, команды, админов или притворяться живым участником чата, жалующимся на бота. Если просят помолчать — вежливо извинись одной фразой и умолкни.
"""

    status_ctx = {"kind": "group_referee", "chat_id": chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=60)
    
    if error or not response or not getattr(response, "text", None):
        return
        
    reply_text = response.text.strip()
    reply_text = clean_html_formatting(reply_text)
    
    try:
        await bot_client.send_message(
            entity=chat_id,
            message=f"⚖️ {reply_text}",
            reply_to=msg_id,
            parse_mode='html'
        )
        state = load_state()
        state["last_referee_run"] = datetime.now().isoformat()
        save_state(state)
        logger.info(f"Referee intervention ({style}) successfully sent to chat_id={chat_id}")
    except Exception as e:
        logger.error(f"Failed to send referee intervention: {e}")


async def handle_term_explainer(bot_client, event, term):
    """Быстрое объяснение стоматологического термина из базы знаний."""
    chat_id = event.chat_id
    msg_id = event.message.id
    
    cooldown = check_user_cooldown(chat_id, event.sender_id, "what", seconds=30)
    if cooldown > 0:
        await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста, подождите {cooldown} сек перед повторным запросом термина.", reply_to=msg_id)
        return
        
    # Термин уходит в промпт как есть, поэтому его длина ограничена: запрос на
    # четыре тысячи символов раздул бы промпт и вытеснил из него справку.
    term = (term or "").strip()[:TERM_EXPLAINER_MAX_CHARS]
    if not term:
        await bot_client.send_message(
            entity=chat_id,
            message="📖 <i>Укажите термин: например</i> <code>/что BOPT</code>",
            reply_to=msg_id,
            parse_mode='html',
        )
        return

    keywords = extract_keywords(term)
    wiki_corpus, _ = await search_knowledge_corpus(keywords[:12])

    prompt = f"""
Ты — толковый словарь стоматологического сообщества "StomChat".
Объясни стоматологический термин или аббревиатуру: "{term}".

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}
[КРИТИЧЕСКОЕ ПРАВИЛО ДЛЯ СПРАВКИ: Игнорируй любые факты из справки, которые не относятся напрямую к текущему вопросу. Не начинай цитировать случайную теорию или инструкции, если об этом прямо не просили!]
[КЛИНИЧЕСКИЙ ЗДРАВЫЙ СМЫСЛ: Справка и архив содержат живые чаты участников, где могут быть ошибки, заблуждения или галлюцинации. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО слепо подтверждать или копировать сомнительные, ненаучные утверждения из базы. Фильтруй всё через призму доказательной медицины (EBM), здравого клинического смысла и золотых стандартов стоматологии! Если совет из базы кажется сомнительным, устаревшим или небезопасным — укажи на это или проигнорируй его.]

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Объясни термин ровно в 1-2 предложениях (не более 350 символов). Предельно кратко и научно-популярно для коллег.
2. Никаких приветствий, «Данный термин означает...» и прочей воды. Сразу определение.
3. Разметка: только HTML (<b>жирный</b>). Никакого Markdown.
4. ЕСЛИ термин неоднозначен (несколько значений) — коротко укажи оба варианта через «; или».
5. ЕСЛИ справка пуста и термин тебе незнаком — честно напиши: «Точных данных по этому термину нет в нашей базе. Уточни у коллег!» — и ничего не выдумывай.
"""
    status_ctx = {"kind": "group_explainer", "chat_id": chat_id, "thinking_level": "MEDIUM"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=60)
    
    if error or not response or not getattr(response, "text", None):
        # Голый return оставлял врача, спросившего термин, вообще без ответа.
        logger.warning("term explainer generation failed chat=%s: %s", chat_id, error)
        await bot_client.send_message(
            entity=chat_id,
            message="⚠️ <i>Не удалось разобрать термин — модели сейчас недоступны. "
                    "Попробуйте через пару минут.</i>",
            reply_to=msg_id,
            parse_mode='html',
        )
        return
        
    reply_text = response.text.strip()
    reply_text = clean_html_formatting(reply_text)
    
    try:
        await bot_client.send_message(
            entity=chat_id,
            message=f"📖 <b>{__import__('html').escape(term).upper()}:</b> {reply_text}",
            reply_to=msg_id,
            parse_mode='html'
        )
        logger.info(f"Term explanation sent for term={term}")
    except Exception as e:
        logger.error(f"Failed to send term explanation: {e}")


PING_QUIET_START_HOUR = 22   # с 22:00 …
PING_QUIET_END_HOUR = 9      # … до 09:00 проактивных сообщений не шлём
MAX_PINGS_PER_CYCLE = 5      # чтобы джоб не занимал LLM-шлюз на минуты
MAX_PING_FAILURES = 3        # после стольких неудач подряд перестаём долбиться
# Потолок на пачку приглашений в чат. Без него бралось 20% активных кандидатов:
# при 749 врачах это до 150 личных сообщений подряд без единой паузы. По журналам
# доминирующий отказ у этого бота — обрыв связи (51 723 события), но FloodWait по
# ним НЕ измерить: telethon спит внутри вызова молча, а его логгер приглушён до
# ERROR. Так что потолок ставится по конструкции, а не по замеру, и это сказано
# прямо.
GROUP_PING_BATCH_MAX = 25
# Пауза между отправками в пачке: Telegram ограничивает не только объём, но и
# частоту. 25 сообщений по секунде — это 25 секунд на цикл, приемлемо для джоба.
GROUP_PING_DELAY_SECONDS = 1.0


def select_ping_targets(candidates, batch_max=None):
    """
    Кому из кандидатов уходит приглашение в этом цикле.

    Вынесено из тела рассылки отдельной функцией, чтобы потолок проверялся
    поведением, а не наличием константы: проверка «константа объявлена и разумна»
    проходила и после того, как применение потолка убрали, — то есть не значила
    ничего.

    Берём случайные 20% (минимум один), но не больше потолка. Без потолка при 749
    врачах уходило до 150 личных сообщений подряд, настолько быстро, насколько
    успевает сеть. Урезание пишется в журнал: разослать 25 из 150 и промолчать
    значит соврать о покрытии.
    """
    if not candidates:
        return []
    limit = GROUP_PING_BATCH_MAX if batch_max is None else batch_max
    sample_size = max(1, int(math.ceil(len(candidates) * 0.20)))
    sample_size = min(sample_size, limit, len(candidates))
    targets = random.sample(list(candidates), sample_size)
    skipped = len(candidates) - sample_size
    if skipped:
        logger.info(
            "Group ping batch capped: кандидатов %s, разослано будет %s "
            "(потолок %s) — остальные %s попадут в следующие циклы",
            len(candidates), sample_size, limit, skipped,
        )
    return targets


def is_ping_quiet_hours(now=None):
    """
    Ночное окно, когда проактивные сообщения запрещены.

    Планировщик крутится круглосуточно, и "как продвигается твой случай?"
    или "🔥 в чате горячо спорят" прилетало в 03:40. Часовой пояс конкретного
    врача нам неизвестен, поэтому ориентируемся на локальное время бота —
    аудитория чата в основном с ним в одном поясе.
    """
    hour = (now or datetime.now()).hour
    if PING_QUIET_START_HOUR > PING_QUIET_END_HOUR:  # окно переходит через полночь
        return hour >= PING_QUIET_START_HOUR or hour < PING_QUIET_END_HOUR
    return PING_QUIET_START_HOUR <= hour < PING_QUIET_END_HOUR


def commit_pm_ping(chat_id_str, **fields):
    """
    Точечно обновляет ОДНУ запись пингов, перечитывая состояние с диска.

    Цикл пингов идёт минутами: LLM-вызов на пользователя плюс трёхсекундный
    шаг глобального гейта. Прежний вариант держал снимок состояния всё это
    время и сохранял его одним куском в конце, откатывая last_activity,
    записанный handle_private_message в это же окно: врач писал боту в 03:05,
    а в 04:00 получал "ты пропал на два дня".
    """
    state = load_state()
    entry = state.setdefault("pm_pings", {}).setdefault(str(chat_id_str), {})
    entry.update(fields)
    save_state(state)


def drop_pm_ping(chat_id_str):
    """Удаляет запись пингов и СРАЗУ сохраняет."""
    state = load_state()
    if state.setdefault("pm_pings", {}).pop(str(chat_id_str), None) is not None:
        save_state(state)


def set_ping_opt_out(chat_id, reason=""):
    """Врач попросил не писать — больше проактивных сообщений не отправляем."""
    commit_pm_ping(chat_id, pings_opted_out=True, opt_out_reason=reason[:120])
    logger.info(f"User {chat_id} opted out of proactive pings. Reason: {reason[:80]!r}")


async def check_and_send_pm_pings(bot_client):
    """Проверяет неактивных пользователей в ЛС и отправляет им персонализированный пинг."""
    try:
        if is_ping_quiet_hours():
            logger.debug("PM pings skipped: quiet hours.")
            return

        state = load_state()
        pings = state.get("pm_pings", {})
        if not pings:
            return

        now = datetime.now()
        updated = False
        sent_this_cycle = 0

        for chat_id_str, info in list(pings.items()):
            try:
                if sent_this_cycle >= MAX_PINGS_PER_CYCLE:
                    logger.info(f"PM ping cycle limit reached ({MAX_PINGS_PER_CYCLE}); rest will be handled next hour.")
                    break

                # Отписавшихся не трогаем никогда.
                if info.get("pings_opted_out"):
                    continue

                # Заблокировавший бота пользователь раньше получал свежий
                # 60-секундный LLM-вызов и попытку отправки КАЖДЫЙ час
                # бесконечно: ping_sent выставлялся только после успеха.
                if info.get("ping_failures", 0) >= MAX_PING_FAILURES:
                    continue

                last_activity = _parse_state_dt(info.get("last_activity"))
                ping_sent = info.get("ping_sent", False)
                unanswered_pings = info.get("unanswered_pings", 0)

                # Если пользователь пропустил даже 1 пинг — никогда больше не навязываемся
                if unanswered_pings >= 1 or ping_sent:
                    continue

                # Редкий график: первый и единственный вежливый фоллоу-ап через 7 дней (168 ч)
                delay_hours = 168

                # Если прошла неделя с момента переписки и пинг еще не отправлялся
                if now - last_activity > timedelta(hours=delay_hours):
                    chat_id = int(chat_id_str)
                    days_ago = 7
                    logger.info(
                        f"Generating proactive DM ping for chat_id={chat_id} "
                        f"(unanswered={unanswered_pings}, delay={delay_hours}h)..."
                    )
                    
                    # Загружаем последние сообщения и профиль, чтобы сформировать контекстный живой пинг
                    history = await database.get_last_pm_messages(chat_id, limit=6)
                    context_str = "\n".join([f"{m['sender_name']}: {m['text']}" for m in history])
                    
                    user_profile = await database.get_user_profile(chat_id)
                    portrait = user_profile.get("profile_portrait") or ""
                    portrait_hint = f"\nКлинический профиль врача: {portrait}\n" if portrait else ""

                    prompt = f"""Ты — опытный старший коллега-стоматолог из сообщества "StomChat". 
Врач-стоматолог обращался к тебе в ЛС {days_ago} дней назад.
{portrait_hint}
История вашей последней переписки:
{context_str}

Задачи:
1. Проанализируй переписку: обсуждался ли в ней РЕАЛЬНЫЙ клинический случай, сложный пациент, снимки, методика или выбор тактики лечения (например: эндодонтия зуба, боль после вмешательства, скол, фиксация, анестезия сложного пациента, имплантат)?
2. ЕСЛИ реального клинического кейса НЕ БЫЛО (врач просто открывал меню, нажимал кнопки, тестировал команды или задавал пустые общие вопросы) — верни СТРОГО ОДНО СЛОВО: NONE. Запрещено навязываться и слать пустые приветствия!
3. ЕСЛИ обсуждался реальный клинический случай — напиши строго 1 короткое, персонализированное предложение от коллеги к коллеге, вежливо поинтересовавшись динамикой и исходом этого конкретного случая (например: «Коллега, как динамика по зубу 3.6? Удалось пройти канал?»).
4. Тон: сдержанный, уважительный, профессиональный. Без панибратства, без фамильярности («ты куда пропал»), без спама.
5. Разметка: только HTML (<b>жирный</b>, <i>курсив</i>).
"""
                    status_ctx = {"kind": "pm_ping", "chat_id": chat_id, "thinking_level": "HIGH"}
                    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=60)
                    
                    if not error and response and getattr(response, "text", None):
                        reply_text = response.text.strip()
                        if not reply_text or reply_text.upper() == "NONE" or len(reply_text) < 15:
                            logger.info(f"PM ping for chat_id={chat_id}: no clinical case to follow up (NONE).")
                            commit_pm_ping(chat_id_str, ping_sent=True, ping_failures=0, unanswered_pings=1)
                            continue

                        reply_text = clean_html_formatting(reply_text)

                        # Генерация заняла десятки секунд. Перечитываем запись:
                        # врач мог написать сам, пока мы сочиняли ему "ты пропал".
                        fresh = load_state().get("pm_pings", {}).get(chat_id_str, {})
                        if fresh.get("pings_opted_out") or fresh.get("ping_sent", False):
                            continue
                        if datetime.now() - _parse_state_dt(fresh.get("last_activity")) <= timedelta(hours=delay_hours):
                            logger.info(f"User {chat_id} became active while ping was generating. Skipping.")
                            continue

                        try:
                            await bot_client.send_message(entity=chat_id, message=reply_text, parse_mode='html')
                            await database.save_pm_message(chat_id, "Assistant", reply_text)
                            # Коммитим сразу, увеличивая счетчик неотвеченных пингов
                            commit_pm_ping(
                                chat_id_str,
                                ping_sent=True,
                                ping_failures=0,
                                unanswered_pings=unanswered_pings + 1,
                                last_ping_time=datetime.now().isoformat(),
                            )
                            sent_this_cycle += 1
                        except ValueError as ve:
                            if "Could not find the input entity" in str(ve):
                                logger.warning(f"User {chat_id} entity not found. Removing from PM pings.")
                                drop_pm_ping(chat_id_str)
                                continue
                            raise
                        except Exception as send_err:
                            if tg_safety.classify(send_err) == tg_safety.KIND_FLOOD:
                                wait_seconds = tg_safety.flood_wait_seconds(send_err)
                                logger.warning(
                                    "DM ping hit FloodWait chat_id=%s wait=%ss — счётчик "
                                    "НЕ увеличен (это наша скорость, не врач), рассылка "
                                    "остановлена до следующего цикла",
                                    chat_id, wait_seconds,
                                )
                                break
                            failures = info.get("ping_failures", 0) + 1
                            commit_pm_ping(chat_id_str, ping_failures=failures)
                            logger.warning(
                                f"Failed to deliver DM ping to {chat_id} "
                                f"(failure {failures}/{MAX_PING_FAILURES}): {send_err}"
                            )
                            continue

                        updated = True
                        logger.info(f"Proactive DM ping sent to chat_id={chat_id}: '{reply_text}'")
                    else:
                        logger.error(f"Failed to generate DM ping for chat_id={chat_id}: {error}")
            except Exception as e:
                logger.error(f"Error processing DM ping for user {chat_id_str}: {e}")

        if updated:
            logger.info(f"PM ping cycle finished: {sent_this_cycle} ping(s) delivered.")
    except Exception as g_err:
        logger.error(f"Global error in check_and_send_pm_pings: {g_err}")


async def check_and_send_group_activity_pings(bot_client):
    """Отключено: безадресная рассылка пингов в ЛС о событиях в группе порождала спам и блокировки бота."""
    return
