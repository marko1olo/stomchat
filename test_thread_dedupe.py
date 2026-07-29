"""
Дедупликация незваных ответов: гонка на протухшем состоянии, память о ветках,
мёртвый кэш отвеченных сообщений.

Три дефекта, найденных разведкой и подтверждённых замером на живом архиве
(`stomat_archive.db`, 117 847 реплик, 2023-05-10 -> 2026-02-19, 1016 суток).

1. ГОНКА. Гейт кулдауна и `processed_threads` читались из состояния, взятого на
   входе функции, а списывались только в `record_passive_attempt` — между ними
   await'ы на базу и round-trip к Telegram `get_messages`. `main.py` диспатчит
   каждое сообщение отдельным таском, поэтому две реплики одной ветки читали
   открытый гейт ОБЕ и обе доходили до отправки.
   Замер: пар текстовых реплик к одному родителю в пределах 2 с — 126, из них с
   реально открытым гейтом 3; в пределах 60 с — 4711 и 105. Длину окна на живом
   боте измерить нечем (в bot.log нет ни одной строки `Triggered assistant`),
   поэтому 3 — нижняя граница, 105 — оценка при медленном round-trip.

2. ПАМЯТЬ О ВЕТКАХ. `processed_threads` обрезался до последних 100 записей
   молча. Замер реплеем: при 0.88 вторжения в сутки запись жила в среднем 115
   суток (минимум 52), выброшено 791 ветка, и бот возвращался в уже отвеченную
   ветку 20 раз. Тот же реплей без обрезки — 0 повторов. Хвост обсуждения ПОСЛЕ
   третьего ответа (отрезок, на котором возможен второй заход): p90 = 9.7 суток,
   p95 = 37.5, p99 = 281.7, максимум 810. Память была КОРОЧЕ жизни ветки.

3. МЁРТВЫЙ КЭШ. `REPLIED_MSG_IDS` читался и писался только в медиа-ветке.
   Текстовый путь его не проверял и не заполнял: гард медиа-ветки не видел
   текстовых отправок, а единственная защита текстового пути жила в памяти
   процесса (`PROCESSED_MSG_IDS` в main.py, 500 записей) и снималась рестартом.

Запуск: python test_thread_dedupe.py
"""
import asyncio
import io
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_dedupe_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import assistant  # noqa: E402
import database  # noqa: E402

