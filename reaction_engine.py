# -*- coding: utf-8 -*-
"""
reaction_engine.py — Батчевый нейросетевой движок реакций StomChat.

Архитектура:
- Сообщения накапливаются батчами по 8-10 штук на чат (или по таймауту тишины 3 мин).
- ОДИН легковесный запрос к нейросети (Gemini Flash-Lite / Groq) на весь батч из 10 сообщений.
- Нейросеть видит контекст живого диалога и выбирает НЕ БОЛЕЕ 2 самых достойных
  сообщений, возвращая строго обоснованный JSON-массив:
  [{"msg_id": 181504, "reaction": "🔥", "reason": "Крутая эндо-работа с биокерамикой"}]
- ДЕТЕРМИНИРОВАННЫЙ КЛИНИЧЕСКИЙ ГАРД: 100% запрет любых веселых/одобрительных реакций на
  осложнения, поломки инструментов, перфорации, боль, некроз, отек и ятрогении.
- Полная защита от FloodWait, rate-limiting (макс 12 реакций/час на чат, 3 мин кулдаун на автора),
  concurrency-lock против параллельных флашей и таймауты RPC-запросов.
"""

import asyncio
from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("reaction_engine")

# ── Параметры батчинга и лимитов ──────────────────────────────────────────────
BATCH_SIZE_THRESHOLD = 10          # Количество сообщений для немедленного сброса батча
IDLE_FLUSH_INTERVAL = 180.0        # Сброс батча при тишине в чате 3 минуты (если >= 2 сообщений)
MAX_REACTIONS_PER_BATCH = 2        # Не более 2 реакций на пачку из 10 сообщений
MAX_REACTIONS_PER_HOUR = 12        # Защита от спама и FloodWait: не более 12 реакций в час на чат
SENDER_COOLDOWN_SECONDS = 180.0    # Не чаще 1 реакции одному врачу раз в 3 минуты
RPC_TIMEOUT_SECONDS = 10.0         # Таймаут на сетевой RPC-запрос реакции в Telegram
INTER_REACTION_SLEEP = 2.0         # Пауза между отправками реакций в Telegram

# ── Детерминированный негативный клинический фильтр ───────────────────────────
COMPLICATION_PATTERNS = [
    r"\bперфорац\w*",
    r"\bпроби[лт]\w*\s+(?:дно|корен|апекс|стенк)",
    r"\bслома[ллс]\w*",
    r"\bполомк\w*",
    r"\bотлом\w*",
    r"\bоблом\w*",
    r"\bвывели?\s+(?:за\s+апекс|за\s+верхушк)",
    r"\bгипохлор\w*\s+(?:авари|ожог|выход|отек|отёк|всплеск|осложнен)",
    r"\bпарестез\w*",
    r"\bонемен\w*",
    r"\bанафилак\w*",
    r"\bот[её]к\s+квинке",
    r"\bколлапс\b",
    r"\bаспирац\w*",
    r"\bпроглотил\w*",
    r"\bвдохнул\w*",
    r"\bтрещин\w*\s+корн",
    r"\bвертикальн\w*\s+перелом",
    r"\bнекроз\w*",
    r"\bфлегмон\w*",
    r"\bгной\w*",
    r"\bпериостит\w*",
    r"\bятроген\w*",
]
CLINICAL_COMPLICATION_RE = re.compile("|".join(COMPLICATION_PATTERNS), re.IGNORECASE)

# Эмодзи, категорически запрещенные при любых осложнениях, травмах и жалобах
CELEBRATORY_EMOJIS = {"😂", "🤣", "🔥", "👍", "👏", "⚡", "🎉", "👌", "💡", "💯", "❤️", "❤"}


def is_clinical_complication(text: str) -> bool:
    """Определяет наличие врачебной беды, поломки, перфорации или осложнения."""
    if not text:
        return False
    return bool(CLINICAL_COMPLICATION_RE.search(text))


@dataclass
class BufferedMessage:
    msg_id: int
    chat_id: int
    sender_id: int
    sender_name: str
    text: str
    has_media: bool
    timestamp: float
    event: Any


