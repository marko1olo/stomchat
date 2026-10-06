"""
Граница вокруг вызовов Telegram: реальные корутины, реальные бюджеты.

Проверяется tg_safety.py. Не импортирует ни main, ни assistant, ни runtime_guard:
логирование не настраивается, боевые файлы не открываются, сеть не нужна.
telethon тоже не нужен — подделки ошибок объявлены здесь, а раздел [2] отдельно
прогоняет случай, когда telethon импортировать НЕЛЬЗЯ.

Замер, из которого выросли требования (bot.log 12251 стр. 2026-05-18..2026-07-28,
bot.log.1 109798 стр. 2026-01-31..2026-05-13):

    Server closed the connection            181 / 51542
    Attempt N at connecting failed           26 /  2447
    Telegram is having internal issues        0 /    51   (49 из них ServerError и наследники)
    flood (транспортный, HTTP 429)            0 /     3
    FloodWait c секундами (RPC)               0 /     0   <- замерить нельзя: runtime_guard.py:79
                                                            глушит логгер telethon до ERROR
    ChatWriteForbidden / MsgIdInvalid / auth  0 /     0

Запуск: python test_tg_safety.py
"""
import asyncio
import io
import logging
import os
import subprocess
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import tg_safety as T  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def run(coro):
    return asyncio.run(coro)


def timed(coro):
    """Итог вызова и честно замеренное стенное время."""
    began = time.monotonic()
    result = asyncio.run(coro)
    return result, time.monotonic() - began


# --- Подделки ошибок Telegram ---------------------------------------------------
# Имена классов совпадают с telethon: tg_safety классифицирует по именам всего
# MRO, поэтому подделка проходит тот же путь, что настоящая ошибка, но без пакета.

class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class SlowModeWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required (slow mode)")
        self.seconds = seconds


class ServerError(Exception):
    pass


class RpcCallFailError(ServerError):
    """Наследник ServerError: 20 таких строк замерено в bot.log.1."""


class ChatWriteForbiddenError(Exception):
    pass


class MsgIdInvalidError(Exception):
    pass


class AuthKeyUnregisteredError(Exception):
    pass


class UserIsBlockedError(Exception):
    pass


class WhoKnowsError(Exception):
    """Класс, которого нет ни в одном списке: проверяем поведение по умолчанию."""


class ChatWriteForbiddenResetError(ChatWriteForbiddenError, ConnectionResetError):
    """
    Класс, попадающий в ОБА списка сразу: в MRO есть и терминальное имя, и
    повторяемое. Приоритет обязан быть у терминального — иначе повтор будет
    крутиться на ошибке, которая не пройдёт никогда, и сожжёт бюджет врача.
    Без этой проверки список терминальных имён не нёс нагрузки: его отключение
    маскировалось значением по умолчанию (неизвестное = терминальное).
    """


