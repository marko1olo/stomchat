# -*- coding: utf-8 -*-
"""
Тесты для модуля poll_storage.py.

Проверяет:
1. Инициализацию таблиц daily_polls и poll_votes и их индексов.
2. Идемпотентность повторной инициализации.
3. Сохранение опроса и чтение со всеми клиническими полями.
4. Корректный парсинг JSON-опций и конверсию типов (is_closed, options).
5. Обновление опроса при повторном сохранении (ON CONFLICT).
6. Запись голосов от нескольких врачей.
7. Защиту от повторного голосования (UNIQUE(poll_id, user_id)).
8. Автоматический расчёт правильности ответа (is_correct) для квизов.
9. Подсчёт статистики врача для профиля (/profile) и точности в % с разбивкой по темам.
10. Изоляцию квизов от lifestyle/обычных опросов при расчёте точности.
11. Сводную статистику опроса (get_poll_summary_stats).
12. Получение активных опросов и закрытие опроса (close_poll).
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Обеспечиваем безопасный вывод на Windows в любой кодировке
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import poll_storage


class TestPollStorage(unittest.IsolatedAsyncioTestCase):
    """Набор асинхронных тестов для хранилища опросов и статистики врачей."""

    async def asyncSetUp(self) -> None:
        """Создает временную базу данных для каждого теста."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_stomat_bot.db")
        # Инициализируем таблицы
        await poll_storage.init_poll_tables(self.db_path)

    async def asyncTearDown(self) -> None:
        """Очищает временные файлы базы данных."""
        self.temp_dir.cleanup()

    async def test_init_tables_idempotency(self) -> None:
        """Проверка инициализации таблиц и идемпотентности повторного вызова."""
        # Повторный вызов не должен выбрасывать исключений
        await poll_storage.init_poll_tables(self.db_path)

        async with poll_storage.get_db_connection(self.db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) as cursor:
                rows = await cursor.fetchall()
                tables = [r["name"] for r in rows]
                self.assertIn("daily_polls", tables)
                self.assertIn("poll_votes", tables)

            # Проверка индексов
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            ) as cursor:
                rows = await cursor.fetchall()
                indexes = [r["name"] for r in rows]
                self.assertIn("idx_daily_polls_chat_active", indexes)
                self.assertIn("idx_daily_polls_topic", indexes)
                self.assertIn("idx_poll_votes_poll_id", indexes)
                self.assertIn("idx_poll_votes_user_id", indexes)

    async def test_save_and_get_poll(self) -> None:
        """Проверка сохранения и чтения клинического опроса."""
        poll_id = 9988776655
        chat_id = -1001987654321
        options = [
            "1. Латеральная конденсация гуттаперчи",
            "2. Вертикальная горячая конденсация",
            "3. Термафил",
            "4. Метод одного штифта с биокерамикой",
        ]

        saved = await poll_storage.save_poll(
            poll_id=poll_id,
            chat_id=chat_id,
            case_msg_id=501,
            poll_msg_id=502,
            resolution_msg_id=505,
            poll_type="quiz",
            topic="endo",
            question="Какой протокол обтурации предпочтителен при сложной анатомии C-shape?",
            options=options,
            correct_option_id=1,
            explanation_brief="Вертикальная горячая конденсация обеспечивает максимальное запечатывание перешейков.",
            explanation_deep="При анатомии C-shape перешейки требуют трёхмерного разогрева гуттаперчи (системы типа Calamus/B&L).",
            is_closed=False,
            db_path=self.db_path,
        )
        self.assertTrue(saved)

        poll = await poll_storage.get_poll(poll_id, db_path=self.db_path)
        self.assertIsNotNone(poll)
        assert poll is not None

        self.assertEqual(poll["id"], poll_id)
        self.assertEqual(poll["chat_id"], chat_id)
        self.assertEqual(poll["case_msg_id"], 501)
        self.assertEqual(poll["poll_msg_id"], 502)
        self.assertEqual(poll["resolution_msg_id"], 505)
        self.assertEqual(poll["poll_type"], "quiz")
        self.assertEqual(poll["topic"], "endo")
        self.assertEqual(poll["correct_option_id"], 1)
        self.assertFalse(poll["is_closed"])
        self.assertEqual(len(poll["options"]), 4)
        self.assertEqual(poll["options"][1], "2. Вертикальная горячая конденсация")
        self.assertIn("C-shape", poll["explanation_deep"])

        # Проверка чтения несуществующего опроса
        non_existent = await poll_storage.get_poll(12345, db_path=self.db_path)
        self.assertIsNone(non_existent)

    async def test_save_poll_dict_and_update(self) -> None:
        """Проверка сохранения через словарь и обновление данных через ON CONFLICT."""
        poll_data = {
            "poll_id": 112233,
            "chat_id": -1001234,
            "poll_type": "regular",
            "topic": "materials",
            "question": "Какой адгезив предпочитаете в жевательном отделе?",
            "options": ["4-е поколение", "7-е поколение универсальный", "Самопротравливающий"],
            "db_path": self.db_path,
        }
        res1 = await poll_storage.save_poll(poll_data)
        self.assertTrue(res1)

        poll1 = await poll_storage.get_poll(112233, db_path=self.db_path)
        self.assertIsNotNone(poll1)
        assert poll1 is not None
        self.assertIsNone(poll1["resolution_msg_id"])

        # Обновляем опрос: добавляем resolution_msg_id и глубокий разбор
        res2 = await poll_storage.save_poll(
            poll_id=112233,
            chat_id=-1001234,
            resolution_msg_id=777,
            explanation_deep="Обзор адгезивных протоколов",
            db_path=self.db_path,
        )
        self.assertTrue(res2)

        poll2 = await poll_storage.get_poll(112233, db_path=self.db_path)
        self.assertIsNotNone(poll2)
        assert poll2 is not None
        self.assertEqual(poll2["resolution_msg_id"], 777)
        self.assertEqual(poll2["explanation_deep"], "Обзор адгезивных протоколов")
        # Ранее сохранённые варианты ответа должны остаться на месте
        self.assertEqual(len(poll2["options"]), 3)

    async def test_record_votes_multiple_doctors(self) -> None:
        """Проверка сохранения голосов от нескольких врачей."""
        poll_id = 1001
        await poll_storage.save_poll(
            poll_id=poll_id,
            chat_id=-1001,
            poll_type="quiz",
            topic="therapy",
            question="Тестовый вопрос?",
            options=["A", "B", "C"],
            correct_option_id=0,
            db_path=self.db_path,
        )

        v1 = await poll_storage.record_vote(
            poll_id=poll_id,
            user_id=101,
            user_name="Доктор Иванов",
            selected_option=0,
            is_correct=True,
            db_path=self.db_path,
        )
        v2 = await poll_storage.record_vote(
            poll_id=poll_id,
            user_id=102,
            user_name="Доктор Смирнов",
            selected_option=1,
            is_correct=False,
            db_path=self.db_path,
        )
        v3 = await poll_storage.record_vote(
            poll_id=poll_id,
            user_id=103,
            user_name="Доктор Петров",
            selected_option=0,
            is_correct=True,
            db_path=self.db_path,
        )

        self.assertTrue(v1)
        self.assertTrue(v2)
        self.assertTrue(v3)

        votes = await poll_storage.get_poll_votes(poll_id, db_path=self.db_path)
        self.assertEqual(len(votes), 3)

    async def test_duplicate_vote_protection(self) -> None:
        """Проверка защиты от повторного голосования (UNIQUE(poll_id, user_id))."""
        poll_id = 2001
        await poll_storage.save_poll(
            poll_id=poll_id,
            chat_id=-1001,
            poll_type="quiz",
            topic="surgery",
            question="Вопрос по имплантации?",
            options=["Опция 1", "Опция 2"],
            correct_option_id=1,
            db_path=self.db_path,
        )

        # Первый голос врача 555
        vote_first = await poll_storage.record_vote(
            poll_id=poll_id,
            user_id=555,
            user_name="Д-р Хаус",
            selected_option=1,
            is_correct=True,
            db_path=self.db_path,
        )
        self.assertTrue(vote_first)

        # Повторный голос того же врача в том же опросе
        vote_second = await poll_storage.record_vote(
            poll_id=poll_id,
            user_id=555,
            user_name="Д-р Хаус",
            selected_option=0,
            is_correct=False,
            db_path=self.db_path,
        )
        self.assertFalse(vote_second, "Повторный голос одного врача должен отклоняться")

        # Проверяем, что в базе остался только 1 исходный голос
        votes = await poll_storage.get_poll_votes(poll_id, db_path=self.db_path)
        self.assertEqual(len(votes), 1)
        self.assertEqual(votes[0]["selected_option"], 1)

    async def test_auto_evaluation_is_correct(self) -> None:
        """Проверка авто-определения правильности ответа, когда is_correct не передан (None)."""
        poll_id = 3001
        await poll_storage.save_poll(
            poll_id=poll_id,
            chat_id=-1001,
            poll_type="quiz",
            topic="ortho",
            question="Вопрос по ортодонтии?",
            options=["Вариант А", "Вариант Б", "Вариант В"],
            correct_option_id=2,  # Правильный ответ - индекс 2
            db_path=self.db_path,
        )

        # Врач 1 отвечает вариант 2 (правильно)
        await poll_storage.record_vote(
            poll_id=poll_id,
            user_id=201,
            user_name="Д-р Ортодонтов",
            selected_option=2,
            is_correct=None,
            db_path=self.db_path,
        )
        # Врач 2 отвечает вариант 0 (неправильно)
        await poll_storage.record_vote(
            poll_id=poll_id,
            user_id=202,
            user_name="Д-р Интерн",
            selected_option=0,
            is_correct=None,
            db_path=self.db_path,
        )

        votes = await poll_storage.get_poll_votes(poll_id, db_path=self.db_path)
        vote_dict = {v["user_id"]: v["is_correct"] for v in votes}
        self.assertEqual(vote_dict[201], 1)
        self.assertEqual(vote_dict[202], 0)

    async def test_doctor_quiz_stats_and_accuracy(self) -> None:
        """Проверка расчёта статистики врача для профиля (/profile) и точности по темам."""
        user_id = 777

        # Опрос 1: Эндодонтия (квиз)
        await poll_storage.save_poll(
            poll_id=4001,
            chat_id=-1001,
            poll_type="quiz",
            topic="endo",
            question="Эндо 1?",
            options=["1", "2"],
            correct_option_id=0,
            db_path=self.db_path,
        )
        # Опрос 2: Хирургия (квиз)
        await poll_storage.save_poll(
            poll_id=4002,
            chat_id=-1001,
            poll_type="quiz",
            topic="surgery",
            question="Хирургия 1?",
            options=["1", "2"],
            correct_option_id=1,
            db_path=self.db_path,
        )
        # Опрос 3: Хирургия (квиз)
        await poll_storage.save_poll(
            poll_id=4003,
            chat_id=-1001,
            poll_type="quiz",
            topic="surgery",
            question="Хирургия 2?",
            options=["1", "2"],
            correct_option_id=1,
            db_path=self.db_path,
        )
        # Опрос 4: Лайфстайл (не квиз, без правильного ответа)
        await poll_storage.save_poll(
            poll_id=4004,
            chat_id=-1001,
            poll_type="lifestyle",
            topic="lifestyle",
            question="Кофе или чай перед сменой?",
            options=["Кофе", "Чай"],
            correct_option_id=None,
            db_path=self.db_path,
        )

        # Врач 777 голосует:
        # Эндо 1: Правильно
        await poll_storage.record_vote(
            poll_id=4001,
            user_id=user_id,
            user_name="Доктор Профи",
            selected_option=0,
            is_correct=True,
            db_path=self.db_path,
        )
        # Хирургия 1: Правильно
        await poll_storage.record_vote(
            poll_id=4002,
            user_id=user_id,
            user_name="Доктор Профи",
            selected_option=1,
            is_correct=True,
            db_path=self.db_path,
        )
        # Хирургия 2: Неправильно
        await poll_storage.record_vote(
            poll_id=4003,
            user_id=user_id,
            user_name="Доктор Профи",
            selected_option=0,
            is_correct=False,
            db_path=self.db_path,
        )
        # Лайфстайл: Проголосовал (не должно ломать % точности в квизах)
        await poll_storage.record_vote(
            poll_id=4004,
            user_id=user_id,
            user_name="Доктор Профи",
            selected_option=0,
            is_correct=None,
            db_path=self.db_path,
        )

        stats = await poll_storage.get_user_quiz_stats(user_id, db_path=self.db_path)

        # Всего квизов: 3 (endo, surgery, surgery). Лайфстайл исключён из квиз-статистики.
        self.assertEqual(stats["total_answers"], 3)
        self.assertEqual(stats["correct_answers"], 2)
        self.assertEqual(stats["incorrect_answers"], 1)
        # Точность: 2 из 3 = 66.7%
        self.assertEqual(stats["accuracy_percent"], 66.7)
        self.assertEqual(stats["total_votes"], 4)

        # Проверка разбивки по темам
        by_topic = stats["by_topic"]
        self.assertIn("endo", by_topic)
        self.assertIn("surgery", by_topic)

        # Эндо: 1 из 1 = 100.0%
        self.assertEqual(by_topic["endo"]["total"], 1)
        self.assertEqual(by_topic["endo"]["correct"], 1)
        self.assertEqual(by_topic["endo"]["accuracy_percent"], 100.0)

        # Хирургия: 1 из 2 = 50.0%
        self.assertEqual(by_topic["surgery"]["total"], 2)
        self.assertEqual(by_topic["surgery"]["correct"], 1)
        self.assertEqual(by_topic["surgery"]["accuracy_percent"], 50.0)

        # Проверка врача без голосов
        empty_stats = await poll_storage.get_user_quiz_stats(999999, db_path=self.db_path)
        self.assertEqual(empty_stats["total_answers"], 0)
        self.assertEqual(empty_stats["correct_answers"], 0)
        self.assertEqual(empty_stats["accuracy_percent"], 0.0)
        self.assertEqual(empty_stats["by_topic"], {})

    async def test_get_poll_summary_stats(self) -> None:
        """Проверка сводной статистики опроса (число проголосовавших, % правильных ответов)."""
        poll_id = 5001
        await poll_storage.save_poll(
            poll_id=poll_id,
            chat_id=-1001,
            poll_type="quiz",
            topic="materials",
            question="Сводная статистика: вопрос?",
            options=["Опция 0", "Опция 1", "Опция 2"],
            correct_option_id=1,
            db_path=self.db_path,
        )

        # 4 врача голосуют: 3 правильно (опция 1), 1 неправильно (опция 0)
        doctors = [
            (10, "Врач 1", 1, True),
            (20, "Врач 2", 1, True),
            (30, "Врач 3", 1, True),
            (40, "Врач 4", 0, False),
        ]
        for uid, name, opt, corr in doctors:
            await poll_storage.record_vote(
                poll_id=poll_id,
                user_id=uid,
                user_name=name,
                selected_option=opt,
                is_correct=corr,
                db_path=self.db_path,
            )

        summary = await poll_storage.get_poll_summary_stats(poll_id, db_path=self.db_path)
        self.assertEqual(summary["poll_id"], poll_id)
        self.assertEqual(summary["total_votes"], 4)
        self.assertEqual(summary["correct_votes"], 3)
        self.assertEqual(summary["incorrect_votes"], 1)
        # 3 из 4 = 75.0%
        self.assertEqual(summary["accuracy_percent"], 75.0)
        self.assertEqual(summary["votes_by_option"], {1: 3, 0: 1})
        self.assertEqual(summary["topic"], "materials")
        self.assertFalse(summary["is_closed"])

        # Проверка сводки для опроса без голосов
        empty_summary = await poll_storage.get_poll_summary_stats(999999, db_path=self.db_path)
        self.assertEqual(empty_summary["total_votes"], 0)
        self.assertEqual(empty_summary["accuracy_percent"], 0.0)

    async def test_active_polls_and_close_poll(self) -> None:
        """Проверка выборки активных опросов и корректного закрытия опроса."""
        chat_1 = -100111
        chat_2 = -100222

        # Создаём 2 опроса в chat_1 и 1 опрос в chat_2
        await poll_storage.save_poll(
            poll_id=6001,
            chat_id=chat_1,
            question="Чат 1 Опрос 1",
            options=["A", "B"],
            db_path=self.db_path,
        )
        await poll_storage.save_poll(
            poll_id=6002,
            chat_id=chat_1,
            question="Чат 1 Опрос 2",
            options=["A", "B"],
            db_path=self.db_path,
        )
        await poll_storage.save_poll(
            poll_id=6003,
            chat_id=chat_2,
            question="Чат 2 Опрос 1",
            options=["A", "B"],
            db_path=self.db_path,
        )

        active_chat1 = await poll_storage.get_active_polls(chat_1, db_path=self.db_path)
        self.assertEqual(len(active_chat1), 2)
        active_ids = [p["id"] for p in active_chat1]
        self.assertIn(6001, active_ids)
        self.assertIn(6002, active_ids)

        # Закрываем опрос 6001
        closed = await poll_storage.close_poll(6001, db_path=self.db_path)
        self.assertTrue(closed)

        # Проверяем, что опрос 6001 исчез из активных
        active_after_close = await poll_storage.get_active_polls(chat_1, db_path=self.db_path)
        self.assertEqual(len(active_after_close), 1)
        self.assertEqual(active_after_close[0]["id"], 6002)

        # Проверяем детальный статус опроса 6001
        poll_info = await poll_storage.get_poll(6001, db_path=self.db_path)
        self.assertIsNotNone(poll_info)
        assert poll_info is not None
        self.assertTrue(poll_info["is_closed"])
        self.assertIsNotNone(poll_info["closed_at"])

        # Попытка закрыть несуществующий опрос возвращает False
        close_non_existent = await poll_storage.close_poll(999999, db_path=self.db_path)
        self.assertFalse(close_non_existent)

    async def test_schema_table_columns_exact(self) -> None:
        """Проверка точного соответствия колонок ТЗ для daily_polls и poll_votes."""
        async with poll_storage.get_db_connection(self.db_path) as db:
            # daily_polls columns
            async with db.execute("PRAGMA table_info(daily_polls)") as cursor:
                rows = await cursor.fetchall()
                cols = {r["name"]: r for r in rows}

            expected_daily_polls_cols = [
                "id", "chat_id", "case_msg_id", "poll_msg_id", "resolution_msg_id",
                "poll_type", "topic", "question", "options_json", "correct_option_id",
                "explanation_brief", "explanation_deep", "created_at", "closed_at", "is_closed"
            ]
            for col_name in expected_daily_polls_cols:
                self.assertIn(col_name, cols, f"Колонка {col_name} должна присутствовать в daily_polls")

            # Проверка первичного ключа
            self.assertEqual(cols["id"]["pk"], 1)
            # chat_id NOT NULL
            self.assertEqual(cols["chat_id"]["notnull"], 1)

            # poll_votes columns
            async with db.execute("PRAGMA table_info(poll_votes)") as cursor:
                rows = await cursor.fetchall()
                v_cols = {r["name"]: r for r in rows}

            expected_vote_cols = [
                "id", "poll_id", "user_id", "user_name", "selected_option", "is_correct", "voted_at"
            ]
            for col_name in expected_vote_cols:
                self.assertIn(col_name, v_cols, f"Колонка {col_name} должна присутствовать в poll_votes")

            # Проверка уникального ограничения UNIQUE(poll_id, user_id)
            async with db.execute("PRAGMA index_list(poll_votes)") as cursor:
                index_rows = await cursor.fetchall()
                unique_indices = [r["name"] for r in index_rows if r["unique"] == 1]
                found_composite_unique = False
                for idx_name in unique_indices:
                    async with db.execute(f"PRAGMA index_info('{idx_name}')") as idx_cursor:
                        col_rows = await idx_cursor.fetchall()
                        indexed_cols = [c["name"] for c in col_rows]
                        if set(indexed_cols) == {"poll_id", "user_id"}:
                            found_composite_unique = True
                            break
                self.assertTrue(found_composite_unique, "Индекс UNIQUE(poll_id, user_id) должен присутствовать в poll_votes")

    async def test_corrupted_options_json_resilience(self) -> None:
        """Проверка устойчивости get_poll при некорректном JSON в options_json."""
        async with poll_storage.get_db_connection(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO daily_polls (id, chat_id, poll_type, topic, question, options_json)
                VALUES (7771, -1001, 'quiz', 'therapy', 'Битый JSON?', 'НЕ_JSON_СТРОКА{{{')
                """
            )
            await db.commit()

        poll = await poll_storage.get_poll(7771, db_path=self.db_path)
        self.assertIsNotNone(poll)
        assert poll is not None
        # При сбое парсинга options должен безопасно возвращать пустой список
        self.assertEqual(poll["options"], [])

    async def test_get_active_polls_today_and_silent_close(self) -> None:
        """Проверка выборки активных опросов строго за сегодня и тихого закрытия устаревших."""
        chat_id = -100555

        # 1. Создаем старый опрос (вчерашний)
        await poll_storage.save_poll(
            poll_id=8001,
            chat_id=chat_id,
            question="Вчерашний опрос",
            options=["1", "2"],
            created_at="2026-09-25 12:00:00",
            db_path=self.db_path,
        )

        # 2. Создаем сегодняшний активный опрос
        await poll_storage.save_poll(
            poll_id=8002,
            chat_id=chat_id,
            question="Сегодняшний опрос",
            options=["A", "B"],
            db_path=self.db_path,
        )

        # Проверяем, что get_active_polls_today возвращает ТОЛЬКО сегодняшний
        today_polls = await poll_storage.get_active_polls_today(chat_id, db_path=self.db_path)
        self.assertEqual(len(today_polls), 1)
        self.assertEqual(today_polls[0]["id"], 8002)

        # Вызываем тихое закрытие устаревших опросов
        stale_closed = await poll_storage.close_stale_polls_silently(chat_id, db_path=self.db_path)
        self.assertEqual(stale_closed, 1)

        # Вчерашний опрос теперь закрыт
        p_old = await poll_storage.get_poll(8001, db_path=self.db_path)
        assert p_old is not None
        self.assertTrue(p_old["is_closed"])

        # Сегодняшний опрос по-прежнему активен
        p_today = await poll_storage.get_poll(8002, db_path=self.db_path)
        assert p_today is not None
        self.assertFalse(p_today["is_closed"])

        # Вызываем тихое закрытие ВСЕХ активных опросов (например, в ночное время после 22:00 МСК)
        all_closed = await poll_storage.close_all_active_polls_silently(chat_id, db_path=self.db_path)
        self.assertEqual(all_closed, 1)

        # Теперь и сегодняшний опрос закрыт, ночных случайных разборов не произойдет
        p_today_after = await poll_storage.get_poll(8002, db_path=self.db_path)
        assert p_today_after is not None
        self.assertTrue(p_today_after["is_closed"])


def run_tests() -> int:
    """Запуск набора тестов с человекочитаемым форматированием."""
    suite = unittest.TestLoader().loadTestsFromTestCase(TestPollStorage)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests())
