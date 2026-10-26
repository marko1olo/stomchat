"""
inflight_triage.py — In-Flight Generation Session Tracking & Fast Follow-up Triage Engine.

Обеспечивает:
1. Отслеживание активных генераций клинических ответов в реальном времени per (chat_id, sender_id).
2. Перехват догоняющих сообщений: если врач отправил /ask или обращение к боту, а через несколько секунд
   дописал детали вопроса, клинические параметры или прикрепил снимок, пока бот формирует ответ.
3. Мгновенный LLM-триаж (In-Flight Triage): определяет намерение врача (SUPPLEMENT, REPLACE, WAIT_FOR_ANSWER, CANCEL).
   При SUPPLEMENT или REPLACE старая генерация отменяется, формируется объединенный запрос (merged_query),
   и запускается актуальный ответ без штрафного кулдауна.
4. Предварительный сбор контекста для пустой команды /ask (проверка предшествующего сообщения врача или 3.5с окно ожидания).
"""

import asyncio
import time
import json
import re
import logging
from typing import Optional, List, Dict, Any, Set, Tuple

logger = logging.getLogger(__name__)

# Максимальное число перезапусков генерации на одной сессии (защита от флуда)
MAX_INFLIGHT_RESTARTS = 3

# Время жизни сессии в памяти (сек)
SESSION_MAX_LIFETIME_SECONDS = 180.0


class InFlightSession:
    """Сессия генерации ответа в реальном времени."""

    def __init__(self, chat_id: int, sender_id: int, trigger_msg_id: int, original_query: str):
        self.chat_id: int = chat_id
        self.sender_id: int = sender_id
        self.trigger_msg_ids: Set[int] = {trigger_msg_id} if trigger_msg_id else set()
        self.original_query: str = (original_query or "").strip()
        self.start_time: float = time.time()
        self.current_task: Optional[asyncio.Task] = None
        self.followup_messages: List[Dict[str, Any]] = []
        self._unprocessed_followups: List[Dict[str, Any]] = []
        self._followup_event: asyncio.Event = asyncio.Event()
        self.is_completed: bool = False
        self.is_cancelled: bool = False
        self.restart_count: int = 0

    def record_followup(self, msg_id: int, text: str, has_media: bool = False, date: Any = None):
        """Регистрирует новое входящее сообщение от того же автора во время генерации."""
        item = {
            "msg_id": msg_id,
            "text": (text or "").strip(),
            "has_media": bool(has_media),
            "date": date,
            "received_at": time.time(),
        }
        self.followup_messages.append(item)
        self._unprocessed_followups.append(item)
        if msg_id:
            self.trigger_msg_ids.add(msg_id)
        self._followup_event.set()
        logger.info(
            "InFlightSession [%s:%s]: registered follow-up msg_id=%s text=%r",
            self.chat_id, self.sender_id, msg_id, item["text"][:60]
        )

    def pop_unprocessed_followups(self) -> List[Dict[str, Any]]:
        """Извлекает накопленные необработанные догоняющие сообщения и сбрасывает событие."""
        items = list(self._unprocessed_followups)
        self._unprocessed_followups.clear()
        self._followup_event.clear()
        return items

    async def wait_for_followup(self, timeout: Optional[float] = None) -> bool:
        """Ожидает поступления нового сообщения от автора."""
        try:
            if timeout is not None:
                await asyncio.wait_for(self._followup_event.wait(), timeout=timeout)
            else:
                await self._followup_event.wait()
            return True
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False

    def add_trigger_msg_id(self, msg_id: int):
        """Добавляет ID сообщения к адресатам ответа."""
        if msg_id:
            self.trigger_msg_ids.add(msg_id)

    def set_current_task(self, task: asyncio.Task):
        """Устанавливает текущую исполняемую задачу генерации."""
        self.current_task = task

    def mark_completed(self):
        """Помечает сессию успешно завершенной."""
        self.is_completed = True

    def cancel(self):
        """Отменяет генерацию и помечает сессию отмененной."""
        self.is_cancelled = True
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()