class Recorder(logging.Handler):
    """Свой приёмник записей: root не настраиваем, в файлы не пишем."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(r.getMessage() for r in self.records)

    def warnings(self):
        return [r for r in self.records if r.levelno >= logging.WARNING]


def recorder():
    log = logging.getLogger("test_tg_safety.capture")
    log.handlers = []
    log.setLevel(logging.DEBUG)
    log.propagate = False
    handler = Recorder()
    log.addHandler(handler)
    return log, handler


# Приёмник для СОБСТВЕННОГО логгера модуля: там, где logger не передан, отказ
# уходит в logging.getLogger("tg_safety"). Без своего обработчика Python включает
# logging.lastResort и сыплет предупреждения в stderr посреди вывода набора.
# Обработчик ставит тест, а не модуль — раздел [1] отдельно проверяет, что
# импорт tg_safety никаких обработчиков не навешивает.
_OWN_LOG = logging.getLogger("tg_safety")
_OWN_LOG.handlers = []
_OWN_LOG.setLevel(logging.DEBUG)
_OWN_LOG.propagate = False
_OWN_RECORDER = Recorder()
_OWN_LOG.addHandler(_OWN_RECORDER)


def raiser(*errors):
    """
    Корутина, поднимающая заданные ошибки по очереди; последняя повторяется.

    Возвращает фабрику — именно так guard и обязан принимать вызов: корутину
    нельзя дождаться дважды, а повтор создаёт новый вызов.
    """
    state = {"i": 0}

    async def call():
        await asyncio.sleep(0)
        index = min(state["i"], len(errors) - 1)
        state["i"] += 1
        err = errors[index]
        if err is None:
            return "доставлено"
        raise err() if isinstance(err, type) else err

    return call, state


print("\n[1] Импорт безвреден: ни сети, ни конфигурации, ни настройки логирования")
# Замер 28 июля 2026 по этому проекту: импорт patch_assistant.py ПЕРЕПИСАЛ
# assistant.py, benchmark.py отправил платные запросы в Groq, delist.py сходил в
# API Google. Один импорт стоил испорченного боевого файла и квоты. Для tg_safety
# это критично отдельно: его будут импортировать из assistant.py и main.py, то
# есть на пути подъёма бота.
_probe = (
    "import sys, logging, json;"
    "root_before = len(logging.getLogger().handlers);"
    "import tg_safety;"
    "print(json.dumps({"
    "'telethon': 'telethon' in sys.modules,"
    "'root_handlers_added': len(logging.getLogger().handlers) - root_before,"
    "'tg_logger_handlers': len(logging.getLogger('tg_safety').handlers),"
    "'level_forced': logging.getLogger().level,"
    "}))"
)
HERE = os.path.dirname(os.path.abspath(__file__))


class _Dead:
    """Заглушка на случай, если проба не уложилась в срок: см. probe()."""

    returncode = -1
    stdout = ""
    stderr = "проба не уложилась в отведённое время"


def probe(source, seconds=60):
    """
    Прогнать пробу отдельным процессом.

    Свой срок обязателен, и превышение обязано превращаться в строку [FAIL], а
    не в падение набора: сломанный модуль, который спит на FloodWait без оглядки
    на бюджет, подвешивал ровно эту пробу, и набор умирал до сводки — то есть
    диверсия не давала ни одной строки [FAIL].
    """
    try:
        return subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=HERE, timeout=seconds,
        )
    except subprocess.TimeoutExpired:
        return _Dead()


_out = probe(_probe)
check("отдельный процесс с импортом tg_safety завершился без ошибок",
      _out.returncode == 0, (_out.stderr or "")[-300:])
_facts = {}
if _out.returncode == 0:
    import json as _json

    try:
        _facts = _json.loads((_out.stdout or "").strip().split("\n")[-1])
    except Exception as exc:
        check("вывод пробы разобран", False, f"{exc}: {_out.stdout[-200:]}")
if _facts:
    check("импорт tg_safety НЕ подтягивает telethon",
          _facts["telethon"] is False,
          "ленивое разрешение классов сломано: модуль стал зависеть от пакета, "
          "которого может не быть")
    check("импорт не добавляет обработчиков в корневой логгер",
          _facts["root_handlers_added"] == 0,
          "модуль настраивает логирование за бота и перебьёт runtime_guard")
    check("импорт не навешивает свой обработчик",
          _facts["tg_logger_handlers"] == 0)
    check("импорт не задаёт уровень корневого логгера",
          _facts["level_forced"] == logging.WARNING,
          f"корневой уровень стал {_facts.get('level_forced')}")

_src = io.open("tg_safety.py", encoding="utf-8").read()
check("в модуле нет обращений к сети мимо telethon",
      not any(bad in _src for bad in ("import requests", "import socket",
                                      "import httpx", "urllib.request")),
      "появилась вторая сетевая зависимость")
check("модуль не читает конфигурацию бота",
      "import config" not in _src and "from config" not in _src,
      "чтение конфигурации на импорте — побочный эффект")
check("модуль не настраивает логирование",
      "basicConfig" not in _src and "setLevel(" not in _src,
      "модуль перебивает настройку логирования бота")


print("\n[2] Разбор ошибки: повторяемое отделено от терминального")
# Замер: 49 из 51 строки «Telegram is having internal issues» в bot.log.1 — это
# ServerError и его наследники (RpcCallFailError 20, PersistentTimestampOutdated 20,
# ServerError -500 «No workers running» 9). То есть повторяемый класс здесь
# основной, а не теоретический. Терминальных (ChatWriteForbidden, MsgIdInvalid,
# auth) в журналах ноль — и это ровно причина их различать: повтор такой ошибки
# просидит весь бюджет врача и всё равно не доставит ничего.
check("FloodWait — повторяемый", T.classify(FloodWaitError(30)) == T.KIND_FLOOD)
check("SlowModeWait — повторяемый",
      T.classify(SlowModeWaitError(5)) == T.KIND_FLOOD)
check("секунды берутся у сервера, а не выдумываются",
      T.flood_wait_seconds(FloodWaitError(45)) == 45.0)
check("не-flood не даёт секунд", T.flood_wait_seconds(ServerError()) is None)
check("нечисловые секунды отвергаются",
      T.flood_wait_seconds(FloodWaitError("сорок пять")) is None,
      "мусор в поле секунд привёл бы к сну на неизвестный срок")
check("ServerError — повторяемый",
      T.classify(ServerError()) == T.KIND_TRANSIENT)
check("наследник ServerError тоже повторяемый (замер: 20 RpcCallFailError)",
      T.classify(RpcCallFailError()) == T.KIND_TRANSIENT)
check("обрыв соединения повторяемый (замер: 51542 строки)",
      T.classify(ConnectionResetError()) == T.KIND_TRANSIENT)
check("таймаут транспорта повторяемый (замер: 1171 строка)",
      T.classify(TimeoutError()) == T.KIND_TRANSIENT)
check("ChatWriteForbidden терминальный",
      T.classify(ChatWriteForbiddenError()) == T.KIND_TERMINAL)
check("MsgIdInvalid терминальный",
      T.classify(MsgIdInvalidError()) == T.KIND_TERMINAL)
check("ошибка ключа сессии терминальная",
      T.classify(AuthKeyUnregisteredError()) == T.KIND_TERMINAL)
check("неизвестный класс считается терминальным",
      T.classify(WhoKnowsError()) == T.KIND_TERMINAL,
      "повтор непонятной ошибки молча просидит весь бюджет врача")
check("при совпадении с обоими списками терминальное сильнее повторяемого",
      T.classify(ChatWriteForbiddenResetError()) == T.KIND_TERMINAL,
      "класс с терминальным именем в MRO пошёл на повтор: бюджет будет сожжён "
      "на ошибке, которая не пройдёт никогда")
check("is_retryable согласован с classify",
      T.is_retryable(ServerError()) and T.is_retryable(FloodWaitError(1))
      and not T.is_retryable(ChatWriteForbiddenError()))

# Настоящие классы telethon, если пакет есть: проверяем ветку isinstance, а не
# только имена. Без пакета раздел пропускается — и это тоже требование.
try:
    from telethon import errors as _tl_errors
except Exception:
    _tl_errors = None
    print("      telethon недоступен — ветка isinstance пропущена (это допустимо)")
if _tl_errors is not None:
    _flood = _tl_errors.FloodWaitError(request=None, capture=42)
    check("настоящий telethon FloodWaitError разобран",
          T.classify(_flood) == T.KIND_FLOOD and T.flood_wait_seconds(_flood) == 42.0)
    # Ровно тот текст, который замерен в bot.log.1 девять раз.
    check("настоящий telethon ServerError повторяемый",
          T.classify(_tl_errors.ServerError(
              request=None, message="No workers running")) == T.KIND_TRANSIENT)
    check("настоящий ChatWriteForbiddenError терминальный",
          T.classify(_tl_errors.ChatWriteForbiddenError(request=None)) == T.KIND_TERMINAL)
    check("настоящий MsgIdInvalidError терминальный",
          T.classify(_tl_errors.MsgIdInvalidError(request=None)) == T.KIND_TERMINAL)

# Без telethon модуль обязан работать целиком. Проверяем в отдельном процессе,
# запретив импорт пакета.
_no_tl = "\n".join([
    "import sys, asyncio, json",
    # Так Python поднимает ImportError на любую попытку импорта пакета.
    "sys.modules['telethon'] = None",
    "import tg_safety as T",
    "class FloodWaitError(Exception):",
    "    def __init__(self, sec):",
    "        self.seconds = sec",
    "async def boom():",
    "    raise FloodWaitError(5)",
    "res = asyncio.run(T.guard(lambda: boom(), op='t', chat_id=1, timeout=0.5))",
    "print(json.dumps({'reason': res.reason, 'kind': res.kind,",
    "                  'classes': T._telethon_classes()['flood'] == ()}))",
])
_out2 = probe(_no_tl, seconds=30)
check("без telethon модуль не падает на импорте и работает",
      _out2.returncode == 0, (_out2.stderr or "")[-300:])
if _out2.returncode == 0:
    import json as _json2

    _f2 = _json2.loads((_out2.stdout or "").strip().split("\n")[-1])
    check("без telethon набор классов пуст, а не наполовину разрешён",
          _f2["classes"] is True)
    check("без telethon FloodWait всё равно распознан по имени",
          _f2["kind"] == T.KIND_FLOOD and _f2["reason"] == T.REASON_FLOOD_OVER_BUDGET,
          f"получено {_f2}")


print("\n[3] Успех проходит насквозь и не выдумывает отказов")
_call, _ = raiser(None)
_res, _spent = timed(T.guard(_call, op="send_message", chat_id=777, timeout=1.0))
check("успешный вызов возвращает ok", _res.ok is True)
check("значение операции возвращается вызывающему", _res.value == "доставлено")
check("успех — одна попытка", _res.attempts == 1, f"attempts={_res.attempts}")
check("успех не тратит бюджет на сон", _spent < 0.3, f"{_spent:.3f} с")
check("итог приводится к bool", bool(_res) is True)
check("на успехе врач не помечен виноватым", _res.user_at_fault is False)


print("\n[4] timeout — ПОЛНЫЙ бюджет операции, а не бюджет попытки")
# Дефект вложенных бюджетов всплывал в этой кодовой базе четыре раза. Здесь он
# был бы особенно дорог: сон на FloodWait идёт ВНУТРИ вызова (telethon при
# request_retries=10 и flood_sleep_threshold=60 отсыпает до 600 с молча), а
# сверху стоят чужие потолки — PING_PHASE_TIMEOUT_SECONDS=1200 у пингов и
# терпение сторожа у сводки. Бюджет на попытку означал бы, что 5 попыток по 90 с
# дают 450 с там, где вызывающий отмерил 90.
_hang_calls = {"n": 0}


async def _hangs():
    _hang_calls["n"] += 1
    await asyncio.sleep(30)


_res, _spent = timed(T.guard(lambda: _hangs(), op="send_message", chat_id=1,
                             timeout=0.6))
check("зависший вызов снимается по дедлайну", _res.ok is False)
check("причина отказа — исчерпанный бюджет", _res.reason == T.REASON_TIMEOUT,
      f"reason={_res.reason}")
check("зависание снято около дедлайна, а не позже",
      0.5 <= _spent <= 1.3, f"{_spent:.3f} с при бюджете 0.6 с")
check("зависание не повторяется вслепую", _res.attempts == 1,
      f"attempts={_res.attempts}")
check("замер elapsed в итоге совпадает со стенным временем",
      abs(_res.elapsed - _spent) < 0.2,
      f"elapsed={_res.elapsed:.3f} стенное={_spent:.3f}")

# Многократный FloodWait по 0.15 с в бюджете 2.5 с: спит и повторяет, но сумма
# всех сонов и попыток обязана остаться внутри 2.5 с.
_call, _state = raiser(FloodWaitError(0.15))
_res, _spent = timed(T.guard(_call, op="send_message", chat_id=2, timeout=2.5,
                             cooldown=None))
check("повторов было несколько (бюджет пилится, а не тратится за раз)",
      _res.attempts >= 3, f"attempts={_res.attempts}")
check("сумма всех сонов и попыток НЕ вышла за общий бюджет",
      _spent <= 2.5 + 0.4, f"{_spent:.3f} с при бюджете 2.5 с")
check("модуль действительно спал, а не крутил повторы вплотную",
      _spent >= 0.3, f"{_spent:.3f} с")
check("отказ по исчерпанию бюджета на flood назван своим именем",
      _res.reason == T.REASON_FLOOD_OVER_BUDGET, f"reason={_res.reason}")
check("число попыток в итоге совпадает с числом реальных вызовов",
      _res.attempts == _state["i"], f"{_res.attempts} против {_state['i']}")

# Ключевая часть: последней попытке достаётся ОСТАТОК бюджета, а не весь бюджет
# заново. Первая версия набора этого не ловила — диверсия «срок на каждую попытку»
# проходила её целиком, потому что ни в одном сценарии зависание не следовало за
# повторами. Здесь три мгновенных обрыва съедают откатами 0.75 с из 1.2 с, и
# зависший четвёртый вызов обязан быть снят через ~0.45 с, а не через 1.2 с.
_hang_span = {"seconds": None}
_stage = {"n": 0}


async def _fails_then_hangs():
    _stage["n"] += 1
    if _stage["n"] <= 3:
        raise ConnectionResetError()
    began = time.monotonic()
    try:
        await asyncio.sleep(30)
    finally:
        _hang_span["seconds"] = time.monotonic() - began


_res, _spent = timed(T.guard(lambda: _fails_then_hangs(), op="send_message",
                             chat_id=7, timeout=1.2, cooldown=None,
                             transient_backoff=0.25, min_retry_slice=0.05,
                             max_transient_attempts=9))
check("зависание после повторов случилось на четвёртой попытке",
      _res.attempts == 4 and _stage["n"] == 4,
      f"attempts={_res.attempts} вызовов={_stage['n']}")
check("последней попытке достался ОСТАТОК бюджета, а не бюджет целиком",
      _hang_span["seconds"] is not None and _hang_span["seconds"] < 0.8,
      f"зависший вызов держали {_hang_span['seconds']} с при остатке ~0.45 с "
      f"от бюджета 1.2 с — значит срок выдаётся на каждую попытку заново")
check("общее время не вышло за бюджет, несмотря на 4 попытки и 3 отката",
      _spent <= 1.6, f"{_spent:.3f} с при бюджете 1.2 с")


print("\n[5] FloodWait дольше остатка бюджета НЕ усыпляет")
# Главная проверка набора. Сценарий: врач в ЛС нажал /quiz. assistant.py:2846
# отправляет статус «Генерирую клиническую викторину…». Telegram отвечает
# FLOOD_WAIT_45. Сейчас 45 <= flood_sleep_threshold (60, замерено по
# inspect.signature(TelegramBaseClient.__init__)), поэтому telethon спит 45 с и
# повторяет — до request_retries=10, то есть до 600 с внутри ОДНОЙ строки, без
# возможности прерывания и без строки в журнале (runtime_guard.py:79 глушит INFO
# telethon). Кулдаун /quiz — 60 с, он уже истёк, и повторное нажатие заводит
# второй такой же висящий вызов.
# Спать дольше, чем отмерил вызывающий, бессмысленно всегда: наверху сработает
# свой таймаут, готовый (уже оплаченный) ответ будет выброшен, а секунды сна
# потрачены впустую.
T.reset_cooldowns()
_call, _state = raiser(FloodWaitError(45))
_res, _spent = timed(T.guard(_call, op="send_message:quiz_status", chat_id=555,
                             timeout=0.5))
check("сна не было: вернулись быстрее, чем длится сам FloodWait",
      _spent < 0.3, f"{_spent:.3f} с при FLOOD_WAIT_45")
check("сна не было: вернулись даже раньше конца своего бюджета",
      _spent < 0.5, f"{_spent:.3f} с при бюджете 0.5 с")
check("отказ, а не тихое ожидание", _res.ok is False)
check("причина названа: ожидание не влезает в бюджет",
      _res.reason == T.REASON_FLOOD_OVER_BUDGET, f"reason={_res.reason}")
check("серверное ожидание сохранено в итоге для вызывающего",
      _res.flood_seconds == 45.0, f"flood_seconds={_res.flood_seconds}")
check("повтор не заводился: вторая попытка была бы вторым висящим вызовом",
      _state["i"] == 1, f"вызовов {_state['i']}")
check("вид ошибки — flood, чтобы вызывающий не списал это на врача",
      _res.kind == T.KIND_FLOOD)

# Тот же FloodWait, но бюджет позволяет его выждать — тогда спим и повторяем.
_call, _state = raiser(FloodWaitError(0.2), None)
_res, _spent = timed(T.guard(_call, op="send_message", chat_id=556, timeout=3.0,
                             cooldown=None))
check("FloodWait внутри бюджета выжидается и вызов повторяется",
      _res.ok is True and _res.attempts == 2,
      f"ok={_res.ok} attempts={_res.attempts}")
check("выжидание было настоящим (не меньше присланных секунд)",
      _spent >= 0.2, f"{_spent:.3f} с при FLOOD_WAIT 0.2 с")
check("выжидание не превратилось в сон на весь бюджет",
      _spent < 1.5, f"{_spent:.3f} с")

# Ожидание ровно на границе: спать до самого дедлайна тоже нельзя — проснёмся
# без времени на сам запрос.
_call, _state = raiser(FloodWaitError(1.0))
_res, _spent = timed(T.guard(_call, op="send_message", chat_id=557, timeout=1.2,
                             cooldown=None))
check("ожидание, после которого не осталось времени на запрос, не выжидается",
      _res.reason == T.REASON_FLOOD_OVER_BUDGET and _spent < 0.3,
      f"reason={_res.reason} {_spent:.3f} с")

# FloodWait без пригодного значения секунд: спать наугад нельзя.
_call, _ = raiser(FloodWaitError(None))
_res, _spent = timed(T.guard(_call, op="send_message", chat_id=558, timeout=1.0,
                             cooldown=None))
check("FloodWait без секунд не приводит к сну наугад",
      _res.reason == T.REASON_FLOOD_UNKNOWN_WAIT and _spent < 0.3,
      f"reason={_res.reason} {_spent:.3f} с")


print("\n[6] Терминальную ошибку не повторяем: повтор только сжигает дедлайн")
# ChatWriteForbidden, MsgIdInvalid и ошибки ключа сессии в журналах не
# встретились ни разу — ноль за 122049 строк. Но если такое случится, повтор с
# откатом просидит весь бюджет врача и всё равно не доставит ничего. Худший
# случай — assistant.py:3729: сводка уже сгенерирована LLM, единственный путь
# доставки — этот edit_message, и время, потраченное на повторы, отнимается у
# самой доставки.
for _err, _label in ((ChatWriteForbiddenError, "ChatWriteForbidden"),
                     (MsgIdInvalidError, "MsgIdInvalid"),
                     (AuthKeyUnregisteredError, "ошибка ключа сессии"),
                     (WhoKnowsError, "неизвестный класс")):
    _call, _state = raiser(_err)
    _res, _spent = timed(T.guard(_call, op="edit_message", chat_id=3, timeout=3.0,
                                 cooldown=None))
    check(f"{_label}: ровно одна попытка",
          _state["i"] == 1, f"вызовов {_state['i']}")
    check(f"{_label}: отказ мгновенный, бюджет не сожжён",
          _res.ok is False and _spent < 0.3, f"{_spent:.3f} с")
    check(f"{_label}: причина — терминальная ошибка",
          _res.reason == T.REASON_TERMINAL, f"reason={_res.reason}")

# Обрыв связи, наоборот, повторяется — и внутри того же общего бюджета.
_call, _state = raiser(ConnectionResetError, ConnectionResetError, None)
_res, _spent = timed(T.guard(_call, op="send_message", chat_id=4, timeout=3.0,
                             cooldown=None, transient_backoff=0.05,
                             min_retry_slice=0.05))
check("обрыв связи повторяется и вызов в итоге проходит",
      _res.ok is True and _res.attempts == 3,
      f"ok={_res.ok} attempts={_res.attempts}")
check("повтор обрыва уложился в бюджет", _spent < 1.5, f"{_spent:.3f} с")

_call, _state = raiser(ServerError)
_res, _spent = timed(T.guard(_call, op="send_message", chat_id=5, timeout=3.0,
                             cooldown=None, transient_backoff=0.05,
                             min_retry_slice=0.05, max_transient_attempts=3))
check("бесконечный ServerError прекращается по потолку попыток",
      _res.reason == T.REASON_TRANSIENT_ATTEMPTS, f"reason={_res.reason}")
check("потолок попыток соблюдён точно", _state["i"] == 3,
      f"вызовов {_state['i']} при потолке 3")
check("остановка по попыткам не тратит весь бюджет", _spent < 1.5,
      f"{_spent:.3f} с при бюджете 3.0 с")

_call, _state = raiser(ServerError)
_res, _spent = timed(T.guard(_call, op="send_message", chat_id=6, timeout=0.4,
                             cooldown=None))
check("на короткий бюджет откат не заводится вовсе",
      _res.reason == T.REASON_TRANSIENT_NO_BUDGET and _state["i"] == 1,
      f"reason={_res.reason} вызовов {_state['i']}")


print("\n[7] Ни одного молчаливого отказа")
# Замер, из-за которого это обязательно: assistant.py:1857-1869 — два пустых
# `except Exception: pass` вокруг get_messages и get_me на пути клинического
# снимка. Отказ там оставляет is_direct_reply=False и is_mentioned=False, врач
# уходит в пассивную ветку с двухчасовым кулдауном, и в журнале НЕТ НИЧЕГО:
# собственные INFO telethon тоже подавлены (runtime_guard.py:79). Врач видит,
# что бот проигнорировал прямой вопрос со снимком.
for _err, _label in ((FloodWaitError(5), "flood дольше бюджета"),
                     (ChatWriteForbiddenError, "терминальная ошибка"),
                     (ServerError, "исчерпанный транспорт")):
    _log, _rec = recorder()
    _call, _ = raiser(_err)
    _res = run(T.guard(_call, op="send_message:snapshot", chat_id=-1001820467444,
                       timeout=0.4, logger=_log, cooldown=None))
    _warnings = _rec.warnings()
    check(f"{_label}: отказ записан на уровне WARNING",
          len(_warnings) == 1, f"записей WARNING: {len(_warnings)}")
    _text = _rec.text()
    check(f"{_label}: в записи есть имя операции",
          "send_message:snapshot" in _text)
    check(f"{_label}: в записи есть chat id",
          "-1001820467444" in _text)
    check(f"{_label}: в записи есть затраченное время",
          "elapsed=" in _text and "s " in _text)
    check(f"{_label}: в записи есть причина",
          f"reason={_res.reason}" in _text)
    check(f"{_label}: в записи есть класс ошибки",
          type(_res.error).__name__ in _text if _res.error else True)

_log, _rec = recorder()
_call, _ = raiser(None)
run(T.guard(_call, op="send_message", chat_id=1, timeout=1.0, logger=_log))
check("успех не засоряет журнал предупреждениями",
      len(_rec.warnings()) == 0, f"записей WARNING: {len(_rec.warnings())}")

_log, _rec = recorder()
_call, _ = raiser(ConnectionResetError, None)
run(T.guard(_call, op="send_message", chat_id=1, timeout=2.0, logger=_log,
            transient_backoff=0.05, min_retry_slice=0.05))
check("восстановление после обрыва видно в журнале",
      "tg recovered" in _rec.text(), _rec.text()[:200])


print("\n[8] Кулдаун после flood: по ключу чата, чаты друг друга не глушат")
# Замер по исходникам telethon 1.42.0: кэш flood-ожиданий заведён по
# `CONSTRUCTOR_ID` запроса (client/users.py), то есть один FloodWait на
# SendMessageRequest роняет FloodWaitError на ЛЮБУЮ следующую отправку в ЛЮБОЙ
# чат. Именно поэтому у групповых пингов (assistant.py:5341) после первого
# FloodWait весь остаток пачки падает мгновенно и 30-50 врачам разом ставится
# +1 отказ. Наш кулдаун обязан быть устроен наоборот.
_cd = T.FloodCooldown(max_entries=4)
_call_a, _state_a = raiser(FloodWaitError(30))
_res_a = run(T.guard(_call_a, op="send_message", chat_id=111, timeout=0.4,
                     cooldown=_cd))
check("после flood чат закрыт кулдауном",
      _cd.remaining(111) > 0, f"remaining={_cd.remaining(111)}")
check("другой чат кулдауном НЕ закрыт", _cd.remaining(222) == 0.0,
      "остановка одного чата глушит остальные — это ровно дефект telethon")
_call_b, _state_b = raiser(None)
_res_b = run(T.guard(_call_b, op="send_message", chat_id=222, timeout=1.0,
                     cooldown=_cd))
check("отправка в другой чат проходит, пока первый на кулдауне",
      _res_b.ok is True)
_call_a2, _state_a2 = raiser(None)
_res_a2 = run(T.guard(_call_a2, op="send_message", chat_id=111, timeout=1.0,
                      cooldown=_cd))
check("повторное обращение к закрытому чату не идёт в сеть вовсе",
      _state_a2["i"] == 0 and _res_a2.reason == T.REASON_FLOOD_COOLDOWN,
      f"вызовов {_state_a2['i']} reason={_res_a2.reason}")
check("вызов на кулдауне не молчит", _res_a2.ok is False)

_cd.clear(111)
check("снятие кулдауна работает точечно", _cd.remaining(111) == 0.0)

_cd_small = T.FloodCooldown(max_entries=3)
for _i in range(40):
    _cd_small.block(_i, 60)
check("память кулдауна ограничена потолком", len(_cd_small) <= 3,
      f"записей {len(_cd_small)} при потолке 3")
_cd_expire = T.FloodCooldown(max_entries=100)
_cd_expire.block(1, 0.05)
time.sleep(0.1)
check("истёкшая запись перестаёт держать чат", _cd_expire.remaining(1) == 0.0)
_cd_none = T.FloodCooldown()
_cd_none.block(None, 30)
check("chat_id=None не создаёт общей записи на всех",
      len(_cd_none) == 0 and _cd_none.remaining(None) == 0.0,
      "безымянный ключ стал бы общим тормозом для всех чатов")


print("\n[9] Вина врача отделена от вины Telegram")
# Замер: assistant.py:5348 (групповые пинги) и 5168 (ЛС-пинги) ловят
# `except Exception` и делают ping_failures += 1 на ЛЮБОМ отказе. Комментарий на
# 5169 сам перечисляет FloodWait среди причин. MAX_PING_FAILURES = 3, и на
# третьем отказе assistant.py:5318 навсегда исключает врача из кандидатов:
# сброс счётчика бывает только на успешной отправке, а её больше не будет —
# врача уже не выбирают. Живой врач, который бота не блокировал, теряет
# приглашения в чат навсегда.
def _outcome_for(err):
    _call, _ = raiser(err)
    return run(T.guard(_call, op="send_message:group_ping", chat_id=9,
                       timeout=0.4, cooldown=None))


check("FloodWait НЕ считается виной врача",
      _outcome_for(FloodWaitError(5)).user_at_fault is False,
      "врача исключат из приглашений за лимит Telegram")
check("обрыв связи НЕ считается виной врача",
      _outcome_for(ServerError).user_at_fault is False)
check("исчерпанный бюджет НЕ считается виной врача",
      T.guard is not None and run(T.guard(lambda: _hangs(), op="p", chat_id=9,
                                          timeout=0.3)).user_at_fault is False)
check("UserIsBlocked — вина врача (он действительно заблокировал бота)",
      _outcome_for(UserIsBlockedError).user_at_fault is True,
      "иначе счётчик отказов не сработает никогда и заблокировавший останется "
      "вечным кандидатом")
check("неизвестный peer — вина врача (так его ловит текущий код)",
      _outcome_for(ValueError("Could not find the input entity for "
                              "PeerUser(user_id=1)")).user_at_fault is True)
check("прочий ValueError виной врача не считается",
      _outcome_for(ValueError("что-то другое")).user_at_fault is False)
check("flooded отмечает именно flood",
      _outcome_for(FloodWaitError(5)).flooded is True
      and _outcome_for(ServerError).flooded is False)


print("\n[10] Готовая корутина отвергается: повтор был бы невозможен")
# Корутину нельзя дождаться дважды. Если бы guard принимал готовую корутину,
# первый же повтор упал бы на RuntimeError «cannot reuse already awaited
# coroutine» — то есть повтор существовал бы только на бумаге.
async def _probe_type_error():
    coro = _hangs()
    try:
        await T.guard(coro, op="send_message", chat_id=1, timeout=1.0)
        return "TypeError не поднят: guard принял готовую корутину"
    except TypeError as exc:
        return str(exc)
    except BaseException as exc:
        # Любая другая ошибка — тоже провал, но она не имеет права уронить
        # набор: тогда вместо строки [FAIL] был бы просто ненулевой код выхода.
        return f"вместо TypeError получено {type(exc).__name__}: {exc}"
    finally:
        if asyncio.iscoroutine(coro):
            coro.close()


_msg = run(_probe_type_error())
check("готовая корутина отвергнута с TypeError", "guard ожидает функцию" in _msg,
      f"получено: {_msg}")
check("в отказе объяснено, что повтор был бы невозможен",
      "повтор был бы невозможен" in _msg, f"получено: {_msg}")


print("\n[11] Отмена снаружи не проглатывается и не оставляет висящих задач")
# runtime_guard.create_task снимает фоновые задачи, а PING_PHASE_TIMEOUT_SECONDS
# = 1200 снимает проход пингов целиком (main.py:773-800). Если guard проглотит
# отмену, снятая задача продолжит жить и будет писать врачу от имени уже
# отменённой работы.
async def _cancel_probe():
    started_flag = {"inside": False, "finished": False}

    async def slow():
        started_flag["inside"] = True
        try:
            await asyncio.sleep(5)
        finally:
            started_flag["finished"] = True
        return "поздно"

    task = asyncio.ensure_future(
        T.guard(lambda: slow(), op="send_message", chat_id=1, timeout=30)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
        outcome = "отмена проглочена"
    except asyncio.CancelledError:
        outcome = "отмена доведена"
    await asyncio.sleep(0.1)
    return outcome, started_flag


_outcome, _flags = run(_cancel_probe())
check("отмена снаружи доведена до вызывающего", _outcome == "отмена доведена",
      f"получено: {_outcome}")
check("внутренняя операция действительно запускалась", _flags["inside"] is True)
check("внутренняя задача не осталась висеть после отмены",
      _flags["finished"] is True,
      "снятая работа продолжает жить и может написать врачу от имени отменённой")


# Второй путь отмены: снимают не guard, а саму операцию внутри. Первая версия
# набора его не проверяла, и диверсия «отмену превратить в отказ» проходила.
async def _inner_cancel_probe():
    async def cancelled_inside():
        raise asyncio.CancelledError()

    try:
        res = await T.guard(lambda: cancelled_inside(), op="send_message",
                            chat_id=1, timeout=1.0)
        return f"отмена превращена в отказ: reason={res.reason}"
    except asyncio.CancelledError:
        return "отмена доведена"


check("отмена самой операции тоже доводится, а не превращается в отказ",
      run(_inner_cancel_probe()) == "отмена доведена",
      f"получено: {run(_inner_cancel_probe())}")


print("\n[12] Тонкие обёртки бьют в те же границы")


class _FakeClient:
    """Минимальный двойник клиента: запоминает вызовы, ничего не отправляет."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def _act(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))
        if self.error:
            raise self.error
        return f"{name}:ok"

    async def send_message(self, entity=None, message=None, **kwargs):
        return await self._act("send_message", (entity, message), kwargs)

    async def edit_message(self, chat, message_id, text, **kwargs):
        return await self._act("edit_message", (chat, message_id, text), kwargs)

    async def delete_messages(self, chat, ids, **kwargs):
        return await self._act("delete_messages", (chat, ids), kwargs)