# ── Буферы сообщений и состояние рантайма ────────────────────────────────────
_CHAT_BUFFERS: Dict[int, List[BufferedMessage]] = {}
_CHAT_LOCKS: Dict[int, asyncio.Lock] = {}
_CHATS_BEING_FLUSHED: Set[int] = set()
_GLOBAL_REACTION_HISTORY: List[float] = []
_SENDER_REACTION_TIMESTAMPS: Dict[Tuple[int, int], float] = {}
_FLUSHER_TASK: Optional[asyncio.Task] = None


def _get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _CHAT_LOCKS:
        _CHAT_LOCKS[chat_id] = asyncio.Lock()
    return _CHAT_LOCKS[chat_id]


def _is_rate_limited(chat_id: int, sender_id: int) -> bool:
    """Проверяет соблюдение глобальных и per-sender лимитов реакций."""
    now = time.time()
    # 1. Проверка глобального почасового лимита
    cutoff = now - 3600.0
    _GLOBAL_REACTION_HISTORY[:] = [t for t in _GLOBAL_REACTION_HISTORY if t > cutoff]
    if len(_GLOBAL_REACTION_HISTORY) >= MAX_REACTIONS_PER_HOUR:
        logger.debug("[BatchReactions] Hourly reaction rate limit reached (%d/hr)", len(_GLOBAL_REACTION_HISTORY))
        return True

    # 2. Проверка per-sender кулдауна
    key = (chat_id, sender_id)
    last_time = _SENDER_REACTION_TIMESTAMPS.get(key, 0.0)
    if (now - last_time) < SENDER_COOLDOWN_SECONDS:
        logger.debug("[BatchReactions] Sender %s cooldown active (%.1fs remaining)", sender_id, SENDER_COOLDOWN_SECONDS - (now - last_time))
        return True

    return False


# ── Парсинг JSON-массива вердиктов нейросети ─────────────────────────────────
def parse_batch_reactions_json(raw_text: str) -> List[Dict[str, Any]]:
    """Извлекает список вердиктов [{msg_id: int, reaction: str, reason: str}] из ответа LLM."""
    if not raw_text:
        return []

    cleaned = raw_text.strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()
        else:
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned).strip()

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            results = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                msg_id = item.get("msg_id")
                reaction = item.get("reaction")
                reason = str(item.get("reason", "")).strip()
                if msg_id and reaction and str(reaction).lower() not in ("null", "none"):
                    emoji = str(reaction).strip('"\'`').rstrip('\ufe0f')
                    results.append({
                        "msg_id": int(msg_id),
                        "reaction": emoji,
                        "reason": reason
                    })
            return results
    except Exception as e:
        logger.debug("Failed to parse batch reactions JSON: %s (raw text: %r)", e, raw_text[:120])

    return []


# ── Промпт для батча сообщений ────────────────────────────────────────────────
def build_batch_prompt(messages: List[BufferedMessage], allowed_emojis_str: str) -> str:
    """Формирует промпт для батчевой оценки диалога нейросетью."""
    lines = []
    for m in messages:
        media_tag = " (МЕДИА/СНИМОК)" if m.has_media else ""
        clean_text = m.text.replace("\n", " ")[:250] if m.text else "[без текста]"
        lines.append(f"[#{m.msg_id}] {m.sender_name}{media_tag}: «{clean_text}»")

    formatted_batch = "\n".join(lines)

    return f"""Ты — опытный коллега-стоматолог в профессиональном Telegram-чате врачей.
Тебе передан фрагмент переписки коллег из {len(messages)} сообщений:

{formatted_batch}

Список разрешенных эмодзи в чате: {allowed_emojis_str}

ЗАДАЧА:
Оцени живую динамику диалога и выбери НЕ БОЛЕЕ 2 самых достойных сообщений из этого списка, на которые уместно поставить реакцию-эмодзи.
Все остальные сообщения должны остаться БЕЗ реакций.

КЛИНИЧЕСКИЕ ПРАВИЛА И ЭТИКА:
1. НИКОГДА не ставить легкомысленные или радостные реакции (😂, 🤣, 🔥, 👍) на врачебную беду, сломанные инструменты, перфорации, боль, отеки, гной или осложнения! Если врач в отчаянии или жалуется на беду — СТРОГО НОЛЬ РЕАКЦИЙ.
2. На прямые вопросы коллег, просьбы о помощи, сомнения и жалобы на работу бота — СТРОГО НОЛЬ РЕАКЦИЙ (пустой список). Не ставь реакции на вопросы.
3. Реакций достойны только:
   - Реально классные клинические результаты, КЛКТ, прицельные снимки с плотной обтурацией, красивая реставрация -> 🔥, 👏, 👍
   - Остроумный профессиональный юмор коллег, жизненная самоирония -> 😂, 🤣
   - Ценный клинический совет, коллегиальная взаимовыручка, слова благодарности -> 🤝, ❤️, 🙏, 🫡
4. Если подходящих сообщений меньше 2, верни меньше. Если ни одно не заслуживает, верни пустой список []. По умолчанию предпочитай пустой список [].

ФОРМАТ ОТВЕТА СТРОГО JSON-МАССИВ (не более 1-2 элементов):
[
  {{"msg_id": 181504, "reaction": "🔥", "reason": "Качественная обтурация трех каналов 46 биокерамикой"}},
  {{"msg_id": 181508, "reaction": "🤝", "reason": "Дельный совет коллеге по протоколу пескоструйной обработки"}}
]
"""