class InFlightManager:
    """Глобальный менеджер сессий генерации и догоняющего контекста."""

    def __init__(self):
        self._sessions: Dict[Tuple[int, int], InFlightSession] = {}
        self._msg_to_session: Dict[Tuple[int, int], InFlightSession] = {}
        self._initial_waiters: Dict[Tuple[int, int], asyncio.Future] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def register_session(
        self, chat_id: int, sender_id: int, trigger_msg_id: int, query: str
    ) -> InFlightSession:
        """Регистрирует новую сессию генерации для автора в чате."""
        async with self._lock:
            key = (chat_id, sender_id)
            old = self._sessions.get(key)
            if old and not old.is_completed and not old.is_cancelled:
                logger.info("Cancelling previous uncompleted session for %s", key)
                old.cancel()
                self._sessions.pop(key, None)

            session = InFlightSession(chat_id, sender_id, trigger_msg_id, query)
            self._sessions[key] = session
            if trigger_msg_id:
                self._msg_to_session[(chat_id, trigger_msg_id)] = session
            return session

    async def unregister_session(self, session: InFlightSession):
        """Удаляет сессию из реестра."""
        async with self._lock:
            key = (session.chat_id, session.sender_id)
            if self._sessions.get(key) is session:
                self._sessions.pop(key, None)
            for mid in list(session.trigger_msg_ids):
                m_key = (session.chat_id, mid)
                if self._msg_to_session.get(m_key) is session:
                    self._msg_to_session.pop(m_key, None)

    def get_active_session(
        self,
        chat_id: int,
        sender_id: Optional[int] = None,
        reply_to_msg_id: Optional[int] = None,
        msg_id: Optional[int] = None,
    ) -> Optional[InFlightSession]:
        """Возвращает активную (незавершенную) сессию для сообщения/автора."""
        now = time.time()
        # 1. Поиск по reply_to_msg_id
        if reply_to_msg_id:
            s = self._msg_to_session.get((chat_id, reply_to_msg_id))
            if s and not s.is_completed and not s.is_cancelled:
                if (now - s.start_time) < SESSION_MAX_LIFETIME_SECONDS:
                    return s

        # 2. Поиск по msg_id
        if msg_id:
            s = self._msg_to_session.get((chat_id, msg_id))
            if s and not s.is_completed and not s.is_cancelled:
                if (now - s.start_time) < SESSION_MAX_LIFETIME_SECONDS:
                    return s

        # 3. Поиск по sender_id
        if sender_id:
            s = self._sessions.get((chat_id, sender_id))
            if s and not s.is_completed and not s.is_cancelled:
                if (now - s.start_time) < SESSION_MAX_LIFETIME_SECONDS:
                    return s

        return None

    def has_active_session(self, chat_id: int, sender_id: int) -> bool:
        """Проверяет наличие активной сессии генерации для автора."""
        s = self._sessions.get((chat_id, sender_id))
        if s and not s.is_completed and not s.is_cancelled:
            if (time.time() - s.start_time) < SESSION_MAX_LIFETIME_SECONDS:
                return True
        return False

    def record_incoming_message(
        self,
        chat_id: int,
        sender_id: int,
        msg_id: int,
        text: str,
        has_media: bool = False,
        reply_to_msg_id: Optional[int] = None,
        date: Any = None,
    ) -> Optional[InFlightSession]:
        """
        Перехватывает новое сообщение:
        - Если есть ждущий pre-gather waiter на пустой /ask — разрешает его.
        - Если есть активная сессия генерации — добавляет follow-up.
        """
        key = (chat_id, sender_id)

        # 1. Проверяем pre-gather waiter
        fut = self._initial_waiters.get(key)
        if fut and not fut.done():
            fut.set_result({
                "msg_id": msg_id,
                "text": (text or "").strip(),
                "has_media": bool(has_media),
                "reply_to_msg_id": reply_to_msg_id,
                "date": date,
            })
            logger.info("Resolved initial follow-up waiter for %s msg_id=%s", key, msg_id)

        # 2. Проверяем активную генерацию
        session = self.get_active_session(
            chat_id=chat_id, sender_id=sender_id, reply_to_msg_id=reply_to_msg_id, msg_id=msg_id
        )
        if session:
            session.record_followup(msg_id, text, has_media=has_media, date=date)
            return session

        return None

    async def wait_for_immediate_followup(
        self, chat_id: int, sender_id: int, timeout: float = 3.5
    ) -> Optional[Dict[str, Any]]:
        """
        Ожидает в течение timeout секунд непосредственного ввода вопроса врачом
        после вызова пустой команды /ask.
        """
        loop = asyncio.get_running_loop()
        key = (chat_id, sender_id)
        fut = loop.create_future()
        self._initial_waiters[key] = fut
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            if self._initial_waiters.get(key) is fut:
                self._initial_waiters.pop(key, None)


