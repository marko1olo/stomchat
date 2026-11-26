# -*- coding: utf-8 -*-
"""
intent_router.py — Архитектура классификации намерений (Zero-Slash Routing),
группового триажа и арбитража клинических споров StomChat/EndoChat.

Функционал:
1. Zero-Slash Intent Router: мгновенная детерминированная классификация запросов
   врача (веб-поиск, калькулятор, квиз, симулятор кейсов, закладки, стиль, меню/помощь)
   с защитой от ложных срабатываний на клинические протоколы, экстренную помощь и соматику.
2. Group Triage Engine: пассивный анализ активности в группах. Защита от спама,
   отсечение бытового шума и коротких реплик, соблюдение административного регламента тишины.
3. Clinical Dispute Detector & Referee: дифференциация здоровой профессиональной дискуссии
   от токсичного конфликта и опасной псевдонауки. Активация режима рефери ТОЛЬКО при реальной необходимости.
4. Anti-Mentor & Peer-to-Peer Protocol: категорический запрет менторского, поучающего тона,
   нотаций и судейских канцеляризмов. Бот — это уважаемый коллега-эксперт у кресла.
5. In-Flight Lock & Concurrency Manager: защита от race conditions и спама при одновременных
   сообщениях от нескольких врачей в активном групповом чате.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("intent_router")

# ── Константы намерений (Zero-Slash Routing) ──────────────────────────────────
INTENT_WEB_SEARCH = "INTENT_WEB_SEARCH"
INTENT_CALCULATOR = "INTENT_CALCULATOR"
INTENT_QUIZ = "INTENT_QUIZ"
INTENT_CASE = "INTENT_CASE"
INTENT_BOOKMARKS = "INTENT_BOOKMARKS"
INTENT_STYLE = "INTENT_STYLE"
INTENT_MENU = "INTENT_MENU"
INTENT_HELP = "INTENT_HELP"
INTENT_CLINICAL_DISPUTE = "INTENT_CLINICAL_DISPUTE"
INTENT_PASSIVE_ENGAGEMENT = "INTENT_PASSIVE_ENGAGEMENT"
INTENT_CLINICAL_CHAT = "CLINICAL_CHAT"


# ── Представление распознанного намерения ─────────────────────────────────────
class UserIntent:
    """Представление распознанного намерения пользователя (Zero-Slash Routing)."""
    def __init__(self, name: Optional[str], query: str = ""):
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


# ── Модели группового триажа и споров ─────────────────────────────────────────
@dataclass
class GroupTriageDecision:
    """Вердикт группового триажа по входящему сообщению."""
    should_respond: bool
    action_mode: str  # "IGNORE", "DIRECT_SUMMON", "CLINICAL_DISPUTE_REFEREE", "PASSIVE_CASE_ASSIST", "RESCUE"
    confidence: float
    reason: str
    cleaned_query: str = ""


@dataclass
class DisputeAnalysisResult:
    """Результат анализа дискуссии на наличие клинического спора."""
    is_dispute: bool
    dispute_type: str  # "NONE", "HEALTHY_DEBATE", "TOXIC_CONFLICT", "DANGEROUS_PSEUDOSCIENCE", "CALL_FOR_ARBITRATION"
    should_intervene: bool
    reason: str
    key_points: List[str] = field(default_factory=list)


# ── Регулярные выражения и лексические паттерны ──────────────────────────────
_ANESTHETIC_DRUGS = r'(артикаин|ультракаин|убистезин|септонест|скандонест|мепивакаин|лидокаин|новокаин|бупивакаин|примакаин|брилокаин|анестетик)'

_NON_ANESTHETIC_PHARMA = re.compile(
    r'\b(?:амоксиклав|амоксициллин|аугментин|ибупрофен|парацетамол|нурофен|линкомицин|ципролет|'
    r'цефтриаксон|кеторол|нимесил|найз|супрастин|дексаметазон|антибиотик|нпвс|нпвп)\w*\b',
    re.IGNORECASE
)

_SOMATIC_EMERGENCY_RE = re.compile(
    r'\b(?:криз|инфаркт|стенокарди|преэклампси|эклампси|блокад|аритми|гипертензи|давлен|'
    r'кардиостимулятор|пейсмейкер|порок|аллерги)\w*\b',
    re.IGNORECASE
)

_EMERGENCY_HELP_RE = re.compile(
    r'\b(?:первая|неотложная|скорая|доврачебная|оказание)\s+помощ|\bпомощь\s+(?:при|пациент|взросл|дет)',
    re.IGNORECASE
)

_REAL_CASE_MARKERS_RE = re.compile(
    r'пациент(ка)?\s+\d+|жалобы\s+на|у\s+меня\s+(?:кейс|клинический\s+случай|пациент)|'
    r'разбор\s+(?:кейса|случая)|клинический\s+случай\s*:|\b(?:зуб|зуба|зубе|зубом)\s+\d{2}\b|\b\d{2}\s+(?:зуб|зуба|зубе|зубом)\b',
    re.IGNORECASE
)

_QUIZ_ANSWER_RE = re.compile(
    r'^(?:мой\s+ответ|ответ|вариант)\s+[a-dа-г]\b',
    re.IGNORECASE
)

# Токсичные маркеры и ключевые фразы конфликта
_CONFLICT_PHRASES = [
    "где вас учили", "кто так делает", "кто так препарирует", "бедный зуб",
    "бедный пациент", "вы протокол читали", "вы вообще стоматолог",
    "удалять и только удалять", "это под удаление", "зачем полезли", "что за работа",
    "курам на смех", "полная лажа", "чушь собачья",
    "руки оторвать", "руки отсохнут", "из жопы", "руки из жопы",
    "рассудите нас", "кто прав", "давайте литературу", "позовите рефери"
]
_SINGLE_CONFLICT_KWS = [
    "бред", "чушь", "дичь", "херня", "говно", "безрукий", "какой дурак",
    "херню", "рукожоп", "рукожопие", "помойку", "мусорку",
    "кривые руки", "уродство",
    "ахинея", "ппц", "пиздец", "бредятина", "какой дебил", "убейся",
    "дебилизм", "идиот", "идиотизм", "тупой", "тупость", "придурок", "даун",
    "рукожопый", "криворукий", "жопорукий", "косорукий", "ересь", "чепуха",
    "психушка", "дурка", "высер", "дерьмо", "говнище",
    "днище", "лажовый", "шиза", "дебил", "кретин", "олень", "баран", "тормоз",
    "позорище", "позор", "стыдоба", "срач", "клоун", "цирк", "клоунада",
    "хрень", "галиматья", "шарага", "колхозный", "безрукие", "убожество",
    "убого", "бракодел", "халтура", "калечите", "калечить", "безграмотность"
]
_CONFLICT_RE = re.compile(
    rf"(\b({'|'.join(re.escape(k) for k in _SINGLE_CONFLICT_KWS)})(е|я|ом|а|ы|и|у|ой|ем|ах|ами|ями|ов|ев)?\b|{'|'.join(re.escape(p) for p in _CONFLICT_PHRASES)})",
    re.IGNORECASE
)

# Опасные и запрещенные методики (псевдонаука/ятрогения)
_DANGEROUS_METHODS_RE = re.compile(
    r"\b(?:резорцин[- ]?формалин\w*|форедент|депофорез\w*|мышьяк\w*|парапаста|триоксидент\s+в\s+пульпу|крезодент\s+в\s+канал)\b",
    re.IGNORECASE
)

# Тривиальный шум и бытовые реплики
_TRIVIAL_NOISE_RE = re.compile(
    r"^(?:ок|ок[ее]й|ладно|ясно|понял|понятно|спс|спасибо|благодарю|привет|хай|ку|"
    r"добрый\s+день|доброе\s+утро|добрый\s+вечер|здравствуйте|здравствуй|"
    r"да|нет|ага|угу|неа|плюс|\+|\+1|согласен|поддерживаю|точно|факт|правда|"
    r"лол|хах\w*|хех\w*|хихи\w*|кек|ахах\w*|ппц|хм+|ну+|ой)$",
    re.IGNORECASE
)

# Жалобы на работу бота
_BOT_COMPLAINT_RE = re.compile(
    r"\b(?:бот|нейросет\w*|ассистент|гпт|gpt)\b.*\b(?:сломал\w*|завис\w*|глючит\w*|бредит\w*|тупит\w*|ошиб\w*|надоел\w*|спамит\w*)|"
    r"\b(?:сломал\w*|завис\w*|глючит\w*|бредит\w*|тупит\w*|ошиб\w*|надоел\w*|спамит\w*)\b.*\b(?:бот|нейросет\w*|ассистент|гпт|gpt)|"
    r"\b(?:он|ты)\s+(?:сломал\w*|завис\w*|глючит\w*|бредит\w*|тупит\w*|ошибся|гонишь|надоел\w*)|"
    r"(?:^\s*(?:хватит|перестань|отстань|не\s+пиши|без\s+спама|стоп)(?:[!.\s]*$|[,!]\s+(?:бот|спам\w*|уже|писать|отвечать|флуд\w*)))|"
    r"\b(?:хватит|перестань)\s+(?:писать|спамить|отвечать|флудить|лезть|бредить)\b|"
    r"\b(?:отстань|не\s+пиши|без\s+спама)\b",
    re.IGNORECASE
)


# ── Zero-Slash Intent Classifier ─────────────────────────────────────────────
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
    if not _EMERGENCY_HELP_RE.search(norm):
        if norm_no_punct in (
            'меню', 'главное меню', 'открой меню', 'покажи меню', 'кнопки меню',
            'вызови меню', 'назад в меню', 'открой главное меню', 'покажи главное меню',
            '/menu', '/меню', '/start', '⌨️ меню'
        ) or re.match(r'^(?:открой|покажи|вызови|перейди\s+в|вернуться\s+в|назад\s+в)?\s*(?:главное\s+)?меню$', norm_no_punct):
            return UserIntent(INTENT_MENU)

        if norm_no_punct in (
            'помощь', 'хелп', 'help', 'справка', 'памятка', 'инструкция', 'инструкция к боту', 'команды', 'список команд', '/help'
        ) or re.match(r'^(что\s+ты\s+умеешь|что\s+ты\s+можешь|что\s+умеешь|что\s+можешь|твои\s+возможности|возможности\s+бота)$', norm_no_punct) \
          or re.match(r'^(как\s+тобой\s+пользоваться|как\s+пользоваться\s+ботом|как\s+пользоваться)$', norm_no_punct) \
          or re.match(r'^(список\s+команд|какие\s+команды)$', norm_no_punct):
            return UserIntent(INTENT_MENU)

    # 2. INTENT_STYLE
    if norm_no_punct in ('стиль', 'стиль общения', 'настройка стиля', 'настройки стиля', 'выбор стиля', '/style') \
       or re.match(r'^(смени|сменить|измени|изменить|поменяй|поменять|переключи|переключить|настрой|настроить|выбери|выбрать)\s+(стиль|стиль\s+общения|тон|тон\s+общения)$', norm_no_punct) \
       or re.match(r'^хочу\s+другой\s+(стиль|тон)$', norm_no_punct) \
       or re.match(r'^(смени|поменяй|измени)\s+тон$', norm_no_punct) \
       or re.match(r'^(настройка|настройки|выбор)\s+(стиля|тона)$', norm_no_punct):
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
    if not _QUIZ_ANSWER_RE.match(norm):
        if norm_no_punct in ('викторина', 'квиз', 'клиническая викторина', 'стоматологический квиз', 'тест по стоматологии', 'клинический квиз', 'клиническая задача', '/quiz') \
           or re.match(r'^(давай|хочу|запусти|проведи|сыграем\s+в|поиграем\s+в|го)\s+(клиническую\s+)?(викторину|квиз)$', norm_no_punct) \
           or re.match(r'^(проверь|проэкзаменуй|протестируй)\s+(мои\s+)?(знания|меня)$', norm_no_punct) \
           or re.match(r'^(дай|задай|хочу)\s+(мне\s+)?(клинический\s+)?(вопрос|задачу|задачку|тест)$', norm_no_punct) \
           or re.match(r'^хочу\s+тест$', norm_no_punct):
            return UserIntent(INTENT_QUIZ)

    # 5. INTENT_CASE
    if not _REAL_CASE_MARKERS_RE.search(norm):
        if norm_no_punct in (
            'клинический кейс', 'клинический симулятор', 'симулятор', 'интерактивный кейс',
            'диагностический симулятор', 'симулятор кейсов', '/case'
        ) or re.match(r'^(давай|хочу|запусти|начни|начать|сыграем\s+в|поиграем\s+в|го|включи)\s+(клинический\s+)?(кейс|симулятор)$', norm_no_punct) \
          or re.match(r'^(давай|хочу|запусти|начни|начать)\s+клинический\s+случай$', norm_no_punct) \
          or re.match(r'^(сыграем|поиграем|сыграть|поиграть)\s+в\s+диагностику$', norm_no_punct) \
          or re.match(r'^(сыграть|поиграть)\s+в\s+(кейс|симулятор)$', norm_no_punct) \
          or re.match(r'^(давай|хочу|запусти|включи|начни|начать)\s+симулятор$', norm_no_punct) \
          or re.match(r'^начать\s+кейс$', norm_no_punct):
            return UserIntent(INTENT_CASE)

    # 6. INTENT_CALCULATOR
    if norm_no_punct in (
        'калькулятор', 'калькулятор анестезии', 'шпаргалка по анестезии', 'расчет анестезии',
        'расчёт анестезии', 'дозы анестетиков', 'максимальная доза анестезии', 'расчет карпул',
        'расчёт карпул', '/calc'
    ):
        return UserIntent(INTENT_CALCULATOR, clean)

    # Red-Team Guard: исключаем запросы антибиотиков, НПВС и тяжелой соматики
    if not _NON_ANESTHETIC_PHARMA.search(norm) and not _SOMATIC_EMERGENCY_RE.search(norm):
        if re.match(r'^(посчитай|рассчитай|расчет|расчёт|калькулятор)\s+(мне\s+)?(анестези[юия]|дозировк[уи]|доз[уа]|карпул[ы]?)\b', norm_no_punct) \
           or re.match(r'^(посчитай|рассчитай)\s+доз[уа]\b', norm_no_punct) \
           or re.match(rf'^(сколько|какая)\s+(карпул|дозировка|доза|максимальная\s+доза)\s+(?:препарата\s+)?{_ANESTHETIC_DRUGS}[а-я]*\b', norm_no_punct) \
           or re.match(rf'^(дозировк[аиеу]?|доза|дозы|расчет|расчёт)\s+(препарата\s+)?{_ANESTHETIC_DRUGS}[а-я]*\b', norm_no_punct) \
           or re.match(rf'^(посчитай|рассчитай)\s+(дозировку\s+)?{_ANESTHETIC_DRUGS}[а-я]*\b', norm_no_punct) \
           or re.match(r'^(дозировка|расчет|расчёт)\s+анестезии\b', norm_no_punct):
            return UserIntent(INTENT_CALCULATOR, clean)

    # 7. INTENT_WEB_SEARCH
    m_goog = re.match(r'^(?:погугли|загугли|гугли|погуглить)\s*(.*)$', norm, re.IGNORECASE)
    if m_goog:
        prefix_len = len(norm) - len(m_goog.group(1))
        q = clean[prefix_len:].strip()
        return UserIntent(INTENT_WEB_SEARCH, q)

    m_net = re.match(r'^(?:поищи|найди|поиск)\s+в\s+(?:интернете|инете|сети|гугле|google)\s*(.*)$', norm, re.IGNORECASE)
    if m_net:
        prefix_len = len(norm) - len(m_net.group(1))
        q = clean[prefix_len:].strip()
        return UserIntent(INTENT_WEB_SEARCH, q)

    m_art = re.match(r'^(?:найди|поищи)\s+статьи(?:\s+(?:про|о|об|по|для)\b)?\s*(.*)$', norm, re.IGNORECASE)
    if m_art:
        prefix_len = len(norm) - len(m_art.group(1))
        raw_q = clean[prefix_len:].strip()
        raw_q = re.sub(r'^(?:про|о|об|по|для)\s+', '', raw_q, flags=re.IGNORECASE).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    m_pub_full = re.match(r'^(?:статьи\s+на\s+pubmed|публикации\s+на\s+pubmed)(?:\s+(?:про|о|об|по|для)\b)?\s*(.*)$', norm, re.IGNORECASE)
    if m_pub_full:
        prefix_len = len(norm) - len(m_pub_full.group(1))
        raw_q = clean[prefix_len:].strip()
        raw_q = re.sub(r'^(?:про|о|об|по|для)\s+', '', raw_q, flags=re.IGNORECASE).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    m_pub = re.match(r'^(?:что|посмотри\s+что)\s+(?:говорит|пишет|есть\s+в)\s+pubmed(?:\s+(?:про|о|об|по)\b)?\s*(.*)$', norm, re.IGNORECASE)
    if m_pub:
        prefix_len = len(norm) - len(m_pub.group(1))
        raw_q = clean[prefix_len:].strip()
        raw_q = re.sub(r'^(?:про|о|об|по)\s+', '', raw_q, flags=re.IGNORECASE).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    m_pub_short = re.match(r'^(?:pubmed|пабмед)\s*[:\s]\s*(.*)$', norm, re.IGNORECASE)
    if m_pub_short:
        prefix_len = len(norm) - len(m_pub_short.group(1))
        q = clean[prefix_len:].strip()
        return UserIntent(INTENT_WEB_SEARCH, q)

    m_res = re.match(r'^(?:какие|есть\s+ли)\s+(?:свежие|новые|последние|научные)\s+исследования(?:\s+(?:по|про|о|об)\b)?\s*(.*)$', norm, re.IGNORECASE)
    if m_res:
        prefix_len = len(norm) - len(m_res.group(1))
        raw_q = clean[prefix_len:].strip()
        raw_q = re.sub(r'^(?:по|про|о|об)\s+', '', raw_q, flags=re.IGNORECASE).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    # Протоколы через "найди протокол"
    m_proto = re.match(r'^(?:найди|поищи)\s+протокол(?:\s+(?:про|о|об|по|для)\b)?\s*(.*)$', norm, re.IGNORECASE)
    if m_proto:
        prefix_len = len(norm) - len(m_proto.group(1))
        raw_q = clean[prefix_len:].strip()
        raw_q = re.sub(r'^(?:про|о|об|по|для)\s+', '', raw_q, flags=re.IGNORECASE).strip()
        return UserIntent(INTENT_WEB_SEARCH, raw_q)

    return UserIntent(None)


# ── Мгновенный расчет анестезии ───────────────────────────────────────────────
def calculate_anesthesia_instant(text: str) -> Optional[str]:
    """
    Выполняет мгновенный клинический расчет дозировки анестезии и допустимого количества карпул
    через эталонный клинический движок anesthesia_calc.
    """
    if not text:
        return None

    try:
        import assistant
        ped_guard = getattr(assistant, "check_pediatric_anesthesia_safety", None)
        if ped_guard:
            res_ped = ped_guard(text)
            if res_ped and getattr(res_ped, "get", None) and res_ped.get("is_pediatric"):
                return res_ped.direct_response
    except Exception:
        pass

    try:
        import anesthesia_calc
        res = anesthesia_calc.calculate_anesthesia_dose(text)
        if res.is_valid:
            return res.html_report

        drug_key = anesthesia_calc.detect_anesthetic_drug(text)
        if "mepivacaine" in drug_key:
            return (
                "🧮 <b>Расчет дозировки: Мепивакаин 3% (Скандонест без вазоконстриктора)</b>\n\n"
                "• <b>Норма:</b> 4.4 мг/кг.\n"
                "• <b>Абсолютный максимум:</b> не более <b>400 мг</b> (≈ 7.4 карпулы по 1.8 мл).\n"
                "• <b>1 карпула 1.8 мл 3%:</b> = <b>54 мг</b> мепивакаина гидрохлорида.\n\n"
                "💡 <i>Укажите вес пациента для точного расчета, например: «дозировка скандонеста на 60 кг» или «скандонест ребенку 20 кг».</i>"
            )
        elif "lidocaine" in drug_key:
            return (
                "🧮 <b>Расчет дозировки: Лидокаин 2% (с адреналином)</b>\n\n"
                "• <b>Норма:</b> 7 мг/кг (взрослые), 4.4 мг/кг (дети).\n"
                "• <b>Абсолютный максимум:</b> не более <b>500 мг</b> (≈ 13.8 карпул по 1.8 мл).\n"
                "• <b>1 карпула 1.8 мл 2%:</b> = <b>36 мг</b> лидокаина гидрохлорида.\n\n"
                "💡 <i>Укажите вес пациента для точного расчета, например: «лидокаин на 70 кг».</i>"
            )
        elif "articaine" in drug_key:
            return (
                "🧮 <b>Расчет дозировки: Артикаин 4% (с эпинефрином 1:100 000 / 1:200 000)</b>\n\n"
                "• <b>Норма:</b> 7 мг/кг (взрослые), 5 мг/кг (дети от 4 лет).\n"
                "• <b>Абсолютный максимум:</b> не более <b>500 мг</b> (≈ 7 карпул по 1.7 мл).\n"
                "• <b>1 карпула 1.7 мл 4%:</b> = <b>68 мг</b> артикаина гидрохлорида.\n\n"
                "💡 <i>Укажите вес пациента для точного расчета, например: «артикаин на 70 кг».</i>"
            )
    except Exception as e:
        logger.debug(f"calculate_anesthesia_instant execution error: {e}")

    return None


# ── Групповой триаж и пассивная вовлеченность ──────────────────────────────────
def should_passively_engage(
    text: str,
    has_media: bool = False,
    is_group: bool = True,
    chat_id: int = 0,
    sender_id: int = 0,
    is_admin_or_bot: bool = False,
    recent_texts: Optional[List[str]] = None
) -> Tuple[bool, str]:
    """
    Системный аудит пассивного вовлечения в групповом чате:
    - Бот не должен встревать в бытовые шутки, короткие реплики и неклинический флуд.
    - Бот не должен спамить в чате. Незваный пассивный ответ строго подавлен,
      если отключен в конфигурации (ENABLE_PASSIVE_TEXT=False / ENABLE_PASSIVE_MEDIA=False).
    - Возвращает (should_engage: bool, reason: str).
    """
    if not is_group:
        return False, "not_a_group"

    if is_admin_or_bot:
        return False, "admin_or_bot_sender"

    # 1. Проверка конфигурации активности
    try:
        import config
        summon_only = getattr(config, "SUMMON_ONLY_GROUP_MODE", False)
        if summon_only:
            return False, "summon_only_mode_enabled"

        if has_media:
            enable_media = getattr(config, "ENABLE_PASSIVE_MEDIA", False)
            if not enable_media:
                return False, "passive_media_disabled_by_policy"
        else:
            enable_text = getattr(config, "ENABLE_PASSIVE_TEXT", False)
            if not enable_text:
                return False, "passive_text_disabled_by_policy"
    except Exception as e:
        logger.debug(f"Config check in should_passively_engage failed: {e}")
        return False, "config_error"

    # 2. Фильтрация бытового шума и коротких реплик
    clean = (text or "").strip()
    if not has_media:
        if len(clean) < 15:
            return False, "too_short_text_noise"
        if _TRIVIAL_NOISE_RE.match(clean):
            return False, "trivial_chit_chat_noise"
        if _BOT_COMPLAINT_RE.search(clean):
            return False, "meta_bot_complaint_ignored"

    # 3. Фильтрация личных обращений к другим людям (@username)
    mentions = re.findall(r"@\w{4,}", clean)
    if mentions:
        try:
            bot_username = (getattr(config, "BOT_USERNAME", "") or "").lstrip("@").strip().lower()
        except Exception:
            bot_username = ""
        other_mentions = [m for m in mentions if not bot_username or m.lower() != f"@{bot_username}"]
        if other_mentions:
            return False, "addressed_to_other_user"

    return True, "clinical_engagement_allowed"


# ── Детектор клинического спора и режим арбитража ─────────────────────────────
def detect_clinical_dispute(
    text: str,
    context_msgs: Optional[List[str]] = None
) -> DisputeAnalysisResult:
    """
    Определяет, возник ли в чате реальный клинический спор, требующий вмешательства рефери.
    СТРОГО разграничивает:
    1. Здоровая коллегиальная дискуссия (сохранять vs удалять, штифт vs вкладка, биокерамика vs латералка)
       -> should_intervene: False (врачи имеют право спорить без цензуры бота).
    2. Острая токсичная перепалка с взаимными оскорблениями ("бракодел", "где вас учили", "руки оторвать")
       -> should_intervene: True (разрядка обстановки через объективные научные данные EBM).
    3. Агрессивное навязывание заведомо опасных или ятрогенных методик (резорцин, мышьяк)
       -> should_intervene: True (клиническая безопасность).
    """
    if not text:
        return DisputeAnalysisResult(False, "NONE", False, "empty_text")

    clean = text.strip()

    # 1. Проверка на опасные ятрогенные методики
    if _DANGEROUS_METHODS_RE.search(clean):
        return DisputeAnalysisResult(
            is_dispute=True,
            dispute_type="DANGEROUS_PSEUDOSCIENCE",
            should_intervene=True,
            reason="Зафиксировано навязывание опасных/запрещенных клинических методик (резорцин/мышьяк/депофорез)."
        )

    # 2. Проверка токсичных маркеров конфликта
    has_conflict_kw = bool(_CONFLICT_RE.search(clean))
    if has_conflict_kw:
        return DisputeAnalysisResult(
            is_dispute=True,
            dispute_type="TOXIC_CONFLICT",
            should_intervene=True,
            reason="Обнаружен переход на личности или острая эмоциональная перепалка между коллегами."
        )

    # 3. Анализ длины цепочки дискуссии (реплик коллег)
    if context_msgs and len(context_msgs) >= 4:
        context_str = " ".join(context_msgs).lower()
        if _CONFLICT_RE.search(context_str):
            return DisputeAnalysisResult(
                is_dispute=True,
                dispute_type="TOXIC_CONFLICT",
                should_intervene=True,
                reason="Конфликт обнаружен в контексте предыдущих сообщений цепочки дискуссии."
            )
        # Профессиональная, но мирная дискуссия: вмешательство не требуется
        return DisputeAnalysisResult(
            is_dispute=True,
            dispute_type="HEALTHY_DEBATE",
            should_intervene=False,
            reason="Идет мирная профессиональная дискуссия коллег. Вмешательство рефери не требуется."
        )

    return DisputeAnalysisResult(
        is_dispute=False,
        dispute_type="NONE",
        should_intervene=False,
        reason="Обычный диалог без признаков спора или конфликта."
    )


# ── Системные инструкции рефери: Запрет менторства и поучений ────────────────
DISPUTE_REFEREE_SYSTEM_INSTRUCTIONS = """
Ты — практикующий врач-стоматолог, опытный коллега у кресла в профессиональном Telegram-сообществе.
Твоя цель — разрядить клинический спор или перепалку коллег ТОЛЬКО за счет твердых клинических фактов доказательной медицины, объективных биомеханических критериев и спокойного авторитета практики.