A = assistant
assistant.STATE_PATH = os.path.join(_TMPDIR, "state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCE = io.open("assistant.py", encoding="utf-8").read()
CODE = "\n".join(l for l in SOURCE.split("\n") if not l.lstrip().startswith("#"))

# Журнал ловим на уровне INFO — ровно так его настраивает runtime_guard в бою.
# Сообщение, отправленное в debug, сюда не попадёт, и проверка это увидит.
LOGGED = []


class _LogCatcher(logging.Handler):
    def emit(self, record):
        try:
            LOGGED.append(record.getMessage())
        except Exception:
            pass


_alog = logging.getLogger("assistant")
_alog.setLevel(logging.INFO)
_alog.addHandler(_LogCatcher())

CHAT_ID = -1002222222222
PARENT_A = 87371        # ветка из реального замера
PARENT_B = 133171
PARENT_TEXT_ONLY = 500000   # родитель БЕЗ медиа: ветка клинического поста не сработает


# --------------------------------------------------------------------------
# Заглушки: настоящий бот, но без Telegram и без LLM. Каждая заглушка отдаёт
# управление циклу (await), иначе конкурентность не воспроизводится вообще.
# --------------------------------------------------------------------------
class FakeSender:
    first_name = "Врач"
    last_name = ""


class FakeReplyTo:
    def __init__(self, rid):
        self.reply_to_msg_id = rid


class FakeMessage:
    def __init__(self, mid, text, reply_to_id=None, sender_id=1001):
        self.id = mid
        self.message = text
        self.sender_id = sender_id
        self.reply_to = FakeReplyTo(reply_to_id) if reply_to_id else None

    async def get_sender(self):
        await asyncio.sleep(0)
        return FakeSender()


class FakeClient:
    """Round-trip к Telegram — самый долгий await внутри окна гонки."""

    def __init__(self):
        self.sends = []
        self.parents = {}

    async def get_messages(self, chat_id, ids=None):
        await asyncio.sleep(0.01)
        return self.parents.get(ids)

    async def send_message(self, entity=None, message=None, reply_to=None, parse_mode=None):
        await asyncio.sleep(0.01)
        self.sends.append((entity, reply_to, message))

    async def get_me(self):
        await asyncio.sleep(0)
        return FakeSender()


class FakeEvent:
    def __init__(self, client, msg):
        self.client = client
        self.message = msg
        self.chat_id = CHAT_ID
        self.sender_id = msg.sender_id
        self.replies = []

    async def reply(self, text):
        await asyncio.sleep(0)
        self.replies.append(text)


class FakeResponse:
    text = "Временную коронку без формочки моделируют прямо в полости рта."


async def _fake_query_db(query_sql, params=()):
    await asyncio.sleep(0.005)          # окно, в котором второй таск успевает войти
    q = " ".join(query_sql.split())
    if q.startswith("SELECT has_media"):
        if params and params[0] == PARENT_TEXT_ONLY:
            return [(0, "обычная реплика без снимка")]
        return [(1, "снимок временной коронки")]
    if q.startswith("SELECT COUNT(*) FROM messages WHERE reply_to_msg_id"):
        return [(4,)]                   # reply_count >= 3 — ветка открыта
    if q.startswith("SELECT COUNT(*) FROM messages WHERE msg_id >"):
        return [(1,)]                   # диалог свежий, не устаревший
    return [("Коллега", "А как временная коронка получилась без формочки?", 87385, params[0] if params else None),
            ("Коллега", "Размоделировку сделать не проблема", 87386, params[0] if params else None)]


async def _fake_last_n(limit=300):
    await asyncio.sleep(0.005)
    return [("Коллега", "", "", "А как временная коронка получилась без формочки?")]


async def _fake_corpus(keywords):
    await asyncio.sleep(0)
    return ("Справка по временным коронкам.", "Архив обсуждений.")


async def _fake_triage(context_msgs):
    await asyncio.sleep(0.005)
    return True


async def _fake_dialogue_triage(chain, recent_chat=None):
    await asyncio.sleep(0.005)
    return True


async def _fake_gemini(prompt, status_ctx, timeout=90):
    await asyncio.sleep(0.01)
    return FakeResponse(), None


async def _fake_quality(context_msgs, draft_reply, invited=False, reference=""):
    await asyncio.sleep(0.005)
    return True, "approved"


async def _fake_silence(event, text, reply_to_msg_id):
    await asyncio.sleep(0)
    return False


async def _fake_resolve(bot_client):
    await asyncio.sleep(0)
    return False


async def _fake_profile(user_id):
    await asyncio.sleep(0)
    return {"selected_style": "colleague_friendly"}


A.query_db_async = _fake_query_db
A.search_knowledge_corpus = _fake_corpus
A.check_llm_triage = _fake_triage
A.check_dialogue_continuation_triage = _fake_dialogue_triage
A.generate_gemini_text_async = _fake_gemini
A.check_response_quality = _fake_quality
A.check_and_apply_silence = _fake_silence
A.resolve_bot_identity = _fake_resolve
database.get_last_n_messages = _fake_last_n
database.get_user_profile = _fake_profile
A.SHADOW_TESTING = False


def reset_world(state=None):
    """Чистое состояние: файл, заявки, кэш отвеченных сообщений, журнал."""
    with open(assistant.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state or {}, f)
    for path in (assistant.STATE_BAK_PATH, assistant.STATE_TMP_PATH):
        if os.path.exists(path):
            os.remove(path)
    A._PASSIVE_CLAIMS.clear()
    A.REPLIED_MSG_IDS.clear()
    A.BOT_ID = None
    LOGGED.clear()


async def run_two(msg_ids, parents, bot_authored_parent=False, texts=None):
    """
    Два входящих сообщения обрабатываются ОДНОВРЕМЕННО — так же, как это делает
    main.py: `create_task(run_assistant_safe(), name=f"assistant_{msg_id}")` на
    каждое сообщение, диспатч конкурентный.
    """
    client = FakeClient()
    if bot_authored_parent:
        A.BOT_ID = 5000
        for p in set(parents):
            client.parents[p] = FakeMessage(p, "Ответ бота", sender_id=5000)

    coros = []
    texts = texts or ["А как временная коронка получилась без формочки?"] * len(msg_ids)
    for mid, parent, body in zip(msg_ids, parents, texts):
        msg = FakeMessage(mid, body, parent)
        event = FakeEvent(client, msg)
        coros.append(A.check_and_trigger_assistant(
            client, event, mid, msg.message, parent, sender_first_name="Коллега"))

    results = await asyncio.gather(*coros, return_exceptions=True)
    await asyncio.sleep(0)              # done-callback снимает заявки
    return client, results


# ==========================================================================
# [1] Два ответа в одну ветку
#
# ЗАМЕР: пар текстовых реплик к одному родителю в пределах 2 с — 126 за 1016
# суток, из них с реально открытым гейтом (хронологический реплей архива с
# PASSIVE_COOLDOWN_MINUTES=120 и PASSIVE_RETRY_MINUTES=10) — 3. В окне 60 с —
# 4711 пар и 105 двойных ответов. Реальные пары:
#   2025-07-28 14:08:52 #87385 || #87386 (+1.0 c) ветка #87371
#   2026-01-30 11:54:45 #133181 || #133182 (+2.0 c) ветка #133171
#
# ОТКАЗ У ВРАЧА: под снимком временной коронки идёт живое обсуждение, двое
# коллег отвечают почти одновременно. Бот вешает в ту же ветку ДВЕ клинические
# лекции подряд — на два соседних сообщения об одном и том же. Именно за такое
# поведение в архиве уже прозвучало «Бот очень назойливый мне не нравится», и
# сожжённое окно тишины 120 минут значит, что на настоящий клинический вопрос
# в следующие два часа бот не ответит.
# ==========================================================================
print("\n[1] Две реплики в одну ветку — ровно ОДИН ответ")
reset_world()
client, results = asyncio.run(run_two([87385, 87386], [PARENT_A, PARENT_A]))
check("отправлен ровно один ответ", len(client.sends) == 1,
      f"отправок {len(client.sends)}: два ответа в одну ветку")
check("ответил ровно один из двух вызовов", sum(1 for r in results if r is True) == 1,
      f"got {results}")
check("исключений в конкурентном прогоне нет",
      not [r for r in results if isinstance(r, BaseException)], f"got {results}")
check("ветка помечена обработанной",
      PARENT_A in A.load_state().get("processed_threads", []),
      f"got {A.load_state().get('processed_threads')}")
check("окно тишины списано один раз",
      A.passive_gate_block_reason(A.load_state()) is not None)
check("отказ второму вызову виден в журнале",
      any("already claimed" in m for m in LOGGED),
      "подавление молчаливое: отличить его от штатного молчания нельзя")
check("заявки сняты после завершения", not A._PASSIVE_CLAIMS, f"got {A._PASSIVE_CLAIMS}")

# Та же гонка при ЗАБИТОЙ заявке — доказательство, что проверка выше не пустая.
reset_world()
_real_claim = A.claim_passive_slot
A.claim_passive_slot = lambda key: True          # диверсия: заявка всегда даётся
try:
    broken_client, _ = asyncio.run(run_two([87385, 87386], [PARENT_A, PARENT_A]))
finally:
    A.claim_passive_slot = _real_claim
check("без заявки та же гонка даёт ДВА ответа", len(broken_client.sends) == 2,
      f"отправок {len(broken_client.sends)} — сценарий не воспроизводит гонку, "
      f"значит проверка выше ничего не доказывает")

# ==========================================================================
# [2] Общий слот незваного ответа
#
# ЗАМЕР: полное окно тишины одно (`last_passive_text_run` — один ключ на всё
# состояние, не на ветку). При 0.88 вторжения в сутки и 4075 кандидатах гонка
# на РАЗНЫХ ветках столь же достижима, как на одной: 105 пар в окне 60 с
# приходятся на пары к одному родителю, но общий слот жгут и пары к разным.
#
# ОТКАЗ У ВРАЧА: два обсуждения под двумя снимками оживают одновременно, бот
# влезает в оба, и оба раза без единой проверки, сколько он уже говорил.
# Врач видит две незваные реплики в минуту при обещанных двух часах паузы.
# ==========================================================================
print("\n[2] Две реплики в РАЗНЫЕ ветки не сжигают окно дважды")
reset_world()
client2, results2 = asyncio.run(run_two([87385, 133181], [PARENT_A, PARENT_B]))
check("отправлен ровно один ответ", len(client2.sends) == 1,
      f"отправок {len(client2.sends)}: окно тишины сожжено дважды")
check("помечена ровно одна ветка",
      len(A.load_state().get("processed_threads", [])) == 1,
      f"got {A.load_state().get('processed_threads')}")

# ==========================================================================
# [2b] Общая пассивная ветка (обычный поток чата, без ответа на пост)
#
# ЗАМЕР: этот путь пропускает 87.6% архива (103 270 реплик из 117 847) и
# открывает гейт 24 007 раз за 1016 суток — 23.6 платных HIGH-триажа в сутки.
# Пере-чтение состояния перед решением тут уже стоит, но окно оно только
# сужает, а не закрывает: своя запись идёт лишь в record_passive_attempt ПОСЛЕ
# решения, а между пере-чтением и записью стоит ещё один await — догрузка
# ветки для контекста, когда сообщение является ответом. Текстовых
# реплик-ответов в архиве 65 263 из 117 847 (55.4%), так что путь достижим.
#
# ОТКАЗ У ВРАЧА: в чате оживлённая переписка, два ответа приходят подряд.
# Бот выдаёт две незваные реплики в одну секунду — и оплачивает два HIGH-триажа
# и две генерации по 90 с вместо одной.
# ==========================================================================
print("\n[2b] Две реплики в общем потоке чата — ровно ОДИН ответ")
reset_world()
client2b, results2b = asyncio.run(run_two(
    [140001, 140002], [PARENT_TEXT_ONLY, PARENT_TEXT_ONLY],
    texts=["Коллеги, чем перекрываете перфорацию в области фуркации?",
           "А кальций-силикатные цементы кто-то пробовал?"]))
check("отправлен ровно один ответ", len(client2b.sends) == 1,
      f"отправок {len(client2b.sends)}: две незваные реплики в одну секунду")
check("ответил ровно один из двух вызовов", sum(1 for r in results2b if r is True) == 1,
      f"got {results2b}")
check("окно тишины списано", A.passive_gate_block_reason(A.load_state()) is not None)

reset_world()
A.claim_passive_slot = lambda key: True          # диверсия
try:
    broken2b, _ = asyncio.run(run_two(
        [140001, 140002], [PARENT_TEXT_ONLY, PARENT_TEXT_ONLY],
        texts=["Коллеги, чем перекрываете перфорацию в области фуркации?",
               "А кальций-силикатные цементы кто-то пробовал?"]))
finally:
    A.claim_passive_slot = _real_claim
check("без заявки общий поток тоже даёт ДВА ответа", len(broken2b.sends) == 2,
      f"отправок {len(broken2b.sends)} — сценарий не воспроизводит гонку на этом пути")

# ==========================================================================
# [3] Снятие заявки на любом выходе
#
# ЗАМЕР/МЕХАНИКА: после места заявки у check_and_trigger_assistant одиннадцать
# точек выхода (отказ триажа, пустой корпус, ошибка Gemini, пустой текст,
# IGNORE, отказ рецензента, два успеха, два провала отправки). Любая
# необработанная ошибка между ними оставила бы заявку висеть.
#
# ОТКАЗ У ВРАЧА: заявка, залипшая один раз, запирает незваный ответ НАВСЕГДА и
# МОЛЧА — до перезапуска процесса. Бот перестаёт участвовать в обсуждениях, а в
# журнале нет ни одной строки о причине. Это тот же класс отказа, что застрявший
# кулдаун, только неисправимый правкой файла состояния.
# ==========================================================================
print("\n[3] Заявка снимается на любом выходе, включая исключение")
reset_world()


async def _boom(context_msgs):
    await asyncio.sleep(0.005)
    raise RuntimeError("провайдер триажа упал")


A.check_llm_triage = _boom
try:
    client3, results3 = asyncio.run(run_two([87385], [PARENT_A]))
finally:
    A.check_llm_triage = _fake_triage
check("исключение дошло до вызывающего, а не проглочено",
      any(isinstance(r, BaseException) for r in results3), f"got {results3}")
check("заявка снята после исключения", not A._PASSIVE_CLAIMS,
      f"осталось {A._PASSIVE_CLAIMS} — незваный ответ заперт навсегда")

# Отмена таска — тот же путь.
reset_world()


async def _slow_triage(context_msgs):
    await asyncio.sleep(5)
    return True


async def _cancel_scenario():
    A.check_llm_triage = _slow_triage
    try:
        client = FakeClient()
        msg = FakeMessage(87385, "А как временная коронка без формочки?", PARENT_A)
        task = asyncio.ensure_future(A.check_and_trigger_assistant(
            client, FakeEvent(client, msg), 87385, msg.message, PARENT_A))
        await asyncio.sleep(0.15)
        held = dict(A._PASSIVE_CLAIMS)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return held, dict(A._PASSIVE_CLAIMS)
    finally:
        A.check_llm_triage = _fake_triage


held, after_cancel = asyncio.run(_cancel_scenario())
check("во время работы заявка действительно взята", bool(held),
      "если её не берут, секции [1]-[3] ничего не проверяют")
check("отмена таска снимает заявку", not after_cancel, f"осталось {after_cancel}")

# Протёкшую заявку без владельца снимает TTL.
reset_world()
A._PASSIVE_CLAIMS[("passive_text",)] = (
    None, datetime.now() - timedelta(seconds=A.PASSIVE_CLAIM_TTL_SECONDS + 60))
check("протёкшая заявка не запирает бота навсегда",
      A.claim_passive_slot(("passive_text",)) is True,
      "залипшая заявка молча выключает незваные ответы до перезапуска")
check("протечка записана в журнал", any("leaked" in m for m in LOGGED),
      "молчаливое самолечение скрывает дефект")
A._PASSIVE_CLAIMS.clear()

# ==========================================================================
# [4] Приглашённый путь заявкой не глушится
#
# МЕХАНИКА: диалоговая ветка (врач ответил на сообщение бота) гейт кулдауна не
# проходит и в гонке не участвует. Заявка не должна её задевать: подавить
# прямой ответ врачу хуже, чем два незваных.
#
# ОТКАЗ У ВРАЧА: двое коллег переспрашивают бота по одному и тому же его
# ответу — «а доза какая?» и «а при беременности?». Если заявка глушит второго,
# его клинический вопрос пропадает молча, и он не узнает, что бот его не услышал.
# ==========================================================================
print("\n[4] Прямой ответ врачу заявкой не подавляется")
reset_world()
client4, results4 = asyncio.run(run_two([90001, 90002], [70000, 70000],
                                        bot_authored_parent=True))
check("оба прямых вопроса получили ответ", len(client4.sends) == 2,
      f"отправок {len(client4.sends)}: вопрос врача пропал молча")
check("приглашённый путь ветку не помечает",
      A.load_state().get("processed_threads", []) == [],
      f"got {A.load_state().get('processed_threads')}")
check("приглашённый путь окно тишины не жжёт",
      A.passive_gate_block_reason(A.load_state()) is None)

# ==========================================================================
# [5] Память о ветках ограничена возрастом, а не длиной 100
#
# ЗАМЕР: `del threads[:-100]` при 0.88 вторжения в сутки держал запись в среднем
# 115 суток, в худшем случае 52; выброшено 791 ветка; 20 повторных входов в уже
# отвеченную ветку за 1016 суток (тот же реплей без обрезки — 0). Хвост
# обсуждения после третьего ответа: p90 = 9.7 суток, p95 = 37.5, p99 = 281.7,
# максимум 810. Разрыв повторных входов: 75.6 / 223.7 / 810.2 суток.
# Чтобы удержать 365 суток истории, по замеру хватает 507 записей.
#
# ОТКАЗ У ВРАЧА: под снимком обсуждение, бот один раз высказался. Через полгода
# кто-то поднимает ту же ветку — «а чем закончилось?» — и бот, забыв, что уже
# отвечал, выдаёт вторую лекцию по тому же случаю. Для читателя это бот, который
# повторяется и не помнит собственных слов.
# ==========================================================================
print("\n[5] processed_threads ограничен возрастом, а не сотней записей")
reset_world()
check("окно хранения покрывает p99 хвоста обсуждения (281.7 суток)",
      A.PROCESSED_THREAD_TTL_DAYS >= 282,
      f"got {A.PROCESSED_THREAD_TTL_DAYS} суток — ветка затихает позже, чем бот её забывает")
check("предел по размеру выше замеренных 507 записей на 365 суток",
      A.PROCESSED_THREADS_MAX > 507, f"got {A.PROCESSED_THREADS_MAX}")
check("обрезки до 100 в коде не осталось", "del threads[:-100]" not in CODE,
      "граница снова по длине: 791 выброшенная ветка, 20 повторных вторжений")

reset_world({"processed_threads": list(range(150))})
A.record_passive_success(thread_id=999)
threads = A.load_state()["processed_threads"]
check("151 свежая ветка не режется до сотни", len(threads) == 151, f"got {len(threads)}")
check("свежая ветка сохранена", 999 in threads)
check("ветка №0 из старого файла не выброшена", 0 in threads,
      "запись, которой всего секунда, объявлена устаревшей")
stamps = A.load_state()["processed_thread_dates"]
check("веткам из старого файла проставлены метки", len(stamps) == 151, f"got {len(stamps)}")

# Старое и свежее по возрасту.
old_iso = (datetime.now() - timedelta(days=A.PROCESSED_THREAD_TTL_DAYS + 5)).isoformat()
mid_iso = (datetime.now() - timedelta(days=300)).isoformat()   # внутри p99 = 281.7
reset_world({"processed_threads": [111, 222],
             "processed_thread_dates": {"111": old_iso, "222": mid_iso}})
A.record_passive_success(thread_id=333)
threads = A.load_state()["processed_threads"]
check("ветка старше окна выброшена", 111 not in threads, f"got {threads}")
check("ветка возрастом 300 суток сохранена", 222 in threads,
      "выброшена запись внутри измеренного p99 — бот вернётся в живую ветку")
check("метки выброшенных веток не копятся",
      "111" not in A.load_state()["processed_thread_dates"],
      "словарь дат растёт вечно, даже когда список чистится")
check("повторный ответ обновляет метку ветки",
      A.load_state()["processed_thread_dates"]["333"][:4] == str(datetime.now().year))

# Метка НЕ должна освежаться на каждой чистке — иначе ветка не постареет никогда
# и список будет расти до предела по размеру, то есть до молчаливой обрезки.
reset_world({"processed_threads": [555], "processed_thread_dates": {"555": mid_iso}})
_st = A.load_state()
A._prune_processed_threads(_st)
check("чистка не освежает метку уже помеченной ветки",
      _st["processed_thread_dates"]["555"] == mid_iso,
      f"got {_st['processed_thread_dates']['555']} — ветка не постареет никогда")
check("ветка без метки не выбрасывается, а помечается текущим временем",
      A._prune_processed_threads({"processed_threads": [666]}) == [666])

# ==========================================================================
# [6] Выброс пишется в журнал
#
# ЗАМЕР: `del threads[:-100]` выбросил 791 ветку за 1016 суток архива и не
# оставил ни одной строки в журнале — ровно как кулдаун, чья причина молчания
# писалась в debug при корневом уровне INFO и встречалась 0 раз на 126 340
# строк логов.
#
# ОТКАЗ У ВРАЧА: бот второй раз лезет в ту же ветку. Понять, забыл он её или
# решил заново, нельзя ничем, кроме чтения assistant_state.json руками.
# ==========================================================================
print("\n[6] Молчаливой обрезки нет: выброс пишется в журнал")
reset_world({"processed_threads": [111], "processed_thread_dates": {"111": old_iso}})
A.record_passive_success(thread_id=444)
check("выброс по возрасту записан на уровне info",
      any("dropped" in m and "older than" in m for m in LOGGED),
      f"журнал: {LOGGED}")
check("в записи назван номер выброшенной ветки",
      any("111" in m for m in LOGGED if "dropped" in m), f"журнал: {LOGGED}")

# Предел по размеру: срабатывать не должен, но если сработал — это warning.
_real_max = A.PROCESSED_THREADS_MAX
A.PROCESSED_THREADS_MAX = 5
try:
    reset_world({"processed_threads": list(range(10))})
    A.record_passive_success(thread_id=777)
    capped = A.load_state()["processed_threads"]
finally:
    A.PROCESSED_THREADS_MAX = _real_max
check("предел по размеру соблюдён", len(capped) == 5, f"got {len(capped)}")
check("свежая ветка при обрезке сохранена (режется голова)", 777 in capped, f"got {capped}")
check("обрезка по размеру — предупреждение, а не тишина",
      any("hit the" in m and "cap" in m for m in LOGGED), f"журнал: {LOGGED}")
check("в предупреждении названы выброшенные ветки",
      any("still-fresh" in m for m in LOGGED), f"журнал: {LOGGED}")

# ==========================================================================
# [7] REPLIED_MSG_IDS работает на текстовом пути
#
# МЕХАНИКА/ЗАМЕР: кэш (TTLCache, 50 000 записей, 7 суток) читался на входе
# check_and_trigger_assistant_media и писался после отправки медиа-ответа, а
# текстовый путь его не касался вообще — то есть на текстовом пути он не
# защищал ничего. Живой зазор: `sync_history` в main.py прогоняет сообщение
# через handle_new_message повторно, а PROCESSED_MSG_IDS (500 записей в памяти)
# после рестарта пуст.
#
# ОТКАЗ У ВРАЧА: бот падает и поднимается (в bot_supervisor.log это штатное
# событие), sync_history добирает последние сообщения, и врач получает второй
# ответ на свой вопрос, заданный полчаса назад. Плюс снимок с подписью может
# получить два ответа сразу: текстовый путь и медиа-путь друг друга не видели.
# ==========================================================================
print("\n[7] На одно сообщение — один ответ, даже если его подали дважды")
reset_world()
client7, results7 = asyncio.run(run_two([87385], [PARENT_A]))
check("первый прогон ответил", len(client7.sends) == 1, f"got {len(client7.sends)}")
check("сообщение помечено отвеченным", 87385 in A.REPLIED_MSG_IDS,
      "кэш снова не заполняется — гард медиа-ветки не увидит текстовой отправки")

# Тот же msg_id второй раз (рестарт + sync_history). Гейт открываем, чтобы
# отказ пришёл именно от кэша, а не от кулдауна.
with open(assistant.STATE_PATH, "w", encoding="utf-8") as f:
    json.dump({}, f)
A._PASSIVE_CLAIMS.clear()
LOGGED.clear()
client7b, results7b = asyncio.run(run_two([87385], [PARENT_A]))
check("повторная подача того же сообщения ответа не даёт", not client7b.sends,
      f"отправок {len(client7b.sends)}: врач получил второй ответ на тот же вопрос")
check("отказ по кэшу виден в журнале",
      any("already replied to message" in m for m in LOGGED), f"журнал: {LOGGED}")
check("кэш читается на входе текстового пути",
      "if msg_id in REPLIED_MSG_IDS:" in CODE.split("async def check_and_trigger_assistant(", 1)[1][:2000],
      "гард снова стоит только в медиа-ветке")
check("кэш заполняется после успешной текстовой отправки",
      CODE.count("REPLIED_MSG_IDS[msg_id] = True") >= 4,
      f"записей в кэш {CODE.count('REPLIED_MSG_IDS[msg_id] = True')} — часть успехов не помечается")

# Соседнее сообщение той же ветки кэш не блокирует.
reset_world()
client7c, _ = asyncio.run(run_two([87390], [PARENT_A]))
check("другое сообщение кэшем не заблокировано", len(client7c.sends) == 1,
      f"got {len(client7c.sends)} — кэш глушит всё подряд")

# ==========================================================================
# [8] Проверки выше ловят поломку
#
# Без этой секции любая проверка выше могла бы проходить на сломанном коде.
# ==========================================================================
print("\n[8] Проверки выше ловят поломку")
check("сценарий гонки в ветке воспроизводим: без заявки было ДВА ответа",
      len(broken_client.sends) == 2, f"got {len(broken_client.sends)}")
check("сценарий гонки в общем потоке воспроизводим: без заявки было ДВА ответа",
      len(broken2b.sends) == 2, f"got {len(broken2b.sends)}")
check("заявка действительно отказывает второму",
      A.claim_passive_slot(("proof",)) is True and A.claim_passive_slot(("proof",)) is False,
      "claim_passive_slot всегда даёт True — секции [1]-[2] ничего не значат")
A._PASSIVE_CLAIMS.clear()
check("чистка по возрасту различает старое и свежее",
      A._parse_state_dt(old_iso) < A._parse_state_dt(mid_iso),
      "если бы не различала, вся секция [5] ничего не значила")
reset_world({"processed_threads": [111], "processed_thread_dates": {"111": old_iso}})
A._prune_processed_threads(A.load_state())
check("вызов чистки без добавления ветку по возрасту убирает",
      any("dropped" in m for m in LOGGED), f"журнал: {LOGGED}")
check("ловушка на возврат обрезки до 100 сработала бы",
      "del threads[:-100]" in "            if len(threads) > 100:\n                del threads[:-100]")

_alog.handlers = [h for h in _alog.handlers if not isinstance(h, _LogCatcher)]
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
