# -*- coding: utf-8 -*-
"""
clinical_rescue.py — Демон спасения «повисших» клинических кейсов (Unanswered Clinical Rescue).

Решает проблему вакуума у стоматологического кресла:
Когда врач выкладывает клинический снимок или задает острый клинический вопрос в группу,
а коллеги заняты на приеме (0 ответов за 15–90 минут), бот деликатно подключается
с доказательным EBM-разбором и открытым вопросом для вовлечения коллег:
«👨‍⚕️ Коллеги сейчас на приёме у кресла, поэтому подключусь по вашему случаю: ...»

Инварианты безопасности:
1. База данных SQLite: фильтрация по таймстампу (15–90 мин назад), строго 0 ответов ботов и врачей.
2. Не вмешиваться в живую беседу (Activity Guard: если за последние 5 мин >2 сообщений — отложить).
3. Строгий кулдаун: не более 1 спасения в 30 минут, не более 4 в сутки на чат.
4. Защита от удаленных сообщений: автоматический фоллбек на отправку без reply_to.
5. Персистентность: таблица clinical_rescues сохраняет обработанные msg_id навсегда.
"""

from __future__ import annotations

import asyncio
import datetime
import html
import logging
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger("clinical_rescue")

# Маркеры клинического вопроса
QUESTION_MARKERS = [
    "подскажите", "как поступить", "какой протокол", "что делать",
    "кто сталкивался", "посоветуйте", "какой силер", "как пройти",
    "чем обработать", "как лучше", "поделитесь опытом", "что думаете",
    "мнения?", "ваше мнение", "как лечить", "что выбрать", "посоветуете",
    "какая тактика", "кто как делает", "в чем причина"
]

CLINICAL_KEYWORDS = [
    "зуб", "канал", "снимок", "кт", "клкт", "апекс", "мв2", "mb2",
    "мта", "mta", "перфорац", "отлом", "облом", "поломк", "перелом", "сепарац", "штифт",
    "файл", "инструмент", "ирригац", "пульпит", "периодонтит", "билдап", "оверлей", "боль", "корень",
    "фуркац", "трещин", "резорбц", "гипохлорит", "naocl", "эдта", "edta", "коффердам",
    "биокерамик", "силер", "ступеньк", "некроз", "пародонт", "резорцин",
    "гутаперч", "гуттаперч", "феррул", "дентин", "эмаль", "анестези"
]

# Кулдауны и лимиты
RESCUE_COOLDOWN_SECONDS = 1800  # 30 минут между спасениями
MAX_RESCUES_PER_DAY = 4
CHECK_INTERVAL_SECONDS = 180    # проверка раз в 3 минуты


def ensure_rescue_table(db_path: str):
    """Гарантирует существование таблицы clinical_rescues в БД."""
    try:
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS clinical_rescues (
                    msg_id INTEGER PRIMARY KEY,
                    chat_id INTEGER,
                    rescued_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_clinical_rescues_chat_date ON clinical_rescues(chat_id, rescued_at)")
            conn.commit()
    except Exception as e:
        logger.warning("Failed to ensure clinical_rescues table: %s", e)


def is_clinical_rescue_candidate(text: str, has_media: bool = False) -> bool:
    """Определяет, является ли сообщение клиническим кейсом / вопросом."""
    clean = (text or "").strip().lower()
    if clean.startswith("/") or clean.startswith("!"):
        return False
    if has_media:
        return True
    if len(clean) < 15:
        return False
    has_q = ("?" in clean) or any(m in clean for m in QUESTION_MARKERS)
    has_clin = any(k in clean for k in CLINICAL_KEYWORDS)
    return has_q and has_clin


def _get_table_columns(conn, table_name: str) -> set:
    """Возвращает множество имен колонок для указанной таблицы."""
    try:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}
    except Exception:
        return set()


