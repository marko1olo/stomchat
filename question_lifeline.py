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
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# По умолчанию задержка 20 минут (1200 сек). В тестах можно переопределять.
DEFAULT_LIFELINE_DELAY_SECONDS = 1200
DEFAULT_MAX_LIFELINES_PER_DAY = 5
DEFAULT_COOLDOWN_SECONDS = 3600  # 1 час между срабатываниями (было 4 часа)

# Маркеры клинического вопроса
QUESTION_INQUIRY_MARKERS = [
    "подскажите", "как поступить", "какой протокол", "что делать",
    "кто сталкивался", "посоветуйте", "какой силер", "как пройти",
    "чем обработать", "как лучше", "поделитесь опытом", "что думаете",
    "мнения?", "ваше мнение", "как лечить", "что выбрать", "посоветуете",
    "какая тактика", "кто как делает", "в чем причина"
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


def is_clinical_question_candidate(
    text: str,
    has_media: bool = False,
    sender_is_bot: bool = False
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

    lower = clean_text.lower()

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

    # Проверка клинической специфики (или снимок)
    has_clinical_term = any(kw in lower for kw in CLINICAL_KEYWORDS)
    if not (has_clinical_term or has_media):
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
        send_message_callback: Optional[Callable] = None
    ):
        self.delay_seconds = delay_seconds
        self.max_per_day = max_per_day
        self.cooldown_seconds = cooldown_seconds
        self.llm_caller = llm_caller
        self.send_message_callback = send_message_callback

        # msg_id -> PendingQuestion
        self.pending_questions: Dict[int, PendingQuestion] = {}
        # chat_id -> List[datetime.datetime] (время отправленных lifelines)
        self.sent_lifelines: Dict[int, List[datetime.datetime]] = {}

    def is_rate_limited(self, chat_id: int, now: Optional[datetime.datetime] = None) -> bool:
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
        media_description: Optional[str] = None
    ) -> Optional[PendingQuestion]:
        """
        Регистрирует новый кандидатный вопрос и запускает таймер на delay_seconds.
        """
        if self.is_rate_limited(chat_id):
            logger.info("Lifeline rate limited for chat=%s, skipping tracking msg_id=%s", chat_id, msg_id)
            return None

        # Отменяем любые предыдущие висящие вопросы от этого же автора в этом чате
        for p in list(self.pending_questions.values()):
            if p.chat_id == chat_id and p.sender_id == sender_id:
                self.cancel_question(p.msg_id, reason="new_question_from_same_author")

        now = datetime.datetime.now()
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

        logger.info("🚨 Срабатывание Lifeline («Первый ответ») для вопроса msg_id=%s от %s", pq.msg_id, pq.sender_name)

        # Резолвим LLM caller
        caller = self.llm_caller
        if caller is None:
            try:
                import blocking_tools
                caller = blocking_tools.generate_gemini_text_async
            except ImportError:
                caller = None

        response_text = await generate_lifeline_response(
            question_text=pq.text,
            doctor_name=pq.sender_name,
            media_desc=media_desc,
            llm_caller=caller
        )

        if not response_text:
            logger.warning("Lifeline generation returned empty text for msg_id=%s", pq.msg_id)
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
        "Коллега, вопрос очень практический.\n\n"
        "В подобных клинических ситуациях первичный ориентир — "
        "сохранение анатомии и работа под увеличением (микроскоп, предварительно "
        "изогнутые ручные файлы #08–#10, обильная ирригация NaOCl и ЭДТА без избыточного давления).\n\n"
        "💬 <i>Коллеги, кто недавно проходил подобный случай у себя — как вели?</i>"
    )

    if llm_caller is None:
        return fallback_text

    media_context = f"\nОписание прикрепленного снимка/фото: {media_desc}\n" if media_desc else ""

    prompt = f"""Ты — опытный коллега-стоматолог в профессиональном чате врачей StomChat.
Доктор {doctor_name} задал клинический вопрос:
\"\"\"{question_text}\"\"\"{media_context}

Сейчас разгар рабочего дня, коллеги у кресел и никто пока не успел ответить. Твоя задача — поддержать коллегу по протоколу «Первый ответ» (Lifeline).

ТРЕБОВАНИЯ К ОТВЕТУ:
1. Тон: теплый, профессиональный, коллега к коллеге (на «вы»).
2. Краткость: ровно 2-3 коротких абзаца (до 500-600 символов).
3. Структура:
   — Краткая валидация клинической сути развилки (без пересказа вопроса).
   — 1-2 практических шага по современным протоколам (например, изогнутые ручные файлы 08-10, гелевая ЭДТА, микроскоп, ультразвуковая активация, экспозиция гипохлорита). БЕЗ лекций из учебников!
   — Обязательно в конце открытый вопрос к залу: «Коллеги, кто недавно проходил подобное на практике — какими инструментами/протоколами решали у себя?».

СТРОЖАЙШИЙ ЗАПРЕТ:
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
        return resp.text.strip()
    except Exception as exc:
        logger.warning("Error generating lifeline response via LLM: %s", exc)
        return fallback_text
