# -*- coding: utf-8 -*-
"""
Модуль «Unanswered Question Lifeline» (Протокол «Первый ответ»).

Решает проблему низкой активности чата (тишина у стоматологического кресла):
Если врач задает клинический вопрос или выкладывает снимок/кейс, а коллеги
в течение 20 минут не отвечают (все заняты на приеме):
  1. Бот не дает вопросу утонуть в тишине.
  2. Генерирует теплое коллегиальное резюме с 1-2 доказательными ориентирами EBM.
  3. Заканчивает открытым вопросом-передачей микрофона залу:
     «Коллеги, кто сталкивался с подобным кейсом в клинике? Как вели у себя?».
  4. Если любой врач отвечает ДО истечения 20 минут — таймер отменяется.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# По умолчанию задержка 20 минут (1200 сек). В тестах можно переопределять.
from dental_vocab import has_dental_term

DEFAULT_LIFELINE_DELAY_SECONDS = 1200
DEFAULT_MAX_LIFELINES_PER_DAY = 5
DEFAULT_COOLDOWN_SECONDS = 3600  # 1 час между срабатываниями (было 4 часа)
DEFAULT_MAX_LIFELINE_AGE_SECONDS = 2400  # 40 минут (предел актуальности у кресла)

# Маркеры клинического вопроса
QUESTION_INQUIRY_MARKERS = [
    "подскажите", "как поступить", "какой протокол", "что делать",
    "кто сталкивался", "посоветуйте", "какой силер", "как пройти",
    "чем обработать", "как лучше", "поделитесь опытом", "что думаете",
    "мнения?", "ваше мнение", "как лечить", "что выбрать", "посоветуете",
    "какая тактика", "кто как делает", "в чем причина",
    "срочно", "sos", "помогите", "алгоритм", "дозировк", "протокол при", "неотложн", "что колоть"
]

CLINICAL_KEYWORDS = [
    "зуб", "канал", "снимок", "кт", "клкт", "апекс", "мв2", "mb2",
    "мта", "mta", "перфорац", "отлом", "штифт", "ирригац", "пульпит",
    "периодонтит", "билдап", "оверлей", "боль", "корень", "фуркац",
    "трещин", "гипохлорит", "naocl", "эдта", "edta", "коффердам",
    "биокерамик", "силер", "ступеньк", "некроз", "пародонт", "резорцин",
    "гутаперч", "гуттаперч", "феррул", "дентин", "эмаль", "анестези",
    "церасил", "ceraseal", "bioroot", "ah plus", "аш плюс", "паф",
    "байпас", "протейпер", "соко", "система файлов", "кавитрон",
    "ультразвук", "эндочак", "эндомотор", "термафил", "сквирт"
]

NON_CLINICAL_STOPWORDS = [
    "скриншот", "интерфейс", "мем", "приложение", "меню", "текст",
    "нет зубов", "зубы отсутствуют", "отсутствуют зубы", "зубов нет",
    "не является рентгеном", "не содержит зубов", "не относится к стоматологии",
    "не медицинск", "не клиническ"
]

_EXPLICIT_NON_DENTAL_MEDIA_RE = re.compile(
    r'(?i)(?:'
    r'не\s+является\s+[^.\n]{0,40}(?:медицинск\w*|стоматолог\w*|клиническ\w*|диагностик\w*|снимк\w*|рентген\w*)|'
    r'не\s+относит\w*\s+к\s+[^.\n]{0,30}(?:медицин\w*|стоматолог\w*)|'
    r'не\s+имеет\s+отношения\s+к\s+[^.\n]{0,30}(?:медицин\w*|стоматолог\w*)|'
    r'(?:картинк\w*|изображен\w*|снимок|фотографи\w*)\s+не\s+(?:является\s+)?(?:медицинск\w*|стоматолог\w*|клиническ\w*)|'
    r'немедицинск\w*|неклиническ\w*|'
    r'поздравительн\w*\s+открытк\w*|открытк\w*\s+с\s+праздник\w*|праздничн\w*\s+открытк\w*|'
    r'скриншот\w*\s+[^.\n]{0,30}(?:интерфейс\w*|приложен\w*|соцсет\w*|социальн\w*\s+сет\w*|профил\w*|переписк\w*|чат\w*|экран\w*|сайт\w*|мессенджер\w*|бот\w*|канал\w*|программ\w*)|'
    r'интерфейс\w*\s+[^.\n]{0,30}(?:цифров\w*|программн\w*|telegram|телеграм|бот\w*|мессенджер\w*|приложен\w*|комплекс\w*)|'
    r'карточк\w*\s+спикер\w*|рекламн\w*\s+баннер\w*|афиш\w*|презентационн\w*\s+карточк\w*|'
    r'мем|мем[а-яё]*|комикс\w*|карикатур\w*|юмористическ\w*|'
    r'чек|квитанци\w*|накладн\w*|банковск\w*\s+(?:перевод|чек|выписк\w*)|прайс[- ]?лист\w*|'
    r'фотографи\w*\s+(?:человек\w*|лиц\w*|селфи)|портрет|'
    r'(?:медицинск\w*|анатомическ\w*|стоматологическ\w*|зубочелюстн\w*|зубн\w*|внутриротов\w*|рентгенолог\w*)\s+[^.\n]{0,60}(?:структур\w*|объект\w*|патолог\w*|элемент\w*|снимк\w*|ряд\w*|данные)\s+[^.\n]{0,40}(?:не\s+дифференциру\w*|не\s+визуализиру\w*|отсутству\w*|не\s+обнаружен\w*)|'
    r'(?:зуб\w*|коронков\w*|корнев\w*|периодонт\w*|костн\w*\s+ткан\w*|рентгенограмм\w*)\s+[^.\n]{0,40}(?:на\s+(?:изображени\w*|снимк\w*|кадр\w*)\s+)?отсутству\w*'
    r')'
)

def is_explicitly_non_dental_media(description: Optional[str]) -> bool:
    if not description:
        return False
    d_clean = description.strip().lower()
    return any(sw in d_clean for sw in NON_CLINICAL_STOPWORDS) or bool(_EXPLICIT_NON_DENTAL_MEDIA_RE.search(description))



def is_clinical_question_candidate(
    text: str,
    has_media: bool = False,
    sender_is_bot: bool = False,
    media_description: Optional[str] = None
) -> bool:
    """
    Эвристически определяет, является ли сообщение клиническим вопросом коллеги,
    требующим внимания и рискующим остаться без ответа.
    """
    if sender_is_bot:
        return False

    clean_text = (text or "").strip()
    if len(clean_text) < 15 and not (has_media and len(clean_text) >= 5):
        return False

    lower = clean_text.lower().replace('ё', 'е')

    # Игнорируем команды и прямые обращения к боту (их сразу обрабатывает ассистент)
    if lower.startswith("/") or lower.startswith("!"):
        return False
    if "бот" in lower or "@" in lower:
        return False

    # Проверка наличия знака вопроса или вопросительных маркеров
    has_question_mark = "?" in lower
    has_inquiry_marker = any(marker in lower for marker in QUESTION_INQUIRY_MARKERS)

    if not (has_question_mark or has_inquiry_marker):
        return False

    # Проверка описания медиа на неклинические стоп-слова
    if media_description and is_explicitly_non_dental_media(media_description):
        return False

    # Проверка клинической специфики (по списку ключевых слов или единому словарю триажа)
    has_clinical_term = any(kw in lower for kw in CLINICAL_KEYWORDS) or has_dental_term(clean_text)
    has_clin_desc = bool(media_description and (any(kw in media_description.lower() for kw in CLINICAL_KEYWORDS) or has_dental_term(media_description)))

    # Ликвидация слепого пролезания с медиа:
    if not has_clinical_term:
        if not (has_media and has_clin_desc):
            return False

    return True


@dataclass
class PendingQuestion:
    msg_id: int
    chat_id: int
    sender_id: int
    sender_name: str
    text: str
    created_at: datetime.datetime
    has_media: bool = False
    media_description: Optional[str] = None
    timer_task: Optional[asyncio.Task] = None
    is_cancelled: bool = False


class QuestionLifelineManager:
    """
    Управляет таймерами спасательного круга для клинических вопросов врачей.
    """

    def __init__(
        self,
        delay_seconds: int = DEFAULT_LIFELINE_DELAY_SECONDS,
        max_per_day: int = DEFAULT_MAX_LIFELINES_PER_DAY,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        llm_caller: Optional[Callable] = None,
        send_message_callback: Optional[Callable] = None,
        typing_context_factory: Optional[Callable] = None
    ):
        self.delay_seconds = delay_seconds
        self.max_per_day = max_per_day
        self.cooldown_seconds = cooldown_seconds
        self.max_age_seconds = DEFAULT_MAX_LIFELINE_AGE_SECONDS
        self.llm_caller = llm_caller
        self.send_message_callback = send_message_callback
        self.typing_context_factory = typing_context_factory

        # msg_id -> PendingQuestion
        self.pending_questions: Dict[int, PendingQuestion] = {}
        # chat_id -> List[datetime.datetime] (время отправленных lifelines)
        self.sent_lifelines: Dict[int, List[datetime.datetime]] = {}

    def cleanup_stale_questions(self, max_age_seconds: int = 3600) -> int:
        """Удаляет из памяти зависшие, отмененные или протухшие вопросы старше 1 часа."""
        now = datetime.datetime.now()
        stale_ids = []
        for mid, pq in list(self.pending_questions.items()):
            if (now - pq.created_at).total_seconds() > max_age_seconds or pq.is_cancelled:
                stale_ids.append(mid)
        for mid in stale_ids:
            self.cancel_question(mid, reason="stale_cleanup")
        return len(stale_ids)

    def is_rate_limited(self, chat_id: int, now: Optional[datetime.datetime] = None) -> bool:
        self.cleanup_stale_questions()
        """Проверяет суточный лимит и кулдаун отправки lifeline в чат."""
        current_time = now or datetime.datetime.now()
        history = self.sent_lifelines.get(chat_id, [])

        # Оставляем только за последние 24 часа
        cutoff_24h = current_time - datetime.timedelta(hours=24)
        recent = [t for t in history if t > cutoff_24h]
        self.sent_lifelines[chat_id] = recent

        if len(recent) >= self.max_per_day:
            return True

        if recent:
            last_sent = max(recent)
            if (current_time - last_sent).total_seconds() < self.cooldown_seconds:
                return True

        return False

    def track_question(
        self,
        msg_id: int,
        chat_id: int,
        sender_id: int,
        sender_name: str,
        text: str,
        has_media: bool = False,
        media_description: Optional[str] = None,
        msg_date: Optional[datetime.datetime] = None
    ) -> Optional[PendingQuestion]:
        """
        Регистрирует новый кандидатный вопрос и запускает таймер на delay_seconds.
        Проверяет свежесть сообщения (msg_date) во избежание спама старыми вопросами.
        """
        if self.is_rate_limited(chat_id):
            logger.info("Lifeline rate limited for chat=%s, skipping tracking msg_id=%s", chat_id, msg_id)
            return None

        now = datetime.datetime.now()

        # Защита от спама на старые вопросы из оффлайн-очереди/бэклога (когда пациент уже ушел)
        if msg_date:
            age = (datetime.datetime.now(datetime.timezone.utc) - msg_date).total_seconds() if msg_date.tzinfo else (now - msg_date).total_seconds()
            if age > self.delay_seconds:
                logger.info(
                    "Lifeline skipping stale msg_id=%s (msg_date=%s is %.1fs old > delay %ds)",
                    msg_id, msg_date, age, self.delay_seconds
                )
                return None

        # Отменяем любые предыдущие висящие вопросы от этого же автора в этом чате
        for p in list(self.pending_questions.values()):
            if p.chat_id == chat_id and p.sender_id == sender_id:
                self.cancel_question(p.msg_id, reason="new_question_from_same_author")
        pq = PendingQuestion(
            msg_id=msg_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            created_at=now,
            has_media=has_media,
            media_description=media_description
        )

        task = asyncio.create_task(
            self._timer_worker(pq),
            name=f"lifeline_{chat_id}_{msg_id}"
        )
        pq.timer_task = task
        self.pending_questions[msg_id] = pq

        logger.info(
            "⏳ Запущен Lifeline-таймер (%d сек) для клинического вопроса msg_id=%s от %s",
            self.delay_seconds, msg_id, sender_name
        )
        return pq

    def cancel_question(self, msg_id: int, reason: str = "") -> bool:
        """Отменяет таймер для конкретного вопроса."""
        pq = self.pending_questions.pop(msg_id, None)
        if not pq:
            return False

        pq.is_cancelled = True
        if pq.timer_task and not pq.timer_task.done():
            pq.timer_task.cancel()
        logger.info("🚫 Lifeline отменен для msg_id=%s (причина: %s)", msg_id, reason)
        return True

    def on_human_message(
        self,
        chat_id: int,
        sender_id: int,
        reply_to_msg_id: Optional[int] = None,
        text: Optional[str] = None,
        sender_name: Optional[str] = None
    ) -> None:
        """
        Событие: пришло сообщение от человека.
        Отменяем таймер спасательного круга, если:
        1. Это прямой ответ на отслеживаемый вопрос (reply_to_msg_id).
        2. Это обращение к автору вопроса по имени/юзернейму.
        Не отменяем вопрос из-за случайного нерелевантного сообщения другого участника.
        """
        # Если ответили напрямую на отслеживаемый вопрос
        if reply_to_msg_id and reply_to_msg_id in self.pending_questions:
            self.cancel_question(reply_to_msg_id, reason=f"human_reply_from_sender_{sender_id}")
            return

        # Если в сообщении есть обращение к автору одного из висящих вопросов
        if text:
            lower_text = text.lower()
            for msg_id, pq in list(self.pending_questions.items()):
                if pq.chat_id != chat_id or pq.sender_id == sender_id:
                    continue
                author_name = (pq.sender_name or "").strip().lower()
                if author_name and len(author_name) >= 3 and author_name in lower_text:
                    self.cancel_question(msg_id, reason=f"human_mentioned_author_{pq.sender_name}")

    async def _timer_worker(self, pq: PendingQuestion) -> None:
        """Асинхронный воркер ожидания 20 минут."""
        try:
            await asyncio.sleep(self.delay_seconds)
            if pq.is_cancelled:
                return

            # Проверяем лимиты еще раз перед отправкой
            if self.is_rate_limited(pq.chat_id):
                logger.info("Lifeline rate limit hit at trigger time for msg_id=%s", pq.msg_id)
                self.pending_questions.pop(pq.msg_id, None)
                return

            await self._execute_lifeline(pq)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Error in Lifeline timer worker for msg_id=%s: %s", pq.msg_id, exc)
        finally:
            self.pending_questions.pop(pq.msg_id, None)

    async def _execute_lifeline(self, pq: PendingQuestion) -> None:
        """Генерирует и отправляет поддерживающий коллегиальный ответ."""
        # Stale Question Guard: если вопрос старше max_age_seconds (40 мин),
        # пациент уже встал с кресла и ушел. Запоздалый совет бота нелеп и дискредитирует сервис.
        now = datetime.datetime.now()
        elapsed = (now - pq.created_at).total_seconds()
        if elapsed > self.max_age_seconds:
            logger.info(
                "⏳ Lifeline Expired: вопрос msg_id=%s устарел (прошло %.0fs > max %ds). Пациент уже ушел от кресла. Lifeline отменен.",
                pq.msg_id, elapsed, self.max_age_seconds
            )
            return

        # Activity Guard: проверяем активность чата за последние 5 минут
        try:
            import database
            recent_activity = await database.check_recent_chat_activity(minutes=5)
            if recent_activity >= 3:
                logger.info(
                    "⏳ Lifeline Activity Guard: в чате идет живое обсуждение (%d сообщ за 5 мин). Lifeline отменен для msg_id=%s.",
                    recent_activity, pq.msg_id
                )
                return
        except Exception as e:
            logger.debug("Activity guard check error for lifeline: %s", e)

        # Подтягиваем актуальное описание медиа/снимка из базы, если при старте оно еще анализировалось
        media_desc = pq.media_description
        if not media_desc and pq.has_media:
            try:
                import database
                db_desc = await database.get_media_description(pq.msg_id)
                if db_desc and db_desc.strip() and db_desc.strip() != "-":
                    media_desc = db_desc.strip()
                    logger.info("📸 Lifeline успешно подтянул свежее описание снимка из базы для msg_id=%s", pq.msg_id)
            except Exception as e:
                logger.warning("Не удалось получить media_description из БД для msg_id=%s: %s", pq.msg_id, e)

        # Non-Dental Filter: если медиа прямо не является стоматологическим — отменяем Lifeline
        if media_desc and is_explicitly_non_dental_media(media_desc):
            logger.info("🛑 Lifeline отменен для msg_id=%s: обнаружено не-клиническое изображение (%s)",
                        pq.msg_id, media_desc[:80])
            try:
                import clinical_rescue
                import config
                clinical_rescue.mark_case_and_album_rescued(config.DB_PATH, pq.msg_id, pq.chat_id, sender_id=pq.sender_id)
            except Exception:
                pass
            return

        logger.info("🚨 Срабатывание Lifeline («Первый ответ») для вопроса msg_id=%s от %s", pq.msg_id, pq.sender_name)

        # Резолвим LLM caller
        caller = self.llm_caller
        if caller is None:
            try:
                import blocking_tools
                caller = blocking_tools.generate_gemini_text_async
            except ImportError:
                caller = None

        if self.typing_context_factory:
            try:
                async with self.typing_context_factory(pq.chat_id):
                    response_text = await generate_lifeline_response(
                        question_text=pq.text,
                        doctor_name=pq.sender_name,
                        media_desc=media_desc,
                        llm_caller=caller
                    )
            except Exception as typing_err:
                logger.debug("Lifeline typing action error: %s", typing_err)
                response_text = await generate_lifeline_response(
                    question_text=pq.text,
                    doctor_name=pq.sender_name,
                    media_desc=media_desc,
                    llm_caller=caller
                )
        else:
            response_text = await generate_lifeline_response(
                question_text=pq.text,
                doctor_name=pq.sender_name,
                media_desc=media_desc,
                llm_caller=caller
            )

        if not response_text:
            logger.warning("Lifeline generation returned empty text for msg_id=%s", pq.msg_id)
            try:
                import clinical_rescue
                import config
                clinical_rescue.mark_case_and_album_rescued(config.DB_PATH, pq.msg_id, pq.chat_id, sender_id=pq.sender_id)
            except Exception:
                pass
            return

        # Запоминаем отправку
        now = datetime.datetime.now()
        self.sent_lifelines.setdefault(pq.chat_id, []).append(now)

        # Отправляем сообщение через коллбек
        if self.send_message_callback:
            try:
                await self.send_message_callback(
                    chat_id=pq.chat_id,
                    reply_to_msg_id=pq.msg_id,
                    text=response_text
                )
                logger.info("✅ Lifeline успешно опубликован в чат=%s для msg_id=%s", pq.chat_id, pq.msg_id)
            except Exception as send_err:
                logger.exception("Failed to send Lifeline message: %s", send_err)


async def _call_llm_adapter(caller: Callable, prompt: str, ctx: dict, timeout: float = 30.0):
    """Адаптер вызова LLM: пробует keyword 'status_ctx', затем позиционный аргумент, затем 'context'."""
    try:
        return await caller(prompt, status_ctx=ctx, timeout=timeout)
    except TypeError:
        try:
            return await caller(prompt, ctx, timeout=timeout)
        except TypeError:
            return await caller(prompt, context=ctx, timeout=timeout)


async def generate_lifeline_response(
    question_text: str,
    doctor_name: str,
    media_desc: Optional[str] = None,
    llm_caller: Optional[Callable] = None
) -> Optional[str]:
    """
    Генерирует теплое, коллегиальное, практическое сообщение первого ответа.
    Строго без менторства, кафедральной воды и нудных лекций.
    """
    fallback_text = (
        "Вопрос очень практический.\n\n"
        "В подобных клинических ситуациях первичный ориентир — "
        "сохранение анатомии и работа под увеличением (микроскоп, предварительно "
        "изогнутые ручные файлы #08–#10, обильная ирригация NaOCl и ЭДТА без избыточного давления)."
    )

    if llm_caller is None:
        return fallback_text

    media_context = f"\nОписание прикрепленного снимка/фото: {media_desc}\n" if media_desc else ""

    prompt = f"""Ты — опытный врач-стоматолог в профессиональном сообществе врачей StomChat.
