"""
Команда веб-поиска: она вообще существует, и её отказы слышны.

Что было. Слой качества веб-поиска (web_lookup.py) и транспорт
(blocking_tools.web_search_async, search_engine_safe.perform_search) в проекте
построены и покрыты проверками, но не вызывались НИКЕМ. Замер по исходникам:
слов web_search_async, perform_search, search_engine_safe, web_lookup, DDGS,
tavily в assistant.py, main.py и summarizer.py — ноль во всех трёх файлах.
Функции у врача не было.

Чего это стоило врачу — замер по боевым базам, только чтение:
  * stomat_archive.db кончается 2026-02-19 13:13:49 при 117 847 репликах. На
    2026-07-29 это 160 дней слепоты: о новом материале, отозванном препарате или
    изменившейся рекомендации в корпусе нет ничего и не появится;
  * из 12 784 фактов stomat_wiki.db ссылку содержат 4 (0.0313%), DOI — ноль,
    PubMed упомянут в двух. То есть проверяемый источник бот дать не мог вообще,
    а для медицинского утверждения источник — половина ответа.

Дефекты, которые эта проверка закрывает, и последствие каждого:

Д1. Команды не было. Врач спрашивает про материал последних месяцев — получает
    пересказ чужого мнения из чата без ссылки и не может ничего проверить.

Д2. Отказ провайдера молча. Поиск не отработал — врач ждёт ответа, которого не
    будет, и в журнале об этом ни строки. В этом файле молчание уже стоило
    двухчасовых кулдаунов за вопросы, которые бот просто не разобрал.

Д3. Пустая выдача выдавалась за рекламу. prepare() на пустом списке отдаёт
    NOTHING_USABLE («нашлась только реклама клиник»), хотя провайдер вернул ноль
    строк. Врач по такому тексту решит, что тема утонула в рекламе, и переспросит
    иначе — вместо того чтобы искать другими словами.

Д4. Реклама клиник вместо ответа. Выдача общего поиска по стоматологии на первых
    местах держит цены на имплантацию и «запишитесь на бесплатную консультацию».
    Для профессионала такой ответ хуже молчания: он выдаёт бота за источник,
    которому нельзя доверять.

Д5. Дочерний срок больше родительского. blocking_tools._run_json_tool ждёт
    `timeout + 10`, поэтому «45 с и до двух попыток» стоят вызывающему не 90 с, а
    110. Вызывающий, посчитавший 90, вылетел бы по своему таймауту раньше, чем
    поиск успел бы честно отказаться, — и врач не получил бы даже причины.

Д6. Ответ мог не влезть в сообщение. Битая или переросшая разметка отклоняется
    Telegram ЦЕЛИКОМ: врач не увидит ни ответа, ни ссылок.

Проверки поведенческие: гоняется настоящая команда настоящего обработчика
(assistant.handle_private_message) с подставным провайдером и подставной
генерацией. Ни одного живого сетевого запроса, ни одной записи в боевую базу.
«В исходнике есть строка» здесь не проверяется нигде: такая проверка в этом
проекте уже пропустила снятый потолок.

Сроки в блоке [10] на время прогона подменяются другими числами — проверяется
механизм дедлайна, а сами числа и их вложенность проверяет блок [9]. Подмена
устроена так, чтобы ветку выбирало сравнение бюджетов, а не длительность сна:
тайминговые проверки под параллельной нагрузкой флакуют.

Запуск: python test_search_command.py
"""
import asyncio
import contextlib
import logging
import os
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_websearch_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import config  # noqa: E402

config.DB_PATH = os.path.join(_TMPDIR, "test.db")

import assistant  # noqa: E402
import blocking_tools  # noqa: E402
import database  # noqa: E402
import tg_safety  # noqa: E402