КАТЕГОРИЧЕСКИЕ ТРЕБОВАНИЯ И АНТИ-МЕНТОРСКИЙ СТАНДАРТ:
1. ЗАПРЕТ МЕНТОРСТВА И ПОУЧЕНИЙ:
   - Ты — коллега-эксперт у кресла, а не строгий школьный учитель и не моралист.
   - НИКАКИХ нотаций, поучений, нравоучений, призывов «давайте жить дружно» или лекций по этике.
   - Общайся на равных, уважительно, строго по клинической сути вопроса.
2. ЗАПРЕТ СУДЕЙСКИХ ШТАМПОВ И АКАДЕМИЧЕСКИХ АББРЕВИАТУР (ZERO ACRONYMS):
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать канцелярские ярлыки: «Вердикт:», «EBM-Арбитраж:», «Арбитраж:», «Решение:», «Заключение:», «Резюме:».
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО сыпать в текст аббревиатурами ассоциаций (ESE, AAE, ITI, Cochrane, EBM)! Врачи у кресла воспринимают это как занудную теорию.
   - Начинай сразу с клинического анализа ситуации и прикладных фактов.
3. ПРИНЦИП КЛИНИЧЕСКОЙ СУТИ:
   - Обозначь границы применимости обеих методик (когда оправдан протокол А, а когда протокол Б).
   - Приводи конкретные цифры выживаемости, четкие критерии (феррул >=1.5 мм, остаточная толщина дентина >1 мм, герметизм).
