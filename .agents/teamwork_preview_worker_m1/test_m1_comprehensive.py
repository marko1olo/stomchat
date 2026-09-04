"""
Comprehensive verification of Milestone M1 features in user_memory.py:
1. Deduplication of clinical_summary (in-section, cross-section, bullets, normalization, headers).
2. format_users_chunk_context with max_chars budgeting (strict <= max_chars, whole profile inclusion).
3. reset_pm_memory_cooldown (per-user and global).
4. update_clinician_memory_async with mocked LLM producing duplicates -> verified clean in DB.
5. Trivial message filter & group daemon idle check.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_test_m1_")
import config
config.DB_PATH = os.path.join(_TMPDIR, "test_m1.db")

import database
import user_memory

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))


async def run_all_m1_tests():
    print("\n--- [M1.1] Deduplication of clinical_summary ---")

    test_input = """
Специализация:
- Стоматолог-ортопед, гнатолог
- Стоматолог-ортопед, гнатолог.

Арсенал и оснащение:
- Дентальный микроскоп Carl Zeiss OPMI PROergo
- Ультразвуковой аппарат Woodpecker
- Дентальный микроскоп Carl Zeiss OPMI PROergo;
• Ультразвуковой аппарат Woodpecker

Клинические протоколы:
- Спиртовой протокол адгезии OptiBond FL
- Ирригация гипохлоритом натрия 3% с активацией
- Спиртовой протокол адгезии OptiBond FL
- Дентальный микроскоп Carl Zeiss OPMI PROergo

Кейсы:
- Зуб 3.6: деструкция костной ткани у апекса дистального корня.
- Зуб 3.6: деструкция костной ткани у апекса дистального корня
- Ирригация гипохлоритом натрия 3% с активацией
"""

    deduped = user_memory.deduplicate_clinical_summary(test_input)
    check("Специализация: сохранена", "Специализация:" in deduped)
    check("Арсенал и оснащение: сохранено", "Арсенал и оснащение:" in deduped)
    check("Клинические протоколы: сохранено", "Клинические протоколы:" in deduped)
    check("Кейсы: сохранено", "Кейсы:" in deduped)

    check("Ортопед, гнатолог ровно 1 раз", deduped.count("Стоматолог-ортопед, гнатолог") == 1)
    check("Carl Zeiss ровно 1 раз (удален дубль внутри и межсекционный дубль в Протоколах)",
          deduped.count("Carl Zeiss OPMI PROergo") == 1)
    check("Woodpecker ровно 1 раз", deduped.count("Woodpecker") == 1)
    check("OptiBond FL ровно 1 раз", deduped.count("OptiBond FL") == 1)
    check("Ирригация гипохлоритом ровно 1 раз (удален дубль в Кейсах)",
          deduped.count("Ирригация гипохлоритом натрия 3%") == 1)
    check("Кейс зуба 3.6 ровно 1 раз", deduped.count("Зуб 3.6") == 1)

    # Edge cases: inline headers
    inline_input = """
Специализация: Терапевт-микроскопист. Терапевт-микроскопист.
Арсенал и оснащение: Микроскоп Leica. Микроскоп Leica.
Клинические протоколы: Адгезивный протокол.
Кейсы: Ревизия каналов 4.6. Микроскоп Leica.
"""
    inline_deduped = user_memory.deduplicate_clinical_summary(inline_input)
    check("Инлайн Специализация ровно 1 раз", inline_deduped.count("Терапевт-микроскопист") == 1)
    check("Инлайн Leica ровно 1 раз", inline_deduped.count("Микроскоп Leica") == 1)
    check("Инлайн Адгезивный протокол ровно 1 раз", inline_deduped.count("Адгезивный протокол") == 1)
    check("Инлайн Кейс 4.6 ровно 1 раз", inline_deduped.count("Ревизия каналов 4.6") == 1)

    # Empty and whitespace
    check("Пустая строка возвращает пустую", user_memory.deduplicate_clinical_summary("") == "")
    check("Пробелы возвращают пустую", user_memory.deduplicate_clinical_summary("   \n\t  ") == "")

    print("\n--- [M1.2] format_users_chunk_context with max_chars budgeting ---")
    await database.init_db()

    # Save 10 doctors with detailed profiles
    for i in range(1, 11):
        await database.save_user_memory(
            user_id=2000 + i,
            specialty=f"Специализация #{i} (хирург-имплантолог)",
            group_summary=f"Врач #{i} выполняет установку имплантатов Straumann и костную пластику в области жевательных зубов.",
            first_name=f"Доктор-{i}",
            username=f"doc_{i}"
        )

    all_uids = [2000 + i for i in range(1, 11)]

    # 1. Default max_chars (2000)
    ctx_default = await user_memory.format_users_chunk_context(all_uids)
    check("Контекст по умолчанию не пуст", len(ctx_default) > 0)
    check("Контекст по умолчанию укладывается в 2000 символов", len(ctx_default) <= 2000, f"got {len(ctx_default)}")
    check("Первый доктор присутствует", "Доктор-1" in ctx_default)

    # 2. Strict max_chars = 650 (allows exactly 2 doctors without breaking sentences)
    ctx_650 = await user_memory.format_users_chunk_context(all_uids, max_chars=650)
    check("Контекст 650 символов строго <= 650", len(ctx_650) <= 650, f"got {len(ctx_650)}")
    check("Доктор-1 есть в 650", "Доктор-1" in ctx_650)
    check("Доктор-2 есть в 650", "Доктор-2" in ctx_650)
    check("Доктор-5 не влез и опущен целиком (нет mid-sentence truncation)", "Доктор-5" not in ctx_650)

    # 3. Small max_chars < header length -> returns empty
    ctx_tiny = await user_memory.format_users_chunk_context(all_uids, max_chars=100)
    check("Слишком малый бюджет отдает пустую строку", ctx_tiny == "")

    # 4. max_chars = None -> includes all
    ctx_unlimited = await user_memory.format_users_chunk_context(all_uids, max_chars=None)
    check("Безлимитный бюджет включает всех 10 врачей", "Доктор-10" in ctx_unlimited)

    print("\n--- [M1.3] reset_pm_memory_cooldown helper ---")
    user_memory._LAST_PM_UPDATE_TS[3001] = time.time()
    user_memory._LAST_PM_UPDATE_TS[3002] = time.time()
    check("Кулдаун установлен для 3001 и 3002",
          3001 in user_memory._LAST_PM_UPDATE_TS and 3002 in user_memory._LAST_PM_UPDATE_TS)

    # Reset single user
    user_memory.reset_pm_memory_cooldown(3001)
    check("Сброс для 3001 удалил только 3001",
          3001 not in user_memory._LAST_PM_UPDATE_TS and 3002 in user_memory._LAST_PM_UPDATE_TS)

    # Reset all users
    user_memory.reset_pm_memory_cooldown()
    check("Сброс без аргументов очистил всех", len(user_memory._LAST_PM_UPDATE_TS) == 0)

    print("\n--- [M1.4] update_clinician_memory_async with LLM deduplication E2E ---")
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "specialty": "Эндодонтист",
        "rewritten_summary": """Специализация:
