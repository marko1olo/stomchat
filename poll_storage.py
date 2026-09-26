# -*- coding: utf-8 -*-
"""
Модуль асинхронного хранения данных опросов и статистики врачей (stomat_bot.db).

Архитектура таблиц:
1. daily_polls:
   - id INTEGER PRIMARY KEY (уникальный poll_id из Telegram)
   - chat_id INTEGER NOT NULL
   - case_msg_id INTEGER (ID сообщения с описанием кейса/снимком)
   - poll_msg_id INTEGER (ID сообщения с самим опросом)
   - resolution_msg_id INTEGER (ID сообщения с глубоким клиническим разбором)
   - poll_type TEXT NOT NULL ('quiz', 'regular', 'lifestyle')
   - topic TEXT NOT NULL ('endo', 'ortho', 'surgery', 'therapy', 'materials', 'lifestyle')
   - question TEXT NOT NULL
   - options_json TEXT NOT NULL
   - correct_option_id INTEGER
   - explanation_brief TEXT
   - explanation_deep TEXT
   - created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   - closed_at TIMESTAMP
   - is_closed BOOLEAN DEFAULT 0

2. poll_votes:
   - id INTEGER PRIMARY KEY AUTOINCREMENT
   - poll_id INTEGER NOT NULL
   - user_id INTEGER NOT NULL
   - user_name TEXT
   - selected_option INTEGER NOT NULL
   - is_correct BOOLEAN
   - voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   - UNIQUE(poll_id, user_id) -- один врач = один голос в опросе
"""

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Union

import aiosqlite

logger = logging.getLogger(__name__)

# Допустимые типы опросов и тематики
VALID_POLL_TYPES: Set[str] = {"quiz", "regular", "lifestyle"}
VALID_TOPICS: Set[str] = {"endo", "ortho", "surgery", "therapy", "materials", "lifestyle"}


def _resolve_db_path(db_path: Optional[str] = None) -> str:
    """Возвращает актуальный путь к БД SQLite (из параметра или config.DB_PATH)."""
    if db_path is not None and db_path.strip():
        return db_path.strip()
    try:
        import config
        return getattr(config, "DB_PATH", "stomat_bot.db")
    except Exception:
        return "stomat_bot.db"


_WAL_INITIALIZED = set()


@asynccontextmanager
async def get_db_connection(db_path: Optional[str] = None) -> AsyncIterator[aiosqlite.Connection]:
    """Асинхронный контекстный менеджер подключения к базе SQLite."""
    target_path = _resolve_db_path(db_path)
    async with aiosqlite.connect(target_path, timeout=30.0) as db:
        await db.execute("PRAGMA busy_timeout = 30000")
        if target_path not in _WAL_INITIALIZED:
            try:
                await db.execute("PRAGMA journal_mode = WAL")
                _WAL_INITIALIZED.add(target_path)
            except Exception as e:
                logger.warning("Could not set WAL journal mode for %s: %s", target_path, e)
        db.row_factory = aiosqlite.Row
        yield db