def evaluate_batch_sync(messages: List[BufferedMessage], timeout: float = 5.5) -> List[Dict[str, Any]]:
    """Синхронный вызов нейросети на батч сообщений с детерминированной клинической валидацией."""
    import assistant
    try:
        import gemini_client
    except ImportError:
        logger.warning("gemini_client not available for batch reactions")
        return []

    allowed_list = sorted({e.rstrip('\ufe0f') for e in assistant.ALLOWED_REACTIONS})
    allowed_str = " ".join(allowed_list)

    prompt = build_batch_prompt(messages, allowed_str)
    status_context = {
        "kind": "react_standalone_triage",
        "thinking_level": "LOW",
        "max_tokens": 200
    }

    try:
        response = gemini_client.generate_text(prompt, status_context=status_context, timeout=timeout)
        if not response:
            return []
        raw_text = getattr(response, "text", "") or ""
        parsed = parse_batch_reactions_json(raw_text)

        msg_map = {m.msg_id: m for m in messages}
        valid_results = []
        for item in parsed:
            mid = item.get("msg_id")
            emoji = item.get("reaction")
            if not mid or not emoji or mid not in msg_map:
                continue

            msg_obj = msg_map[mid]

            # 1. Валидация разрешенных эмодзи
            if emoji not in assistant.ALLOWED_REACTIONS and f"{emoji}\ufe0f" not in assistant.ALLOWED_REACTIONS:
                logger.debug("[BatchReactions] Rejected emoji %r not in ALLOWED_REACTIONS", emoji)
                continue

            # 2. ЖЕЛЕЗНЫЙ КЛИНИЧЕСКИЙ ГАРД: запрет реакций на осложнения
            if is_clinical_complication(msg_obj.text):
                logger.info("[BatchReactions] Hard blocked reaction %r on clinical complication msg_id=%s: %r", emoji, mid, msg_obj.text[:80])
                continue

            # 3. Лимиты частоты отправки
            if _is_rate_limited(msg_obj.chat_id, msg_obj.sender_id):
                continue

            valid_results.append(item)
            if len(valid_results) >= MAX_REACTIONS_PER_BATCH:
                break

        return valid_results
    except Exception as exc:
        logger.warning("evaluate_batch_sync error: %s", exc)
        return []