def check_recent_chat_activity(db_path: str, chat_id: int, minutes: int = 5) -> int:
    """Считает число сообщений в чате за последние N минут (Activity Guard)."""
    try:
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")
            cols = _get_table_columns(conn, "messages")
            date_col = "date" if "date" in cols else "timestamp"
            where_clauses = [f"{date_col} >= datetime('now', ?)"]
            params = [f"-{minutes} minutes"]
            if "chat_id" in cols:
                where_clauses.append("chat_id = ?")
                params.append(chat_id)
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM messages WHERE {' AND '.join(where_clauses)}", tuple(params))
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.debug("Error checking recent chat activity: %s", e)
        return 0


def find_unanswered_clinical_candidates(
    db_path: str,
    chat_id: int,
    bot_id: int = 0,
    min_age_minutes: int = 15,
    max_age_minutes: int = 90
) -> List[Dict[str, Any]]:
    """
    Находит в SQLite сообщения с клиническими вопросами/снимками,
    оставшиеся без единого ответа коллег и бота.
    """
    ensure_rescue_table(db_path)
    candidates = []
    try:
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA cache_size = -4000")
            conn.row_factory = sqlite3.Row
            cols = _get_table_columns(conn, "messages")
            bot_cols = _get_table_columns(conn, "bot_sent_messages")

            id_col = "msg_id" if "msg_id" in cols else "id"
            date_col = "date" if "date" in cols else "timestamp"
            text_col = "text" if "text" in cols else "message"

            where_clauses = [
                f"m.{date_col} >= datetime('now', ?)",
                f"m.{date_col} <= datetime('now', ?)",
                "m.sender_id != ?",
                "m.reply_to_msg_id IS NULL",
                f"NOT EXISTS (SELECT 1 FROM messages r WHERE r.reply_to_msg_id = m.{id_col} AND r.sender_id != m.sender_id)",
                f"NOT EXISTS (SELECT 1 FROM clinical_rescues cr WHERE cr.msg_id = m.{id_col})"
            ]
            params: List[Any] = [
                f"-{max_age_minutes} minutes",
                f"-{min_age_minutes} minutes",
                bot_id
            ]

            if "chat_id" in cols:
                where_clauses.insert(0, "m.chat_id = ?")
                params.insert(0, chat_id)

            if "reply_to_msg_id" in bot_cols:
                where_clauses.append(f"m.{id_col} NOT IN (SELECT reply_to_msg_id FROM bot_sent_messages WHERE reply_to_msg_id IS NOT NULL)")

            query = f"""
                SELECT m.{id_col} as target_msg_id, m.sender_id, m.sender_name, m.{text_col} as msg_text, m.{date_col} as msg_date, m.has_media
                FROM messages m
                WHERE {' AND '.join(where_clauses)}
                ORDER BY m.{id_col} ASC
            """
            cursor = conn.cursor()
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            for r in rows:
                raw_text = r["msg_text"] or ""
                has_media = bool(r["has_media"])
                if is_clinical_rescue_candidate(raw_text, has_media):
                    target_id = r["target_msg_id"]
                    candidates.append({
                        "id": target_id,
                        "msg_id": target_id,
                        "chat_id": chat_id,
                        "sender_id": r["sender_id"],
                        "sender_name": r["sender_name"] or "Коллега",
                        "message": raw_text,
                        "text": raw_text,
                        "timestamp": r["msg_date"],
                        "date": r["msg_date"],
                        "has_media": has_media
                    })
    except Exception as e:
        logger.error("Error querying unanswered clinical candidates: %s", e)
    return candidates


def mark_case_rescued(db_path: str, msg_id: int, chat_id: int):
    """Помечает сообщение как спасенное в БД."""
    try:
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")
            now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("""
                INSERT OR REPLACE INTO clinical_rescues (msg_id, chat_id, rescued_at)
                VALUES (?, ?, ?)
            """, (msg_id, chat_id, now_iso))
            conn.commit()
    except Exception as e:
        logger.warning("Failed to mark case %s as rescued: %s", msg_id, e)