4. ОБЪЕМ И РАЗМЕТКА:
   - Ответ должен быть емким: до 350–450 символов.
   - Форматирование: только чистый HTML (<b>жирный</b>). Без Markdown.

КОНТРАСТНЫЕ ПРИМЕРЫ (СХЕМА: КАК НЕЛЬЗЯ vs КАК НАДО):

❌ ПЛОХО (ШКОЛЬНЫЙ УЧИТЕЛЬ / МЕНТОР / СУДЬЯ):
«Вердикт: Коллеги, прекратите ссору! Как врач с многолетним стажем, напомню вам основы деонтологии: нельзя так грубо общаться с коллегами. И запомните раз и навсегда: удалять зуб без попытки перелечивания — это безграмотно! Будьте добрее друг к другу.»

✅ ХОРОШО (КОЛЛЕГА У КРЕСЛА / ПРАКТИЧЕСКИЙ РАЗБОР):
«По протоколу сохранения: при сохранном ферруле <b>>=1.5 мм</b> и отсутствии продольной трещины эндодонтическое перелечивание с последующей культевой вкладкой и коронкой дает <b>88–92%</b> 10-летней выживаемости. Если же разрушение глубже 2 мм под десну и соотношение коронка/корень хуже 1:1 — попытка сохранения сопряжена с высоким риском перелома, здесь надежнее имплантация.»
"""


# ── Менеджер блокировок группового триажа (Anti-Race & Anti-Spam) ─────────────
class GroupTriageLockManager:
    """
    Менеджер конкурентности и защиты от race conditions в групповом чате.
    Предотвращает одновременную обработку параллельных сообщений и троттлит спам.
    """
    def __init__(self):
        self._chat_locks: Dict[int, asyncio.Lock] = {}
        self._in_flight_chats: Set[int] = set()
        self._last_chat_response_time: Dict[int, float] = {}

    def get_chat_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    async def acquire_triage_slot(self, chat_id: int, sender_id: int, cooldown_seconds: float = 30.0) -> bool:
        """
        Атомарно захватывает слот на обработку сообщения в чате.
        Возвращает False, если в чате уже идет обработка или активен кулдаун.
        """
        now = time.time()
        lock = self.get_chat_lock(chat_id)
        async with lock:
            if chat_id in self._in_flight_chats:
                logger.debug(f"[GroupTriage] Chat {chat_id} is already in-flight. Skipping duplicate race.")
                return False

            last_time = self._last_chat_response_time.get(chat_id, 0.0)
            if (now - last_time) < cooldown_seconds:
                logger.debug(f"[GroupTriage] Chat {chat_id} cooldown active (remaining: {cooldown_seconds - (now - last_time):.1f}s).")
                return False

            self._in_flight_chats.add(chat_id)
            return True

    async def release_triage_slot(self, chat_id: int, mark_success: bool = True):
        """Освобождает слот обработки чата."""
        lock = self.get_chat_lock(chat_id)
        async with lock:
            self._in_flight_chats.discard(chat_id)
            if mark_success:
                self._last_chat_response_time[chat_id] = time.time()


# Глобальный синглтон менеджера блокировок группового триажа
triage_lock_manager = GroupTriageLockManager()
