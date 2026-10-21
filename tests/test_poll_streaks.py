"""
Unit & Integration тесты для механики Clinical Quiz Streaks, слотов публикации опросов
и защиты точности профиля врача от non-quiz искажений.
100% архитектурный паритет между StomChat и EndoChat.
"""

import os
import sys
import tempfile
import pytest
from datetime import datetime, date, timedelta, timezone

# Гарантируем правильный sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import config
import poll_storage
import user_memory


@pytest.fixture(autouse=True)
def temp_db_environment(monkeypatch, tmp_path):
    """Изолированная временная база данных для каждого теста."""
    db_file = tmp_path / "test_streaks.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    # Сбрасываем пул соединений
    poll_storage._db_pool = None
    import asyncio
    asyncio.run(poll_storage.init_poll_tables())
    yield
    poll_storage._db_pool = None


# ==============================================================================
# 1. ТЕСТЫ АЛГОРИТМА РАСЧЕТА СТРИКОВ (calculate_quiz_streaks)
# ==============================================================================

def test_calculate_quiz_streaks_empty():
    today = date(2026, 10, 22)
    cur, max_s = poll_storage.calculate_quiz_streaks([], today)
    assert cur == 0
    assert max_s == 0


def test_calculate_quiz_streaks_today_only():
    today = date(2026, 10, 22)
    cur, max_s = poll_storage.calculate_quiz_streaks(["2026-10-22"], today)
    assert cur == 1
    assert max_s == 1


def test_calculate_quiz_streaks_yesterday_only():
    """Если врач ответил вчера, стрик НЕ сгорает, пока сегодняшние сутки идут."""
    today = date(2026, 10, 22)
    cur, max_s = poll_storage.calculate_quiz_streaks(["2026-10-21"], today)
    assert cur == 1
    assert max_s == 1


def test_calculate_quiz_streaks_two_days_ago_broken():
    """Если последний ответ был 2 дня назад, текущий стрик сгорел, рекорд остался."""
    today = date(2026, 10, 22)
    cur, max_s = poll_storage.calculate_quiz_streaks(["2026-10-20"], today)
    assert cur == 0
    assert max_s == 1


def test_calculate_quiz_streaks_consecutive_3_days_including_today():
    today = date(2026, 10, 22)
    dates = ["2026-10-22", "2026-10-21", "2026-10-20"]
    cur, max_s = poll_storage.calculate_quiz_streaks(dates, today)
    assert cur == 3
    assert max_s == 3


def test_calculate_quiz_streaks_consecutive_3_days_ending_yesterday():
    today = date(2026, 10, 22)
    dates = ["2026-10-21", "2026-10-20", "2026-10-19"]
    cur, max_s = poll_storage.calculate_quiz_streaks(dates, today)
    assert cur == 3
    assert max_s == 3


def test_calculate_quiz_streaks_duplicates_and_unsorted():
    today = date(2026, 10, 22)
    dates = ["2026-10-21", "2026-10-22", "2026-10-21", "2026-10-22", "2026-10-20"]
    cur, max_s = poll_storage.calculate_quiz_streaks(dates, today)
    assert cur == 3
    assert max_s == 3


def test_calculate_quiz_streaks_historical_record():
    today = date(2026, 10, 22)
    dates = [
        "2026-10-22", "2026-10-21",
        "2026-09-05", "2026-09-04", "2026-09-03", "2026-09-02", "2026-09-01"
    ]
    cur, max_s = poll_storage.calculate_quiz_streaks(dates, today)
    assert cur == 2
    assert max_s == 5


# ==============================================================================
# 2. ИНТЕГРАЦИОННЫЕ ТЕСТЫ БАЗЫ ДАННЫХ И get_user_quiz_stats СО СТРИКАМИ
# ==============================================================================

