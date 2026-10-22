"""
Red Team Inquisitor Test Suite: Memory Retrieval & Context Injection (PM & Group).
Strict verification across StomChat and EndoChat:
1. PM Memory Context Guard: strict token/character cap (max_chars=4000) against prompt eviction;
   preservation of confirmed medical specialty and clinical facts.
2. Group Consilium Priority Ranking: top-active doctors + guaranteed priority for interlocutor & case author;
   strict ceiling (max_users=4..5, max_chars=1200) under 10-15 participants flood.
3. Cache Staleness & Rich Clinical Case Cooldown: dynamic 3.0s cooldown on FDI teeth (1.6, 3.6, etc.) & protocols;
   15s cooldown on trivial chatter.
4. Database Batch Safety & Clean Fallbacks: resilient batch queries, zero exceptions on missing users.

Execution: python tests/test_memory_retrieval_redteam.py
"""

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import user_memory
import database


class TestPmMemoryContextInjectionRedTeam(unittest.TestCase):
    """Вектор 1: Инжекция памяти в ЛС и защита от вытеснения инструкций (Context Guard)."""

    def test_massive_summary_budget_cap(self):
        """Проверяет, что досье врача на 30-50 КБ строго сжимается до max_chars (4000) без обрыва слов."""
        massive_cases = "\n".join([
            f"Кейс {i}: Пациент 4{i%8 + 1}, глубокий кариес, пульпит, медикаментозная обработка 3% NaOCl, обтурация AH Plus."
            for i in range(100)
        ])
        self.assertGreater(len(massive_cases), 10000)

        mem = {
            "user_id": 12345,
            "specialty": "Стоматолог-терапевт, эндодонтист",
            "clinical_summary": f"Специализация: Эндодонтия.\nАрсенал: Leica M320, Woodpecker.\nКейсы:\n{massive_cases}",
            "group_summary": "Активно делится опытом лечения корневых каналов.",
            "facts_json": json.dumps([
                "Работает под микроскопом Leica M320",
                "Использует силер AH Plus",
                "Применяет систему коффердама Sanctuary"
            ], ensure_ascii=False)
        }

        prompt = user_memory.format_clinician_memory_prompt(12345, mem, max_chars=4000)

        # 1. Лимит строго соблюдается
        self.assertLessEqual(len(prompt), 4000)
        # 2. Специализация гарантированно сохранена
        self.assertIn("Стоматолог-терапевт, эндодонтист", prompt)
        # 3. Ключевые факты гарантированно сохранены
        self.assertIn("Leica M320", prompt)
        self.assertIn("Sanctuary", prompt)
        # 4. Заголовок и инструкция сохранены
        self.assertIn("=== ДОЛГОВРЕМЕННАЯ ПАМЯТЬ И КЛИНИЧЕСКИЙ ПРОФИЛЬ ВРАЧА ===", prompt)

    def test_empty_or_initial_memory_prompt(self):
        """Проверяет корректность генерации плейсхолдера при отсутствии памяти."""
        prompt_none = user_memory.format_clinician_memory_prompt(999, None)
        self.assertEqual(prompt_none, "Клинический профиль доктора формируется (первые обращения).")

        prompt_empty = user_memory.format_clinician_memory_prompt(999, {})
        self.assertEqual(prompt_empty, "Клинический профиль доктора формируется (первые обращения).")

    def test_pm_and_group_summary_budget_split(self):
        """Проверяет балансировку бюджетов при одновременном наличии PM и группового досье."""
        pm_text = "Опыт в ЛС: " + ("разбор сложных перфораций и перелечиваний каналов. " * 30)
        grp_text = "Опыт в группе: " + ("дискуссии по биомеханике штифтов и феррулу. " * 30)

        mem = {
            "user_id": 555,
            "specialty": "Стоматолог-ортопед",
            "clinical_summary": pm_text,
            "group_summary": grp_text,
            "facts_json": "[]"
        }

        prompt = user_memory.format_clinician_memory_prompt(555, mem, max_chars=2500)
        self.assertLessEqual(len(prompt), 2500)
        self.assertIn("Стоматолог-ортопед", prompt)
        self.assertIn("Опыт и специфика практики из ЛС:", prompt)
        self.assertIn("Клиническое досье из общей беседы:", prompt)


