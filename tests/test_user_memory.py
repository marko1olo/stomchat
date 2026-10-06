"""
Тесты долговременной клинической памяти врача (user_memory.py и database.py).

Проверяет:
1. Создание таблицы user_memories и индексов в database.py.
2. Сохранение и чтение памяти врача (get_user_memory, save_user_memory).
3. Соблюдение строгого лимита в 64 КБ (64 000 символов) на пользователя.
4. Пакетную выборку для чанков группы (get_users_memory_batch, format_users_chunk_context).
5. Форматирование контекстного промпта для ЛС (format_clinician_memory_prompt).
6. Отсечение тривиальных сообщений (is_trivial_message).
7. Обратную совместимость с user_profiles (profile_portrait).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import config
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_test_mem_")
config.DB_PATH = os.path.join(_TMPDIR, "test_mem.db")

import database
import user_memory

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))


async def run_tests():
    print("\n[1] Инициализация БД с таблицей user_memories")
    await database.init_db()
    mem = await database.get_user_memory(123456)
    check("память нового пользователя пуста", mem["user_id"] == 123456 and mem["clinical_summary"] == "")

    print("\n[2] Сохранение и обновление профиля врача")
    facts = ["Ортопед-гнатолог", "Работает в артикуляторе Amann Girrbach", "Препарирование BOPT"]
    await database.save_user_memory(
        user_id=123456,
        specialty="Ортопед",
        clinical_summary="Врач специализируется на тотальных реабилитациях и вертикальном препарировании.",
        facts_json=json.dumps(facts, ensure_ascii=False),
        message_count=5,
        username="doc_ortho",
        first_name="Алексей"
    )

    mem_loaded = await database.get_user_memory(123456)
    check("специализация сохранена", mem_loaded["specialty"] == "Ортопед")
    check("резюме сохранено", "тотальных реабилитациях" in mem_loaded["clinical_summary"])
    check("username сохранен", mem_loaded["username"] == "doc_ortho")
    check("first_name сохранен", mem_loaded["first_name"] == "Алексей")
    check("факты распарсились", len(json.loads(mem_loaded["facts_json"])) == 3)

    print("\n[3] Соблюдение лимита памяти 64 КБ (64 000 символов)")
    huge_text = "А" * 100000  # 100 КБ
    await database.save_user_memory(user_id=999999, clinical_summary=huge_text)
    mem_huge = await database.get_user_memory(999999)
    check("резюме обрезано до 64 000 символов", len(mem_huge["clinical_summary"]) == 64000,
          f"got {len(mem_huge['clinical_summary'])}")

    print("\n[4] Форматирование клинического промпта для ЛС")
    prompt_str = user_memory.format_clinician_memory_prompt(123456, mem_loaded)
    check("промпт содержит специализацию", "Ортопед" in prompt_str)
    check("промпт содержит резюме", "тотальных реабилитациях" in prompt_str)
    check("промпт содержит факты", "Препарирование BOPT" in prompt_str)

    empty_prompt = user_memory.format_clinician_memory_prompt(777, None)
    check("пустой промпт отдает аккуратную заглушку", "формируется" in empty_prompt)

    print("\n[5] Пакетная загрузка для чанка до 20 пользователей в группе")
    for i in range(1, 6):
        await database.save_user_memory(
            user_id=1000 + i,
            specialty=f"Специализация-{i}",
            clinical_summary=f"Опыт врача {i} в клинической практике.",
            username=f"doc_{i}",
            first_name=f"Доктор-{i}"
        )

    batch_ids = [1001, 1002, 1003, 1004, 1005, 9999]
    batch_res = await database.get_users_memory_batch(batch_ids)
    check("пакет вернул 5 сохраненных врачей", len(batch_res) == 5)
    check("врач 1002 на месте", batch_res[1002]["first_name"] == "Доктор-2")

    chunk_ctx = await user_memory.format_users_chunk_context(batch_ids)
    check("контекст чанка сформирован", "ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ" in chunk_ctx)
    check("врач Доктор-1 упомянут", "Доктор-1" in chunk_ctx)
    check("врач Доктор-5 упомянут", "Доктор-5" in chunk_ctx)

    print("\n[6] Проверка тривиальных сообщений (отсечение спама)")
    check("привет тривиален", user_memory.is_trivial_message("привет"))
    check("спасибо тривиально", user_memory.is_trivial_message("Большое спасибо!"))
    check("/help тривиален", user_memory.is_trivial_message("/help"))
    check("короткий текст тривиален", user_memory.is_trivial_message("ок"))
    check("клинический вопрос НЕ тривиален", not user_memory.is_trivial_message(
        "Коллега, подскажи протокол фиксации винира на OptiBond FL при вертикальном препарировании"))

    print("\n[7] Обратная совместимость: fallback на user_profiles")
    await database.set_user_portrait(8888, "Терапевт-микроскопист, перелечивание каналов", 100)
    fb_mem = await user_memory.get_clinician_memory(8888)
    check("память подхватила портрет из profiles", "Терапевт-микроскопист" in fb_mem["clinical_summary"])

    print("\n[8] Память беседы (group_summary) и лимит 8 КБ (8 000 символов)")
    huge_group_text = "Б" * 20000  # 20 КБ
    await database.save_user_memory(
        user_id=123456,
        group_summary=huge_group_text,
        last_group_analyzed_id=500
    )
    mem_grp = await database.get_user_memory(123456)
    check("память беседы обрезана до 8 000 символов", len(mem_grp["group_summary"]) == 8000,
          f"got {len(mem_grp['group_summary'])}")
    check("last_group_analyzed_id сохранен", mem_grp["last_group_analyzed_id"] == 500)

    print("\n[9] Выборка для демона беседы")
    from datetime import datetime, timezone
    now_dt = datetime.now(timezone.utc)
    # Добавляем сообщения в messages
    await database.save_message(
        msg_id=1001,
        sender_id=777001,
        sender_name="Доктор Иван",
        sender_username="doc_ivan",
        text="Коллеги, кто работает с ультразвуком в эндодонтии? Какие насадки берете?",
        date=now_dt
    )
    await database.save_message(
        msg_id=1002,
        sender_id=777001,
        sender_name="Доктор Иван",
        sender_username="doc_ivan",
        text="Я использую Woodpecker, но насадки быстро ломаются при мощности выше 4.",
        date=now_dt
    )
    await database.save_message(
        msg_id=1003,
        sender_id=777001,
        sender_name="Доктор Иван",
        sender_username="doc_ivan",
        text="Особенно на ирригации гипохлоритом 3% с непрерывной подачей.",
        date=now_dt
    )

    unprocessed = await database.get_unprocessed_group_users(min_new_messages=3, limit=5)
    check("демон нашел врача с 3+ сообщениями", any(u["user_id"] == 777001 for u in unprocessed))

    user_msgs = await database.get_user_messages_since(777001, since_msg_id=0, limit=10)
    check("демон извлек все сообщения врача", len(user_msgs) == 3)
    check("текст первого сообщения совпадает", "ультразвуком" in user_msgs[0]["text"])

    print("\n[10] Форматирование контекста с системными комментариями для модели")
    pm_formatted = user_memory.format_clinician_memory_prompt(123456, mem_grp)
    check("ЛС промпт содержит системный комментарий", "Справочная информация для ассистента" in pm_formatted)
    check("ЛС промпт содержит досье", "тотальных реабилитациях" in pm_formatted)

    grp_formatted = await user_memory.format_users_chunk_context([123456])
    check("промпт беседы содержит заголовок беседы", "НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ" in grp_formatted)
    check("промпт беседы содержит системный комментарий", "Справочная информация для ассистента" in grp_formatted)

    print("\n[11] Проверка интервалов: ЛС раз в 4 сообщения, беседа раз в 4 часа (14400 с)")
    check("интервал ЛС равен 4 сообщениям", user_memory.PM_UPDATE_EVERY_N_MESSAGES == 4)
    check("интервал демона беседы равен 4 часам (14400 с)", user_memory.GROUP_MEMORY_DAEMON_INTERVAL == 14400)
    check("демон корректно возвращает max_id и max_msg_id", unprocessed[0].get("max_id") == 1003 and unprocessed[0].get("max_msg_id") == 1003)

    # Проверка: если сообщений не было (last_analyzed = 1003), выборка пустая (нейросеть не дергается)
    await database.save_user_memory(user_id=777001, last_group_analyzed_id=1003)
    unprocessed_empty = await database.get_unprocessed_group_users(min_new_messages=3, limit=5)
    check("если нет новых сообщений, список пуст (нейросеть не вызывается)", len(unprocessed_empty) == 0)

    print("\n" + "=" * 62)
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(run_tests())
    finally:
        try:
            database._DB_EXECUTOR.shutdown(wait=True)
        except Exception:
            pass
        shutil.rmtree(_TMPDIR, ignore_errors=True)