@pytest.mark.asyncio
async def test_get_user_quiz_stats_with_streak():
    doctor_id = 998877

    # Создаем 3 квиза за разные дни
    # День 1: 2 дня назад
    dt_2days_ago = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    p1 = 101
    await poll_storage.save_poll(
        poll_id=p1, chat_id=-1001, poll_type="quiz",
        question="Тест 1", options=["A", "B"], correct_option_id=0,
        explanation_brief="OK", explanation_deep="OK", topic="endo"
    )
    async with poll_storage.get_db_connection() as conn:
        await conn.execute("UPDATE daily_polls SET created_at = ? WHERE id = ?", (dt_2days_ago, p1))
        await conn.commit()

    await poll_storage.record_vote(
        poll_id=p1, user_id=doctor_id, user_name="Д-р Тест",
        selected_option=0, is_correct=True
    )
    async with poll_storage.get_db_connection() as conn:
        await conn.execute("UPDATE poll_votes SET voted_at = ? WHERE poll_id = ? AND user_id = ?", (dt_2days_ago, p1, doctor_id))
        await conn.commit()

    # День 2: вчера
    dt_yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    p2 = 102
    await poll_storage.save_poll(
        poll_id=p2, chat_id=-1001, poll_type="quiz",
        question="Тест 2", options=["A", "B"], correct_option_id=1,
        explanation_brief="OK", explanation_deep="OK", topic="endo"
    )
    async with poll_storage.get_db_connection() as conn:
        await conn.execute("UPDATE daily_polls SET created_at = ? WHERE id = ?", (dt_yesterday, p2))
        await conn.commit()

    await poll_storage.record_vote(
        poll_id=p2, user_id=doctor_id, user_name="Д-р Тест",
        selected_option=1, is_correct=True
    )
    async with poll_storage.get_db_connection() as conn:
        await conn.execute("UPDATE poll_votes SET voted_at = ? WHERE poll_id = ? AND user_id = ?", (dt_yesterday, p2, doctor_id))
        await conn.commit()

    # День 3: сегодня
    p3 = 103
    await poll_storage.save_poll(
        poll_id=p3, chat_id=-1001, poll_type="quiz",
        question="Тест 3", options=["A", "B"], correct_option_id=0,
        explanation_brief="OK", explanation_deep="OK", topic="surgery"
    )
    await poll_storage.record_vote(
        poll_id=p3, user_id=doctor_id, user_name="Д-р Тест",
        selected_option=0, is_correct=True
    )

    stats = await poll_storage.get_user_quiz_stats(doctor_id)
    assert stats["total_answers"] == 3
    assert stats["correct_answers"] == 3
    assert stats["accuracy_percent"] == 100.0
    assert stats["current_streak"] == 3
    assert stats["max_streak"] == 3
    assert stats["streak_days"] == 3


@pytest.mark.asyncio
async def test_non_quiz_polls_do_not_poison_accuracy_or_streaks():
    """Голоса в опросах мнений (discussion) НЕ должны портить точность квизов."""
    doctor_id = 554433

    # 1. Доктор правильно ответил на 1 квиз
    quiz_id = 201
    await poll_storage.save_poll(
        poll_id=quiz_id, chat_id=-1001, poll_type="quiz",
        question="Клинический квиз", options=["А", "Б"], correct_option_id=0,
        explanation_brief="OK", explanation_deep="OK", topic="therapy"
    )
    await poll_storage.record_vote(
        poll_id=quiz_id, user_id=doctor_id, user_name="Д-р Иванов",
        selected_option=0, is_correct=True
    )

    # 2. Доктор проголосовал в 2 дискуссионных опросах (опросы мнений)
    disc_id_1 = 202
    await poll_storage.save_poll(
        poll_id=disc_id_1, chat_id=-1001, poll_type="regular",
        question="Каким силером работаете?", options=["Эпоксидный", "Биокерамика"],
        correct_option_id=None, explanation_brief="", explanation_deep="", topic="endo"
    )
    await poll_storage.record_vote(
        poll_id=disc_id_1, user_id=doctor_id, user_name="Д-р Иванов",
        selected_option=1, is_correct=False
    )

    disc_id_2 = 203
    await poll_storage.save_poll(
        poll_id=disc_id_2, chat_id=-1001, poll_type="lifestyle",
        question="Тактика при трещине", options=["Удаление", "Коронка"],
        correct_option_id=None, explanation_brief="", explanation_deep="", topic="lifestyle"
    )
    await poll_storage.record_vote(
        poll_id=disc_id_2, user_id=doctor_id, user_name="Д-р Иванов",
        selected_option=0, is_correct=None
    )

    stats = await poll_storage.get_user_quiz_stats(doctor_id)
    # total_answers должен учитывать ТОЛЬКО квизы!
    assert stats["total_answers"] == 1
    assert stats["correct_answers"] == 1
    assert stats["accuracy_percent"] == 100.0  # Точность НЕ должна упасть до 33%!
    assert stats["total_votes"] == 3          # Общее число голосов всех типов
    assert stats["current_streak"] == 1


# ==============================================================================
# 3. ТЕСТЫ СЛОТОВ ПУБЛИКАЦИИ ОПРОСОВ (has_poll_today со слотами)
# ==============================================================================