async def init_poll_tables(db_path: Optional[str] = None) -> None:
    """
    Создаёт таблицы daily_polls и poll_votes, а также необходимые индексы.
    Идемпотентно (IF NOT EXISTS).
    """
    async with get_db_connection(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_polls (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                case_msg_id INTEGER,
                poll_msg_id INTEGER,
                resolution_msg_id INTEGER,
                poll_type TEXT NOT NULL,
                topic TEXT NOT NULL,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                correct_option_id INTEGER,
                explanation_brief TEXT,
                explanation_deep TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP,
                is_closed BOOLEAN DEFAULT 0
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS poll_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                selected_option INTEGER NOT NULL,
                is_correct BOOLEAN,
                voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(poll_id, user_id)
            )
            """
        )

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_polls_chat_active ON daily_polls(chat_id, is_closed)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_polls_topic ON daily_polls(topic)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_polls_created_at ON daily_polls(created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_poll_votes_poll_id ON poll_votes(poll_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_poll_votes_user_id ON poll_votes(user_id)"
        )

        await db.commit()
    logger.debug("Таблицы и индексы опросов успешно инициализированы в %s", _resolve_db_path(db_path))


async def save_poll(
    poll_id: Optional[Union[int, Dict[str, Any]]] = None,
    chat_id: Optional[int] = None,
    case_msg_id: Optional[int] = None,
    poll_msg_id: Optional[int] = None,
    resolution_msg_id: Optional[int] = None,
    poll_type: Optional[str] = None,
    topic: Optional[str] = None,
    question: Optional[str] = None,
    options: Optional[Union[List[str], str]] = None,
    options_json: Optional[str] = None,
    correct_option_id: Optional[int] = None,
    explanation_brief: Optional[str] = None,
    explanation_deep: Optional[str] = None,
    is_closed: Optional[bool] = None,
    closed_at: Optional[str] = None,
    created_at: Optional[str] = None,
    db_path: Optional[str] = None,
    **kwargs: Any,
) -> bool:
    """
    Сохраняет опрос в таблицу daily_polls.
    Поддерживает вызов как через именованные аргументы, так и через передачу словаря.
    В случае повторного сохранения обновляет только переданные поля, сохраняя неизменными ранее записанные.
    """
    # Поддержка передачи словаря первым позиционным аргументом
    if isinstance(poll_id, dict):
        data = poll_id
        poll_id = data.get("poll_id") if data.get("poll_id") is not None else data.get("id")
        chat_id = data.get("chat_id", chat_id)
        case_msg_id = data.get("case_msg_id", case_msg_id)
        poll_msg_id = data.get("poll_msg_id", poll_msg_id)
        resolution_msg_id = data.get("resolution_msg_id", resolution_msg_id)
        poll_type = data.get("poll_type", poll_type)
        topic = data.get("topic", topic)
        question = data.get("question", question)
        if "options" in data and options is None:
            options = data["options"]
        if "options_json" in data and options_json is None:
            options_json = data["options_json"]
        correct_option_id = data.get("correct_option_id", correct_option_id)
        explanation_brief = data.get("explanation_brief", explanation_brief)
        explanation_deep = data.get("explanation_deep", explanation_deep)
        is_closed = data.get("is_closed", is_closed)
        closed_at = data.get("closed_at", closed_at)
        created_at = data.get("created_at", created_at)
        if db_path is None and "db_path" in data:
            db_path = data["db_path"]

    if poll_id is None and "id" in kwargs:
        poll_id = kwargs["id"]

    if poll_id is None:
        raise ValueError("Обязательный идентификатор poll_id не указан")
    if chat_id is None:
        raise ValueError("Обязательный идентификатор chat_id не указан")

    # Сериализация вариантов ответа
    final_options_json: Optional[str] = None
    if options_json is not None:
        if isinstance(options_json, str):
            final_options_json = options_json
        else:
            final_options_json = json.dumps(options_json, ensure_ascii=False)
    elif options is not None:
        if isinstance(options, str):
            final_options_json = options
        else:
            final_options_json = json.dumps(options, ensure_ascii=False)

    target_poll_id = int(poll_id)
    target_chat_id = int(chat_id)

    async with get_db_connection(db_path) as db:
        async with db.execute(
            "SELECT id FROM daily_polls WHERE id = ?", (target_poll_id,)
        ) as cursor:
            existing = await cursor.fetchone()

        if existing:
            # Обновление существующего опроса: изменяем только явно переданные поля
            update_fields: List[str] = ["chat_id = ?"]
            params: List[Any] = [target_chat_id]

            if case_msg_id is not None:
                update_fields.append("case_msg_id = ?")
                params.append(case_msg_id)
            if poll_msg_id is not None:
                update_fields.append("poll_msg_id = ?")
                params.append(poll_msg_id)
            if resolution_msg_id is not None:
                update_fields.append("resolution_msg_id = ?")
                params.append(resolution_msg_id)
            if poll_type is not None:
                update_fields.append("poll_type = ?")
                params.append(str(poll_type))
            if topic is not None:
                update_fields.append("topic = ?")
                params.append(str(topic))
            if question is not None:
                update_fields.append("question = ?")
                params.append(str(question))
            if final_options_json is not None:
                update_fields.append("options_json = ?")
                params.append(final_options_json)
            if correct_option_id is not None:
                update_fields.append("correct_option_id = ?")
                params.append(correct_option_id)
            if explanation_brief is not None:
                update_fields.append("explanation_brief = ?")
                params.append(explanation_brief)
            if explanation_deep is not None:
                update_fields.append("explanation_deep = ?")
                params.append(explanation_deep)
            if is_closed is not None:
                update_fields.append("is_closed = ?")
                params.append(1 if is_closed else 0)
                if is_closed:
                    update_fields.append("closed_at = COALESCE(?, closed_at, CURRENT_TIMESTAMP)")
                    params.append(closed_at)
            elif closed_at is not None:
                update_fields.append("closed_at = ?")
                params.append(closed_at)
            if created_at is not None:
                update_fields.append("created_at = ?")
                params.append(created_at)

            params.append(target_poll_id)
            sql = f"UPDATE daily_polls SET {', '.join(update_fields)} WHERE id = ?"
            await db.execute(sql, params)
        else:
            # Создание новой записи опроса
            closed_val = 1 if is_closed else 0
            insert_sql = """
                INSERT INTO daily_polls (
                    id, chat_id, case_msg_id, poll_msg_id, resolution_msg_id,
                    poll_type, topic, question, options_json, correct_option_id,
                    explanation_brief, explanation_deep, is_closed, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            insert_params = (
                target_poll_id,
                target_chat_id,
                case_msg_id,
                poll_msg_id,
                resolution_msg_id,
                str(poll_type or "quiz"),
                str(topic or "therapy"),
                str(question or ""),
                final_options_json or "[]",
                correct_option_id,
                explanation_brief,
                explanation_deep,
                closed_val,
                closed_at,
            )
            await db.execute(insert_sql, insert_params)
            if created_at is not None:
                await db.execute(
                    "UPDATE daily_polls SET created_at = ? WHERE id = ?",
                    (created_at, target_poll_id),
                )

        await db.commit()

    return True


def _format_poll_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    """Форматирует строку из daily_polls в словарь с распакованным options."""
    d = dict(row)
    options_raw = d.get("options_json")
    parsed_options: List[str] = []
    if options_raw:
        try:
            data = json.loads(options_raw)
            if isinstance(data, list):
                parsed_options = [str(x) for x in data]
        except Exception:
            parsed_options = []
    d["options"] = parsed_options
    d["is_closed"] = bool(d.get("is_closed", 0))
    return d


async def get_poll(poll_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Получает опрос по ID из таблицы daily_polls."""
    async with get_db_connection(db_path) as db:
        async with db.execute(
            "SELECT * FROM daily_polls WHERE id = ?", (int(poll_id),)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _format_poll_dict(row)


async def record_vote(
    poll_id: int,
    user_id: int,
    user_name: str,
    selected_option: int,
    is_correct: Optional[bool] = None,
    db_path: Optional[str] = None,
    allow_update: bool = False,
) -> bool:
    """
    Сохраняет голос врача в poll_votes с защитой от повторного голосования (UNIQUE(poll_id, user_id)).
    Если is_correct не передан (None), проверяет по daily_polls правильность выбранного ответа для квиза.
    Возвращает True, если голос успешно записан; False, если врач уже голосовал ранее (дубликат).
    Если allow_update=True, при повторном голосовании обновляет выбранный ответ.
    """
    target_poll_id = int(poll_id)
    target_user_id = int(user_id)
    target_option = int(selected_option)

    async with get_db_connection(db_path) as db:
        # Автоматическое определение правильности ответа для квизов, если is_correct не задан явно
        evaluated_is_correct = is_correct
        if evaluated_is_correct is None:
            async with db.execute(
                "SELECT poll_type, correct_option_id FROM daily_polls WHERE id = ?",
                (target_poll_id,),
            ) as cursor:
                poll_row = await cursor.fetchone()
                if poll_row and poll_row["poll_type"] == "quiz" and poll_row["correct_option_id"] is not None:
                    evaluated_is_correct = target_option == poll_row["correct_option_id"]

        correct_val: Optional[int] = None
        if evaluated_is_correct is not None:
            correct_val = 1 if evaluated_is_correct else 0

        if allow_update:
            cursor = await db.execute(
                """
                INSERT INTO poll_votes (
                    poll_id, user_id, user_name, selected_option, is_correct, voted_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(poll_id, user_id) DO UPDATE SET
                    selected_option = excluded.selected_option,
                    is_correct = excluded.is_correct,
                    user_name = excluded.user_name,
                    voted_at = CURRENT_TIMESTAMP
                """,
                (target_poll_id, target_user_id, user_name, target_option, correct_val),
            )
        else:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO poll_votes (
                    poll_id, user_id, user_name, selected_option, is_correct
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (target_poll_id, target_user_id, user_name, target_option, correct_val),
            )
        await db.commit()
        return cursor.rowcount > 0



async def get_user_vote(poll_id: int, user_id: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Проверяет, отвечал ли конкретный врач на данный опрос, и возвращает запись голоса."""
    async with get_db_connection(db_path) as db:
        async with db.execute(
            "SELECT * FROM poll_votes WHERE poll_id = ? AND user_id = ?",
            (int(poll_id), int(user_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def close_poll(poll_id: int, db_path: Optional[str] = None) -> bool:
    """
    Закрывает опрос, выставляя is_closed = 1 и closed_at = CURRENT_TIMESTAMP.
    Возвращает True, если опрос существовал и статус обновлён, иначе False.
    """
    async with get_db_connection(db_path) as db:
        cursor = await db.execute(
            """
            UPDATE daily_polls
            SET is_closed = 1,
                closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (int(poll_id),),
        )
        await db.commit()
        return cursor.rowcount > 0


async def save_poll_resolution(poll_id: int, resolution_msg_id: int, db_path: Optional[str] = None) -> bool:
    """Фиксирует ID сообщения с глубоким EBM-разбором и закрывает опрос."""
    async with get_db_connection(db_path) as db:
        cursor = await db.execute(
            """
            UPDATE daily_polls
            SET resolution_msg_id = ?,
                is_closed = 1,
                closed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(resolution_msg_id), int(poll_id)),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_active_polls(chat_id: int, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Получает список всех незакрытых (активных) опросов для указанного чата."""
    async with get_db_connection(db_path) as db:
        async with db.execute(
            """
            SELECT * FROM daily_polls
            WHERE chat_id = ? AND is_closed = 0
            ORDER BY created_at DESC
            """,
            (int(chat_id),),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_format_poll_dict(row) for row in rows]


async def get_active_polls_today(chat_id: int, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Получает незакрытые опросы СТРОГО за сегодняшние сутки по МСК (максимум 1 опрос)."""
    async with get_db_connection(db_path) as db:
        async with db.execute(
            """
            SELECT * FROM daily_polls
            WHERE chat_id = ? 
              AND is_closed = 0
              AND resolution_msg_id IS NULL
              AND date(created_at, '+3 hours') = date('now', '+3 hours')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (int(chat_id),),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_format_poll_dict(row) for row in rows]


async def close_stale_polls_silently(chat_id: int, db_path: Optional[str] = None) -> int:
    """Молча закрывает в БД любые старые незакрытые опросы (до сегодняшних суток), не отправляя спам в чат."""
    async with get_db_connection(db_path) as db:
        cursor = await db.execute(
            """
            UPDATE daily_polls
            SET is_closed = 1,
                closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP)
            WHERE chat_id = ?
              AND is_closed = 0
              AND date(created_at, '+3 hours') < date('now', '+3 hours')
            """,
            (int(chat_id),),
        )
        await db.commit()
        return cursor.rowcount


async def close_all_active_polls_silently(chat_id: int, db_path: Optional[str] = None) -> int:
    """Молча закрывает в БД абсолютно все незакрытые опросы (и прошлые, и текущие), предотвращая ночные спонтанные разборы."""
    async with get_db_connection(db_path) as db:
        cursor = await db.execute(
            """
            UPDATE daily_polls
            SET is_closed = 1,
                closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP)
            WHERE chat_id = ?
              AND is_closed = 0
            """,
            (int(chat_id),),
        )
        await db.commit()
        return cursor.rowcount


async def has_poll_today(chat_id: int, db_path: Optional[str] = None) -> bool:
    """
    Проверяет наличие опроса за сегодняшние календарные сутки по московскому времени (UTC+3).
    Исключает ночные рассинхроны между UTC и местным временем.
    """
    async with get_db_connection(db_path) as db:
        async with db.execute(
            """
            SELECT 1 FROM daily_polls 
            WHERE chat_id = ? 
              AND date(created_at, '+3 hours') = date('now', '+3 hours')
            LIMIT 1
            """,
            (int(chat_id),),
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None



async def get_user_quiz_stats(user_id: int, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Возвращает расширенную статистику ответов врача на клинические квизы для профиля /profile:
    - total_answers: всего ответов в квизах
    - correct_answers: количество правильных ответов
    - incorrect_answers: количество неверных ответов
    - accuracy_percent: процент точности (0.0 - 100.0)
    - by_topic: детальная разбивка по клиническим темам (endo, ortho, surgery, therapy, materials, lifestyle)
    - total_votes: общее количество участий во всех опросах (включая regular и lifestyle)
    """
    target_user_id = int(user_id)
    async with get_db_connection(db_path) as db:
        query = """
            SELECT
                v.poll_id,
                v.selected_option,
                v.is_correct,
                p.topic,
                p.poll_type
            FROM poll_votes v
            LEFT JOIN daily_polls p ON v.poll_id = p.id
            WHERE v.user_id = ?
        """
        async with db.execute(query, (target_user_id,)) as cursor:
            rows = await cursor.fetchall()

    total_quiz_answers = 0
    correct_quiz_answers = 0
    total_all_votes = len(rows)
    topic_data: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        poll_type = row["poll_type"] or "quiz"
        is_correct = row["is_correct"]

        # Квизом считается опрос с типом quiz либо голос с явно вычисленным флагом правильности
        is_quiz = (poll_type == "quiz") or (is_correct is not None)
        if not is_quiz:
            continue

        total_quiz_answers += 1
        topic = str(row["topic"]) if row["topic"] else "general"

        if topic not in topic_data:
            topic_data[topic] = {"total": 0, "correct": 0, "accuracy_percent": 0.0}

        topic_data[topic]["total"] += 1

        if is_correct in (1, True):
            correct_quiz_answers += 1
            topic_data[topic]["correct"] += 1

    # Подсчёт точности по темам
    for topic, stats in topic_data.items():
        if stats["total"] > 0:
            stats["accuracy_percent"] = round((stats["correct"] / stats["total"]) * 100.0, 1)
        else:
            stats["accuracy_percent"] = 0.0

    # Общая точность врача
    if total_quiz_answers > 0:
        accuracy_percent = round((correct_quiz_answers / total_quiz_answers) * 100.0, 1)
    else:
        accuracy_percent = 0.0

    return {
        "user_id": target_user_id,
        "total_answers": total_quiz_answers,
        "correct_answers": correct_quiz_answers,
        "incorrect_answers": total_quiz_answers - correct_quiz_answers,
        "accuracy_percent": accuracy_percent,
        "by_topic": topic_data,
        # Синонимы и алиасы для максимального удобства вызывающего кода
        "total": total_quiz_answers,
        "correct": correct_quiz_answers,
        "accuracy": accuracy_percent,
        "topics": topic_data,
        "total_votes": total_all_votes,
    }


async def get_poll_summary_stats(poll_id: int, db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Возвращает сводную статистику по опросу:
    - сколько врачей проголосовало (total_votes)
    - количество правильных ответов (correct_votes)
    - процент правильных ответов (accuracy_percent)
    - распределение голосов по вариантам (votes_by_option)
    """
    target_poll_id = int(poll_id)

    async with get_db_connection(db_path) as db:
        # Информация об опросе
        async with db.execute(
            "SELECT * FROM daily_polls WHERE id = ?", (target_poll_id,)
        ) as cursor:
            poll_row = await cursor.fetchone()

        # Все голоса по опросу
        async with db.execute(
            "SELECT selected_option, is_correct FROM poll_votes WHERE poll_id = ?",
            (target_poll_id,),
        ) as cursor:
            votes = await cursor.fetchall()

    poll_meta = _format_poll_dict(poll_row) if poll_row else None

    total_votes = len(votes)
    correct_votes = sum(1 for v in votes if v["is_correct"] in (1, True))
    incorrect_votes = total_votes - correct_votes

    votes_by_option: Dict[int, int] = {}
    for v in votes:
        opt = int(v["selected_option"])
        votes_by_option[opt] = votes_by_option.get(opt, 0) + 1

    accuracy_percent = 0.0
    if total_votes > 0:
        accuracy_percent = round((correct_votes / total_votes) * 100.0, 1)

    return {
        "poll_id": target_poll_id,
        "total_votes": total_votes,
        "correct_votes": correct_votes,
        "incorrect_votes": incorrect_votes,
        "accuracy_percent": accuracy_percent,
        "votes_by_option": votes_by_option,
        # Алиасы
        "total_voters": total_votes,
        "correct_percent": accuracy_percent,
        "is_closed": poll_meta["is_closed"] if poll_meta else False,
        "poll_type": poll_meta["poll_type"] if poll_meta else None,
        "topic": poll_meta["topic"] if poll_meta else None,
        "question": poll_meta["question"] if poll_meta else None,
    }


async def get_poll_votes(poll_id: int, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Возвращает список всех отдельных голосов по опросу."""
    async with get_db_connection(db_path) as db:
        async with db.execute(
            "SELECT * FROM poll_votes WHERE poll_id = ? ORDER BY voted_at ASC",
            (int(poll_id),),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
