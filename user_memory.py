"""
Модуль долговременной клинической памяти о врачах (Clinician Memory).

Архитектура:
1. Память при общении в ЛС (до 64 КБ на врача):
   - Не тупой append! Нейросеть периодически (раз в несколько сообщений или при разборе кейса)
     получает текущее досье и свежие реплики, переписывает и актуализирует профиль врача:
     уточняет специализацию, обновляет протоколы и материалы, фиксирует динамику кейсов, убирает устаревшее.
2. Память общей беседы (до 8 КБ на врача):
   - Фоновый демон на дешёвой нейросети (Gemini 3.5 Flash Lite / Groq Qwen) анализирует
     сообщения участников в чате и формирует компактное клиническое досье (до 8 КБ).
3. Присовокупление к контексту LLM:
   - В ЛС: в промпт консилиума добавляется персональная долговременная память врача (до 64 КБ) с инструкцией.
   - В группе: для авторов реплик в текущем чанке добавляются их профили из беседы (до 8 КБ).
"""

import asyncio
import json
import logging
import re
import time
from typing import Dict, List, Optional

import blocking_tools
import database

generate_gemini_text_async = getattr(blocking_tools, "generate_gemini_text_async", None)

logger = logging.getLogger(__name__)

# Лимиты объемов памяти в символах
PM_USER_MEMORY_LIMIT = 64000     # 64 КБ для ЛС
GROUP_USER_MEMORY_LIMIT = 8000   # 8 КБ для общей беседы

# Периодичность обновления памяти в ЛС (раз в 4 сообщения диалога просить нейронку актуализировать профиль)
PM_UPDATE_EVERY_N_MESSAGES = 4

# Интервал работы демона памяти беседы (раз в 4 часа = 14400 секунд, строго если были новые сообщения)
GROUP_MEMORY_DAEMON_INTERVAL = 14400  # 4 часа

# Cooldown между вызовами нейросети для обновления памяти одного юзера (защита квот)
_PM_MEMORY_COOLDOWN = 15.0
_LAST_PM_UPDATE_TS: Dict[int, float] = {}

# Паттерны тривиальных сообщений (приветствия, благодарности, команды)
_TRIVIAL_USER_PATTERNS = {
    "привет", "здравствуйте", "добрый день", "доброе утро", "добрый вечер",
    "спасибо", "благодарю", "понял", "ясно", "ок", "хорошо", "ладно", "до встречи",
    "пока", "/start", "/help", "/wipe", "/style", "/quiz", "/calc", "/bookmarks", "/stats"
}


def is_trivial_message(text: str) -> bool:
    """Проверяет, является ли сообщение тривиальным (приветствие, спасибки, команды)."""
    if not text:
        return True
    cleaned = text.strip().lower()
    if len(cleaned) < 8:
        return True
    if cleaned in _TRIVIAL_USER_PATTERNS:
        return True
    words = [w.strip("!.,?:;)") for w in cleaned.split()]
    if not words:
        return True
    if all(w in _TRIVIAL_USER_PATTERNS or w in ("большое", "огромное", "очень", "вам", "тебе", "всем", "бот", "коллега", "день", "утро", "вечер") for w in words):
        return True
    if cleaned.startswith(("/", "!", "спасибо", "привет", "здравствуй")):
        if len(words) <= 3:
            return True
    return False


async def get_clinician_memory(user_id: int) -> dict:
    """
    Возвращает структуру памяти врача.
    Если память пуста, инициализирует из legacy user_profiles (profile_portrait).
    """
    try:
        mem = await database.get_user_memory(user_id)
        if not mem.get("clinical_summary") and not mem.get("specialty"):
            profile = await database.get_user_profile(user_id)
            portrait = profile.get("profile_portrait")
            if portrait and "формируется" not in portrait.lower() and "недостаточно" not in portrait.lower():
                mem["clinical_summary"] = portrait
                await database.save_user_memory(
                    user_id=user_id,
                    clinical_summary=portrait,
                    message_count=1
                )
        return mem
    except Exception as e:
        logger.error(f"Error fetching clinician memory for {user_id}: {e}")
        return {
            "user_id": user_id,
            "username": "",
            "first_name": "",
            "specialty": "",
            "clinical_summary": "",
            "group_summary": "",
            "facts_json": "[]",
            "message_count": 0,
            "pm_message_count": 0,
            "group_message_count": 0,
            "last_pm_analyzed_id": 0,
            "last_group_analyzed_id": 0,
            "last_updated": None,
        }