ОБЯЗАТЕЛЬНЫЙ ФИЛЬТР БЕЗОПАСНОСТИ (ESCAPE HATCH):
Если в вопросе или описании изображения НЕТ стоматологического клинического случая
(изображен скриншот интерфейса, смартфона, приложения, сайта, бота, программы, меню, текст переписки, мем, документ, лицо человека или бытовая фотография) —
ты ОБЯЗАН ответить РОВНО ОДНИМ МАРКЕРОМ:
NOT_CLINICAL_CASE
КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать клинические советы для не-стоматологических картинок!

Доктор {doctor_name} задал клинический вопрос:
\"\"\"{question_text}\"\"\"{media_context}

Сейчас разгар рабочего дня, врачи на приеме у кресел и никто пока не успел ответить. Твоя задача — поддержать врача по протоколу «Первый ответ» (Lifeline).

ТРЕБОВАНИЯ К ОТВЕТУ:
1. Тон: сдержанный, профессиональный, на равных («Врач — Врачу», на «вы»).
2. Краткость: ровно 2-3 коротких абзаца (до 500-600 символов).
3. Структура:
   — КАТЕГОРИЧЕСКИЙ ЗАПРЕТ ЗЕРКАЛИРОВАНИЯ И ПЕРЕСКАЗА: ни единого слова пересказа вопроса или кейса доктора («Вы столкнулись с...», «Итак, в ситуации...»). Врач сам знает свой вопрос.
   — НОВЫЙ ВДУМЧИВЫЙ СИНТЕЗ: сразу суть клинической развилки от биомеханики/биологии и 1-2 практических шага по современным протоколам (размеры, экспозиция, материалы), которые доктор ещё НЕ озвучивал. БЕЗ лекций из учебников!
   — Заканчивай ответ точкой на четком клиническом ориентире. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО добавлять в конце дежурные вовлекающие вопросы («Кто сталкивался?», «Как вели?») — в чате они выглядят искусственно и навязчиво.

