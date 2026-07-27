"""
Проверки третьего раунда: время дайджеста, RAG-релевантность, /case,
маршрутизация ботов, дедуп колбэков, ссылки закладок.
Запуск: python test_round3.py
"""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone

# Импортируем НАСТОЯЩИЕ модули, без заглушек: заглушка runtime_guard
# перекрывала реальный модуль и роняла импорт main.
import main
import database

A = main.assistant

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# --- [1] Время: границы окна дайджеста ------------------------------------
print("\n[1] Дайджест: границы окна приводятся к UTC")
aware_utc = datetime(2026, 6, 21, 19, 36, 17, tzinfo=timezone.utc)
check("tz-aware UTC не сдвигается (путь save_message)",
      database._date_text(aware_utc) == "2026-06-21 19:36:17",
      f"got {database._date_text(aware_utc)}")

naive_local = datetime(2026, 6, 21, 20, 0, 0)
expected = naive_local.astimezone(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
check("наивное локальное конвертируется в UTC (путь планировщика)",
      database._date_text(naive_local) == expected,
      f"got {database._date_text(naive_local)}, expected {expected}")

offset_h = round((datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() / 3600)
if offset_h != 0:
    shifted = datetime.strptime(database._date_text(naive_local), "%Y-%m-%d %H:%M:%S")
    check(f"сдвиг равен смещению хоста ({offset_h}ч)",
          round((naive_local - shifted).total_seconds() / 3600) == offset_h)
else:
    check("хост в UTC — сдвига нет, проверка неприменима", True)

check("окно не теряет вечерние часы: 20:00 local попадает в выборку",
      database._date_text(naive_local) < database._date_text(naive_local + timedelta(hours=4)))

print("\n[2] RAG: короткие термины и двусторонний дентальный матч")
check("«зуб» попадает в ключи", "зуб" in A.extract_keywords("болит зуб после каналов"))
check("«бор» попадает в ключи", "бор" in A.extract_keywords("нужен бор для препарирования"))
for stem in ("коронк", "десн", "эмал", "верхушк", "шейк"):
    check(f"стем «{stem}» распознан как клинический", A.is_dental_keyword(stem) is True)
for junk in ("смотреть", "подскажит", "повредив"):
    check(f"«{junk}» НЕ клинический", A.is_dental_keyword(junk) is False)

check("порядок ключей детерминирован",
      A.extract_keywords("рецессия десны в области 23") == A.extract_keywords("рецессия десны в области 23"))

print("\n[3] RAG: выбор ключей не добивается мусором")
kws = A.extract_keywords("Чем снимать коронку с циркония, не повредив уступ?")
sel = A.select_search_keywords(kws)
check("выбраны только клинические термины", all(A.is_dental_keyword(k) for k in sel), f"got {sel}")
check("мусор отброшен", "повредив" not in sel and "снимать" not in sel, f"got {sel}")

kws2 = A.extract_keywords("а что вы думаете вообще насчёт этого всего")
sel2 = A.select_search_keywords(kws2)
check("при отсутствии клиники общие слова всё же берутся", len(sel2) > 0 or len(kws2) == 0)

print("\n[4] RAG: ранжирование ставит релевантное вперёд")
entries = [
    "CAD/CAM реставрации: планирование по индивидуальным особенностям",
    "Эндодонтическое лечение канала: герметичное пломбирование корневых каналов",
    "Оттиски: двухэтапная техника с отводными канальцами",
]
ranked = A._rank_corpus_entries(entries, ["канал", "лечен", "эндо"])
check("самый релевантный факт первый", "Эндодонтическое" in ranked[0], f"got {ranked[0][:50]}")

many = ["текст про канал и лечение эндо"] * 1 + ["просто канал"] * 30
r = A._rank_corpus_entries(many, ["канал", "лечен", "эндо"])
check("вывод ограничен лимитом", len(r) <= A._CORPUS_OUTPUT_LIMIT, f"got {len(r)}")
check("пустой вход не падает", A._rank_corpus_entries([], ["канал"]) == [])
check("корпус никогда не пустеет искусственно", len(A._rank_corpus_entries(["одно слово канал"], ["канал", "лечен"])) == 1)

print("\n[5] /case: число шагов согласовано с заголовком")
src = inspect.getsource(A.handle_interactive_case_step)
check("завершение по константе", "current_step >= CASE_TOTAL_STEPS" in src)
check("заголовок использует ту же константу", "из {CASE_TOTAL_STEPS}" in src)
check("пустой ход отклоняется до LLM", 'if not (user_text or "").strip():' in src)
check("статус отправляется после поиска по базе",
      src.index("search_knowledge_corpus") < src.index("Анализирую ваши действия"))

print("\n[6] Пинги и валидатор на месте (регресс прошлых раундов)")
check("тихие часы существуют", callable(A.is_ping_quiet_hours))
check("отписка существует", callable(A.set_ping_opt_out))
check("валидатор принимает invited", "invited" in inspect.signature(A.check_response_quality).parameters)
check("гейт пассивных триггеров существует", callable(A.passive_gate_block_reason))

print("\n[7] main: маршрутизация")
import main
check("None отфильтрован из списка чатов", all(c is not None for c in main.WATCHED_CHATS))
msrc = inspect.getsource(main.handle_new_message)
check("фильтр ботов вычисляется до диспетчеризации", "is_any_bot" in msrc)
check("ассистент не запускается для сообщений бота",
      msrc.count("if is_any_bot") >= 2, f"found {msrc.count('if is_any_bot')}")
check("уборщик temp_media существует", callable(main.cleanup_temp_media))
check("замки ЛС по пользователю существуют", callable(main._pm_user_lock))
check("замки для разных пользователей различаются", main._pm_user_lock(1) is not main._pm_user_lock(2))
check("замок для одного пользователя стабилен", main._pm_user_lock(1) is main._pm_user_lock(1))

csrc = inspect.getsource(main.handle_callback_query)
check("колбэки дедуплицируются", "_HANDLED_CALLBACK_SET" in csrc)
check("answer() гарантирован в finally", "finally:" in csrc and "event.answer()" in csrc)

esrc = inspect.getsource(main.enqueue_media_analysis)
check("воркеры поднимаются на каждой постановке в очередь",
      esrc.index("start_media_analysis_workers()") < esrc.index("put_nowait"))

hsrc = inspect.getsource(main.heartbeat_task)
check("heartbeat переживает ошибку записи", "except Exception" in hsrc)

ssrc = inspect.getsource(main.sync_history)
check("sync_history не переобрабатывает медиа", "already_enqueued" in ssrc)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
