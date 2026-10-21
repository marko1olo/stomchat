# -*- coding: utf-8 -*-
"""
tests/test_clinical_rescue.py — Комплексные тесты для clinical_rescue.py.
"""

import asyncio
import datetime
import os
import sqlite3
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import clinical_rescue as cr


def run_tests():
    print("\n[1] Эвристический фильтр кандидатов на спасение (is_clinical_rescue_candidate)")
    check("клинический вопрос распознан",
          cr.is_clinical_rescue_candidate("Коллеги, подскажите протокол при обломке в средней трети?", False))
    check("сообщение со снимком распознано",
          cr.is_clinical_rescue_candidate("Снимок 46 зуба", True))
    check("смайл со снимком распознан",
          cr.is_clinical_rescue_candidate("🦷", True))
    check("обычное приветствие отклонено",
          not cr.is_clinical_rescue_candidate("Привет всем, хорошего дня!", False))
    check("команда бота отклонена",
          not cr.is_clinical_rescue_candidate("/torque soco", False))
    check("слишком короткий текст отклонен",
          not cr.is_clinical_rescue_candidate("зуб болит?", False))

    print("\n[2] Запросы к SQLite: поиск повисших кейсов без ответов")
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test_rescue.db")

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                sender_id INTEGER,
                sender_name TEXT,
                message TEXT,
                timestamp TEXT,
                reply_to_msg_id INTEGER,
                has_media INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE bot_sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                reply_to_msg_id INTEGER,
                message TEXT,
                sent_at TEXT
            )
        """)
        conn.commit()

    now = datetime.datetime.now(datetime.timezone.utc)
    t_5m = (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    t_25m = (now - datetime.timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S")
    t_40m = (now - datetime.timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
    t_120m = (now - datetime.timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S")

    chat_id = -1001999888
    bot_id = 777777

    with sqlite3.connect(db_path) as conn:
        # msg 1: слишком свежий (5 мин)
        conn.execute("INSERT INTO messages VALUES (1, ?, 10, 'Dr A', 'Как пройти обломок в 36 зубе?', ?, NULL, 0)",
                     (chat_id, t_5m))
        # msg 2: идеальный кандидат (25 мин, 0 ответов)
        conn.execute("INSERT INTO messages VALUES (2, ?, 11, 'Dr B', 'Снимок зуба 46, периодонтит, что думаете?', ?, NULL, 1)",
                     (chat_id, t_25m))
        # msg 3: 40 мин, но коллега ответил msg 4
        conn.execute("INSERT INTO messages VALUES (3, ?, 12, 'Dr C', 'Какая доза амоксиклава при абсцессе?', ?, NULL, 0)",
                     (chat_id, t_40m))
        conn.execute("INSERT INTO messages VALUES (4, ?, 13, 'Dr D', '875/125 2 раза в день', ?, 3, 0)",
                     (chat_id, t_25m))
        # msg 5: слишком старый (120 мин)
        conn.execute("INSERT INTO messages VALUES (5, ?, 14, 'Dr E', 'Апекс зуба 21 широкий', ?, NULL, 0)",
                     (chat_id, t_120m))
        # msg 6: 25 мин, но бот уже ответил
        conn.execute("INSERT INTO messages VALUES (6, ?, 15, 'Dr F', 'Пульпит 15 зуба анестезия не берет', ?, NULL, 0)",
                     (chat_id, t_25m))
        conn.execute("INSERT INTO bot_sent_messages VALUES (1, ?, 6, 'Буферизация 8.4% содой', ?)",
                     (chat_id, t_25m))
        conn.commit()

    candidates = cr.find_unanswered_clinical_candidates(db_path, chat_id, bot_id=bot_id)
    check("найден ровно 1 повисший кейс", len(candidates) == 1)
    if candidates:
        check("выбран корректный msg_id 2", candidates[0]["id"] == 2)
        check("автор корректен", candidates[0]["sender_name"] == "Dr B")

    print("\n[3] Персистентность и суточные лимиты")
    cr.ensure_rescue_table(db_path)
    check("число спасений за 24ч изначально 0", cr.get_rescues_count_24h(db_path, chat_id) == 0)

    cr.mark_case_rescued(db_path, 2, chat_id)
    check("число спасений за 24ч стало 1", cr.get_rescues_count_24h(db_path, chat_id) == 1)

    # Повторный поиск не должен вернуть msg 2
    candidates_after = cr.find_unanswered_clinical_candidates(db_path, chat_id, bot_id=bot_id)
    check("спасенный кейс больше не возвращается", len(candidates_after) == 0)

    print("\n[4] Activity Guard: защита от вмешательства в живой диалог")
    act_0 = cr.check_recent_chat_activity(db_path, chat_id, minutes=5)
    check("активность за 5 мин отражает добавленные сообщения", act_0 >= 1)

    print("\n[5] Безопасная отправка и фоллбек Ghost Reply")
    fake_bot = MagicMock()
    # Симулируем исключение на reply_to (сообщение удалено)
    fake_bot.send_message = AsyncMock(side_effect=[
        Exception("Message to reply not found (deleted)"),
        MagicMock(id=999)
    ])

    cand = {"id": 100, "chat_id": chat_id, "sender_name": "ТестВрач", "message": "Снимок 47 зуба", "has_media": True}

    async def test_exec():
        # Подменяем генерацию текста
        orig_gen = cr.generate_clinical_rescue_text
        cr.generate_clinical_rescue_text = AsyncMock(return_value="👨‍⚕️ Коллеги сейчас на приёме у кресла... Совет: ...")
        res = await cr.execute_rescue_for_candidate(fake_bot, cand, db_path)
        cr.generate_clinical_rescue_text = orig_gen
        return res

    success = asyncio.run(test_exec())
    check("спасение успешно отправлено через фоллбек без reply_to", success is True)
    check("было сделано 2 попытки отправки", fake_bot.send_message.call_count == 2)

    print("\n[6] Реальная production-схема БД (messages с msg_id/text/date, bot_sent_messages с msg_id/chat_id)")
    prod_tmp_dir = tempfile.mkdtemp()
    prod_db_path = os.path.join(prod_tmp_dir, "test_prod_rescue.db")

    with sqlite3.connect(prod_db_path) as conn:
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id INTEGER,
                reply_to_msg_id INTEGER,
                sender_id INTEGER,
                sender_name TEXT,
                sender_username TEXT,
                text TEXT,
                date TIMESTAMP,
                has_media BOOLEAN,
                media_type TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE bot_sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id INTEGER,
                chat_id INTEGER
            )
        """)
        # msg_id=201: 40 мин назад, клинический вопрос, без ответа
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media) VALUES (201, NULL, 50, 'Доктор Иванов', 'Коллеги, какой протокол при переломе инструмента SOCO?', ?, 0)",
                     (t_40m,))
        # msg_id=202: 25 мин назад, отвечен доктором 52
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media) VALUES (202, NULL, 51, 'Доктор Петров', 'Как обойти ступеньку в МВ2?', ?, 0)",
                     (t_25m,))
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media) VALUES (203, 202, 52, 'Доктор Сидоров', 'Попробуйте C-Pilot 08 с изгибом', ?, 0)",
                     (t_5m,))
        conn.commit()

    prod_candidates = cr.find_unanswered_clinical_candidates(prod_db_path, chat_id, bot_id=bot_id)
    check("production-схема: найден ровно 1 повисший кейс", len(prod_candidates) == 1)
    if prod_candidates:
        check("production-схема: выбран корректный msg_id 201", prod_candidates[0]["id"] == 201)
        check("production-схема: текст сохранен", "SOCO" in prod_candidates[0]["message"])

    prod_act = cr.check_recent_chat_activity(prod_db_path, chat_id, minutes=10)
    check("production-схема: activity guard видит свежие сообщения", prod_act >= 1)

    # Тест записи спасения на production-схеме
    fake_bot2 = MagicMock()
    fake_bot2.send_message = AsyncMock(return_value=MagicMock(id=9999))
    cand_prod = prod_candidates[0] if prod_candidates else {"id": 201, "chat_id": chat_id, "sender_name": "Доктор", "message": "вопрос", "has_media": False}

    async def test_prod_exec():
        orig_gen = cr.generate_clinical_rescue_text
        cr.generate_clinical_rescue_text = AsyncMock(return_value="👨‍⚕️ EBM совет...")
        res = await cr.execute_rescue_for_candidate(fake_bot2, cand_prod, prod_db_path)
        cr.generate_clinical_rescue_text = orig_gen
        return res

    prod_res = asyncio.run(test_prod_exec())
    check("production-схема: execute_rescue успешен", prod_res is True)

    with sqlite3.connect(prod_db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT msg_id FROM bot_sent_messages WHERE msg_id = 9999")
        check("production-схема: bot_sent_messages записан", cur.fetchone() is not None)

    try:
        os.remove(prod_db_path)
        os.rmdir(prod_tmp_dir)
    except Exception:
        pass

    # Очистка
    try:
        os.remove(db_path)
        os.rmdir(tmp_dir)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