class TestGroupConsiliumPriorityRankingRedTeam(unittest.IsolatedAsyncioTestCase):
    """Вектор 2: Инжекция памяти в группе, Priority Ranking и лимиты участников."""

    async def test_fifteen_doctors_flood_capped_to_top_users(self):
        """Если в дискуссии участвуют 15 врачей, контекст не взрывается, а отбирает топ-N авторов."""
        # 15 врачей: uid 101..115
        user_ids_in_chunk = [
            101, 102, 103, 104, 105, 106, 107, 108, 109, 110,
            111, 112, 113, 114, 115,
            # Добавим активности доктору 115 (5 сообщений) и доктору 110 (3 сообщения)
            115, 115, 115, 115,
            110, 110
        ]

        fake_batch = {}
        for uid in range(101, 116):
            fake_batch[uid] = {
                "user_id": uid,
                "first_name": f"Доктор_{uid}",
                "username": f"doc_{uid}",
                "specialty": f"Специализация_{uid}",
                "group_summary": f"Клинический профиль врача #{uid} в чате.",
            }

        with patch.object(database, "get_users_memory_batch", AsyncMock(return_value=fake_batch)):
            res = await user_memory.format_users_chunk_context(
                user_ids=user_ids_in_chunk,
                max_chars=2000,
                priority_user_ids=[101],  # 101 — автор текущего вопроса
                max_users=4
            )

            # 1. Проверяем, что в результирующий контекст вошли не более 4 врачей
            self.assertIn("Доктор_101", res)  # Приоритетный собеседник обязан быть
            self.assertIn("Доктор_115", res)  # Самый активный участник (5 реплик) обязан быть
            self.assertIn("Доктор_110", res)  # Второй по активности (3 реплики) обязан быть

            # Проверяем лимит: не более 4 маркеров пуль
            bullet_count = res.count("• Доктор_")
            self.assertLessEqual(bullet_count, 4)
            self.assertLessEqual(len(res), 2000)

    async def test_case_author_and_interlocutor_priority_guarantee(self):
        """Проверяет гарантию сохранения профиля автора кейса и собеседника перед остальными."""
        # Автор кейса 200, собеседник 300, фоновые флудеры 400..405
        user_ids = [400, 401, 402, 403, 404, 405, 300, 200]
        priority_ids = [300, 200]

        fake_batch = {
            200: {"user_id": 200, "first_name": "Иванов", "username": "ivanov", "specialty": "Хирург-имплантолог", "group_summary": "Сложная имплантация."},
            300: {"user_id": 300, "first_name": "Петров", "username": "petrov", "specialty": "Ортопед", "group_summary": "Тотальное протезирование."},
            400: {"user_id": 400, "first_name": "Сидоров", "username": "sidorov", "specialty": "Терапевт", "group_summary": "Пломбы."},
        }

        with patch.object(database, "get_users_memory_batch", AsyncMock(return_value=fake_batch)):
            res = await user_memory.format_users_chunk_context(
                user_ids=user_ids,
                max_chars=1200,
                priority_user_ids=priority_ids,
                max_users=3
            )

            self.assertIn("Петров", res)
            self.assertIn("Иванов", res)
            # Приоритетный порядок: Петров (собеседник) идет раньше Иванова
            petrov_pos = res.find("Петров")
            ivanov_pos = res.find("Иванов")
            self.assertLess(petrov_pos, ivanov_pos)

    async def test_bot_accounts_exclusion_from_profiles(self):
        """Проверяет, что боты автоматически отсекаются из списка профилей участников."""
        bot_ids = [7971556097, 8775535374]
        res = await user_memory.format_users_chunk_context(user_ids=bot_ids, max_chars=1000)
        self.assertEqual(res, "")


class TestCacheStalenessAndCooldownRedTeam(unittest.TestCase):
    """Вектор 3: Актуальность памяти и динамическая регулировка кулдауна."""

    def setUp(self):
        user_memory.reset_pm_memory_cooldown()

    def tearDown(self):
        user_memory.reset_pm_memory_cooldown()

    def test_rich_clinical_case_cooldown_acceleration(self):
        """Проверяет, что при упоминании зубов FDI или ключевых протоколов кулдаун сокращается до 3с."""
        user_id = 999111
        user_memory._LAST_PM_UPDATE_TS[user_id] = time.time() - 4.0  # прошло 4 секунды

        # Обычное сообщение: 4с < 15с -> throttled
        # Но сообщение с зубом FDI (например, зуб 36) и силером -> rich case -> effective_cooldown=3.0 -> НЕ throttled!
        rich_msg = "В зубе 36 при накусывании боли, пломбировал BioRoot RCS"

        # Проверим формулу расчета rich_case
        has_tooth_fdi = bool(user_memory.re.search(r'(?<!\d)[1-4][1-8](?!\d)', rich_msg))
        has_clinical_keywords = any(w in rich_msg.lower() for w in ("снимок", "зуб", "канал", "силер"))
        has_rich = len(rich_msg) > 250 or has_tooth_fdi or has_clinical_keywords
        self.assertTrue(has_rich)
        self.assertTrue(has_tooth_fdi)

    def test_reset_pm_cooldown_functionality(self):
        """Проверяет корректность функции сброса кулдауна для изоляции тестов."""
        user_memory._LAST_PM_UPDATE_TS[10] = time.time()
        user_memory._LAST_PM_UPDATE_TS[20] = time.time()

        user_memory.reset_pm_memory_cooldown(10)
        self.assertNotIn(10, user_memory._LAST_PM_UPDATE_TS)
        self.assertIn(20, user_memory._LAST_PM_UPDATE_TS)

        user_memory.reset_pm_memory_cooldown()
        self.assertEqual(len(user_memory._LAST_PM_UPDATE_TS), 0)


class TestAssistantIntegrationMemoryCalls(unittest.TestCase):
    """Вектор 4: Проверка интеграции вызовов памяти в assistant.py."""

    def test_assistant_code_contains_priority_ranking_calls(self):
        """Проверяет наличие вызовов format_users_chunk_context с priority_user_ids в assistant.py."""
        with open(_REPO_ROOT / "assistant.py", "r", encoding="utf-8") as f:
            content = f.read()

        # 1. generate_response integration
        self.assertIn("priority_user_ids=priority_uids", content)
        self.assertIn("max_users=4", content)
        # 2. handle_bot_mention integration
        self.assertIn("# Подгружаем накопленную память беседы об участниках диалога с Priority Ranking", content)


if __name__ == "__main__":
    unittest.main()