- Стоматолог-эндодонтист
- Стоматолог-эндодонтист

Арсенал и оснащение:
- Микроскоп Carl Zeiss
- Ультразвук Woodpecker
- Микроскоп Carl Zeiss

Клинические протоколы:
- Спиртовой протокол OptiBond FL
- Спиртовой протокол OptiBond FL

Кейсы:
- Разбор зуба 3.6: гранулема.
- Микроскоп Carl Zeiss
- Разбор зуба 3.6: гранулема.""",
        "new_facts": ["Эндодонтист", "Микроскоп Carl Zeiss"]
    })

    user_memory.generate_gemini_text_async = AsyncMock(return_value=(mock_resp, None))

    # Turn 1: trigger update
    user_memory.reset_pm_memory_cooldown(5001)
    await user_memory.update_clinician_memory_async(
        user_id=5001,
        user_message="Разбираем сложный клинический кейс зуба 3.6 с микроскопом Carl Zeiss",
        bot_response="Рекомендую спиртовой протокол и ультразвуковую обработку.",
        username="doc_endo",
        first_name="Мария"
    )

    saved_mem = await database.get_user_memory(5001)
    check("Специализация обновлена в БД", saved_mem["specialty"] == "Эндодонтист")
    saved_summary = saved_mem["clinical_summary"]
    check("Раздел Специализация сохранен", "Специализация:" in saved_summary)
    check("Раздел Арсенал и оснащение сохранен", "Арсенал и оснащение:" in saved_summary)
    check("Раздел Клинические протоколы сохранен", "Клинические протоколы:" in saved_summary)
    check("Раздел Кейсы сохранен", "Кейсы:" in saved_summary)

    check("Carl Zeiss в БД строго 1 раз", saved_summary.count("Carl Zeiss") == 1)
    check("OptiBond FL в БД строго 1 раз", saved_summary.count("OptiBond FL") == 1)
    check("зуба 3.6 в БД строго 1 раз", saved_summary.count("зуба 3.6") == 1)

    print("\n--- [M1.5] Trivial message & daemon idle sanity ---")
    check("is_trivial_message('ок') == True", user_memory.is_trivial_message("ок"))
    check("is_trivial_message('Спасибо большое, коллега!') == True",
          user_memory.is_trivial_message("Спасибо большое, коллега!"))
    check("is_trivial_message(клинический запрос) == False",
          not user_memory.is_trivial_message("Какой торк выставить при установке импланта Straumann в кость D3?"))

    # Test that idle daemon makes 0 calls
    user_memory.generate_gemini_text_async.reset_mock()
    await user_memory.process_group_memory_daemon_batch(min_new_messages=10, limit=5)
    check("Холостой такт демона делает 0 обращений к LLM",
          user_memory.generate_gemini_text_async.call_count == 0)

    print("\n" + "=" * 62)
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(run_all_m1_tests())
    finally:
        try:
            database._DB_EXECUTOR.shutdown(wait=True)
        except Exception:
            pass
        shutil.rmtree(_TMPDIR, ignore_errors=True)