async def flush_chat_batch(chat_id: int):
    """Сброс и нейросетевая обработка накопившегося батча сообщений чата."""
    import assistant

    # 1. Проверка глобальной паузы реакций (FloodWait)
    now = time.time()
    flood_until = getattr(assistant, "_REACTION_FLOOD_UNTIL", 0.0)
    if now < flood_until:
        logger.debug("[BatchReactions] Global FloodWait cooldown active (%.1fs remaining). Skipping flush.", flood_until - now)
        return

    # 2. Защита от параллельного сброса батчей одного чата
    global _CHATS_BEING_FLUSHED
    if chat_id in _CHATS_BEING_FLUSHED:
        logger.debug("[BatchReactions] Chat %s is already being flushed. Skipping parallel flush.", chat_id)
        return
    _CHATS_BEING_FLUSHED.add(chat_id)

    try:
        lock = _get_lock(chat_id)
        async with lock:
            buffer = _CHAT_BUFFERS.get(chat_id, [])
            if not buffer:
                return

            batch_to_eval = list(buffer[:BATCH_SIZE_THRESHOLD])
            _CHAT_BUFFERS[chat_id] = buffer[len(batch_to_eval):]

        if not batch_to_eval:
            return

        logger.info("[BatchReactions] Evaluating batch of %d messages for chat_id=%s...", len(batch_to_eval), chat_id)

        # Запуск LLM в отдельном потоке
        chosen_reactions = await asyncio.to_thread(evaluate_batch_sync, batch_to_eval)

        if not chosen_reactions:
            logger.info("[BatchReactions] Evaluated %d messages for chat_id=%s: 0 reactions needed.", len(batch_to_eval), chat_id)
            return

        # Сопоставляем вердикты с объектами сообщений
        msg_map = {m.msg_id: m for m in batch_to_eval}
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji
        try:
            from telethon.errors import FloodWaitError
        except ImportError:
            FloodWaitError = None

        applied_count = 0
        for item in chosen_reactions:
            mid = item["msg_id"]
            emoji = item["reaction"]
            reason = item["reason"]
            msg_obj = msg_map.get(mid)
            if not msg_obj or not msg_obj.event:
                continue

            tg_client = getattr(msg_obj.event, "client", None)
            if not tg_client:
                continue

            # Дополнительная проверка на уже стоящие реакции самого клиента
            try:
                prefetched = getattr(msg_obj.event, "message", None) or msg_obj.event
                existing = getattr(prefetched, "reactions", None)
                if existing:
                    already_reacted = False
                    for r in getattr(existing, "results", []):
                        if getattr(r, "chosen_order", None) is not None:
                            already_reacted = True
                            break
                    if already_reacted:
                        logger.debug("[BatchReactions] Client already reacted to msg_id=%s. Skipping.", mid)
                        continue
            except Exception:
                pass

            try:
                await asyncio.wait_for(
                    tg_client(SendReactionRequest(
                        peer=chat_id,
                        msg_id=mid,
                        reaction=[ReactionEmoji(emoticon=emoji)],
                        add_to_recent=True
                    )),
                    timeout=RPC_TIMEOUT_SECONDS
                )
                applied_count += 1
                curr_t = time.time()
                _GLOBAL_REACTION_HISTORY.append(curr_t)
                _SENDER_REACTION_TIMESTAMPS[(chat_id, msg_obj.sender_id)] = curr_t

                logger.info(
                    "[BatchReactions] Set reaction '%s' on msg_id=%s in chat=%s (Author: %s). Reason: %s",
                    emoji, mid, chat_id, msg_obj.sender_name, reason
                )
                await asyncio.sleep(INTER_REACTION_SLEEP)
            except Exception as exc:
                if FloodWaitError and isinstance(exc, FloodWaitError):
                    wait_sec = getattr(exc, "seconds", 60) or 60
                    setattr(assistant, "_REACTION_FLOOD_UNTIL", time.time() + wait_sec + 15)
                    logger.warning("[BatchReactions] FloodWaitError on reaction send: backing off %ds. Reactions paused globally.", wait_sec)
                    break
                logger.warning("[BatchReactions] Failed to put reaction '%s' on msg_id=%s: %s", emoji, mid, exc)

        logger.info("[BatchReactions] Batch completed: set %d reactions out of %d messages.", applied_count, len(batch_to_eval))
    finally:
        _CHATS_BEING_FLUSHED.discard(chat_id)