_client = _FakeClient()
_res = run(T.send_message(_client, 42, "привет", parse_mode="html", timeout=1.0))
check("send_message доходит до клиента с теми же аргументами",
      _res.ok and _client.calls[0][1] == (42, "привет")
      and _client.calls[0][2].get("parse_mode") == "html",
      f"{_client.calls}")
_res = run(T.edit_message(_client, 42, 7, "новый текст", timeout=1.0))
check("edit_message доходит до клиента",
      _res.ok and _client.calls[1][1] == (42, 7, "новый текст"))
_res = run(T.delete_messages(_client, 42, [7], timeout=1.0))
check("delete_messages доходит до клиента",
      _res.ok and _client.calls[2][1] == (42, [7]))

_bad = _FakeClient(error=FloodWaitError(5))
_res, _spent = timed(T.send_message(_bad, 42, "привет", timeout=0.5))
check("обёртка тоже не спит дольше бюджета",
      _res.reason == T.REASON_FLOOD_OVER_BUDGET and _spent < 0.3,
      f"reason={_res.reason} {_spent:.3f} с")
T.reset_cooldowns()


print("\n[13] Диверсия: проверки выше действительно ловят сломанное поведение")
# Все проверки выше — про поведение, и ноль провалов одинаково выглядит у
# работающего модуля и у слепого набора. Поэтому здесь рядом с настоящим guard
# гоняется наивная обёртка — ровно то, что написали бы «на скорую руку»: таймаут
# на попытку вместо общего бюджета и безусловный сон на FloodWait. Она обязана
# провалить те же самые утверждения. Печатать [FAIL] здесь нельзя: сводный
# прогон считает такие строки и показал бы ложный провал набора.
async def _naive(make, timeout, attempts=3):
    """Как НЕ надо: бюджет на попытку, сон на FloodWait без оглядки на дедлайн."""
    for _ in range(attempts):
        try:
            return await asyncio.wait_for(make(), timeout=timeout)
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
        except Exception:
            await asyncio.sleep(0.05)
    return None


