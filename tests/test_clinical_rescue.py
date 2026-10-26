# -*- coding: utf-8 -*-
"""
tests/test_clinical_rescue.py — Комплексные тесты для clinical_rescue.py.
Включает тесты на:
- Эвристический фильтр кандидатов и ликвидацию слепого спасения (has_media, stop-words)
- Дедупликацию альбомов и серий (Turn / Album Deduplication)
- Escape Hatch для не-стоматологических изображений (NOT_CLINICAL_CASE)
- Динамический BOT_ID
- Персистентность и Activity Guard
"""

import asyncio
import datetime
import os
import sqlite3
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

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
    t_2m = (now - datetime.timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    t_5m = (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    t_25m = (now - datetime.timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
    t_40m = (now - datetime.timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
    t_120m = (now - datetime.timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S")

    chat_id = -1001999888
    bot_id = 777777

    with sqlite3.connect(db_path) as conn:
        # msg 1: слишком свежий (2 мин)
        conn.execute("INSERT INTO messages VALUES (1, ?, 10, 'Dr A', 'Как пройти обломок в 36 зубе?', ?, NULL, 0)",
                     (chat_id, t_2m))
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
    fake_bot.send_message = AsyncMock(side_effect=[
        Exception("Message to reply not found (deleted)"),
        MagicMock(id=999)
    ])

    cand = {"id": 100, "chat_id": chat_id, "sender_name": "ТестВрач", "message": "Снимок 47 зуба", "has_media": True}

    async def test_exec():
        orig_gen = cr.generate_clinical_rescue_text
        cr.generate_clinical_rescue_text = AsyncMock(return_value="Совет: ...")
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
                media_type TEXT,
                media_description TEXT
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

    fake_bot2 = MagicMock()
    fake_bot2.send_message = AsyncMock(return_value=MagicMock(id=9999))
    cand_prod = prod_candidates[0] if prod_candidates else {"id": 201, "chat_id": chat_id, "sender_name": "Доктор", "message": "вопрос", "has_media": False}

    async def test_prod_exec():
        orig_gen = cr.generate_clinical_rescue_text
        cr.generate_clinical_rescue_text = AsyncMock(return_value="EBM совет...")
        res = await cr.execute_rescue_for_candidate(fake_bot2, cand_prod, prod_db_path)
        cr.generate_clinical_rescue_text = orig_gen
        return res

    prod_res = asyncio.run(test_prod_exec())
    check("production-схема: execute_rescue успешен", prod_res is True)

    with sqlite3.connect(prod_db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT msg_id FROM bot_sent_messages WHERE msg_id = 9999")
        check("production-схема: bot_sent_messages записан", cur.fetchone() is not None)

    print("\n[7] Ликвидация слепого спасения и фильтрация не-клинических изображений")
    # Реальный инцидент: скриншот интерфейса бота с текстом рекламы
    check("инцидент: скриншот интерфейса бота отклонен",
          not cr.is_clinical_rescue_candidate("Коллеги! Переходите в бесплатного чат бота...", True,
                                              media_description="На снимке интерфейс цифрового комплекса Docendo Discimus, зубы отсутствуют"))
    check("инцидент: фото 2 из альбома без текста с интерфейсом отклонено",
          not cr.is_clinical_rescue_candidate("", True,
                                              media_description="На снимке интерфейс цифрового комплекса Docendo Discimus, зубы отсутствуют"))
    check("мем со снимком отклонен",
          not cr.is_clinical_rescue_candidate("ахаха", True, media_description="Мем с текстом"))
    check("скриншот приложения отклонен",
          not cr.is_clinical_rescue_candidate("гляньте", True, media_description="Скриншот экрана приложения"))
    check("описание без зубов отклонено",
          not cr.is_clinical_rescue_candidate("", True, media_description="На фотографии зубы отсутствуют"))
    check("описание не является рентгеном отклонено",
          not cr.is_clinical_rescue_candidate("что скажете", True, media_description="Изображение не является рентгеном"))
    check("слепое фото без текста и без описания отклонено",
          not cr.is_clinical_rescue_candidate("", True, media_description=None))
    check("слепое фото с неклиническим текстом отклонено",
          not cr.is_clinical_rescue_candidate("доброе утро коллеги", True, media_description=None))
    check("фото без текста с подтвержденным клиническим описанием разрешено",
          cr.is_clinical_rescue_candidate("", True, media_description="Прицельный рентгеновский снимок зуба 36, очаг в области верхушки корня"))
    check("клинический текст при неклиническом описании отклонен",
          not cr.is_clinical_rescue_candidate("Снимок 46 зуба", True, media_description="Скриншот экрана меню"))

    print("\n[8] Дедупликация альбомов и серий (Turn / Album Deduplication)")
    album_tmp_dir = tempfile.mkdtemp()
    album_db_path = os.path.join(album_tmp_dir, "test_album_dedup.db")
    t_24m = (now - datetime.timedelta(minutes=34)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(album_db_path) as conn:
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id INTEGER,
                reply_to_msg_id INTEGER,
                sender_id INTEGER,
                sender_name TEXT,
                text TEXT,
                date TIMESTAMP,
                has_media BOOLEAN,
                media_description TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE bot_sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id INTEGER,
                chat_id INTEGER
            )
        """)
        # Инцидент: Доктор 100 присылает msg 301 и msg 302 (альбом) в 12:00 (25 мин назад)
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media) VALUES (301, NULL, 100, 'Сергей', 'Коллеги! Переходите...', ?, 1)", (t_25m,))
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media, media_description) VALUES (302, NULL, 100, 'Сергей', '', ?, 1, 'Снимок зуба 46')", (t_25m,))
        # В 12:01 (24 мин назад) бот ответил на msg 301 сообщением msg 303 (reply_to=301)
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media) VALUES (303, 301, ?, 'Бот', 'Приветствую...', ?, 0)", (bot_id, t_24m,))
        conn.execute("INSERT INTO bot_sent_messages (msg_id, chat_id) VALUES (303, ?)", (chat_id,))

        # Доктор 200 присылает сообщение 401 (одиночный повисший клинический кейс 25 мин назад)
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media) VALUES (401, NULL, 200, 'Dr Real', 'Снимок 36 зуба, периодонтит, протокол?', ?, 1)", (t_25m,))
        conn.commit()

    album_candidates = cr.find_unanswered_clinical_candidates(album_db_path, chat_id, bot_id=bot_id)
    check("альбом/серия: найдено ровно 1 неотвеченное сообщение", len(album_candidates) == 1)
    if album_candidates:
        check("альбом/серия: выбран реальный кейс 401", album_candidates[0]["id"] == 401)
        check("альбом/серия: второе фото альбома 302 отброшено из-за ответа бота на 301",
              all(c["id"] != 302 for c in album_candidates))

    # Проверка дедупликации внутри выборки, если автор прислал 2 снимка без ответа
    with sqlite3.connect(album_db_path) as conn:
        # Доктор 300 присылает 2 фото без текста с клиническим описанием (один альбом)
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media, media_description) VALUES (501, NULL, 300, 'Dr Album', '', ?, 1, 'Снимок 14 зуба, апекс')", (t_25m,))
        conn.execute("INSERT INTO messages (msg_id, reply_to_msg_id, sender_id, sender_name, text, date, has_media, media_description) VALUES (502, NULL, 300, 'Dr Album', '', ?, 1, 'Снимок 14 зуба, апекс')", (t_25m,))
        conn.commit()

    album_multi_cands = cr.find_unanswered_clinical_candidates(album_db_path, chat_id, bot_id=bot_id)
    author_300_cands = [c for c in album_multi_cands if c["sender_id"] == 300]
    check("внутрицикловая дедупликация: из альбома автора взят ровно 1 кандидат", len(author_300_cands) == 1)

    # Проверка mark_case_and_album_rescued
    cr.mark_case_and_album_rescued(album_db_path, 501, chat_id, sender_id=300, timestamp=t_25m)
    with sqlite3.connect(album_db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT msg_id FROM clinical_rescues WHERE chat_id = ?", (chat_id,))
        rescued_ids = {r[0] for r in cur.fetchall()}
        check("mark_case_and_album_rescued пометил оба кадра альбома (501 и 502)",
              501 in rescued_ids and 502 in rescued_ids)

    print("\n[9] Escape Hatch для не-стоматологических изображений (NOT_CLINICAL_CASE)")
    # 1. Ответ модели с маркером NOT_CLINICAL_CASE
    fake_esc_cand = {
        "id": 601,
        "chat_id": chat_id,
        "sender_id": 9999,
        "sender_name": "ТестВрач",
        "message": "Что скажете?",
        "has_media": True,
        "timestamp": t_25m
    }

    async def test_escape_hatch():
        with patch("blocking_tools.generate_gemini_text_async") as mock_gemini:
            mock_resp = MagicMock()
            mock_resp.text = "NOT_CLINICAL_CASE"
            mock_gemini.return_value = (mock_resp, None)
            res = await cr.generate_clinical_rescue_text(fake_esc_cand, album_db_path)
            return res

    esc_res = asyncio.run(test_escape_hatch())
    check("escape hatch: generate_clinical_rescue_text возвращает None", esc_res is None)
    with sqlite3.connect(album_db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM clinical_rescues WHERE msg_id = 601")
        check("escape hatch: msg_id 601 помечен в clinical_rescues навсегда", cur.fetchone() is not None)

    # 2. Не-клиническое описание снимка подавляет генерацию без обращения к Gemini
    fake_desc_cand = {
        "id": 602,
        "chat_id": chat_id,
        "sender_id": 9998,
        "sender_name": "ТестВрач2",
        "message": "Гляньте",
        "has_media": True,
        "media_description": "На снимке интерфейс цифрового комплекса Docendo Discimus, зубы отсутствуют",
        "timestamp": t_25m
    }

    async def test_desc_suppress():
        with patch("blocking_tools.generate_gemini_text_async") as mock_gemini:
            res = await cr.generate_clinical_rescue_text(fake_desc_cand, album_db_path)
            # mock_gemini не должен быть вызван
            return res, mock_gemini.call_count

    desc_res, gemini_calls = asyncio.run(test_desc_suppress())
    check("неклиническое описание: генерация возвращает None", desc_res is None)
    check("неклиническое описание: Gemini Vision не вызывался впустую", gemini_calls == 0)

    # 3. execute_rescue_for_candidate не отправляет ничего в чат
    fake_bot_esc = MagicMock()
    fake_bot_esc.send_message = AsyncMock()

    async def test_no_send():
        return await cr.execute_rescue_for_candidate(fake_bot_esc, fake_desc_cand, album_db_path)

    exec_success = asyncio.run(test_no_send())
    check("execute_rescue_for_candidate возвращает False для неклинического кейса", exec_success is False)
    check("execute_rescue_for_candidate не вызвал send_message в чат", fake_bot_esc.send_message.call_count == 0)

    print("\n[10] Динамический BOT_ID")
    # Тест извлечения из токена бота
    orig_token = os.environ.get("TG_BOT_TOKEN")
    try:
        os.environ["TG_BOT_TOKEN"] = "7123456789:AAHxyz_sample_token"
        bid1 = cr.resolve_bot_id()
        check("bot_id извлечен из BOT_TOKEN", bid1 == 7123456789)

        # Тест извлечения из bot_client.me
        fake_client = MagicMock()
        fake_client.me = MagicMock(id=555666777)
        os.environ["TG_BOT_TOKEN"] = ""
        bid2 = cr.resolve_bot_id(bot_client=fake_client)
        check("bot_id извлечен из bot_client.me", bid2 == 555666777)
    finally:
        if orig_token is not None:
            os.environ["TG_BOT_TOKEN"] = orig_token
        else:
            os.environ.pop("TG_BOT_TOKEN", None)

    print("\n[11] Red Team верификация: альбомный инцидент, author group cooldown и ложные подстроки")
    # 1. Точный текст описания зрения из инцидента с доктором Елисеевым
    eliseev_desc = (
        "На представленных изображениях зафиксированы скриншоты интерфейса стоматологического Telegram-бота "
        "«Docendo Discimus» («Учимся вместе»). На первом снимке отображено стартовое меню бота с перечнем основных "
        "клинических и административных разделов: расчет доз анестезии, разбор рентгенонимков, генерация медицинских карт. "
        "Анатомические структуры, зубы, патологические процессы или рентгенограммы на изображениях отсутствуют."
    )
    check("инцидент Елисеева: скриншоты интерфейса бота жестко отклонены",
          not cr.is_clinical_rescue_candidate("", has_media=True, media_description=eliseev_desc))

    # 2. Ложные срабатывания подстрок ('кт' внутри 'доктора', 'кто', 'проект')
    fake_sub_desc = "Фотография доктора на конференции, проект обсуждается в кулуарах"
    check("отсутствие ложного срабатывания на подстроку 'кт' в 'доктор/проект'",
          not cr.is_clinical_rescue_candidate("", has_media=True, media_description=fake_sub_desc))

    # 3. Настоящий клинический снимок с зубами
    real_dental_desc = "Прицельная рентгенограмма зуба 4.6. Деструкция костной ткани в области апекса дистального корня."
    check("настоящий снимок 4.6 зуба успешно одобрен",
          cr.is_clinical_rescue_candidate("", has_media=True, media_description=real_dental_desc))

    # 4. Проверка Author Group Cooldown
    import assistant
    test_author_id = 99887711
    assistant.record_author_group_reply(test_author_id)
    check("author group cooldown активен после ответа",
          assistant.is_author_group_cooldown_active(test_author_id))

    # Создаем временную БД для проверки группировки альбомов и отсева по author cooldown
    inc_tmp_dir = tempfile.mkdtemp()
    inc_db_path = os.path.join(inc_tmp_dir, "test_inc.db")
    with sqlite3.connect(inc_db_path) as conn:
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                msg_id INTEGER,
                sender_id INTEGER,
                sender_name TEXT,
                text TEXT,
                date TEXT,
                reply_to_msg_id INTEGER,
                has_media INTEGER DEFAULT 0,
                media_description TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE bot_sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id INTEGER,
                chat_id INTEGER
            )
        """)
        t_now = datetime.datetime.now(datetime.timezone.utc)
        t_album = (t_now - datetime.timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")

        # Кандидат 1 автора с кулдауном (должен быть отброшен)
        conn.execute("INSERT INTO messages (id, msg_id, sender_id, sender_name, text, date, has_media, media_description) "
                     "VALUES (10, 10, ?, 'Dr CD', 'Зуб 3.6 снимок', ?, 1, ?)",
                     (test_author_id, t_album, real_dental_desc))

        # Альбом из двух кадров нового автора 8888 (кадр 20 и кадр 21 с разницей 1 секунда)
        conn.execute("INSERT INTO messages (id, msg_id, sender_id, sender_name, text, date, has_media, media_description) "
                     "VALUES (20, 20, 8888, 'Dr Album', 'Коллеги, снимок 4.7, что делать с перфорацией?', ?, 1, ?)",
                     (t_album, real_dental_desc))
        conn.execute("INSERT INTO messages (id, msg_id, sender_id, sender_name, text, date, has_media, media_description) "
                     "VALUES (21, 21, 8888, 'Dr Album', '', ?, 1, ?)",
                     (t_album, real_dental_desc))
        conn.commit()

    candidates_found = cr.find_unanswered_clinical_candidates(inc_db_path, chat_id=-10012345, bot_id=1111)
    check("автор под author group cooldown отсеян",
          not any(c["sender_id"] == test_author_id for c in candidates_found))
    check("альбом из 2 кадров сгруппирован в 1 кандидата",
          len([c for c in candidates_found if c["sender_id"] == 8888]) == 1)
    cand_album = next(c for c in candidates_found if c["sender_id"] == 8888)
    check("album_peer_ids содержит второй кадр альбома 21",
          21 in cand_album.get("album_peer_ids", []))

    # Очистка
    try:
        os.remove(inc_db_path)
        os.rmdir(inc_tmp_dir)
    except Exception:
        pass

    # Очистка
    try:
        os.remove(prod_db_path)
        os.rmdir(prod_tmp_dir)
        os.remove(db_path)
        os.rmdir(tmp_dir)
        os.remove(album_db_path)
        os.rmdir(album_tmp_dir)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