СТРОЖАЙШИЙ ЗАПРЕТ:
- СТРОЖАЙШИЙ ЗАПРЕТ ОБРАЩЕНИЯ «КОЛЛЕГА» И «КОЛЛЕГИ»: Категорически запрещено использовать слова «коллега», «коллеги» в качестве обращения, приветствия или вводных слов!
- СТРОЖАЙШЕ ЗАПРЕЩЕНО писать душные наукообразные фразы: «По консенсусу ESE и AAE», «согласно рекомендациям ассоциации», «по гайдлайнам зарубежных обществ». Врачей в РФ бесит эта заумь! Говори чистой клинической логикой у кресла.
- Никаких шаблонных фраз «Здравствуйте, как языковая модель...» или «Это очень сложный случай».
- Никаких длинных списков из 10 пунктов.
- Никакого кафедрального занудства и поучений.
- Язык: строго русский с живыми терминами практикующих стоматологов.

Выдай только текст сообщения с разметкой HTML (<b>, <i>). Без лишних преамбул.
"""

    try:
        status_ctx = {"kind": "lifeline_response", "thinking_level": "LOW", "temperature": 0.3}
        resp, err = await _call_llm_adapter(llm_caller, prompt, status_ctx, timeout=30.0)
        if err or not resp or not getattr(resp, "text", None):
            return fallback_text
        if "NOT_CLINICAL_CASE" in resp.text.strip().upper():
            logger.info("🛑 Lifeline model returned NOT_CLINICAL_CASE. Suppressing reply.")
            return None
        res_text = re.sub(r'(?i)^[,\s—–-]*(?:коллег[аи],?\s*|уважаемые\s+коллеги,?\s*)', '', resp.text.strip()).strip()
        if res_text and res_text[0].islower():
            res_text = res_text[0].upper() + res_text[1:]
        return res_text or fallback_text
    except Exception as exc:
        logger.warning("Error generating lifeline response via LLM: %s", exc)
        return fallback_text