def get_rescues_count_24h(db_path: str, chat_id: int) -> int:
    """Возвращает число спасений за последние 24 часа."""
    try:
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = NORMAL")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM clinical_rescues
                WHERE chat_id = ?
                  AND datetime(rescued_at) >= datetime('now', '-24 hours')
            """, (chat_id,))
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.debug("Error getting 24h rescue count: %s", e)
        return 0


# Локальный трекер последнего времени спасения: chat_id -> datetime
_LAST_RESCUE_TIME: Dict[int, datetime.datetime] = {}


async def generate_clinical_rescue_text(candidate: Dict[str, Any], db_path: str, bot_client=None) -> Optional[str]:
    """Генерирует высококачественный доказательный ответ через LLM."""
    doctor_name = candidate.get("sender_name") or "Коллега"
    question_text = (candidate.get("message") or "").strip()
    has_media = candidate.get("has_media", False)
    msg_id = candidate.get("id")

    # Подтягиваем описание снимка из базы, если было медиа
    clean_d = ""
    multimodal_image_urls = []
    if has_media:
        try:
            import database
            rows = await database.query_db_async("SELECT media_description FROM messages WHERE msg_id = ?", (msg_id,))
            if rows and rows[0] and rows[0][0]:
                cand_desc = str(rows[0][0]).strip()
                if cand_desc and cand_desc not in (
                    "-", "None", "MEDIA_UNAVAILABLE", "[медиа — ошибка анализа]",
                    "[Клинический снимок/рентген]", "[Клинический снимок/медиафайл]",
                    "[Ожидается разбор изображения]"
                    ):
                    clean_d = cand_desc
        except Exception as e:
            logger.debug("Failed to fetch media_description in clinical_rescue: %s", e)

        try:
            import media_tools
            cached_url = media_tools.get_cached_image_data_url(msg_id)
            if cached_url:
                multimodal_image_urls.append(cached_url)
            elif bot_client:
                chat_id = candidate.get("chat_id")
                fetched_urls = await media_tools.extract_multimodal_images_async(bot_client, chat_id, [msg_id], max_images=4)
                if fetched_urls:
                    multimodal_image_urls.extend(fetched_urls)
        except Exception as img_err:
            logger.debug("Failed to extract multimodal images in clinical_rescue: %s", img_err)

    has_text = bool(question_text)
    has_desc = bool(clean_d)
    has_imgs = bool(multimodal_image_urls)

    # Защита от спама на пустые сообщения без медиа и без описания
    if not has_text and not has_desc and not has_imgs:
        logger.warning("Clinical rescue aborted for msg_id=%s: no text, no vision description, and no images.", msg_id)
        return None

    is_endo = "endo" in db_path.lower()
    specialty = "врач стоматолог-эндодонтист у кресла" if is_endo else "врач-стоматолог у кресла"
    chat_title = "EndoChat" if is_endo else "StomChat"
    standards = "стандартам ESE/AAE/FDI" if is_endo else "клиническим протоколам Стоматологической Ассоциации (СтАР) и стандартам доказательной медицины (EBM)"

    if has_text:
        desc_part = f"\n[Описание рентгеновского снимка/фото: {clean_d}]" if has_desc else ""
        case_block = f"Доктор {doctor_name} прислал клинический случай:\n\"{question_text}\"{desc_part}"
    else:
        desc_part = f"\n[Клинико-рентгенологическое описание снимка: {clean_d}]" if has_desc else ""
        case_block = (
            f"Доктор {doctor_name} прислал клинический снимок/рентгенограмму без сопроводительного текста.{desc_part}\n"
            "Твоя задача — провести самостоятельный экспертный клинический разбор видимой картины на изображении. "
            "Определи зуб (FDI), анатомию, состояние каналов/периодонта/коронки, качество обтурации/прилегания или патологические очаги. "
            "Дай конкретное экспертное заключение и доказательный протокол действий. "
            "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать «текст кейса не дошёл, сообщение пустое», выкатывать анкеты с требованием анамнеза или выдавать шаблонные лекции!"
        )

    prompt = (
        f"Ты — практикующий {specialty} в закрытом профессиональном чате врачей {chat_title}.\n"
        f"{case_block}\n\n"
        f"Дай конкретный, краткий, высокоточный доказательный разбор по {standards}.\n\n"
        "СТРОГИЕ ПРАВИЛА ОФОРМЛЕНИЯ:\n"
        f"1. Начни ответ сразу по существу с клинического вердикта: анатомия, зуб FDI, патология (никаких шаблонных преамбул «Доктор, по вашему снимку», никаких эмодзи вроде 👨‍⚕️).\n"
        "2. Дай четкую тактику по пунктам (диагностика, протоколы, размеры файлов/инструментов, ирриганты, материалы, риски).\n"
        "3. Форматирование СТРОГО в чистом HTML (<b>, <i>, <code>). НИКАКОГО сырого Markdown (никаких ** или ##)!\n"
        "4. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать эмодзи и задавать дежурные псевдо-вовлекающие вопросы в конце ответа.\n"
        "5. Если к посту прикреплен снимок или дано его описание, детально проанализируй анатомию, периапикальное состояние и обтурацию. Никогда не говори, что снимки не видны, если они переданы.\n"
    )

    try:
        import blocking_tools
        status_ctx = {
            "kind": "clinical_rescue",
            "chat_id": candidate.get("chat_id"),
            "trigger": "clinical_rescue",
            "image_urls": multimodal_image_urls if multimodal_image_urls else None,
            "media_description": clean_d if clean_d else None,
            "has_media": bool(has_media or multimodal_image_urls or clean_d),
        }
        resp, err = await blocking_tools.generate_gemini_text_async(prompt, status_ctx, timeout=90.0)
        raw_text = getattr(resp, "text", "") or ""
        if raw_text and raw_text.strip():
            # Очистка от случайных Markdown-тегов
            clean_resp = raw_text.strip()
            if clean_resp.startswith("```html"):
                clean_resp = clean_resp[7:]
            if clean_resp.startswith("```"):
                clean_resp = clean_resp[3:]
            if clean_resp.endswith("```"):
                clean_resp = clean_resp[:-3]
            return clean_resp.strip()
    except Exception as exc:
        logger.error("Error generating clinical rescue response: %s", exc)

    return None


async def execute_rescue_for_candidate(
    bot_client,
    candidate: Dict[str, Any],
    db_path: str
) -> bool:
    """Выполняет генерацию, безопасную доставку и регистрацию спасения."""
    chat_id = candidate["chat_id"]
    msg_id = candidate["id"]

    logger.info("🚑 Начат процесс Clinical Rescue для msg_id=%s (автор: %s) в chat_id=%s",
                msg_id, candidate.get("sender_name"), chat_id)

    rescue_text = await generate_clinical_rescue_text(candidate, db_path, bot_client=bot_client)
    if not rescue_text:
        logger.warning("Empty rescue text generated for msg_id=%s", msg_id)
        return False

    # Безопасная отправка с защитой от удаленного сообщения (Ghost Reply)
    sent_msg = None
    try:
        sent_msg = await bot_client.send_message(
            entity=chat_id,
            message=rescue_text,
            reply_to=msg_id,
            parse_mode="html"
        )
    except Exception as reply_err:
        logger.warning("Reply_to failed for rescue msg_id=%s: %s. Retrying directly without reply_to.",
                       msg_id, reply_err)
        try:
            sent_msg = await bot_client.send_message(
                entity=chat_id,
                message=rescue_text,
                parse_mode="html"
            )
        except Exception as send_err:
            logger.error("Failed direct rescue send for msg_id=%s: %s", msg_id, send_err)
            return False

    # Записываем в базу
    sent_id = getattr(sent_msg, "id", None)
    await asyncio.to_thread(mark_case_rescued, db_path, msg_id, chat_id)
    _LAST_RESCUE_TIME[chat_id] = datetime.datetime.now(datetime.timezone.utc)

    # Сохраняем в bot_sent_messages
    try:
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            bot_cols = _get_table_columns(conn, "bot_sent_messages")
            if "reply_to_msg_id" in bot_cols and "sent_at" in bot_cols:
                now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("""
                    INSERT INTO bot_sent_messages (chat_id, reply_to_msg_id, message, sent_at)
                    VALUES (?, ?, ?, ?)
                """, (chat_id, msg_id, rescue_text[:500], now_iso))
            elif sent_id:
                conn.execute("""
                    INSERT OR REPLACE INTO bot_sent_messages (msg_id, chat_id)
                    VALUES (?, ?)
                """, (sent_id, chat_id))
            conn.commit()
    except Exception as log_err:
        logger.warning("Failed to log bot_sent_messages for rescue %s: %s", msg_id, log_err)

    logger.info("✅ Успешно доставлен Clinical Rescue для msg_id=%s (sent_id=%s)", msg_id, sent_id)
    return True


async def run_clinical_rescue_cycle(bot_client, db_path: str, chat_id: int, bot_id: int = 0) -> int:
    """
    Один цикл проверки и спасения.
    Возвращает число спасенных кейсов (0 или 1).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    last_rescue = _LAST_RESCUE_TIME.get(chat_id)
    if last_rescue and (now - last_rescue).total_seconds() < RESCUE_COOLDOWN_SECONDS:
        logger.debug("Clinical Rescue: Cooldown active (%d sec left)",
                     int(RESCUE_COOLDOWN_SECONDS - (now - last_rescue).total_seconds()))
        return 0

    daily_count = await asyncio.to_thread(get_rescues_count_24h, db_path, chat_id)
    if daily_count >= MAX_RESCUES_PER_DAY:
        logger.debug("Clinical Rescue: Daily ceiling reached (%d/%d)", daily_count, MAX_RESCUES_PER_DAY)
        return 0

    # Activity Guard: проверяем живую активность в чате за 5 минут
    recent_activity = await asyncio.to_thread(check_recent_chat_activity, db_path, chat_id, 5)
    if recent_activity >= 3:
        logger.info("⏳ Clinical Rescue Activity Guard: в чате идет беседа (%d сообщ за 5 мин). Откладываем rescue.",
                    recent_activity)
        return 0

    candidates = await asyncio.to_thread(find_unanswered_clinical_candidates, db_path, chat_id, bot_id=bot_id)
    if not candidates:
        return 0

    # Берем старейший кандидат
    target = candidates[0]
    success = await execute_rescue_for_candidate(bot_client, target, db_path)
    return 1 if success else 0


async def clinical_rescue_daemon(
    bot_client,
    db_path: Optional[str] = None,
    chat_id: Optional[int] = None,
    check_interval_sec: int = CHECK_INTERVAL_SECONDS
):
    """
    Фоновый демон спасения неотвеченных кейсов.
    Работает в цикле на протяжении всей жизни бота.
    """
    import config
    active_db = db_path or getattr(config, "DB_PATH", "stomat_bot.db")
    active_chat = chat_id or getattr(config, "SOURCE_CHAT_ID", None)
    bot_id = getattr(config, "BOT_ID", 0)

    if not active_chat:
        logger.warning("Clinical Rescue Daemon: SOURCE_CHAT_ID is not configured. Exiting daemon.")
        return

    logger.info("🚑 Clinical Rescue Daemon запущен (chat=%s, interval=%ds)", active_chat, check_interval_sec)

    while True:
        try:
            await run_clinical_rescue_cycle(bot_client, active_db, active_chat, bot_id=bot_id)
        except asyncio.CancelledError:
            logger.info("Clinical Rescue Daemon stopped.")
            break
        except Exception as e:
            logger.exception("Unexpected error in Clinical Rescue Daemon: %s", e)

        await asyncio.sleep(check_interval_sec)