# Глобальный синглтон менеджера
_GLOBAL_INFLIGHT_MANAGER = InFlightManager()


def get_inflight_manager() -> InFlightManager:
    """Возвращает глобальный менеджер активных сессий."""
    return _GLOBAL_INFLIGHT_MANAGER


async def find_immediate_preceding_question(
    query_db_async_fn,
    sender_id: int,
    current_msg_id: int,
    max_id_diff: int = 8,
) -> Optional[Dict[str, Any]]:
    """
    Проверяет, не отправил ли врач свой вопрос непосредственно ПЕРЕД вызовом команды /ask
    (паттерн: сначала клинический текст/вопрос, затем /ask вторым сообщением).
    """
    if not sender_id or not current_msg_id or not query_db_async_fn:
        return None

    try:
        rows = await query_db_async_fn(
            "SELECT msg_id, text, has_media, date FROM messages "
            "WHERE sender_id = ? AND msg_id < ? ORDER BY msg_id DESC LIMIT 3",
            (sender_id, current_msg_id),
        )
        if not rows:
            return None

        for r in rows:
            mid, text, has_media, dt = r[0], r[1] or "", bool(r[2]), r[3]
            # Сообщение должно быть непосредственно перед текущим (не старше max_id_diff)
            if (current_msg_id - mid) > max_id_diff:
                continue

            clean_text = text.strip()
            # Игнорируем технические команды и пустые строки
            if clean_text.startswith(("/", "бот,", "бот ", "итог", "опрос", "викторина")) or clean_text == "бот":
                continue

            if len(clean_text) >= 5 or has_media:
                logger.info(
                    "find_immediate_preceding_question: found preceding question msg_id=%s from sender=%s: %r",
                    mid, sender_id, clean_text[:60]
                )
                return {
                    "msg_id": mid,
                    "text": clean_text,
                    "has_media": has_media,
                    "date": dt,
                }
    except Exception as exc:
        logger.warning("find_immediate_preceding_question error: %s", exc)

    return None


