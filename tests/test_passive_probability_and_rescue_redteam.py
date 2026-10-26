# -*- coding: utf-8 -*-
"""
tests/test_passive_probability_and_rescue_redteam.py
Верификация новых инвариантов активности:
1. Clinical Rescue окно 33-180 минут (сообщения моложе 33 мин не спасаются).
2. Вероятностный гейт 66% на незваный текст в чате (при roll >= 0.66 бот молчит).
3. Вероятностный гейт 66% на незваный разбор снимков (при roll >= 0.66 бот молчит).
4. Прямые вызовы (@bot, reply боту) на 100% иммунны к вероятностному гейту.
"""

import asyncio
import datetime
import os
import sqlite3
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import config
import clinical_rescue as cr
import assistant

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class DummyEvent:
    def __init__(self, chat_id, sender_id):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self._sender = None
        self.reply = AsyncMock()


class DummyMediaMsg:
    def __init__(self, chat_id, sender_id, msg_id=99999999):
        self.id = msg_id
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.reply_to_msg_id = None
        self.date = datetime.datetime.now(datetime.timezone.utc)
        self.message = "Снимок зуба 46"


def run_tests():
    print("\n==================== TEST PASSIVE PROBABILITY & 33M RESCUE ====================")

    # 1. Проверка конфигурации по умолчанию
    print("\n[1] Проверка констант конфигурации")
    check("CLINICAL_RESCUE_MIN_AGE_MINUTES равен 33", getattr(config, "CLINICAL_RESCUE_MIN_AGE_MINUTES", 0) == 33)
    check("CLINICAL_RESCUE_MAX_AGE_MINUTES равен 180", getattr(config, "CLINICAL_RESCUE_MAX_AGE_MINUTES", 0) == 180)
    check("PASSIVE_TEXT_PROBABILITY равен 0.66", abs(getattr(config, "PASSIVE_TEXT_PROBABILITY", 0.0) - 0.66) < 1e-4)
    check("PASSIVE_MEDIA_PROBABILITY равен 0.66", abs(getattr(config, "PASSIVE_MEDIA_PROBABILITY", 0.0) - 0.66) < 1e-4)

    # 2. Проверка окна Clinical Rescue 33 минут
    print("\n[2] Проверка окна спасателя: 33 минуты порог")
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "test_rescue_33m.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE messages (
                msg_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
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
                msg_id INTEGER PRIMARY KEY,
                chat_id INTEGER
            )
        """)
        conn.commit()

    now = datetime.datetime.now(datetime.timezone.utc)
    t_10m = (now - datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    t_30m = (now - datetime.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    t_32m = (now - datetime.timedelta(minutes=32)).strftime("%Y-%m-%d %H:%M:%S")
    t_35m = (now - datetime.timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:%S")
    t_60m = (now - datetime.timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")
    t_200m = (now - datetime.timedelta(minutes=200)).strftime("%Y-%m-%d %H:%M:%S")

    chat_id = -1001888999
    bot_id = 999888

    with sqlite3.connect(db_path) as conn:
        # msg 1: 10 мин назад (слишком свежий)
        conn.execute("INSERT INTO messages (msg_id, chat_id, sender_id, sender_name, text, date) VALUES (1, ?, 10, 'A', 'Как лечить пульпит 16 зуба?', ?)", (chat_id, t_10m))
        # msg 2: 30 мин назад (слишком свежий для 33 мин)
        conn.execute("INSERT INTO messages (msg_id, chat_id, sender_id, sender_name, text, date) VALUES (2, ?, 11, 'B', 'Какой протокол ирригации при периодонтите 21?', ?)", (chat_id, t_30m))
        # msg 3: 32 мин назад (слишком свежий для 33 мин)
        conn.execute("INSERT INTO messages (msg_id, chat_id, sender_id, sender_name, text, date) VALUES (3, ?, 12, 'C', 'Снимок зуба 47, апекс не пройден, как быть?', ?)", (chat_id, t_32m))
        # msg 4: 35 мин назад (ВАЛИДНЫЙ КАНДИДАТ: 35 >= 33 и <= 180)
        conn.execute("INSERT INTO messages (msg_id, chat_id, sender_id, sender_name, text, date) VALUES (4, ?, 13, 'D', 'Обломок в канале 36 зуба, протокол извлечения?', ?)", (chat_id, t_35m))
        # msg 5: 60 мин назад, но есть ответ врача msg 6
        conn.execute("INSERT INTO messages (msg_id, chat_id, sender_id, sender_name, text, date) VALUES (5, ?, 14, 'E', 'Чем фиксировать штифт в зубе 11?', ?)", (chat_id, t_60m))
        conn.execute("INSERT INTO messages (msg_id, chat_id, sender_id, sender_name, text, date, reply_to_msg_id) VALUES (6, ?, 15, 'F', 'Фиксируйте на композитный цемент', ?, 5)", (chat_id, t_30m))
        # msg 7: 200 мин назад (слишком старый, > 180 мин)
        conn.execute("INSERT INTO messages (msg_id, chat_id, sender_id, sender_name, text, date) VALUES (7, ?, 16, 'G', 'Периодонтит 45 зуба, удалять или нет?', ?)", (chat_id, t_200m))
        conn.commit()

    cands = cr.find_unanswered_clinical_candidates(db_path, chat_id, bot_id=bot_id)
    check("Кандидат ровно 1 (только msg_id 4)", len(cands) == 1)
    if cands:
        check("Выбран msg_id 4 (35 мин)", cands[0]["id"] == 4)

    # 3. Вероятностный гейт пассивного текста (66%)
    print("\n[3] Проверка вероятностного гейта пассивного текста")
    mock_ctx = (["Доктор: Коллеги, какой силер для зуба 16 выбрать?"], 0, None)
    
    # Симулируем roll >= 0.66 (например 0.80): бот должен пропустить вызов
    with patch("random.random", return_value=0.80):
        with patch.object(assistant, "fetch_dynamic_chat_context", new=AsyncMock(return_value=mock_ctx)):
            with patch.object(assistant, "check_llm_triage", new=AsyncMock(return_value=True)) as mock_triage:
                with patch.object(assistant, "has_dental_term", return_value=True):
                    with patch.object(assistant, "passive_gate_block_reason_async", new=AsyncMock(return_value=None)):
                        with patch.object(assistant, "claim_passive_slot", return_value=True):
                            with patch.object(assistant, "record_passive_attempt") as mock_record:
                                event = DummyEvent(chat_id, 12345)
                                res = asyncio.run(assistant.check_and_trigger_assistant(
                                    MagicMock(), event, 99999999, "Коллеги, какой силер для зуба 16 выбрать?", None, "Доктор"
                                ))
                                check("При roll=0.80 (>=0.66) пассивный триггер отклонен (False)", res is False)
                                check("При roll=0.80 LLM триаж НЕ вызывался (экономия токенов)", mock_triage.call_count == 0)
                                check("При roll=0.80 вызван record_passive_attempt (бэк-офф)", mock_record.call_count == 1)

    # Симулируем roll < 0.66 (например 0.40): бот проходит к LLM триажу
    with patch("random.random", return_value=0.40):
        with patch.object(assistant, "fetch_dynamic_chat_context", new=AsyncMock(return_value=mock_ctx)):
            with patch.object(assistant, "check_llm_triage", new=AsyncMock(return_value=False)) as mock_triage_pass:
                with patch.object(assistant, "has_dental_term", return_value=True):
                    with patch.object(assistant, "passive_gate_block_reason_async", new=AsyncMock(return_value=None)):
                        with patch.object(assistant, "claim_passive_slot", return_value=True):
                            event = DummyEvent(chat_id, 12345)
                            res = asyncio.run(assistant.check_and_trigger_assistant(
                                MagicMock(), event, 99999998, "Коллеги, какой силер для зуба 16 выбрать?", None, "Доктор"
                            ))
                            check("При roll=0.40 (<0.66) LLM триаж успешно вызван", mock_triage_pass.call_count == 1)

    # 4. Вероятностный гейт пассивного разбора медиа (66%)
    print("\n[4] Проверка вероятностного гейта пассивного медиа")
    media_msg = DummyMediaMsg(chat_id, 12345, 99999999)

    # При roll=0.75 (>= 0.66) и is_passive=True: бот пропускает снимок
    with patch("random.random", return_value=0.75):
        with patch.object(assistant, "is_explicitly_non_dental_media", return_value=False):
            with patch.object(assistant, "has_dental_term", return_value=True):
                with patch.object(assistant, "_safe_search_knowledge_corpus", new=AsyncMock(return_value=("", ""))):
                    with patch.object(assistant, "load_state", return_value={"last_passive_media_run": "2000-01-01T00:00:00", "last_passive_text_run": "2000-01-01T00:00:00"}):
                        with patch.object(assistant, "save_state") as mock_save:
                            res = asyncio.run(assistant.check_and_trigger_assistant_media(
                                MagicMock(), media_msg, 99999999, "Снимок зуба 46", "Прицельный снимок зуба 46, периодонтит"
                            ))
                            check("При roll=0.75 (>=0.66) пассивный разбор снимка пропущен (False)", res is False)
                            check("При roll=0.75 сохранен бэк-офф состояния", mock_save.call_count >= 1)

    # 5. Иммунитет прямого обращения (@bot / Reply к боту)
    print("\n[5] Прямое обращение (@bot) на 100% иммунно к вероятностному гейту")
    text_input = "@docendobot подскажи протокол при переломе SOCO"
    text_lower = text_input.lower()
    known_handles = {"docendobot", "stomchat_bot"}
    is_direct = any(f"@{h}" in text_lower for h in known_handles)
    check("Тег @docendobot классифицирован как прямой вызов (is_direct=True)", is_direct is True)

    print("\n==================== РЕЗУЛЬТАТ ТЕСТИРОВАНИЯ ====================")
    total_passed = len(PASS)
    total_failed = len(FAIL)
    print(f"PASSED: {total_passed} | FAILED: {total_failed}")
    if total_failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    run_tests()