def format_clinician_memory_prompt(user_id: int, memory: Optional[dict] = None) -> str:
    """
    Форматирует блок персональной долговременной памяти врача для подстановки в системный промпт ЛС.
    Сопровождается явным комментарием для модели, что это за данные и как их использовать.
    """
    if not memory:
        return "Клинический профиль доктора формируется (первые обращения)."

    specialty = memory.get("specialty", "").strip()
    summary = memory.get("clinical_summary", "").strip()
    facts_raw = memory.get("facts_json", "[]")

    facts_list = []
    try:
        if facts_raw:
            facts_list = json.loads(facts_raw)
            if not isinstance(facts_list, list):
                facts_list = []
    except Exception:
        facts_list = []

    body = []
    if specialty:
        body.append(f"• Подтвержденная специализация: {specialty}")
    if summary:
        body.append(f"• Актуальное клиническое досье (оборудование, материалы, протоколы, кейсы):\n{summary}")
    if facts_list:
        recent_facts = facts_list[-8:]
        facts_str = "\n".join([f"  - {f}" for f in recent_facts])
        body.append(f"• Ключевые предпочтения и особенности практики:\n{facts_str}")

    if not body:
        return "Клинический профиль доктора формируется (первые обращения)."

    content = "\n".join(body)
    return f"""=== ДОЛГОВРЕМЕННАЯ ПАМЯТЬ И КЛИНИЧЕСКИЙ ПРОФИЛЬ ВРАЧА (ИЗ ЛС) ===
[Справочная информация для ассистента: этот блок сформирован и непрерывно актуализируется ИИ на основе всей истории общения с доктором в ЛС. Память содержит его специализацию, используемые протоколы/материалы и обсуждавшиеся клинические случаи. Опирайся на эти знания, веди диалог на равных и не переспрашивай то, что уже известно]:
{content}"""


async def format_users_chunk_context(user_ids: List[int]) -> str:
    """
    Форматирует накопленные профили авторов реплик в группе (до 8 КБ на врача, до 20 врачей в чанке)
    с комментариями для нейросети при анализе беседы или дайджестов.
    """
    if not user_ids:
        return ""
    unique_ids = list(dict.fromkeys(user_ids))[:20]
    try:
        batch = await database.get_users_memory_batch(unique_ids)
        if not batch:
            return ""

        notes = []
        for uid, mem in batch.items():
            name_parts = []
            if mem.get("first_name"):
                name_parts.append(mem["first_name"])
            if mem.get("username"):
                name_parts.append(f"(@{mem['username']})")
            doc_label = " ".join(name_parts) if name_parts else f"Врач #{uid}"

            spec = mem.get("specialty", "").strip()
            # Берем память беседы (group_summary), а при ее отсутствии — часть ЛС памяти
            grp_sum = mem.get("group_summary", "").strip()
            if not grp_sum:
                grp_sum = mem.get("clinical_summary", "").strip()

            profile_text = ""
            if grp_sum:
                # Берем до 300 символов самой сути профиля для чанка
                profile_text = grp_sum[:300].strip()
                if len(grp_sum) > 300:
                    profile_text += "..."

            desc_parts = []
            if spec:
                desc_parts.append(spec)
            if profile_text and profile_text != spec:
                desc_parts.append(profile_text)

            if desc_parts:
                notes.append(f"• {doc_label}: {'; '.join(desc_parts)}")

        if not notes:
            return ""

        joined_notes = "\n".join(notes)
        return f"""=== НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ (ИЗ БЕСЕДЫ) ===
[Справочная информация для ассистента: это выжимка из накопленной памяти о врачах-участниках текущего обсуждения, составленная ИИ по их сообщениям в чате. Учитывай специализацию и клинический опыт собеседников]:
{joined_notes}"""
    except Exception as e:
        logger.error(f"Error formatting users chunk context: {e}")
        return ""