@pytest.mark.asyncio
async def test_has_poll_today_slots():
    chat_id = -10012345
    # Изначально опросов нет
    assert await poll_storage.has_poll_today(chat_id, slot="any") is False
    assert await poll_storage.has_poll_today(chat_id, slot="morning") is False
    assert await poll_storage.has_poll_today(chat_id, slot="afternoon") is False

    # В SQLite база хранит timestamps в UTC. Московское время = UTC + 3.
    # Вычисляем сегодняшнюю дату по МСК:
    tz_msk = timezone(timedelta(hours=3))
    now_msk = datetime.now(tz_msk)
    
    # Для 09:30 МСК переводим в UTC:
    morning_msk = datetime(now_msk.year, now_msk.month, now_msk.day, 9, 30, 0, tzinfo=tz_msk)
    today_morning_utc = morning_msk.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    p_m = 301
    await poll_storage.save_poll(
        poll_id=p_m, chat_id=chat_id, poll_type="quiz",
        question="Утренний квиз", options=["A", "B"], correct_option_id=0,
        explanation_brief="", explanation_deep="", topic="endo"
    )
    async with poll_storage.get_db_connection() as conn:
        await conn.execute("UPDATE daily_polls SET created_at = ? WHERE id = ?", (today_morning_utc, p_m))
        await conn.commit()

    # Проверяем: утром опрос есть, днем еще нет
    assert await poll_storage.has_poll_today(chat_id, slot="any") is True
    assert await poll_storage.has_poll_today(chat_id, slot="morning") is True
    assert await poll_storage.has_poll_today(chat_id, slot="afternoon") is False

    # Для 15:30 МСК переводим в UTC:
    afternoon_msk = datetime(now_msk.year, now_msk.month, now_msk.day, 15, 30, 0, tzinfo=tz_msk)
    today_afternoon_utc = afternoon_msk.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    p_a = 302
    await poll_storage.save_poll(
        poll_id=p_a, chat_id=chat_id, poll_type="quiz",
        question="Дневной квиз", options=["C", "D"], correct_option_id=1,
        explanation_brief="", explanation_deep="", topic="ortho"
    )
    async with poll_storage.get_db_connection() as conn:
        await conn.execute("UPDATE daily_polls SET created_at = ? WHERE id = ?", (today_afternoon_utc, p_a))
        await conn.commit()

    # Теперь оба слота закрыты
    assert await poll_storage.has_poll_today(chat_id, slot="morning") is True
    assert await poll_storage.has_poll_today(chat_id, slot="afternoon") is True


# ==============================================================================
# 4. ТЕСТ ИСКЛЮЧЕНИЯ ДУБЛИРОВАНИЯ ТЕМ (get_recent_poll_topics)
# ==============================================================================

@pytest.mark.asyncio
async def test_get_recent_poll_topics():
    chat_id = -10012345
    await poll_storage.save_poll(
        poll_id=401, chat_id=chat_id, poll_type="quiz",
        question="Q1", options=["A", "B"], correct_option_id=0,
        explanation_brief="", explanation_deep="", topic="endo"
    )
    await poll_storage.save_poll(
        poll_id=402, chat_id=chat_id, poll_type="quiz",
        question="Q2", options=["A", "B"], correct_option_id=0,
        explanation_brief="", explanation_deep="", topic="surgery"
    )

    recent = await poll_storage.get_recent_poll_topics(limit=5)
    assert len(recent) == 2
    topics = [r["topic"] for r in recent]
    assert "surgery" in topics
    assert "endo" in topics


# ==============================================================================
# 5. ТЕСТ ОТОБРАЖЕНИЯ СТРИКА В КАРТОЧКЕ ПРОФИЛЯ ДОКТОРА
# ==============================================================================

def test_format_user_profile_card_shows_streak():
    memory = {
        "specialty": "Стоматолог-эндодонтист",
        "group_summary": "Активно обсуждает ирригационные протоколы",
    }
    quiz_stats = {
        "total_answers": 10,
        "correct_answers": 9,
        "accuracy_percent": 90.0,
        "current_streak": 5,
        "max_streak": 7,
        "streak_days": 12,
        "topics": {}
    }

    card = user_memory.format_user_profile_card(memory, "Доктор Смирнов", quiz_stats=quiz_stats)
    assert "90.0%" in card
    assert "Клинический стрик" in card
    assert "5 дн. подряд" in card
    assert "рекорд: 7" in card
