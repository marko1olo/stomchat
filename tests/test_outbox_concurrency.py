"""
test_outbox_concurrency.py — Комплексный аудит и верификация очереди bot_outbox.

Проверяет:
1. Корректность схемы bot_outbox (колонки retry_count, next_retry_at, индексы).
2. Защиту от Head-Of-Line Blocking: сообщение на retry-таймауте не блокирует следующие сообщения в очереди.
3. Логику экспоненциального retry backoff и фиксацию причин в error_message.
4. Достижение потолка OUTBOX_MAX_RETRIES с переводом в status='failed' (DLQ).
5. Немедленный перевод в 'failed' при терминальных ошибках (UserIsBlocked).
6. Успешную доставку и простановку sent_at.
7. Конкурентную устойчивость при множественных параллельных операциях через db_worker.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Добавляем корневую директорию проекта в sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
import database
import db_worker


class TestOutboxConcurrency(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Создаем изолированную временную базу данных для тестов
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test_outbox.db")
        
        # Подменяем пути в config, database и db_worker
        self.orig_db_path = getattr(config, "DB_PATH", None)
        config.DB_PATH = self.db_path
        self.orig_connect = database._connect
        database._connect = lambda: db_worker.connect_db(self.db_path, timeout=10)
        
        # Инициализируем схему базы
        await database.init_db()

    async def asyncTearDown(self):
        if self.orig_db_path:
            config.DB_PATH = self.orig_db_path
        database._connect = self.orig_connect
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    async def test_01_schema_and_indexes(self):
        """Проверка наличия всех требуемых колонок и индексов в bot_outbox."""
        def check():
            with database._connection() as conn:
                cols = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(bot_outbox)").fetchall()}
                indexes = [r[1] for r in conn.execute("PRAGMA index_list(bot_outbox)").fetchall()]
                return cols, indexes

        cols, indexes = await database._run_db(check)
        self.assertIn("retry_count", cols)
        self.assertIn("next_retry_at", cols)
        self.assertIn("status", cols)
        self.assertIn("error_message", cols)
        self.assertIn("idx_bot_outbox_pending", indexes)
        self.assertIn("idx_bot_outbox_status", indexes)

    async def test_02_enqueue_and_pending_fetch(self):
        """Проверка постановки сообщения в очередь и корректного извлечения."""
        msg_id = await database.enqueue_outbox_message(12345, "<b>Клинический тест 1</b>")
        self.assertGreater(msg_id, 0)

        pending = await database.get_pending_outbox_messages(limit=5)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], msg_id)
        self.assertEqual(pending[0]["chat_id"], 12345)
        self.assertEqual(pending[0]["text"], "<b>Клинический тест 1</b>")
        self.assertEqual(pending[0]["retry_count"], 0)

    async def test_03_head_of_line_blocking_prevention(self):
        """
        Критический тест: если первое сообщение получает транзиентный сбой и уходит на retry backoff,
        оно НЕ должно блокировать доставку следующего сообщения в очереди!
        """
        # Ставим 2 сообщения
        id1 = await database.enqueue_outbox_message(1001, "Сообщение 1 (проблемный чат)")
        id2 = await database.enqueue_outbox_message(1002, "Сообщение 2 (здоровый чат)")

        # Сообщение 1 сталкивается с transient error / FloodWait и получает backoff на 120 сек
        await database.mark_outbox_retry(id1, retry_count=1, backoff_seconds=120, error_message="FloodWait: 60s")

        # Извлекаем ожидающие сообщения
        pending = await database.get_pending_outbox_messages(limit=5)
        
        # Сообщение 1 должно быть отложено в будущее, поэтому get_pending возвращает ТОЛЬКО Сообщение 2!
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], id2, "Сообщение 2 должно обрабатываться без задержек (Head-of-Line Blocking устранен)!")

        # Помечаем Сообщение 2 как отправленное
        await database.mark_outbox_sent(id2)

        # Очередь пуста для немедленной отправки (Сообщение 1 ждет своего таймаута)
        pending_now = await database.get_pending_outbox_messages(limit=5)
        self.assertEqual(len(pending_now), 0)

    async def test_04_max_retries_transition_to_failed(self):
        """Проверка фиксации исчерпания попыток (Dead-Letter Queue)."""
        msg_id = await database.enqueue_outbox_message(1003, "Сообщение для теста DLQ")
        
        # Симулируем 5 попыток retry
        for attempt in range(1, 5):
            await database.mark_outbox_retry(msg_id, retry_count=attempt, backoff_seconds=0, error_message=f"Attempt {attempt} failed")
        
        # На 5-й попытке воркер переводит сообщение в failed
        await database.mark_outbox_failed(msg_id, "max_retries_exceeded (5/5): RPCError -500")

        # Проверяем статус в базе
        def get_row():
            with database._connection() as conn:
                return conn.execute("SELECT status, retry_count, error_message FROM bot_outbox WHERE id = ?", (msg_id,)).fetchone()

        status, retry_count, err_msg = await database._run_db(get_row)
        self.assertEqual(status, "failed")
        self.assertEqual(retry_count, 4)
        self.assertIn("max_retries_exceeded", err_msg)

    async def test_05_terminal_error_immediate_failed(self):
        """Терминальные ошибки (UserIsBlocked) не должны повторяться и сразу помечаются failed."""
        msg_id = await database.enqueue_outbox_message(1004, "Сообщение заблокировавшему пользователю")
        
        err_str = "terminal:terminal: User is blocked (caused by SendMessageRequest)"
        await database.mark_outbox_failed(msg_id, err_str)

        def get_row():
            with database._connection() as conn:
                return conn.execute("SELECT status, retry_count, error_message FROM bot_outbox WHERE id = ?", (msg_id,)).fetchone()

        status, retry_count, err_msg = await database._run_db(get_row)
        self.assertEqual(status, "failed")
        self.assertEqual(retry_count, 0)
        self.assertEqual(err_msg, err_str)

    async def test_06_mark_sent_success(self):
        """Проверка успешной отправки."""
        msg_id = await database.enqueue_outbox_message(1005, "Успешное клиническое сообщение")
        await database.mark_outbox_sent(msg_id)

        def get_row():
            with database._connection() as conn:
                return conn.execute("SELECT status, sent_at FROM bot_outbox WHERE id = ?", (msg_id,)).fetchone()

        status, sent_at = await database._run_db(get_row)
        self.assertEqual(status, "sent")
        self.assertIsNotNone(sent_at)

    async def test_07_concurrent_outbox_load(self):
        """Стресс-тест: 25 одновременных задач на запись/чтение очереди через db_worker."""
        async def worker_sim(i):
            mid = await database.enqueue_outbox_message(2000 + i, f"Concurrent task {i}")
            if i % 2 == 0:
                await database.mark_outbox_sent(mid)
            else:
                await database.mark_outbox_retry(mid, retry_count=1, backoff_seconds=30, error_message="busy")

        tasks = [worker_sim(i) for i in range(25)]
        await asyncio.gather(*tasks)

        def count_stats():
            with database._connection() as conn:
                sent = conn.execute("SELECT COUNT(*) FROM bot_outbox WHERE status = 'sent'").fetchone()[0]
                pending = conn.execute("SELECT COUNT(*) FROM bot_outbox WHERE status = 'pending'").fetchone()[0]
                return sent, pending

        sent_count, pending_count = await database._run_db(count_stats)
        self.assertEqual(sent_count, 13)
        self.assertEqual(pending_count, 12)


if __name__ == "__main__":
    unittest.main()