# Файл состояния уводится в temp: набор доходит до handle_private_message, а тот
# пишет кулдауны и метки веток. Без подмены прогон правит боевой
# assistant_state.json, в котором лежат реальные Telegram-id врачей, и «тестовый»
# кулдаун становится настоящим. Это ловит test_isolation [1]; те же три пути
# уводит test_thread_dedupe:55-57.
assistant.STATE_PATH = os.path.join(_TMPDIR, "state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"
import web_lookup as W  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# --- Перехват журнала. «Отказ обязан быть слышен» проверяется ПО ЗАПИСЯМ ------
class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        try:
            self.records.append((record.levelno, record.getMessage()))
        except Exception:
            self.records.append((record.levelno, "<не отформатировалось>"))

    def clear(self):
        self.records = []

    def warnings(self):
        return [msg for level, msg in self.records if level >= logging.WARNING]

    def warned(self, *needles):
        return any(all(n in msg for n in needles) for msg in self.warnings())

    def crashed(self):
        """Обработчик ЛС свалился в свой общий except — это его последняя строка."""
        return any("Unexpected error in handle_private_message" in msg
                   for _level, msg in self.records)


CAP = Capture()
for _logger in (assistant.logger, W.logger):
    _logger.addHandler(CAP)
    _logger.setLevel(logging.DEBUG)


# --- Подставной Telegram ------------------------------------------------------
class Bot:
    """Обычный день: Telegram отвечает. Собираем, что именно ушло врачу."""

    def __init__(self):
        self.sent = []
        self.deleted = []
        self._next_id = 1000

    async def send_message(self, entity=None, message=None, **kw):
        self._next_id += 1
        self.sent.append(message)
        return type("M", (), {"id": self._next_id})()

    async def edit_message(self, chat_id, msg_id, message, **kw):
        self.sent.append(message)
        return type("M", (), {"id": msg_id})()

    async def delete_messages(self, chat_id, msg_ids, **kw):
        self.deleted.append(msg_ids)


class Message:
    """Сообщение врача. Все виды вложений явно пусты — путь команды до них не доходит."""

    def __init__(self, text, msg_id=4242):
        self.id = msg_id
        self.message = text
        self.voice = None
        self.audio = None
        self.photo = None
        self.video = None
        self.document = None
        self.sticker = None


class Event:
    def __init__(self, text, chat_id=777001):
        self.chat_id = chat_id
        self.sender_id = chat_id
        self.message = Message(text)


# --- Подмена всего, что уходит за пределы обработчика -------------------------
SAVED_PM = []


async def _fake_save_pm(chat_id, who, text):
    SAVED_PM.append((chat_id, who, text))


async def _fake_state(chat_id):
    return None


# load_state/save_state пишут assistant_state.json РЯДОМ С ПРОДАКШНОМ: проверка
# не имеет права трогать живой файл состояния бота.
assistant.load_state = lambda: {"pm_pings": {}}
assistant.save_state = lambda state: None
database.save_pm_message = _fake_save_pm
database.get_user_interactive_state = _fake_state


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh():
    """Чистое состояние: пустой журнал, снятый кулдаун, новый бот."""
    CAP.clear()
    del SAVED_PM[:]
    assistant.USER_COOLDOWNS.clear()
    tg_safety.reset_cooldowns()
    return Bot()


@contextlib.contextmanager
def patched(**overrides):
    """Подменить константы бюджета на время прогона и вернуть как было."""
    saved = {name: getattr(W, name) for name in overrides}
    for name, value in overrides.items():
        setattr(W, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(W, name, value)


# --- Подставная выдача провайдера --------------------------------------------
# Форма ровно та, которую отдаёт подпроцесс: список {"text", "url"}.
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/34567890/"
ADA_URL = "https://www.ada.org/resources/perforation-repair"
GOOD_RESULTS = [
    {"text": "Систематический обзор: биодентин при перфорации дна полости зуба "
             "показал закрытие дефекта в 89% случаев при наблюдении 24 месяца.",
     "url": PUBMED_URL},
    {"text": "Рекомендация профессиональной ассоциации: при перфорации до 2 мм "
             "материал выбора — трикальцийсиликатный цемент.",
     "url": ADA_URL},
]
ADS_ONLY = [
    {"text": "Имплантация зубов в Москве. Акция: скидка 30%, рассрочка 0%. "
             "Запишитесь на бесплатную консультацию — наша клиника работает без выходных!",
     "url": "https://implant-moscow-clinic.ru/prices"},
    {"text": "Цены на лечение перфорации. Наши специалисты, лицензия №77-01. "
             "Звоните, оставьте заявку — скидка новым пациентам.",
     "url": "https://stomatolog-zubi.ru/zapis"},
]

ANSWER_WITH_CITATION = (
    "Закрытие перфорации трикальцийсиликатным цементом — материал выбора [1]. "
    "При дефекте до 2 мм результат подтверждён рекомендацией ассоциации [2]."
)


def provider(results, error=None, delay=0.0, boom=None):
    """Фиктивный провайдер: ни одного живого запроса, но форма и подпись живые."""
    calls = []

    async def _call(query, max_results, timeout=None):
        calls.append({"query": query, "max_results": max_results, "timeout": timeout})
        if delay:
            await asyncio.sleep(delay)
        if boom is not None:
            raise boom
        return list(results), error

    _call.calls = calls
    return _call


def generator(text, error=None, boom=None):
    """Фиктивная генерация. Промпт запоминаем: он обязан быть заземлён на выдержки."""
    calls = []

    async def _call(prompt, context, timeout=None):
        calls.append({"prompt": prompt, "timeout": timeout,
                      "kind": (context or {}).get("kind")})
        if boom is not None:
            raise boom
        if error:
            return None, error
        return type("R", (), {"text": text})(), None

    _call.calls = calls
    return _call


def drive(command, results=GOOD_RESULTS, answer=ANSWER_WITH_CITATION,
          search_error=None, gen_error=None, search_boom=None, gen_boom=None,
          bot=None):
    """
    Прогнать НАСТОЯЩУЮ команду настоящего обработчика с подставным провайдером.

    Возвращает (bot, вызовы_поиска, вызовы_генерации). Внутри самой команды не
    мокается ничего: проверяется то, что увидит врач.
    """
    bot = bot or fresh()
    search = provider(results, error=search_error, boom=search_boom)
    gen = generator(answer, error=gen_error, boom=gen_boom)
    real_search = blocking_tools.web_search_async
    real_gen = assistant.generate_gemini_text_async
    blocking_tools.web_search_async = search
    assistant.generate_gemini_text_async = gen
    try:
        run(assistant.handle_private_message(bot, Event(command)))
    finally:
        blocking_tools.web_search_async = real_search
        assistant.generate_gemini_text_async = real_gen
    return bot, search.calls, gen.calls


def doctor_saw(bot):
    """Всё, что ушло врачу, одной строкой: статус, ответ, отказ."""
    return "\n".join(m or "" for m in bot.sent)


print("\n[1] Команда существует и доводит ссылку до врача")
# Д1. Главное: у врача есть путь, которым он получает ПРОВЕРЯЕМЫЙ источник.
bot, search_calls, gen_calls = drive("/web биодентин перфорация дна полости")
seen = doctor_saw(bot)
check("поиск вызван", len(search_calls) >= 1, f"вызовов {len(search_calls)}")
check("генерация вызвана", len(gen_calls) == 1, f"вызовов {len(gen_calls)}")
check("врач получил ссылку из выдачи", PUBMED_URL in seen,
      "ответ без источника для клинического утверждения хуже отсутствия ответа")
check("вторая ссылка выдачи тоже видна", "ada.org" in seen)
check("текст ответа модели доехал", "материал выбора" in seen)
check("ответ помечен как открытые источники, а не база чата",
      "открытым источникам" in seen,
      "врач спутает мнение чата с внешним источником")
check("обработчик не свалился в общий except", not CAP.crashed())
check("статусное сообщение убрано", len(bot.deleted) == 1,
      f"удалений {len(bot.deleted)} — врач останется с «Ищу...» под ответом")
check("ответ уложен в предел Telegram", all(len(m or "") <= 4096 for m in bot.sent),
      f"максимум {max(len(m or '') for m in bot.sent)} символов — Telegram отклонит целиком")
check("ответ ушёл одним сообщением, а не разорван по ссылкам",
      sum(1 for m in bot.sent if PUBMED_URL in (m or "")) == 1)
check("ответ и запрос попали в историю ЛС",
      len(SAVED_PM) == 1 and "Веб-поиск" in SAVED_PM[0][2] and PUBMED_URL in SAVED_PM[0][2],
      "следующий вопрос «а по второй ссылке что?» придёт без ссылок")

print("\n[2] Промпт заземлён, а бюджет приходит сверху")
prompt = gen_calls[0]["prompt"]
check("выдержки попали в промпт", "89%" in prompt and "ВЫДЕРЖКИ" in prompt)
check("реклама в промпт не попала", "рассрочка" not in prompt.lower())
check("промпт требует опираться только на выдержки", "ТОЛЬКО на выдержки" in prompt)
check("генерации выдан бюджет, а не её желаемое число",
      gen_calls[0]["timeout"] is not None
      and 0 < gen_calls[0]["timeout"] <= W.ANSWER_TIMEOUT_SECONDS,
      f"бюджет генерации {gen_calls[0]['timeout']}")
check("поиску выдан бюджет одной попытки",
      search_calls[0]["timeout"] == W.SEARCH_ATTEMPT_TIMEOUT_SECONDS,
      f"выдано {search_calls[0]['timeout']}")
check("у провайдера просим больше, чем покажем",
      search_calls[0]["max_results"] > W.WEB_MAX_SOURCES,
      f"просим {search_calls[0]['max_results']} при показе {W.WEB_MAX_SOURCES}: "
      f"отсев рекламы оставит врача без источников")
check("запрос очищен от пунктуации, но слова целы",
      "биодентин" in search_calls[0]["query"] and "?" not in search_calls[0]["query"])

print("\n[3] Выдача из одной рекламы — честный отказ, а не выдуманный ответ")
# Д4. Худший исход не «нет ответа», а бодрый ответ по рекламе клиники: врач
# принимает решение по цене имплантации, поданной как клинический материал.
bot, search_calls, gen_calls = drive("/web чем закрыть перфорацию", results=ADS_ONLY)
seen = doctor_saw(bot)
check("генерация НЕ запускалась на рекламе", len(gen_calls) == 0,
      "модель пересказала бы рекламу клиники как клинический материал")
check("врач получил честный отказ", "реклама клиник" in seen,
      "молчание тут читается как «бот сломался»")
check("ссылки рекламных клиник врачу не ушли",
      "implant-moscow-clinic.ru" not in seen and "stomatolog-zubi.ru" not in seen)
check("причина отсева в журнале WARNING",
      CAP.warned("web lookup", "пригодных источников нет"),
      f"WARNING: {CAP.warnings()[:3]}")
check("в журнале назван домен, из-за которого отсеяли",
      any("implant" in msg for msg in CAP.warnings()),
      "без домена разобрать ложное срабатывание фильтра невозможно")
check("обработчик на рекламе не свалился", not CAP.crashed())
check("отказ по рекламе отличается от отказа поиска", W.NOTHING_USABLE != W.SEARCH_FAILED)

print("\n[4] Отказ провайдера: врач слышит причину, обработчик жив")
# Д2. Обработчик ЛС держит замок на пользователе (main.py:2272): упавший
# обработчик означает, что все следующие вопросы этого врача не обработаются.
for label, kwargs in (
    ("провайдер вернул ошибку", {"results": [], "search_error": "ddgs: RatelimitException"}),
    ("провайдер бросил исключение",
     {"search_boom": ConnectionError("Server closed the connection")}),
):
    bot, search_calls, gen_calls = drive("/web артикаин детям доза", **kwargs)
    seen = doctor_saw(bot)
    check(f"{label}: обработчик не свалился в общий except", not CAP.crashed(),
          "упавший обработчик оставляет замок на враче: его следующие вопросы не обработаются")
    check(f"{label}: врач получил внятный ответ",
          "Поиск не отработал" in seen or "сбой инструмента" in seen,
          "врач ждёт ответа, которого не будет")
    check(f"{label}: в журнале WARNING с причиной", CAP.warned("web lookup"),
          f"WARNING: {CAP.warnings()[:3]}")
    check(f"{label}: генерация не запускалась", len(gen_calls) == 0,
          "модель выдумала бы ответ и подала его как найденный в источниках")
    check(f"{label}: статус убран, врач не остался с «Ищу...»", len(bot.deleted) == 1)
check("тип упавшего провайдера назван в журнале", CAP.warned("ConnectionError"),
      "без типа ошибки отказ провайдера не разобрать")

# Найдено СВОЕЙ ЖЕ ДИВЕРСИЕЙ: проверки выше ловят отказ поиска только когда
# отказали ВСЕ попытки — тогда WARNING пишет prepare(). А отказ ОДНОЙ попытки,
# после которой вторая нашла, не проверялся никем, и понижение его записи до
# debug прошло молча (0 упавших проверок).
#
# Последствие ровно то, о котором предупреждает сам транспорт
# (blocking_tools:186-189): «tavily не отвечает уже месяц, поиск втихую держится
# на ddgs» не видно НИГДЕ. Врач ответ получает, деградацию инструмента никто не
# замечает до полного отказа обоих провайдеров.
_flaky_calls = []


async def _flaky_search(query, max_results, timeout=None):
    _flaky_calls.append(query)
    if len(_flaky_calls) == 1:
        raise ConnectionError("Server closed the connection")
    return list(GOOD_RESULTS), None


bot = fresh()
_real_search, _real_gen = blocking_tools.web_search_async, assistant.generate_gemini_text_async
_flaky_gen = generator(ANSWER_WITH_CITATION)
blocking_tools.web_search_async = _flaky_search
assistant.generate_gemini_text_async = _flaky_gen
try:
    run(assistant.handle_private_message(
        bot, Event("/web биодентин перфорация дна полости зуба верхней челюсти")))
finally:
    blocking_tools.web_search_async = _real_search
    assistant.generate_gemini_text_async = _real_gen
check("упавшая первая попытка не мешает ответу по второй",
      PUBMED_URL in doctor_saw(bot), "врач потерял ответ из-за одного отказа провайдера")
check("отказ ОДНОЙ попытки тоже записан WARNING",
      CAP.warned("поиск отказал"),
      f"деградация одного провайдера не видна до полного отказа обоих. "
      f"WARNING: {CAP.warnings()[:3]}")
check("в записи назван номер упавшей попытки", CAP.warned("попытка 1"),
      "без номера непонятно, отказал провайдер или запрос был плохой")

print("\n[5] Пустая выдача не выдаётся за рекламу")
# Д3. Два разных мира: «провайдер вернул ноль строк» и «всё отсеяно как реклама».
bot, search_calls, gen_calls = drive(
    "/web зубная фея квартальный отчёт по молочным зубам", results=[])
seen = doctor_saw(bot)
check("врачу сказано «ничего не нашлось»", "ничего не нашлось" in seen)
check("про рекламу клиник при пустой выдаче не соврали", "реклама клиник" not in seen,
      "врач решит, что тема утонула в рекламе, и будет переспрашивать не то")
check("пустая выдача попала в журнал WARNING", CAP.warned("выдача пуста"),
      f"WARNING: {CAP.warnings()[:3]}")
check("на пустой выдаче сделаны обе попытки поиска",
      len(search_calls) == W.SEARCH_ATTEMPTS,
      f"попыток {len(search_calls)} при {W.SEARCH_ATTEMPTS} разрешённых")
check("вторая попытка укорочена",
      len(search_calls) > 1
      and len(search_calls[1]["query"].split()) <= W.SEARCH_SHORT_WORDS,
      "длинная клиническая фраза часто не находит ничего, короткая находит")
check("вторая попытка не повторяет первую дословно",
      len(search_calls) > 1 and search_calls[0]["query"] != search_calls[1]["query"],
      "второй одинаковый запрос — потраченные 55 с бюджета впустую")

print("\n[6] Генерация отвалилась — ссылки всё равно у врача")
# Для клинического вопроса проверяемый источник без пересказа полезнее пересказа
# без источника, поэтому отказ генерации не имеет права съесть найденные ссылки.
for label, kwargs in (("модель вернула ошибку", {"gen_error": "cascade_exhausted"}),
                      ("модель упала", {"gen_boom": RuntimeError("boom")})):
    bot, search_calls, gen_calls = drive("/web биодентин", **kwargs)
    seen = doctor_saw(bot)
    check(f"{label}: ссылка всё равно у врача", PUBMED_URL in seen)
    check(f"{label}: сказано про сбой генерации, а не про отсутствие данных",
          "генерации" in seen or "собрать" in seen)
    check(f"{label}: в журнале WARNING", CAP.warned("web lookup", "не собран"),
          f"WARNING: {CAP.warnings()[:3]}")
    check(f"{label}: обработчик не свалился", not CAP.crashed())

print("\n[7] Пустой запрос и кулдаун не молчат")
bot, search_calls, gen_calls = drive("/web")
check("пустой запрос: поиск не запускался", len(search_calls) == 0,
      "пустой запрос к провайдеру — сожжённая квота на весь чат")
check("пустой запрос: врач получил подсказку с примером",
      "/web" in doctor_saw(bot) and "перфорация" in doctor_saw(bot))
bot = fresh()
drive("/web биодентин", bot=bot)
_, repeat_calls, _ = drive("/web биодентин", bot=bot)
check("повтор в пределах кулдауна не идёт в провайдера", len(repeat_calls) == 0,
      "внешний сервис общий на 749 врачей")
check("про кулдаун врачу сказано", "подождите" in doctor_saw(bot).lower(),
      "молча проигнорированный запрос читается как поломка бота")
check("кулдаун не глушит команду навсегда", assistant.WEB_COOLDOWN_SECONDS <= 60,
      f"{assistant.WEB_COOLDOWN_SECONDS} с — врач уйдёт, не дождавшись")

print("\n[8] Синоним /найди работает, /search не перехвачен")
bot, search_calls, gen_calls = drive("/найди биодентин перфорация")
check("кириллическая команда разобрана", len(search_calls) >= 1,
      "str.lower() на кириллице работает, в отличие от SQLite LOWER()")
check("ссылка доехала и через синоним", PUBMED_URL in doctor_saw(bot))
bot, search_calls, _ = drive("/search BOPT")
check("веб-поиск не перехватил поиск по корпусу", len(search_calls) == 0,
      "команда поиска по базе чата подменена внешним поиском")

print("\n[9] Бюджеты вложены: внутренний строго меньше общего")
# Д5. Тот самый класс, который в этом проекте ловился ЧЕТЫРЕ раза.
check("одна попытка поиска стоит больше своего срока",
      W.SEARCH_ATTEMPT_COST_SECONDS > W.SEARCH_ATTEMPT_TIMEOUT_SECONDS,
      "родитель подпроцесса ждёт timeout + запас на подъём, а не timeout")
check("запас на подъём подпроцесса не меньше настоящего",
      W.SUBPROCESS_SLACK_SECONDS >= blocking_tools._SUBPROCESS_STARTUP_SLACK_SECONDS,
      f"{W.SUBPROCESS_SLACK_SECONDS} против "
      f"{blocking_tools._SUBPROCESS_STARTUP_SLACK_SECONDS} — два числа разъехались")
check("поиск целиком укладывается в бюджет прохода",
      W.SEARCH_TOTAL_COST_SECONDS < W.LOOKUP_TOTAL_COST_SECONDS,
      f"{W.SEARCH_TOTAL_COST_SECONDS} против {W.LOOKUP_TOTAL_COST_SECONDS}")
check("поиск СТРОГО меньше общего бюджета команды",
      W.SEARCH_TOTAL_COST_SECONDS < assistant.WEB_COMMAND_TIMEOUT_SECONDS,
      f"{W.SEARCH_TOTAL_COST_SECONDS} против {assistant.WEB_COMMAND_TIMEOUT_SECONDS}")
check("проход поиска и генерации укладывается в бюджет команды",
      assistant.WEB_LOOKUP_BUDGET_SECONDS < assistant.WEB_COMMAND_TIMEOUT_SECONDS,
      f"{assistant.WEB_LOOKUP_BUDGET_SECONDS} против {assistant.WEB_COMMAND_TIMEOUT_SECONDS}")
_stages = (assistant.WEB_STATUS_TIMEOUT_SECONDS + assistant.WEB_LOOKUP_BUDGET_SECONDS
           + assistant.WEB_DELIVERY_TIMEOUT_SECONDS
           + assistant.WEB_STATUS_CLEANUP_TIMEOUT_SECONDS)
print(f"      поиск {W.SEARCH_TOTAL_COST_SECONDS:.0f} + генерация "
      f"{W.ANSWER_COST_SECONDS:.0f} + троттлинг {W.LLM_PACE_SLACK_SECONDS:.0f} = "
      f"проход {assistant.WEB_LOOKUP_BUDGET_SECONDS:.0f}; сумма этапов {_stages:.0f}; "
      f"команда {assistant.WEB_COMMAND_TIMEOUT_SECONDS:.0f}")
check("сумма этапов не превышает общий бюджет",
      _stages <= assistant.WEB_COMMAND_TIMEOUT_SECONDS,
      f"{_stages} против {assistant.WEB_COMMAND_TIMEOUT_SECONDS}")
check("общий бюджет считается сложением, а не задан числом",
      assistant.WEB_COMMAND_TIMEOUT_SECONDS == _stages,
      "литерал рядом с этапами разъедется при первой правке")
check("на генерацию остаётся больше минимума",
      W.ANSWER_TIMEOUT_SECONDS > W.ANSWER_MIN_TIMEOUT_SECONDS)
check("доставка ответа под сроком, а не бесконечна",
      0 < assistant.WEB_DELIVERY_TIMEOUT_SECONDS <= tg_safety.DEFAULT_TIMEOUT_SECONDS,
      f"{assistant.WEB_DELIVERY_TIMEOUT_SECONDS}")
check("отказ разбора файла тоже под сроком, и он короче доставки",
      0 < assistant.PM_STATUS_EDIT_TIMEOUT_SECONDS < assistant.SUMMARY_DELIVERY_TIMEOUT_SECONDS,
      f"{assistant.PM_STATUS_EDIT_TIMEOUT_SECONDS}")

print("\n[10] Дедлайн отбирает работу на живых корутинах, а не только в числах")
# Ветку выбирает СРАВНЕНИЕ бюджетов, а не длительность сна: единственный сон
# здесь (1.5 с) гарантированно больше запаса (1.0 с), поэтому проверка не
# зависит от того, насколько машина занята.


async def _empty_slow_search(query, timeout):
    await asyncio.sleep(1.5)
    return [], None


_gen_calls = []


async def _counting_gen(prompt, timeout):
    _gen_calls.append(timeout)
    return "ответ [1]", None


CAP.clear()
del _gen_calls[:]
# need = COST(100) + ANSWER_MIN(0.5) + SLACK(0.5) = 101; бюджета 102 хватает на
# одну попытку и уже не хватает на вторую после 1.5 с сна.
with patched(SEARCH_ATTEMPT_COST_SECONDS=100.0, SEARCH_ATTEMPT_TIMEOUT_SECONDS=1.0,
             ANSWER_MIN_TIMEOUT_SECONDS=0.5, SUBPROCESS_SLACK_SECONDS=0.5):
    _skipped = run(W.run_lookup("биодентин перфорация дна полости зуба верхней челюсти",
                                _empty_slow_search, _counting_gen,
                                budget=102.0, log=W.logger))
check("вторая попытка отменена нехваткой бюджета",
      CAP.warned("попытка поиска пропущена"), f"WARNING: {CAP.warnings()[:3]}")
check("врач всё равно получил текст, а не тишину", bool(_skipped["text"]))
check("на отменённой попытке генерация не запускалась", len(_gen_calls) == 0,
      "искать было нечего, а запрос к модели уже стоил бы бюджета")

CAP.clear()
del _gen_calls[:]
# Минимум на генерацию выкручен выше её же бюджета: остаток заведомо мал, и
# врач обязан получить найденные ссылки вместо пустоты.
async def _instant_good_search(query, timeout):
    return list(GOOD_RESULTS), None


with patched(ANSWER_MIN_TIMEOUT_SECONDS=1000.0):
    _no_budget = run(W.run_lookup("биодентин", _instant_good_search, _counting_gen,
                                  budget=2000.0, log=W.logger))
check("без бюджета на генерацию врач получает ссылки",
      _no_budget["outcome"] == W.OUTCOME_NO_BUDGET and PUBMED_URL in _no_budget["text"],
      f"исход {_no_budget['outcome']}")
check("нехватка бюджета на генерацию попала в WARNING",
      CAP.warned("на генерацию осталось"), f"WARNING: {CAP.warnings()[:3]}")
check("генерация без бюджета не вызывалась", len(_gen_calls) == 0,
      "запрос, которому не хватит времени соединиться, сжигает остаток бюджета")

CAP.clear()


async def _hanging_search(query, timeout):
    await asyncio.sleep(60)
    return GOOD_RESULTS, None


_t0 = time.monotonic()
with patched(SEARCH_ATTEMPT_COST_SECONDS=1.0, SEARCH_ATTEMPT_TIMEOUT_SECONDS=0.5,
             ANSWER_MIN_TIMEOUT_SECONDS=0.5, SUBPROCESS_SLACK_SECONDS=0.5):
    _capped = run(asyncio.wait_for(
        W.run_lookup("биодентин", _hanging_search, _counting_gen,
                     budget=3.0, log=W.logger),
        timeout=20.0,
    ))
_hang_elapsed = time.monotonic() - _t0
check("зависший провайдер снят жёстким потолком, а не съел весь бюджет",
      _hang_elapsed < 15.0, f"прошло {_hang_elapsed:.2f} с при сне провайдера 60 с")
check("зависание провайдера дало врачу текст, а не тишину", bool(_capped["text"]))
check("зависание провайдера попало в журнал WARNING", CAP.warned("web lookup"),
      f"WARNING: {CAP.warnings()[:3]}")

print("\n[11] Длинный ответ режется, ссылки остаются")
# Д6. Резать обязан ПЕРЕСКАЗ, а не подпись: ссылка — то, чего у бота не было.
_long = ("Очень подробный разбор клинического случая. " * 200).strip()
_footer = W.format_sources_footer([
    {"host": "pubmed.ncbi.nlm.nih.gov", "url": PUBMED_URL},
    {"host": "ada.org", "url": ADA_URL},
])
_composed = W.compose_answer(_long, _footer)
check("готовое сообщение влезает в предел", len(_composed) <= W.MESSAGE_MAX_CHARS,
      f"{len(_composed)} символов при потолке {W.MESSAGE_MAX_CHARS}")
check("потолок сообщения ниже жёсткого предела Telegram", W.MESSAGE_MAX_CHARS < 4096,
      "битая или переросшая разметка отклоняет сообщение ЦЕЛИКОМ")
check("ссылки выжили обрезку", PUBMED_URL in _composed and ADA_URL in _composed,
      "обрезали не тот конец: врач получил пересказ без источника")
check("обрезка прошла по границе, а не посреди слова",
      not _composed.split("\n\n")[0].rstrip("…").endswith("подроб"),
      "оборванный токен в клиническом тексте читается как другое слово")
check("короткий ответ не обрезан",
      "Короткий ответ [1]." in W.compose_answer("Короткий ответ [1].", _footer))
check("подпись без источников не добавляет пустых строк",
      W.compose_answer("Ответ", "") == "Ответ")

print("\n[12] Разметка ответа безопасна для Telegram")
# Битый тег от модели отклоняет ВСЁ сообщение: врач не увидит ни ответа, ни ссылок.
_dirty = assistant.clean_html_formatting(
    "<b>Доза</b> <u>подчёркнутое</u> <script>alert(1)</script> 5 мг/кг [1]\n" + _footer
)
check("неподдержанный тег экранирован, а не отдан Telegram",
      "<u>" not in _dirty and "&lt;u&gt;" in _dirty)
check("поддержанный тег сохранён", "<b>Доза</b>" in _dirty)
check("ссылка после экранирования цела", PUBMED_URL in _dirty)
check("угловых скобок без пары не осталось", _dirty.count("<") == _dirty.count(">"),
      "непарная скобка — отклонённое сообщение целиком")

# Найдено СВОЕЙ ЖЕ ДИВЕРСИЕЙ: проверка выше трогает clean_html_formatting
# напрямую, а не путь доставки. Снятие очистки в самой команде она пропускала
# молча (0 упавших проверок), хотя ответ модели уходит врачу КАК ЕСТЬ: один
# незакрытый тег — и Telegram отклоняет сообщение целиком, врач не видит ни
# ответа, ни ссылок, а в журнале это выглядит как «ответ доставлен».
_MODEL_BROKEN_HTML = (
    "<u>Доза</u> 5 мг/кг [1]. Незакрытый <b>тег и <script>alert(1)</script> "
    "плюс 3 < 5 и амперсанд & сам по себе."
)
bot, _, _ = drive("/web биодентин доза", answer=_MODEL_BROKEN_HTML)
_delivered = [m for m in bot.sent if "мг/кг" in (m or "")]
check("ответ модели дошёл до врача одним сообщением", len(_delivered) == 1,
      f"сообщений с ответом {len(_delivered)}")
if _delivered:
    _out = _delivered[0]
    check("доставка прогоняет ответ модели через очистку разметки",
          "<u>" not in _out and "<script>" not in _out,
          "неподдержанный тег уходит в Telegram, и он отклоняет ВСЁ сообщение")
    check("непарных угловых скобок в доставленном ответе нет",
          _out.count("<") == _out.count(">"),
          "«3 < 5» из ответа модели ломает разбор HTML целиком")
    check("ссылка выжила очистку разметки", PUBMED_URL in _out)

print("\n[13] Проверки выше ловят поломку")
# Ноль провалов одинаково выглядит у рабочего кода и у слепой проверки.
check("счётчик вызовов подставного провайдера действительно пуст до вызова",
      provider(GOOD_RESULTS).calls == [],
      "вызовы не считаются — блоки [1]-[8] ничего не значат")


def _instant(results):
    return lambda query, timeout: asyncio.sleep(0, result=(list(results), None))


def _instant_answer(text):
    return lambda prompt, timeout: asyncio.sleep(0, result=(text, None))


_probe_ads = run(W.run_lookup("вопрос", _instant(ADS_ONLY),
                              _instant_answer("выдуманный ответ"),
                              budget=W.LOOKUP_TOTAL_COST_SECONDS, log=W.logger))
check("отсев рекламы виден проходу",
      _probe_ads["outcome"] == W.OUTCOME_NOTHING_USABLE,
      f"исход {_probe_ads['outcome']} — фильтр не сработал")
check("выдуманный ответ на рекламе не собран", "выдуманный" not in _probe_ads["text"])
_probe_ok = run(W.run_lookup("вопрос", _instant(GOOD_RESULTS),
                             _instant_answer("ответ [1]"),
                             budget=W.LOOKUP_TOTAL_COST_SECONDS, log=W.logger))
check("на годной выдаче проход доходит до конца",
      _probe_ok["outcome"] == W.OUTCOME_OK and PUBMED_URL in _probe_ok["text"],
      f"исход {_probe_ok['outcome']}")
check("исходы отказа и успеха не совпадают",
      len({W.OUTCOME_OK, W.OUTCOME_SEARCH_FAILED, W.OUTCOME_NOTHING_FOUND,
           W.OUTCOME_NOTHING_USABLE, W.OUTCOME_ANSWER_FAILED,
           W.OUTCOME_NO_BUDGET, W.OUTCOME_EMPTY_QUERY}) == 7)
check("перехватчик журнала подключён и что-то видит", bool(CAP.records),
      "проверки на WARNING слепы, если перехватчик не подключён")
check("подмена констант вернула настоящие числа",
      W.SEARCH_ATTEMPT_COST_SECONDS == W.SEARCH_ATTEMPT_TIMEOUT_SECONDS
      + W.SUBPROCESS_SLACK_SECONDS and W.ANSWER_MIN_TIMEOUT_SECONDS == 20.0,
      f"COST={W.SEARCH_ATTEMPT_COST_SECONDS} MIN={W.ANSWER_MIN_TIMEOUT_SECONDS}")

print("\n[14] Попутный фикс: отказ разбора файла не висит вечно")
# Это ЕДИНСТВЕННОЕ сообщение, которым врач узнаёт, что снимок не открылся, и оно
# шло голым bot_client.edit_message без срока. Обработчик ЛС держит замок на
# пользователе (main.py:2272): пока правка висит, ВСЕ следующие вопросы этого
# врача стоят в очереди и не обрабатываются до перезапуска процесса, а в журнале
# об этом ни строки — зависание не исключение, и except его не ловит.


async def _no_pm_history(chat_id, limit=None):
    return []


database.get_last_pm_messages = _no_pm_history
assistant.media_tools.image_document = lambda message: None


class HangingBot(Bot):
    """Telegram принял правку и не отвечает — тот самый обрыв связи (51 723 строки)."""

    def __init__(self, hang=30.0):
        super().__init__()
        self.hang = hang
        self.edit_started = 0

    async def edit_message(self, chat_id, msg_id, message, **kw):
        self.edit_started += 1
        await asyncio.sleep(self.hang)
        return type("M", (), {"id": msg_id})()


def drive_broken_photo(bot, chat_id):
    """
    Врач прислал снимок без подписи, скачивание отказало.

    chat_id обязан быть РАЗНЫМ у разных прогонов: сообщение без текста командой
    не считается и попадает под 5-секундный кулдаун pm_chat, а под кулдауном
    обработчик выходит до разбора файла. Поймано своей же проверкой — второй
    прогон молча уходил в «Секунду, дочитываю предыдущее сообщение».
    """
    assistant.USER_COOLDOWNS.clear()
    event = Event("", chat_id=chat_id)
    event.message.photo = type("P", (), {})()

    async def _boom(file=None, **kw):
        raise OSError("download failed")

    event.message.download_media = _boom
    started = time.monotonic()
    run(asyncio.wait_for(assistant.handle_private_message(bot, event), timeout=25.0))
    return time.monotonic() - started


CAP.clear()
_hanging = HangingBot()
_saved_edit_budget = assistant.PM_STATUS_EDIT_TIMEOUT_SECONDS
assistant.PM_STATUS_EDIT_TIMEOUT_SECONDS = 0.4
try:
    _returned, _elapsed_edit = True, None
    try:
        _elapsed_edit = drive_broken_photo(_hanging, chat_id=777101)
    except asyncio.TimeoutError:
        _returned = False
finally:
    assistant.PM_STATUS_EDIT_TIMEOUT_SECONDS = _saved_edit_budget
check("обработчик вернулся, а не завис на правке",
      _returned and _elapsed_edit is not None and _elapsed_edit < 10.0,
      f"правка висит 30 с, обработчик держит замок на враче; прошло "
      f"{'не вернулся' if _elapsed_edit is None else '%.2f с' % _elapsed_edit}")
check("правка вообще была попытана", _hanging.edit_started == 1,
      f"попыток {_hanging.edit_started}")
check("недоставленный отказ записан WARNING",
      CAP.warned("не доставлен") or CAP.warned("tg give up"),
      f"WARNING: {CAP.warnings()[:4]}")
check("врача догнал обычный отказ, а не тишина",
      any("Не смог открыть" in (m or "") for m in _hanging.sent),
      "отметка «отказ показан» стояла, а врач ничего не получил")

CAP.clear()
_working = Bot()
_elapsed_ok = drive_broken_photo(_working, chat_id=777102)
check("на живом Telegram отказ уходит правкой статуса",
      any("Не удалось обработать файл" in (m or "") for m in _working.sent),
      "врач остаётся с «Скачиваю и анализирую… Подождите» навсегда")
check("на живом Telegram дубля отказа нет",
      sum(1 for m in _working.sent if "Не смог открыть" in (m or "")) == 0,
      "врач получает два отказа подряд на один файл")
check("живой путь быстрый", _elapsed_ok < 10.0, f"{_elapsed_ok:.2f} с")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