_call, _ = raiser(FloodWaitError(0.6))
_began = time.monotonic()
run(_naive(_call, timeout=0.5))
_naive_flood_spent = time.monotonic() - _began
check("наивная обёртка спит дольше выданного бюджета — проверка [5] это ловит",
      _naive_flood_spent > 0.5,
      f"наивная обёртка уложилась в {_naive_flood_spent:.3f} с — проверка [5] "
      f"слепа и пропустит сон за дедлайном")

_began = time.monotonic()
run(_naive(lambda: _hangs(), timeout=0.4))
_naive_hang_spent = time.monotonic() - _began
check("наивная обёртка тратит бюджет на каждую попытку — проверка [4] это ловит",
      _naive_hang_spent > 0.4 * 2,
      f"три попытки по 0.4 с заняли {_naive_hang_spent:.3f} с — проверка [4] "
      f"не отличила бы общий бюджет от бюджета на попытку")

_call, _state = raiser(ChatWriteForbiddenError)
run(_naive(_call, timeout=0.5))
check("наивная обёртка повторяет терминальную ошибку — проверка [6] это ловит",
      _state["i"] > 1,
      f"вызовов {_state['i']} — проверка [6] не увидела бы разницы между "
      f"терминальным и повторяемым")


print("\n[14] Отказы без явного logger попадают в собственный логгер модуля")
# Большинство мест правки не будут передавать logger, а значит отказ уйдёт в
# logging.getLogger("tg_safety"). Если модуль вдруг начнёт писать в root или
# заведёт свой обработчик, отказ либо потеряется в чужой настройке, либо
# продублируется. Все отказы разделов [8], [9], [12] шли именно этим путём.
_own = _OWN_RECORDER.warnings()
check("отказы без logger собраны в логгере tg_safety", len(_own) >= 8,
      f"записей WARNING: {len(_own)}")
check("каждая такая запись содержит операцию, чат, время и причину",
      all(("op=" in r.getMessage() and "chat_id=" in r.getMessage()
           and "elapsed=" in r.getMessage() and "reason=" in r.getMessage())
          for r in _own),
      "в записи не хватает обязательных полей")
check("сообщение об отказе начинается опознаваемо (можно грепать журнал)",
      all(r.getMessage().startswith("tg give up ") for r in _own))

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