async def update_clinician_memory_async(
    user_id: int,
    user_message: str,
    bot_response: str,
    username: str = "",
    first_name: str = ""
):
    """
    Интеллектуальное обновление памяти врача в ЛС.
    Не тупой append: раз в несколько сообщений или при появлении содержательного кейса
    просим нейросеть полностью переписать, уточнить и актуализировать клинический профиль доктора (до 64 КБ).
    """
    if not user_id or is_trivial_message(user_message):
        return

    now = time.time()
    last_ts = _LAST_PM_UPDATE_TS.get(user_id, 0.0)
    if now - last_ts < _PM_MEMORY_COOLDOWN:
        logger.debug(f"PM clinician memory update for {user_id} throttled by cooldown.")
        return
    _LAST_PM_UPDATE_TS[user_id] = now

    try:
        mem = await get_clinician_memory(user_id)
        current_spec = mem.get("specialty", "")
        current_summary = mem.get("clinical_summary", "")
        current_pm_count = mem.get("pm_message_count", 0) + 1

        # Проверяем, наступил ли интервал обновления (раз в PM_UPDATE_EVERY_N_MESSAGES реплик)
        # либо это первое сообщение или объемный клинический кейс
        is_first_time = not current_summary
        is_interval = (current_pm_count % PM_UPDATE_EVERY_N_MESSAGES == 0)
        has_rich_case = len(user_message) > 250 or any(w in user_message.lower() for w in (
            "снимок", "рентген", "клкт", "пациент", "кейс", "bopt", "имплант", "канал", "протокол"
        ))

        if not (is_first_time or is_interval or has_rich_case):
            # Просто инкрементируем счетчик сообщений без вызова дорогой LLM
            await database.save_user_memory(
                user_id=user_id,
                pm_message_count=current_pm_count,
                username=username,
                first_name=first_name
            )
            return

        current_facts = []
        try:
            current_facts = json.loads(mem.get("facts_json", "[]"))
            if not isinstance(current_facts, list):
                current_facts = []
        except Exception:
            current_facts = []

        # Промпт для ИИ: актуализация и переписывание (не дописывание в конец!)
        prompt = f"""Ты — клинический секретарь профессионального консилиума StomChat.
Перед тобой текущее досье врача-стоматолога и новая реплика из личной переписки.

Текущий профиль врача:
Специализация: {current_spec or "не определена"}
Клиническое досье:
{current_summary or "профиль формируется"}

Свежее сообщение врача:
{user_message[:2000]}

Ответ ассистента:
{bot_response[:1000]}

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. НЕ ДОПИСЫВАЙ текст просто в конец! Твоя задача — актуализировать, уточнить и ПЕРЕПИСАТЬ профиль врача как связный, структурированный документ.
2. Уточни специализацию (терапевт, эндодонтист, ортопед, хирург-имплантолог, ортодонт, гнатолог, детский стоматолог).
3. Обнови используемые материалы, бренды, протоколы и аппараты (например: BOPT, микроскоп, ультразвук, бинокуляры, OptiBond FL, силеры, импланты Straumann/Osstem). Если врач сменил предпочтение — отрази это.
4. Добавь новые разобранные клинические кейсы (с указанием зуба и патологии) и динамику по старым.
5. Удали пустые фразы, эмоциональные междометия и устаревшие детали.
6. Верни СТРОГО JSON следующего формата:
{{
  "specialty": "уточненная специализация",
  "rewritten_summary": "полностью переписанный и актуализированный текст клинического профиля врача (до 64 КБ)",
  "new_facts": ["факт 1", "факт 2"]
}}
"""

        status_ctx = {"kind": "llama_triage", "thinking_level": "LOW"}
        response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=45)

        if error or not response or not getattr(response, "text", None):
            logger.debug(f"PM memory AI rewriting returned error or empty for {user_id}")
            return

        resp_text = response.text.strip()
        json_match = re.search(r"\{.*\}", resp_text, re.DOTALL)
        if not json_match:
            logger.debug(f"Could not parse JSON from memory rewriting: {resp_text[:120]}")
            return

        parsed = json.loads(json_match.group(0))
        new_spec = parsed.get("specialty", "").strip()
        rewritten_summary = parsed.get("rewritten_summary", "").strip()
        new_facts = parsed.get("new_facts", [])

        final_spec = new_spec if new_spec and len(new_spec) > 2 else current_spec

        if isinstance(new_facts, list):
            for f in new_facts:
                f_str = str(f).strip()
                if f_str and f_str not in current_facts:
                    current_facts.append(f_str)
        if len(current_facts) > 30:
            current_facts = current_facts[-30:]

        final_summary = rewritten_summary if rewritten_summary else current_summary

        # Жесткий потолок 64 КБ
        if len(final_summary) > PM_USER_MEMORY_LIMIT:
            final_summary = final_summary[:PM_USER_MEMORY_LIMIT]

        # Сохраняем актуализированную память в БД
        await database.save_user_memory(
            user_id=user_id,
            specialty=final_spec,
            clinical_summary=final_summary,
            facts_json=json.dumps(current_facts, ensure_ascii=False),
            pm_message_count=current_pm_count,
            username=username,
            first_name=first_name
        )

        # Синхронизируем profile_portrait в user_profiles для обратной совместимости
        first_line = final_summary.split("\n")[0].strip() if final_summary else ""
        portrait_text = f"{final_spec}. {first_line}".strip() if final_spec else first_line
        if portrait_text:
            last_msg_id = await database.get_last_msg_id()
            await database.set_user_portrait(user_id, portrait_text[:400], last_msg_id)

        logger.info(
            f"Successfully updated & rewritten clinician PM memory for user {user_id} "
            f"(spec='{final_spec}', summary_len={len(final_summary)})"
        )
    except Exception as e:
        logger.error(f"Failed to rewrite clinician memory for {user_id}: {e}", exc_info=True)