async def _flusher_loop():
    """Фоновый таймер проверки буферов на сброс по таймауту тишины."""
    logger.info("Batch reaction flusher loop active")
    while True:
        try:
            await asyncio.sleep(30.0)
            now = time.time()
            chat_ids = list(_CHAT_BUFFERS.keys())
            for cid in chat_ids:
                if cid in _CHATS_BEING_FLUSHED:
                    continue
                buf = _CHAT_BUFFERS.get(cid, [])
                if len(buf) >= 2 and (now - buf[-1].timestamp) >= IDLE_FLUSH_INTERVAL:
                    logger.info("[BatchReactions] Triggering idle flush for chat %s (buffer size: %d, idle: %.1fs)", cid, len(buf), now - buf[-1].timestamp)
                    asyncio.create_task(flush_chat_batch(cid))
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in batch reaction flusher loop: %s", e)
            await asyncio.sleep(5.0)


def start_engine_worker(loop: Optional[asyncio.AbstractEventLoop] = None):
    """Инициализация фонового таймера батчинга."""
    global _FLUSHER_TASK
    if loop is None:
        loop = asyncio.get_event_loop()
    if _FLUSHER_TASK is None or _FLUSHER_TASK.done():
        _FLUSHER_TASK = loop.create_task(_flusher_loop())
        logger.info("Batch reaction flusher task started")


def stop_engine_worker():
    """Остановка фонового таймера."""
    global _FLUSHER_TASK
    if _FLUSHER_TASK and not _FLUSHER_TASK.done():
        _FLUSHER_TASK.cancel()
        _FLUSHER_TASK = None


async def process_incoming_event(
    event=None,
    client=None,
    msg_id: int = 0,
    chat_id: int = 0,
    sender_id: int = 0,
    sender_name: str = "",
    text: str = "",
    has_media: bool = False,
    **kwargs
) -> bool:
    """
    Точка входа входящих сообщений: накапливает сообщение в буфер чата.
    Если буфер достиг порога BATCH_SIZE_THRESHOLD, запускает немедленную оценку.
    """
    if event is None and not (msg_id and (chat_id or kwargs.get("chat_id"))):
        return False

    if not chat_id and event is not None:
        chat_id = getattr(event, "chat_id", 0) or kwargs.get("chat_id", 0)
    elif not chat_id:
        chat_id = kwargs.get("chat_id", 0)

    is_group = False
    if event is not None:
        is_group = bool(getattr(event, "is_group", False) or (chat_id and chat_id < 0))
    elif chat_id and chat_id < 0:
        is_group = True
    if not is_group:
        return False

    if not sender_id and event is not None:
        try:
            sender = await event.get_sender()
            if not sender or getattr(sender, "bot", False):
                return False
            sender_id = getattr(sender, "id", 0)
            if not sender_name:
                sender_name = getattr(sender, "first_name", "") or getattr(sender, "username", "") or f"Doctor_{sender_id}"
        except Exception:
            return False
    elif not sender_id:
        return False

    if not text and event is not None:
        text = (getattr(event, "text", "") or "").strip()
    else:
        text = (text or "").strip()

    if text.startswith("/"):
        return False

    if not has_media and event is not None:
        has_media = bool(getattr(event, "media", None) or getattr(event, "photo", None) or getattr(event, "document", None))

    if not text and not has_media:
        return False

    if not msg_id and event is not None:
        msg_id = getattr(event, "id", 0)

    if not sender_name:
        sender_name = f"Doctor_{sender_id}"

    # Инициализация таймера если еще не запущен
    global _FLUSHER_TASK
    if _FLUSHER_TASK is None or _FLUSHER_TASK.done():
        try:
            loop = asyncio.get_running_loop()
            start_engine_worker(loop)
        except RuntimeError:
            pass

    item = BufferedMessage(
        msg_id=msg_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        has_media=has_media,
        timestamp=time.time(),
        event=event
    )

    lock = _get_lock(chat_id)
    should_flush = False
    async with lock:
        buf = _CHAT_BUFFERS.setdefault(chat_id, [])
        buf.append(item)
        if len(buf) >= BATCH_SIZE_THRESHOLD:
            should_flush = True

    if should_flush:
        logger.info("[BatchReactions] Buffer reached threshold %d for chat %s. Flushing...", BATCH_SIZE_THRESHOLD, chat_id)
        asyncio.create_task(flush_chat_batch(chat_id))

    return True