async def run_inflight_followup_triage(
    original_query: str,
    followups: List[Dict[str, Any]],
    bot_name: str = "EndoChat",
    generate_gemini_fn: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Быстрый LLM-триаж входящего догоняющего сообщения во время генерации ответа.
    Определяет, нужно ли объединить вопрос, заменить его или продолжить старую генерацию.
    """
    if not followups:
        return {
            "action": "WAIT_FOR_ANSWER",
            "merged_query": original_query,
            "reason": "No followups",
        }

    followup_texts = []
    for f in followups:
        t = (f.get("text") or "").strip()
        if t:
            followup_texts.append(t)
        elif f.get("has_media"):
            followup_texts.append("[Врач прикрепил снимок / медиа-файл]")

    combined_followup = "\n".join(followup_texts).strip()
    orig_clean = (original_query or "").strip()

    # 1. Быстрые эвристики отмены / нетерпения
    combined_low = combined_followup.lower().strip()
    cancel_patterns = [
        r"\b(?:отмена|отмени|отменить|не надо|забей|я сам нашел|я сам разобрался)\b",
        r"^/cancel\b",
    ]
    if any(re.search(pat, combined_low) for pat in cancel_patterns):
        return {
            "action": "CANCEL",
            "merged_query": "",
            "reason": "Explicit user cancellation",
        }

    impatience_patterns = [
        r"^[\?\! \.\,]+$",
        r"\b(?:ждем|ждём|жду|доктор ты где|ну что|ау|побыстрее|долго еще)\b",
    ]
    clinical_markers = (
        "зуб", "канал", "снимок", "пломб", "смол", "адгезив", "силер", "корон", "штифт", "боль", "кт",
        "пациент", "десна", "полость", "эмаль", "дентин", "пульп", "апекс", "мта", "гуттаперч"
    )
    if any(re.search(pat, combined_low) for pat in impatience_patterns) and not any(k in combined_low for k in clinical_markers):
        return {
            "action": "WAIT_FOR_ANSWER",
            "merged_query": orig_clean,
            "reason": "Impatience / conversational ping without clinical content",
        }

    # Если исходный запрос был пустым, догоняющее сообщение однозначно является вопросом!
    if not orig_clean or orig_clean in (
        "Коллега, дай подробный доказательный клинический разбор, протокол и рекомендации по целевому клиническому случаю и снимку выше.",
        "Проведи клинический разбор недавней дискуссии и клинического случая выше.",
    ):
        return {
            "action": "SUPPLEMENT",
            "merged_query": combined_followup,
            "reason": "Initial query was empty or placeholder; user provided the actual question.",
        }

    # 2. LLM-триаж через Gemini Flash
    triage_prompt = f"""Ты — клинический координатор врачебного сообщества {bot_name}.
Врач задал вопрос (или вызвал команду /ask), и бот уже формирует клинический ответ.
Исходный запрос:
\"\"\"{orig_clean}\"\"\"

Пока бот формировал ответ, тот же врач прислал дополнительное сообщение:
\"\"\"{combined_followup}\"\"\"

Определи характер дополнения:
- 'SUPPLEMENT': врач задал сам вопрос (если первый запрос был пустым), уточнил зуб/симптомы/материал/анамнез или расширил вопрос. Сформируй полный объединенный запрос (merged_query).
- 'REPLACE': врач полностью сменил тему или отменил прежний вопрос в пользу другого.
- 'WAIT_FOR_ANSWER': врач просто напоминает о себе, торопит или пишет несвязанное междометие. Прежняя генерация актуальна.
- 'CANCEL': врач отказался от ответа.

Ответь ИСКЛЮЧИТЕЛЬНО в формате JSON:
{{"action": "SUPPLEMENT"|"REPLACE"|"WAIT_FOR_ANSWER"|"CANCEL", "merged_query": "полный текст для ответа", "reason": "краткое объяснение"}}
"""

    if generate_gemini_fn is None:
        try:
            import blocking_tools
            generate_gemini_fn = blocking_tools.generate_gemini_text_async
        except ImportError:
            pass

    if generate_gemini_fn:
        try:
            context = {"kind": "inflight_triage", "thinking_level": "LOW"}
            resp, err = await generate_gemini_fn(triage_prompt, context, timeout=7.0)
            if not err and resp:
                raw_text = getattr(resp, "text", "") or ""
                clean_json = raw_text.strip()
                if "```json" in clean_json:
                    clean_json = clean_json.split("```json", 1)[1].split("```", 1)[0].strip()
                elif "```" in clean_json:
                    clean_json = clean_json.split("```", 1)[1].split("```", 1)[0].strip()

                m = re.search(r"\{.*\}", clean_json, re.DOTALL)
                if m:
                    parsed = json.loads(m.group(0))
                    action = str(parsed.get("action", "")).strip().upper()
                    if action in ("SUPPLEMENT", "REPLACE", "WAIT_FOR_ANSWER", "CANCEL"):
                        merged = parsed.get("merged_query") or f"{orig_clean}\n{combined_followup}"
                        return {
                            "action": action,
                            "merged_query": merged.strip(),
                            "reason": str(parsed.get("reason", "LLM triage decision")),
                        }
        except Exception as triage_err:
            logger.warning("LLM triage error in run_inflight_followup_triage: %s", triage_err)

    # 3. Надежная эвристическая страховка (Fallback)
    # Если в догоняющем тексте есть содержательные слова/термины — объединяем!
    if len(combined_followup) >= 6:
        return {
            "action": "SUPPLEMENT",
            "merged_query": f"{orig_clean}\nДополнение от коллеги: {combined_followup}".strip(),
            "reason": "Fallback substantive follow-up heuristic",
        }

    return {
        "action": "WAIT_FOR_ANSWER",
        "merged_query": orig_clean,
        "reason": "Fallback default",
    }