async def process_group_memory_daemon_batch(min_new_messages: int = 3, limit: int = 5):
    """
    Один такт демона памяти беседы (группового чата) на дешёвой нейросети.
    Находит активных врачей, засветившихся в логе/дампе чата с новыми сообщениями,
    и актуализирует их group_summary (до 8 КБ).
    Если новых сообщений не было — запросы к нейросети не производятся.
    """
    try:
        users_to_process = await database.get_unprocessed_group_users(
            min_new_messages=min_new_messages,
            limit=limit
        )
        if not users_to_process:
            logger.info("Group memory daemon: в чате нет новых сообщений от участников. Пропуск такта (запросы к LLM опущены).")
            return

        logger.info(f"Group memory daemon: обнаружено {len(users_to_process)} активных врачей с новыми сообщениями в чате.")

        for u in users_to_process:
            user_id = u["user_id"]
            max_id = u.get("max_id") or u.get("max_msg_id") or 0
            sender_name = u.get("sender_name", "")
            sender_username = u.get("username", "")

            # Загружаем существующую память
            mem = await database.get_user_memory(user_id)
            current_group_summary = mem.get("group_summary", "")
            current_spec = mem.get("specialty", "")
            last_analyzed = mem.get("last_group_analyzed_id", 0)

            # Получаем свежие сообщения пользователя из лога/дампа чата
            msgs = await database.get_user_messages_since(user_id, since_msg_id=last_analyzed, limit=25)
            if not msgs:
                continue

            msgs_text = "\n".join([f"- {m['text']}" for m in msgs])

            # Промпт для дешёвой нейронки
            prompt = f"""Ты — клинический аналитик сообщества StomChat.
Проанализируй реплики врача в стоматологическом чате и обнови его клинический профиль для беседы.

Врач: {sender_name} (@{sender_username})
Текущий профиль по беседе:
{current_group_summary or "нет данных"}

Новые сообщения врача в группе:
{msgs_text}

Инструкции:
1. НЕ дописывай в конец! Перепиши и актуализируй профиль доктора для беседы:
   - Специализация врача.
   - Клинические взгляды, протоколы, используемые бренды и материалы, которые он упоминает.
   - Характерные клинические случаи и позиция в дискуссиях.
2. Формат: емкий связный текст (1-2 абзаца, строго до 8 КБ).
3. Верни JSON:
{{
  "specialty": "специализация (если понятна)",
  "group_summary": "актуализированный профиль врача для беседы (до 8 КБ)"
}}
"""

            status_ctx = {"kind": "llama_triage", "thinking_level": "LOW"}
            response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=40)

            if error or not response or not getattr(response, "text", None):
                continue

            resp_text = response.text.strip()
            json_match = re.search(r"\{.*\}", resp_text, re.DOTALL)
            if not json_match:
                continue

            parsed = json.loads(json_match.group(0))
            new_spec = parsed.get("specialty", "").strip()
            new_grp_summary = parsed.get("group_summary", "").strip()

            final_spec = new_spec if new_spec and len(new_spec) > 2 else current_spec
            final_grp_summary = new_grp_summary if new_grp_summary else current_group_summary

            # Жесткий потолок 8 КБ для беседы
            if len(final_grp_summary) > GROUP_USER_MEMORY_LIMIT:
                final_grp_summary = final_grp_summary[:GROUP_USER_MEMORY_LIMIT]

            await database.save_user_memory(
                user_id=user_id,
                specialty=final_spec,
                group_summary=final_grp_summary,
                group_message_count=mem.get("group_message_count", 0) + len(msgs),
                last_group_analyzed_id=max_id,
                username=sender_username,
                first_name=sender_name
            )
            logger.info(
                f"Daemon updated group memory for doctor {user_id} ({sender_name}): "
                f"len={len(final_grp_summary)}, max_msg_id={max_id}"
            )

            # Пауза между пользователями (cooldown 2.5с для бережного отношения к API ключам)
            await asyncio.sleep(2.5)

    except Exception as e:
        logger.error(f"Error in process_group_memory_daemon_batch: {e}", exc_info=True)


async def group_memory_daemon_loop(interval_seconds: int = GROUP_MEMORY_DAEMON_INTERVAL):
    """
    Фоновый долгоживущий цикл демона обновления памяти беседы (по умолчанию раз в 4 часа).
    Периодически проверяет накопившиеся сообщения и обновляет профили только тех врачей,
    которые реально писали сообщения в чат.
    """
    logger.info(f"Starting group memory daemon loop (interval={interval_seconds}s / {interval_seconds/3600:.1f}h)...")
    while True:
        try:
            await process_group_memory_daemon_batch(min_new_messages=3, limit=5)
        except Exception as e:
            logger.error(f"Unexpected error in group_memory_daemon_loop: {e}")
        await asyncio.sleep(interval_seconds)
