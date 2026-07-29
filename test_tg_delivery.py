"""
Устойчивость доставки: три места, где врач НЕ получает ответ и не знает почему.

Замер по журналам: `Server closed the connection` — 51542 строки в bot.log.1 и
181 в bot.log, всего 51723. Доминирующий отказ здесь обрыв связи, а не flood.
При этом клиент настроен как timeout=30, request_retries=10, retry_delay=5,
flood_sleep_threshold=20 (main.py:883, 900-910), то есть ОДИН await в Telegram
без границы расходует до 10 x (30 + 20) = 500 с. Родительского срока на этих
путях нет: runtime_guard.create_task — голый asyncio.create_task.

Д1. Готовая сводка группы уходила врачу единственным edit_message без срока.
    Последствие: сводка сгенерирована (и оплачена), врач держит перед собой
    «Собираю и анализирую... Подождите», а сводка не уходит НИКОМУ, и в журнале
    об этом ни строки — зависание не исключение, except его не ловит.

Д2. Два get_me() на пути снимка стояли под пустым `except Exception: pass`.
    Последствие: при обрыве оба флага оставались False, прямое обращение врача
    считалось пассивным и попадало под 2-часовой кулдаун — снимок отбрасывался
    молча, и врач получал наказание за вопрос, который бот не разобрал.

Д3. Десять edit_message в handle_quiz_callback без срока. Спиннер на кнопке
    снимает event.answer() строкой НИЖЕ правки. Страховка в main.py:2315 ловит
    только исключение; при зависании await не возвращается, finally не
    наступает, и кнопка у врача крутится, пока он не нажмёт снова.

Проверки поведенческие: измеряется возвращаемое значение, факт вызова и запись в
журнале. Сроки на время прогона подменяются малыми числами — проверяется
механизм границы, а сами числа и их вложенность проверяются отдельно.

Тайминги берутся МИНИМУМОМ из трёх прогонов: под параллельной нагрузкой
среднее врёт.

Запуск: python test_tg_delivery.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_tgdeliv_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import config  # noqa: E402

config.DB_PATH = os.path.join(_TMPDIR, "test.db")

import assistant  # noqa: E402
import database  # noqa: E402
import tg_safety  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# --- Перехват журнала: «отказ обязан быть слышен» проверяется по записям ------
class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        try:
            self.records.append(record.getMessage())
        except Exception:
            self.records.append("<не отформатировалось>")

    def clear(self):
        self.records = []

    def has(self, *needles):
        return any(all(n in msg for n in needles) for msg in self.records)


CAP = Capture()
assistant.logger.addHandler(CAP)
assistant.logger.setLevel(logging.DEBUG)

# Сроки на прогоне — малые, чтобы гонять реальные корутины за доли секунды.
# Настоящие числа и их вложенность проверяет блок [7].
TEST_BUDGET = 0.3
# Предохранитель от вечного теста. Без границы обработчик не вернётся НИКОГДА,
# поэтому запас здесь щедрый: в зелёном случае прогон занимает доли секунды, а
# платит этим числом только уже сломанный код. Восемь секунд оказалось мало —
# под параллельной нагрузкой прогон перешагивал их и тест флакал.
HANG_LIMIT = 20.0


class HangingBot:
    """Telegram, который принял запрос и не отвечает — тот самый обрыв связи."""

    def __init__(self, hang=30.0):
        self.hang = hang
        self.edits = 0
        self.sent = []

    async def send_message(self, entity=None, message=None, reply_to=None,
                           parse_mode=None, **kw):
        self.sent.append(message)
        return type("M", (), {"id": 999})()

    async def edit_message(self, chat_id, msg_id, message, **kw):
        self.edits += 1
        await asyncio.sleep(self.hang)
        return type("M", (), {"id": msg_id})()

    async def get_me(self):
        raise ConnectionError("Server closed the connection")

    async def get_messages(self, chat_id, ids=None, **kw):
        return None


class WorkingBot:
    """Обычный день: Telegram отвечает."""

    def __init__(self):
        self.edited = []
        self.sent = []

    async def send_message(self, entity=None, message=None, reply_to=None,
                           parse_mode=None, **kw):
        self.sent.append(message)
        return type("M", (), {"id": 999})()

    async def edit_message(self, chat_id, msg_id, message, **kw):
        self.edited.append(message)
        return type("M", (), {"id": msg_id})()

    async def get_me(self):
        raise ConnectionError("Server closed the connection")

    async def get_messages(self, chat_id, ids=None, **kw):
        return None


class Event:
    def __init__(self, chat_id=-1001234567890, sender_id=555, msg_id=100500):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message = type("M", (), {"id": msg_id})()


async def timed(coro_factory, limit=HANG_LIMIT):
    """
    Выполнить с предохранителем. Возвращает (завершилось, секунды).

    Без границы в продакшн-коде обработчик не вернётся никогда — предохранитель
    отличает «граница сработала» от «тест повис».
    """
    started = time.monotonic()
    try:
        await asyncio.wait_for(coro_factory(), timeout=limit)
        return True, time.monotonic() - started
    except asyncio.TimeoutError:
        return False, time.monotonic() - started


def secs(value):
    """
    Время для пояснения к отказу. None означает «не вернулся ни разу».

    Без этой обёртки f"{elapsed:.3f}" на None роняет ВЕСЬ набор с TypeError
    вместо честного FAIL — поймано собственной диверсией: упавший набор не
    показывает ни одной последующей проверки.
    """
    return "не вернулся" if value is None else f"{value:.3f} с"


async def fastest(coro_factory, runs=3, limit=HANG_LIMIT):
    """
    ЛУЧШИЙ из N прогонов: и по времени, и по факту возврата.

    Именно лучший, а не «все N». Требование «завершились все три» — это уже не
    минимум, а И по трём замерам, и оно флакует: под параллельной нагрузкой один
    прогон из трёх перешагивал предохранитель, и проверка падала на здоровом
    коде. Различающая сила при этом не теряется: без границы не возвращается НИ
    ОДИН прогон (проверено диверсией — 0 возвратов из 3).
    """
    best = None
    any_finished = False
    for _ in range(runs):
        tg_safety.reset_cooldowns()
        done, elapsed = await timed(coro_factory, limit=limit)
        any_finished = any_finished or done
        if done:
            best = elapsed if best is None else min(best, elapsed)
    return any_finished, best


async def survives(coro):
    """
    Прогнать и вернуть (не упало, текст исключения).

    Обработчик, свалившийся с исключением, роняет ВЕСЬ набор и прячет все
    следующие проверки — так уже было в этом наборе с f"{None:.3f}". Диверсия
    скептика S4 (снят `and BOT_USERNAME`) показала то же на продакшн-коде:
    re.escape(None) даёт TypeError, и вместо честного FAIL набор просто
    обрывался. Здесь падение становится обычным провалом одной проверки.
    """
    try:
        await coro
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# --- Заглушки окружения ------------------------------------------------------
ROWS = [(1000 + i, f"Врач{i}", None, f"обсуждаем уступ и границу препарирования {i}")
        for i in range(12)]


async def fake_rows(*a, **kw):
    return ROWS


async def fake_llm(prompt, status_ctx=None, timeout=None):
    return type("R", (), {"text": "<b>Суть:</b> спор о границе препарирования."})(), None


async def fake_llm_fails(prompt, status_ctx=None, timeout=None):
    return None, "все модели недоступны"


assistant.generate_gemini_text_async = fake_llm
database.get_last_n_messages = fake_rows
database.get_messages_from = fake_rows
assistant.check_user_cooldown = lambda *a, **kw: 0


async def run():
    print("\n[1] Д1: зависший Telegram не съедает готовую сводку молча")
    real_budget = assistant.SUMMARY_DELIVERY_TIMEOUT_SECONDS
    assistant.SUMMARY_DELIVERY_TIMEOUT_SECONDS = TEST_BUDGET
    try:
        bot = HangingBot()
        CAP.clear()
        done, elapsed = await fastest(
            lambda: assistant.handle_group_summary(HangingBot(), Event(), None))
        check("обработчик сводки ВЕРНУЛСЯ, а не завис навсегда", done,
              f"не вернулся за {HANG_LIMIT} с — граница не сработала")
        check("граница отработала не мгновенно (бюджет реально выждан)",
              elapsed is not None and elapsed >= TEST_BUDGET * 0.5,
              f"вернулся за {secs(elapsed)} при бюджете {TEST_BUDGET} с")
        check("ожидание врача ограничено бюджетом, а не 500 с telethon",
              elapsed is not None and elapsed < TEST_BUDGET + 4.0,
              f"ушло {secs(elapsed)}")
        check("потеря сводки ЗАПИСАНА в журнал (врач не остался без следа)",
              CAP.has("НЕ доставлена"),
              "в журнале нет строки о потерянной сводке")
        check("в журнале названа причина отказа Telegram",
              CAP.has("tg give up", "group_summary"),
              "tg_safety не сообщил причину")
        check("сообщение о потере называет причину машинно-читаемо",
              CAP.has("timeout"), "причина отказа не названа")
        # Строка «tg give up ... timeout» — это запись tg_safety, а не наша:
        # вырезать диагностику из WARNING ассистента можно было незаметно
        # (диверсия скептика S5 прошла зелёной). По журналу врач и лид должны
        # понять, СКОЛЬКО реплик разбора потеряно, иначе потерю сводки не
        # отличить от сводки, которой не было.
        check("в записи о потере назван объём потерянного разбора",
              CAP.has("НЕ доставлена", f"{len(ROWS)} реплик")
              and CAP.has("НЕ доставлена", "символов"),
              "WARNING о потере не называет ни числа реплик, ни длины текста")
        check("в записи о потере назван отказ Telegram, а не только факт",
              CAP.has("НЕ доставлена", "timeout"),
              "причина отказа осталась только в записи tg_safety")
        # Главный инвариант журнала для Д1: потерянная сводка НЕ ИМЕЕТ ПРАВА
        # значиться доставленной. Снять `return` после WARNING удавалось
        # незаметно (диверсия скептика S2 прошла зелёной) — и тогда журнал
        # утверждает «Successfully posted» о сводке, которой врач не видел, то
        # есть врёт ровно там, где Д1 обещал перестать молчать.
        check("потерянная сводка НЕ записана как успешно доставленная",
              not CAP.has("Successfully posted group summary"),
              "журнал сообщает об успехе доставки, которой не было")

        print("\n[2] Д1: в обычный день сводка по-прежнему доходит")
        good = WorkingBot()
        CAP.clear()
        assistant.SUMMARY_DELIVERY_TIMEOUT_SECONDS = real_budget
        done, _ = await timed(
            lambda: assistant.handle_group_summary(good, Event(), None))
        check("обработчик завершился", done)
        check("сводка ушла врачу", len(good.edited) == 1, f"got {len(good.edited)}")
        check("в сводке есть текст модели",
              good.edited and "границе препарирования" in good.edited[0],
              f"got {good.edited[0][:80] if good.edited else None}")
        check("успех отмечен в журнале", CAP.has("Successfully posted group summary"))
        check("ложной тревоги о потере НЕТ", not CAP.has("НЕ доставлена"),
              "успешная доставка записана как потеря")

        print("\n[3] Д1: отказ генерации тоже доходит до врача под сроком")
        assistant.generate_gemini_text_async = fake_llm_fails
        good2 = WorkingBot()
        CAP.clear()
        done, _ = await timed(
            lambda: assistant.handle_group_summary(good2, Event(), None))
        check("обработчик завершился", done)
        check("врач получил отказ, а не тишину",
              good2.edited and "Ошибка генерации" in good2.edited[0],
              f"got {good2.edited}")

        assistant.SUMMARY_DELIVERY_TIMEOUT_SECONDS = TEST_BUDGET
        hang2 = HangingBot()
        CAP.clear()
        done, elapsed = await timed(
            lambda: assistant.handle_group_summary(hang2, Event(), None))
        check("зависание на СЛУЖЕБНОМ сообщении тоже ограничено", done,
              "задача повисла на попытке сказать врачу об отказе")
        assistant.generate_gemini_text_async = fake_llm
    finally:
        assistant.SUMMARY_DELIVERY_TIMEOUT_SECONDS = real_budget

    print("\n[4] Д2: снимок врача разбирается, даже когда get_me отказал")
    # Пассивный кулдаун закрыт: если прямое обращение не опознано, снимок будет
    # отброшен молча — ровно то наказание, которое получал врач.
    blocked_state = {"last_passive_media_run": datetime.now().isoformat()}
    real_load, real_save = assistant.load_state, assistant.save_state
    real_search = assistant.search_knowledge_corpus
    real_id, real_name = assistant.BOT_ID, assistant.BOT_USERNAME
    searched = {"n": 0}

    async def fake_search(keywords):
        searched["n"] += 1
        return "", ""

    class Msg:
        def __init__(self, reply_to=None, chat_id=-1001234567890):
            self.reply_to_msg_id = reply_to
            self.chat_id = chat_id
            self.client = None
            self.id = 777

    class ReplyBot(WorkingBot):
        """Отвечает на get_messages сообщением БОТА, но get_me у него мёртв."""

        def __init__(self, parent_sender):
            super().__init__()
            self.parent_sender = parent_sender
            self.get_me_calls = 0

        async def get_me(self):
            self.get_me_calls += 1
            raise ConnectionError("Server closed the connection")

        async def get_messages(self, chat_id, ids=None, **kw):
            return type("P", (), {"sender_id": self.parent_sender})()

    assistant.load_state = lambda: dict(blocked_state)
    assistant.save_state = lambda state: None
    assistant.search_knowledge_corpus = fake_search
    assistant.generate_gemini_text_async = fake_llm_fails
    assistant.BOT_ID = 4242
    assistant.BOT_USERNAME = "stomchat_bot"
    try:
        bot = ReplyBot(parent_sender=4242)
        searched["n"] = 0
        CAP.clear()
        assistant.REPLIED_MSG_IDS.clear()
        await assistant.check_and_trigger_assistant_media(
            bot, Msg(reply_to=555), 777, "Что тут с уступом?", "снимок зуба")
        check("ответ боту опознан по BOT_ID — снимок пошёл в разбор",
              searched["n"] == 1,
              "снимок отброшен под 2-часовой кулдаун, врач наказан за вопрос")
        check("get_me на горячем пути НЕ вызывается вовсе",
              bot.get_me_calls == 0, f"вызван {bot.get_me_calls} раз")

        print("\n[5] Д2: обращение по @имени опознаётся без сети")
        bot2 = ReplyBot(parent_sender=1)
        searched["n"] = 0
        assistant.REPLIED_MSG_IDS.clear()
        await assistant.check_and_trigger_assistant_media(
            bot2, Msg(reply_to=None), 778, "@stomchat_bot глянь снимок, что с уступом?",
            "снимок зуба")
        check("упоминание по @имени опознано — снимок пошёл в разбор",
              searched["n"] == 1, "обращение по имени принято за пассивное")
        check("get_me не вызывался и здесь", bot2.get_me_calls == 0,
              f"вызван {bot2.get_me_calls} раз")

        # Пассивный снимок при закрытом кулдауне обязан по-прежнему отбрасываться:
        # иначе правка сняла бы защиту от болтливости, а не починила опознание.
        bot3 = ReplyBot(parent_sender=1)
        searched["n"] = 0
        assistant.REPLIED_MSG_IDS.clear()
        await assistant.check_and_trigger_assistant_media(
            bot3, Msg(reply_to=None), 779, "просто фото кофе", "чашка кофе")
        check("пассивный снимок в кулдауне по-прежнему пропускается",
              searched["n"] == 0, "снят пассивный кулдаун — бот станет болтливым")

        # Чужой аккаунт с нашим именем в начале — не обращение к нам. Тот же
        # класс, что «рот» внутри «оборот»: совпадение подстрокой.
        bot4 = ReplyBot(parent_sender=1)
        searched["n"] = 0
        assistant.REPLIED_MSG_IDS.clear()
        await assistant.check_and_trigger_assistant_media(
            bot4, Msg(reply_to=None), 781,
            "@stomchat_bot_old глянь снимок, что с уступом?", "снимок зуба")
        check("@имя_другого_аккаунта не считается обращением к нам",
              searched["n"] == 0,
              "разобрали снимок, которого у нас не просили — совпадение подстрокой")

        # А имя с пунктуацией сразу после — обращение: граница слова, не пробел.
        bot5 = ReplyBot(parent_sender=1)
        searched["n"] = 0
        assistant.REPLIED_MSG_IDS.clear()
        await assistant.check_and_trigger_assistant_media(
            bot5, Msg(reply_to=None), 782,
            "@stomchat_bot, что с уступом?", "снимок зуба")
        check("обращение с запятой сразу после имени опознано",
              searched["n"] == 1, "граница слова принята за пробел")

        print("\n[6] Д2: отказ запроса родителя ЗВУЧИТ, а не глотается")

        class DeafBot(ReplyBot):
            async def get_messages(self, chat_id, ids=None, **kw):
                raise ConnectionError("Server closed the connection")

        deaf = DeafBot(parent_sender=4242)
        CAP.clear()
        searched["n"] = 0
        assistant.REPLIED_MSG_IDS.clear()
        # Раньше здесь стояло check(..., True) — условие, которое не может быть
        # ложным. Падение обработчика роняло весь набор, и проверка про «не
        # упал» ничего не проверяла: она либо не выполнялась вовсе, либо
        # проходила всегда.
        alive, why = await survives(assistant.check_and_trigger_assistant_media(
            deaf, Msg(reply_to=555), 780, "Что тут с уступом?", "снимок зуба"))
        check("отказ запроса родителя записан в журнал",
              CAP.has("родитель"),
              "снимок ушёл под кулдаун молча — ровно исходный дефект")
        check("обработчик не упал на отказе сети", alive, why)
    finally:
        assistant.load_state, assistant.save_state = real_load, real_save
        assistant.search_knowledge_corpus = real_search
        assistant.generate_gemini_text_async = fake_llm
        assistant.BOT_ID, assistant.BOT_USERNAME = real_id, real_name

    print("\n[7] Вложенность бюджетов: внутренний срок внутри внешнего")
    summary_budget = assistant.SUMMARY_DELIVERY_TIMEOUT_SECONDS
    callback_budget = assistant.CALLBACK_EDIT_TIMEOUT_SECONDS
    check(f"бюджет доставки сводки ({summary_budget}) равен принятому в проекте "
          f"({tg_safety.DEFAULT_TIMEOUT_SECONDS})",
          summary_budget == tg_safety.DEFAULT_TIMEOUT_SECONDS,
          "второе число рядом разъедется — этот дефект в проекте всплывал 4 раза")
    # Худший случай одного вызова telethon: request_retries=10 x
    # (timeout=30 + flood_sleep_threshold=20) = 500 с (main.py:883, 900-910).
    # Бюджет обязан быть СТРОГО меньше, иначе граница декоративная.
    check("бюджет сводки строго меньше худшего случая telethon (500 с)",
          summary_budget < 500, f"{summary_budget} с границы не даёт")
    check("бюджет кнопки строго меньше бюджета сводки",
          callback_budget < summary_budget,
          f"кнопка {callback_budget} с при сводке {summary_budget} с")
    check("бюджет кнопки укладывается в общий сетевой потолок проекта (60 с)",
          callback_budget < 60, f"{callback_budget} с")
    check("бюджет кнопки покрывает короткий FloodWait telethon (20+5 с)",
          callback_budget >= 25,
          f"{callback_budget} с обрывает законное ожидание, которое вот-вот кончится")

    print("\n[8] Д3: зависший Telegram не оставляет спиннер крутиться навсегда")
    real_cb = assistant.CALLBACK_EDIT_TIMEOUT_SECONDS
    assistant.CALLBACK_EDIT_TIMEOUT_SECONDS = TEST_BUDGET
    try:
        class Callback:
            def __init__(self, data, chat_id=-1001234567890, message_id=42):
                self.data = data if isinstance(data, bytes) else data.encode()
                self.chat_id = chat_id
                self.message_id = message_id
                self.sender_id = 555
                self.answered = 0

            async def answer(self, text=None, alert=False):
                self.answered += 1

            async def get_message(self):
                return None

            async def edit(self, text=None, parse_mode=None):
                return None

        # Тайминг берём минимумом из трёх прогонов: одиночный замер под
        # параллельной нагрузкой уже флакнул на этой машине.
        pressed = []

        def press():
            cb = Callback("wiki_cat:back")
            pressed.append(cb)
            return assistant.handle_quiz_callback(HangingBot(), cb)

        CAP.clear()
        done, elapsed = await fastest(press)
        check("обработчик кнопки ВЕРНУЛСЯ, а не завис навсегда", done,
              f"не вернулся за {HANG_LIMIT} с — спиннер крутился бы вечно")
        check("СПИННЕР СНЯТ: event.answer() дошёл до Telegram",
              any(c.answered == 1 for c in pressed),
              f"answer() по прогонам: {[c.answered for c in pressed]} — "
              f"у врача кнопка крутится, и он жмёт снова")
        check("ожидание врача ограничено одним бюджетом, а не десятью",
              elapsed is not None and elapsed < TEST_BUDGET * 2 + 4.0,
              f"ушло {secs(elapsed)} при бюджете {TEST_BUDGET} с")
        check("отказ правки кнопки записан в журнал",
              CAP.has("tg give up"), "отказ не слышен")

        print("\n[9] Д3: спиннер снимается на КАЖДОЙ ветке кнопок")
        # Ветки взаимоисключающие, поэтому за одно нажатие тратится один бюджет.
        # Проверяем поведение, а не наличие строки в исходнике.
        real_style = database.set_user_style
        real_rand = assistant.query_random_wiki_fact
        real_page = assistant.query_wiki_fact_page
        real_counts = assistant.wiki_subtopic_counts
        real_corpus = assistant.search_knowledge_corpus

        async def _style(user_id, style):
            return True

        async def _rand():
            return "Случайный факт про гипохлорит."

        async def _counts(cat_id):
            return {}

        async def _corpus(kws):
            return "Справка про уступ.", ""

        database.set_user_style = _style
        assistant.query_random_wiki_fact = _rand
        assistant.wiki_subtopic_counts = _counts
        assistant.search_knowledge_corpus = _corpus

        first_cat = next(iter(assistant.WIKI_TREE))
        first_sub = assistant.WIKI_TREE[first_cat][1]
        first_sub_id = (first_sub[0][0] if isinstance(first_sub[0], (list, tuple))
                        else first_sub[0]) if first_sub else f"{first_cat}_x"

        async def _page_full(subtopic_id, page_idx):
            return "Текст статьи про ирригацию.", 3

        async def _page_empty(subtopic_id, page_idx):
            return None, 0

        BRANCHES = [
            ("style:clinical_dry", "смена стиля", _page_full),
            ("proto:back", "список протоколов", _page_full),
            ("proto:bopt", "статья протокола", _page_full),
            ("wiki_cat:back", "меню энциклопедии", _page_full),
            ("wiki_cat:topics", "рубрикатор", _page_full),
            ("wiki_cat:search_info", "справка о поиске", _page_full),
            ("wiki_cat:random", "случайный факт", _page_full),
            (f"wiki_cat:{first_cat}", "раздел энциклопедии", _page_full),
            (f"wiki_page:{first_sub_id}:0", "страница статьи", _page_full),
            (f"wiki_page:{first_sub_id}:0", "раздел без статей", _page_empty),
        ]
        try:
            for data, human, page_impl in BRANCHES:
                assistant.query_wiki_fact_page = page_impl
                cbx = Callback(data)
                done, _ = await timed(
                    lambda: assistant.handle_quiz_callback(HangingBot(), cbx))
                check(f"{human}: обработчик вернулся при зависании", done,
                      "ветка без границы — спиннер вечный")
                check(f"{human}: спиннер снят", cbx.answered >= 1,
                      f"answer() вызван {cbx.answered} раз")
        finally:
            database.set_user_style = real_style
            assistant.query_random_wiki_fact = real_rand
            assistant.query_wiki_fact_page = real_page
            assistant.wiki_subtopic_counts = real_counts
            assistant.search_knowledge_corpus = real_corpus

        print("\n[10] Д3: в обычный день кнопка по-прежнему работает")
        good = WorkingBot()
        cbg = Callback("wiki_cat:back")
        done, _ = await timed(
            lambda: assistant.handle_quiz_callback(good, cbg))
        check("обработчик завершился", done)
        check("сообщение врачу отредактировано", len(good.edited) == 1,
              f"got {len(good.edited)}")
        check("спиннер снят и в успешном случае", cbg.answered == 1,
              f"got {cbg.answered}")
        check("врач увидел меню энциклопедии",
              good.edited and "Энциклопедия" in good.edited[0],
              f"got {good.edited[0][:60] if good.edited else None}")
    finally:
        assistant.CALLBACK_EDIT_TIMEOUT_SECONDS = real_cb

    print("\n[11] Каждая правка кнопки уходит с бюджетом, а не с чужим сроком")
    # Факт вызова и переданный срок — наблюдаемое поведение, а не строка в файле.
    seen = []
    real_edit = tg_safety.edit_message

    async def spy(client, chat_id, message_id, text, timeout=None, op="",
                  logger=None, **kwargs):
        seen.append((op, timeout))
        return tg_safety.TgOutcome(True, value=None, op=op, chat_id=chat_id)

    tg_safety.edit_message = spy
    try:
        class Cb2:
            def __init__(self, data):
                self.data = data.encode()
                self.chat_id = -1001234567890
                self.message_id = 42
                self.sender_id = 555
                self.answered = 0

            async def answer(self, text=None, alert=False):
                self.answered += 1

        await assistant.edit_callback_message(
            WorkingBot(), Cb2("x"), "текст", "edit_message:проверка")
        check("правка кнопки ушла через границу tg_safety", len(seen) == 1,
              f"вызовов границы: {len(seen)}")
        check("границе передан именно бюджет кнопки",
              seen and seen[0][1] == assistant.CALLBACK_EDIT_TIMEOUT_SECONDS,
              f"передан срок {seen[0][1] if seen else None} при бюджете "
              f"{assistant.CALLBACK_EDIT_TIMEOUT_SECONDS}")
    finally:
        tg_safety.edit_message = real_edit

    print("\n[12] Отмену задачи граница не превращает в тихий отказ")
    # runtime_guard.create_task снимает задачи при остановке бота. Снятая работа
    # обязана остаться снятой, иначе бот допишет врачу от имени отменённой.
    assistant.CALLBACK_EDIT_TIMEOUT_SECONDS = 30

    class Cb3:
        def __init__(self):
            self.data = b"wiki_cat:back"
            self.chat_id = -1001234567890
            self.message_id = 42
            self.sender_id = 555
            self.answered = 0

        async def answer(self, text=None, alert=False):
            self.answered += 1

    try:
        cb3 = Cb3()
        task = asyncio.ensure_future(
            assistant.handle_quiz_callback(HangingBot(), cb3))
        await asyncio.sleep(0.2)
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        check("отмена осталась отменой, а не превратилась в отказ", cancelled,
              "снятая задача продолжила работу")
        check("на отменённой задаче спиннер не трогаем", cb3.answered == 0,
              f"answer() вызван {cb3.answered} раз у уже снятой задачи")
    finally:
        assistant.CALLBACK_EDIT_TIMEOUT_SECONDS = real_cb

    print("\n[13] Д2: пока личность бота не опознана, снимок всё равно разбирают")
    # Блоки [4]-[6] всегда задавали BOT_ID/BOT_USERNAME заранее, поэтому
    # догоняющий резолв — то самое, чем Д2 заменил get_me на горячем пути, — не
    # прогонялся ни разу: убрать его целиком удавалось незаметно (диверсия
    # скептика S1 прошла зелёной). А без резолва при пустом BOT_ID оба флага
    # остаются False, обращение врача считается пассивным и снимок молча уходит
    # под 2-часовой кулдаун — ровно то наказание, которое Д2 обещал снять.
    blocked = {"last_passive_media_run": datetime.now().isoformat()}
    real_load2, real_save2 = assistant.load_state, assistant.save_state
    real_search2 = assistant.search_knowledge_corpus
    real_id2, real_name2 = assistant.BOT_ID, assistant.BOT_USERNAME
    seen2 = {"n": 0}

    async def fake_search2(keywords):
        seen2["n"] += 1
        return "", ""

    class Msg2:
        def __init__(self, reply_to=None):
            self.reply_to_msg_id = reply_to
            self.chat_id = -1001234567890
            self.client = None
            self.id = 777

    class FreshBot(WorkingBot):
        """Личность ещё не опознана, но сеть жива: get_me обязан сработать РАЗ."""

        def __init__(self):
            super().__init__()
            self.get_me_calls = 0

        async def get_me(self):
            self.get_me_calls += 1
            return type("Me", (), {"id": 4242, "username": "StomChat_Bot"})()

        async def get_messages(self, chat_id, ids=None, **kw):
            return type("P", (), {"sender_id": 4242})()

    assistant.load_state = lambda: dict(blocked)
    assistant.save_state = lambda state: None
    assistant.search_knowledge_corpus = fake_search2
    assistant.generate_gemini_text_async = fake_llm_fails
    try:
        assistant.BOT_ID, assistant.BOT_USERNAME = None, None
        fresh = FreshBot()
        seen2["n"] = 0
        assistant.REPLIED_MSG_IDS.clear()
        await assistant.check_and_trigger_assistant_media(
            fresh, Msg2(reply_to=555), 791, "@StomChat_Bot что с уступом?",
            "снимок зуба")
        check("при пустом BOT_ID личность догоняется, а снимок идёт в разбор",
              seen2["n"] == 1,
              "снимок ушёл под 2-часовой кулдаун — врач наказан за вопрос")
        check("догоняющий резолв стоил РОВНО одного запроса к Telegram",
              fresh.get_me_calls == 1, f"get_me вызван {fresh.get_me_calls} раз")
        check("резолв записал имя бота, а не только id",
              assistant.BOT_USERNAME == "stomchat_bot",
              f"BOT_USERNAME={assistant.BOT_USERNAME!r}")

        # Второй снимок: личность уже известна, сеть трогать больше НЕ за что.
        seen2["n"] = 0
        assistant.REPLIED_MSG_IDS.clear()
        await assistant.check_and_trigger_assistant_media(
            fresh, Msg2(reply_to=555), 792, "и тут посмотри", "снимок зуба")
        check("на втором снимке get_me уже не зовут — резолв одноразовый",
              fresh.get_me_calls == 1,
              f"get_me вернулся на горячий путь: {fresh.get_me_calls} вызовов")
        check("второй снимок тоже разобран", seen2["n"] == 1,
              "повторное обращение врача потерялось")

        # Личность не опознать (сеть мертва) — снимок под кулдауном отбрасываем,
        # но отказ резолва обязан быть слышен, иначе тишина как в исходном дефекте.
        assistant.BOT_ID, assistant.BOT_USERNAME = None, None
        dead = ReplyBot(parent_sender=4242)  # get_me у него бросает ConnectionError
        seen2["n"] = 0
        CAP.clear()
        assistant.REPLIED_MSG_IDS.clear()
        alive2, why2 = await survives(assistant.check_and_trigger_assistant_media(
            dead, Msg2(reply_to=555), 793, "@stomchat_bot что с уступом?",
            "снимок зуба"))
        check("неудачный резолв личности ЗАПИСАН в журнал, а не съеден",
              CAP.has("Failed to resolve bot identity"),
              "личность не опознана и об этом ни строки — врач наказан молча")
        check("на неопознанной личности обработчик не падает", alive2, why2)
        check("снимок с неизвестной личностью не разбирают вслепую",
              seen2["n"] == 0,
              "разобрали снимок, не зная, к кому обращались — платная генерация зря")
    finally:
        assistant.load_state, assistant.save_state = real_load2, real_save2
        assistant.search_knowledge_corpus = real_search2
        assistant.generate_gemini_text_async = fake_llm
        assistant.BOT_ID, assistant.BOT_USERNAME = real_id2, real_name2


try:
    asyncio.run(run())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
