"""
Граница вокруг вызовов Telegram: полный бюджет, разбор ошибки, громкий отказ.

Зачем модуль существует. Замер по журналам этого бота (2026-01-31 .. 2026-07-28):

  * `Server closed the connection` — 51542 строки в bot.log.1 и 181 в bot.log;
  * `Attempt N at connecting failed` — 2447 и 26 (из них 1187 ConnectionAbortedError,
    1171 TimeoutError);
  * `Telegram is having internal issues` — 51 строка, из них 49 это ServerError и его
    наследники (RpcCallFailError, PersistentTimestampOutdatedError), включая девять
    `ServerError: RPCError -500: No workers running (caused by GetUsersRequest)`;
  * flood — 3 события, и только на транспортном уровне (HTTP 429). RPC-уровень с
    секундами по этим журналам замерить НЕЛЬЗЯ: runtime_guard.py:79 глушит логгер
    `telethon` до ERROR, а telethon сообщает о сне на FloodWait через INFO
    (`client/users.py`). Ноль строк в журнале не значит ноль FloodWait;
  * ChatWriteForbidden, MsgIdInvalid, SlowMode, UserIsBlocked, auth — ноль за оба
    журнала. Различать их всё равно обязательно: повтор терминальной ошибки только
    сжигает дедлайн, за который врач ждёт ответа.

То есть основной отказ здесь — обрыв связи, а не flood. И именно он подвешивает вызов
надолго: у обоих клиентов `timeout=30, request_retries=10, connection_retries=1000,
retry_delay=5` (main.py), а `flood_sleep_threshold` не задан, значит равен 60. Проверено
по исходникам telethon 1.42.0: FloodWait <= 60 с telethon отсыпает САМ внутри цикла
`request_retries`, то есть до 600 с внутри ОДНОГО `await send_message(...)`, молча.
В assistant.py при 122 вызовах Telegram под `asyncio.wait_for` находятся ровно два, и
оба — скачивание медиа; ни одной отправки. Врач видит зависший бот, в журнале пусто.

Что даёт модуль:

  1. `timeout` — ПОЛНЫЙ бюджет операции, а не бюджет попытки. Дедлайн считается один
     раз, и сон на FloodWait, откат на обрыве и все повторы вычитаются из него же.
     Вложенные бюджеты, где внутренний срок больше внешнего, — дефект, всплывавший
     в этой кодовой базе четыре раза; здесь он невозможен по конструкции.
  2. FloodWait дольше остатка бюджета НЕ усыпляет. Сон за пределами дедлайна
     вызывающего запрещён: наверху всё равно сработает свой таймаут, и время сна
     будет потрачено в пустоту, а работа (уже оплаченная генерация) — потеряна.
  3. Повторяемое отделено от терминального. Повторяются FloodWait, обрыв/таймаут
     транспорта и ServerError. Всё остальное, включая неизвестный класс, — отказ
     с первого раза.
  4. Ни одного молчаливого отказа: каждый отказ — WARNING с операцией, chat id,
     затраченным временем, числом попыток и причиной.
  5. Вина врача отделена от вины Telegram (`TgOutcome.user_at_fault`). FloodWait и
     обрыв — НЕ повод увеличивать счётчик отказов пользователю.

Ограничение, о котором надо знать: снятие вызова по дедлайну — это `task.cancel()`.
Для отправки это значит «неизвестно, ушло ли сообщение». Где дубль недопустим, отмену
обязан дополнять поиск своего сообщения — как уже сделано в summarizer.py через
`_find_recent_matching_message`.

Импорт этого модуля безвреден: ни сети, ни чтения конфигурации, ни настройки
логирования. `telethon` на импорте не подтягивается — классы исключений разрешаются
лениво и кэшируются, а без telethon классификация работает по именам классов.
"""
import asyncio
import logging
import math
import time

_LOG = logging.getLogger(__name__)

# Бюджет по умолчанию согласован с summarizer.TELEGRAM_SEND_TIMEOUT_SECONDS = 90:
# в проекте уже есть отработавший срок на отправку, второе число рядом разъедется.
DEFAULT_TIMEOUT_SECONDS = 90.0

# Сколько времени должно остаться ПОСЛЕ сна, чтобы сон вообще имел смысл. Уснуть
# и проснуться ровно на дедлайне — то же, что не спать, только бюджет потрачен.
MIN_RETRY_SLICE_SECONDS = 1.0

# Откат на транспортном сбое. Совпадает с тем, что делает сам telethon на
# ServerError (`await asyncio.sleep(2)` в client/users.py).
TRANSIENT_BACKOFF_SECONDS = 2.0

# Сколько раз повторять транспортный сбой. Обрывы здесь идут пачками (51542 за
# один журнал), и бесконечный повтор внутри бюджета врача пользы не добавляет.
MAX_TRANSIENT_ATTEMPTS = 3

# Потолок памяти кулдауна. 749 практикующих врачей — верхняя оценка числа ключей.
FLOOD_COOLDOWN_MAX_ENTRIES = 1024

KIND_FLOOD = "flood"
KIND_TRANSIENT = "transient"
KIND_TERMINAL = "terminal"

REASON_OK = "ok"
REASON_TIMEOUT = "timeout"
REASON_TERMINAL = "terminal"
REASON_FLOOD_OVER_BUDGET = "flood_over_budget"
REASON_FLOOD_UNKNOWN_WAIT = "flood_unknown_wait"
REASON_FLOOD_COOLDOWN = "flood_cooldown"
REASON_TRANSIENT_ATTEMPTS = "transient_attempts"
REASON_TRANSIENT_NO_BUDGET = "transient_no_budget"

# Классификация по ИМЕНИ класса. Нужна там, где telethon не импортируется (тесты,
# любая машина без пакета) и там, где isinstance не сработает: telethon собирает
# часть классов ошибок динамически.
_FLOOD_NAMES = frozenset({
    "FloodWaitError", "FloodPremiumWaitError", "SlowModeWaitError",
    "FloodTestPhoneWaitError", "FloodError",
})

# Терминальное: повтор не поможет никогда, а дедлайн сожжёт.
_TERMINAL_NAMES = frozenset({
    "ChatWriteForbiddenError", "ChatAdminRequiredError", "ChannelPrivateError",
    "MsgIdInvalidError", "MessageIdInvalidError", "MessageNotModifiedError",
    "MessageDeleteForbiddenError", "MessageEmptyError", "MessageTooLongError",
    "AuthKeyError", "AuthKeyUnregisteredError", "AuthKeyDuplicatedError",
    "SessionRevokedError", "SessionExpiredError", "UnauthorizedError",
    "BotMethodInvalidError", "PeerIdInvalidError", "UserIsBlockedError",
    "UserIsBotError", "InputUserDeactivatedError", "UserPrivacyRestrictedError",
    "UserDeactivatedError", "UserDeactivatedBanError", "EntityTypeError",
})

# Подмножество терминального, где отказ на стороне ВРАЧА: заблокировал бота,
# удалил аккаунт, закрылся приватностью, неизвестный peer. Только эти отказы
# имеют право увеличивать ping_failures. FloodWait и обрыв — не имеют.
_USER_FAULT_NAMES = frozenset({
    "UserIsBlockedError", "UserIsBotError", "InputUserDeactivatedError",
    "UserPrivacyRestrictedError", "UserDeactivatedError",
    "UserDeactivatedBanError", "PeerIdInvalidError",
})

# Повторяемое: сеть моргнула или у Telegram внутренние проблемы.
_TRANSIENT_NAMES = frozenset({
    "ServerError", "RpcCallFailError", "RpcMcgetFailError",
    "PersistentTimestampOutdatedError", "TimedOutError", "HistoryGetFailedError",
    "TimeoutError", "ConnectionError", "ConnectionResetError",
    "ConnectionAbortedError", "ConnectionRefusedError", "BrokenPipeError",
    "InvalidBufferError", "InvalidChecksumError", "SecurityError",
    "IncompleteReadError", "OSError", "ServerDisconnectedError",
    "DisconnectedError", "MultiError",
})

# Кэш лениво разрешённых классов telethon. None = ещё не пробовали разрешить.
_TELETHON_CLASSES = None


def _telethon_classes():
    """
    Разрешить классы исключений telethon один раз, по требованию и без падений.

    Ленивость не косметика: она делает импорт tg_safety независимым от наличия
    telethon и от его побочных эффектов. Отсутствие пакета, урезанная сборка,
    переименованный класс в новой версии — всё это даёт пустой набор, и
    классификация честно продолжает работать по именам классов.
    """
    global _TELETHON_CLASSES
    if _TELETHON_CLASSES is not None:
        return _TELETHON_CLASSES

    resolved = {"flood": (), "terminal": (), "transient": ()}
    try:
        from telethon import errors as tl_errors
    except Exception:
        _TELETHON_CLASSES = resolved
        return resolved

    def grab(*names):
        found = []
        for name in names:
            cls = getattr(tl_errors, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                found.append(cls)
        return tuple(found)

    # Порядок проверки в classify(): flood -> terminal -> transient. Базовые
    # классы взяты намеренно: FloodError покрывает все виды ожидания, а
    # BadRequestError/ForbiddenError/UnauthorizedError — весь класс «повтор
    # бессмыслен», включая те коды, которых нет в списке имён выше.
    resolved["flood"] = grab("FloodError")
    resolved["terminal"] = grab(
        "BadRequestError", "ForbiddenError", "UnauthorizedError", "AuthKeyError",
    )
    resolved["transient"] = grab("ServerError", "TimedOutError")
    _TELETHON_CLASSES = resolved
    return resolved


def _names_of(exc):
    """Имя класса и имена всех его баз: наследник ServerError тоже повторяемый."""
    return {klass.__name__ for klass in type(exc).__mro__}


def flood_wait_seconds(exc):
    """
    Сколько секунд Telegram велел ждать, или None если это не FloodWait.

    Читаем ровно то, что прислал сервер (`e.seconds` у telethon), и ничего не
    придумываем: выдуманная задержка либо разбудит нас до конца окна — и мы
    получим второй FloodWait, — либо проспит лишнее.
    """
    if not (_names_of(exc) & _FLOOD_NAMES):
        flood_classes = _telethon_classes()["flood"]
        if not (flood_classes and isinstance(exc, flood_classes)):
            return None
    raw = getattr(exc, "seconds", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        return None
    return value


def classify(exc):
    """
    KIND_FLOOD / KIND_TRANSIENT / KIND_TERMINAL.

    Неизвестный класс считается ТЕРМИНАЛЬНЫМ. Это осознанный выбор в пользу
    врача: повторять то, чего мы не понимаем, значит с высокой вероятностью
    молча просидеть весь бюджет на ошибке, которая не пройдёт. Имя класса
    попадает в журнал, и после первого же случая его можно классифицировать.
    """
    if flood_wait_seconds(exc) is not None:
        return KIND_FLOOD
    names = _names_of(exc)
    if names & _FLOOD_NAMES:
        # FloodWait без пригодного `seconds`: ждать столько же нельзя, но это
        # и не терминальный отказ — пусть вызывающий увидит именно flood.
        return KIND_FLOOD

    classes = _telethon_classes()
    if names & _TERMINAL_NAMES:
        return KIND_TERMINAL
    if classes["terminal"] and isinstance(exc, classes["terminal"]):
        return KIND_TERMINAL

    if names & _TRANSIENT_NAMES:
        return KIND_TRANSIENT
    if classes["transient"] and isinstance(exc, classes["transient"]):
        return KIND_TRANSIENT

    return KIND_TERMINAL


def is_retryable(exc):
    """Стоит ли вообще пробовать снова (в рамках оставшегося бюджета)."""
    return classify(exc) in (KIND_FLOOD, KIND_TRANSIENT)


class FloodCooldown:
    """
    «В этот чат до такого-то момента не стучимся» — по ключу чата.

    Ключ именно чат, а не тип запроса. У telethon кэш flood-ожиданий заведён по
    `CONSTRUCTOR_ID` запроса (проверено в client/users.py), поэтому один FloodWait
    на SendMessageRequest роняет отправку В ЛЮБОЙ чат. Здесь наоборот: остановка
    одного чата не имеет права глушить остальных.

    Объём ограничен: словарь без потолка — это утечка на каждого нового
    собеседника. Вытесняем сначала истёкшие, затем самые ранние по сроку.
    """

    __slots__ = ("_until", "_max_entries")

    def __init__(self, max_entries=FLOOD_COOLDOWN_MAX_ENTRIES):
        self._until = {}
        self._max_entries = max(1, int(max_entries))

    def __len__(self):
        return len(self._until)

    def remaining(self, chat_id, now=None):
        """Сколько секунд ещё нельзя обращаться к этому чату. 0.0 — можно."""
        if chat_id is None:
            return 0.0
        until = self._until.get(chat_id)
        if until is None:
            return 0.0
        left = until - (time.monotonic() if now is None else now)
        if left <= 0:
            self._until.pop(chat_id, None)
            return 0.0
        return left

    def block(self, chat_id, seconds):
        """Закрыть чат на `seconds` секунд."""
        if chat_id is None or not isinstance(seconds, (int, float)):
            return
        if not math.isfinite(float(seconds)) or seconds <= 0:
            return
        self._until[chat_id] = time.monotonic() + float(seconds)
        self._evict()

    def clear(self, chat_id=None):
        if chat_id is None:
            self._until.clear()
        else:
            self._until.pop(chat_id, None)

    def _evict(self):
        if len(self._until) <= self._max_entries:
            return
        now = time.monotonic()
        for key in [k for k, until in self._until.items() if until <= now]:
            self._until.pop(key, None)
        if len(self._until) <= self._max_entries:
            return
        # Всё ещё тесно: выбрасываем те, что истекут раньше всех.
        for key, _ in sorted(self._until.items(), key=lambda item: item[1])[
            : len(self._until) - self._max_entries
        ]:
            self._until.pop(key, None)


# Кулдаун по умолчанию. Разделяемое состояние здесь есть, но оно разложено по
# ключам чатов, поэтому два чата в работе друг друга не задевают. Вызывающий
# может передать свой экземпляр (или None, чтобы отключить кулдаун вовсе).
_DEFAULT_COOLDOWN = FloodCooldown()


def default_cooldown():
    return _DEFAULT_COOLDOWN


def reset_cooldowns():
    """Только для проверок и перезапуска: забыть все кулдауны."""
    _DEFAULT_COOLDOWN.clear()


class TgOutcome:
    """
    Итог вызова. Исключение наружу НЕ летит — иначе каждое место adoption'а
    пришлось бы обкладывать своим try/except, и мы вернулись бы к
    `except Exception: pass`, из-за которого прямое обращение врача со снимком
    отбрасывается молча (assistant.py:1866-1869).
    """

    __slots__ = ("ok", "value", "op", "chat_id", "kind", "reason", "error",
                 "elapsed", "attempts", "flood_seconds")

    def __init__(self, ok, value=None, op="", chat_id=None, kind=None,
                 reason=REASON_OK, error=None, elapsed=0.0, attempts=0,
                 flood_seconds=None):
        self.ok = ok
        self.value = value
        self.op = op
        self.chat_id = chat_id
        self.kind = kind
        self.reason = reason
        self.error = error
        self.elapsed = elapsed
        self.attempts = attempts
        self.flood_seconds = flood_seconds

    def __bool__(self):
        return bool(self.ok)

    @property
    def user_at_fault(self):
        """
        Отказ на стороне врача, а не Telegram и не наша.

        Ровно это различение отсутствует в assistant.py:5348 и 5168, где
        `except Exception` увеличивает `ping_failures` на любом отказе. Три
        FloodWait подряд — и живой врач, который бота не блокировал, навсегда
        выпадает из приглашений: сброс счётчика бывает только на успешной
        отправке, а её больше не будет.
        """
        if self.ok or self.error is None:
            return False
        if _names_of(self.error) & _USER_FAULT_NAMES:
            return True
        # Так этот отказ выглядит на практике у telethon, когда peer неизвестен;
        # существующий код ловит его именно по тексту (assistant.py:5349).
        if isinstance(self.error, ValueError):
            return "Could not find the input entity" in str(self.error)
        return False

    @property
    def flooded(self):
        return self.kind == KIND_FLOOD

    def __repr__(self):
        return (
            "TgOutcome(ok=%r op=%r chat_id=%r reason=%r kind=%r attempts=%d "
            "elapsed=%.2f error=%s)" % (
                self.ok, self.op, self.chat_id, self.reason, self.kind,
                self.attempts, self.elapsed,
                type(self.error).__name__ if self.error else None,
            )
        )


def _log_give_up(logger, outcome):
    """
    Отказ обязан быть слышен. Без этой строки зависание Telegram выглядит как
    «бот не ответил» и не диагностируется вообще: telethon свои INFO не пишет
    (runtime_guard.py:79 глушит его до ERROR).
    """
    (logger or _LOG).warning(
        "tg give up op=%s chat_id=%s reason=%s kind=%s attempts=%d "
        "elapsed=%.2fs flood_wait=%s error=%s: %s",
        outcome.op, outcome.chat_id, outcome.reason, outcome.kind,
        outcome.attempts, outcome.elapsed,
        "-" if outcome.flood_seconds is None else "%.0fs" % outcome.flood_seconds,
        type(outcome.error).__name__ if outcome.error is not None else "-",
        outcome.error if outcome.error is not None else "-",
    )


async def _run_bounded(make_awaitable, budget):
    """
    Одна попытка под потолком времени.

    Не `asyncio.wait_for`: он поднимает TimeoutError, и его невозможно отличить
    от TimeoutError, поднятого самой операцией (у telethon есть свой
    TimedOutError, а `Timeout while fetching data` в журнале замерен). Разница
    принципиальная: своё исчерпание бюджета — конец, чужой таймаут — повторяемый
    сбой. Поэтому задача и ожидание разведены явно.

    Возвращает саму задачу, если она завершилась, иначе None.
    """
    task = asyncio.ensure_future(make_awaitable())
    try:
        done, _pending = await asyncio.wait({task}, timeout=budget)
    except BaseException:
        # Отмену/ошибку снаружи не оставляем позади себя висящую задачу.
        task.cancel()
        raise
    if task in done:
        return task
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return None


async def guard(make_awaitable, op, chat_id=None, timeout=DEFAULT_TIMEOUT_SECONDS,
                logger=None, cooldown=_DEFAULT_COOLDOWN,
                max_transient_attempts=MAX_TRANSIENT_ATTEMPTS,
                transient_backoff=None, min_retry_slice=None):
    """
    Выполнить вызов Telegram в границах ПОЛНОГО бюджета `timeout`.

    `make_awaitable` — функция без аргументов, возвращающая корутину:

        res = await tg_safety.guard(
            lambda: bot_client.send_message(entity=chat_id, message=text,
                                            parse_mode='html'),
            op="send_message:quiz_status", chat_id=chat_id, timeout=90)
        if not res.ok:
            return          # причина уже в журнале, WARNING

    Именно функция, а не готовая корутина: корутину нельзя дождаться дважды, а
    повтор обязан создавать новый вызов. Передача корутины — ошибка вызывающего,
    и она поднимается сразу, а не превращается в тихую потерю повторов.

    Дедлайн один на всю операцию. Из него вычитается всё: сон на FloodWait,
    откат на обрыве, каждая попытка. Внутренний срок не может оказаться больше
    внешнего, потому что внутреннего срока просто нет.

    `transient_backoff` и `min_retry_slice` вынесены в параметры, а не только в
    константы модуля, чтобы проверки могли гонять реальные корутины за десятые
    доли секунды вместо секунд. Значения по умолчанию берутся из модуля.
    """
    if not callable(make_awaitable):
        if asyncio.iscoroutine(make_awaitable):
            # Закрываем, иначе получим RuntimeWarning про «never awaited».
            make_awaitable.close()
        raise TypeError(
            "tg_safety.guard ожидает функцию без аргументов, возвращающую "
            "корутину (например lambda: client.send_message(...)); передана "
            "готовая корутина — повтор был бы невозможен"
        )

    log = logger or _LOG
    budget = float(timeout)
    slice_floor = float(
        MIN_RETRY_SLICE_SECONDS if min_retry_slice is None else min_retry_slice
    )
    backoff_base = float(
        TRANSIENT_BACKOFF_SECONDS if transient_backoff is None else transient_backoff
    )
    started = time.monotonic()
    deadline = started + budget
    attempts = 0
    transient_seen = 0

    def outcome(ok, value=None, kind=None, reason=REASON_OK, error=None,
                flood_seconds=None):
        return TgOutcome(
            ok=ok, value=value, op=op, chat_id=chat_id, kind=kind, reason=reason,
            error=error, elapsed=time.monotonic() - started, attempts=attempts,
            flood_seconds=flood_seconds,
        )

    def give_up(kind, reason, error=None, flood_seconds=None):
        result = outcome(False, kind=kind, reason=reason, error=error,
                         flood_seconds=flood_seconds)
        _log_give_up(log, result)
        return result

    if budget <= 0:
        return give_up(None, REASON_TIMEOUT)

    # Кулдаун проверяем ДО сети: если чат заведомо закрыт, обращение только
    # продлит окно flood и потратит бюджет.
    if cooldown is not None and chat_id is not None:
        left = cooldown.remaining(chat_id)
        if left > 0:
            return give_up(KIND_FLOOD, REASON_FLOOD_COOLDOWN,
                           flood_seconds=left)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return give_up(None, REASON_TIMEOUT)

        attempts += 1
        task = await _run_bounded(make_awaitable, remaining)
        if task is None:
            # Бюджет исчерпан на этой попытке — это наш дедлайн, а не чужая ошибка.
            return give_up(None, REASON_TIMEOUT)
        if task.cancelled():
            # Операцию сняли (runtime_guard.create_task, чужой wait_for, сам
            # цикл пингов). Отмену НЕ переводим в отказ: снятая работа обязана
            # остаться снятой, иначе она допишет врачу от имени уже отменённой.
            #
            # Проверка написана явно, хотя `task.exception()` у снятой задачи и
            # сам поднял бы CancelledError: полагаться на побочный эффект чужого
            # метода в этом месте — заявка на то, что первый же рефакторинг с
            # try/except вокруг exception() тихо проглотит отмену.
            raise asyncio.CancelledError()

        error = task.exception()
        if error is None:
            if attempts > 1:
                log.info(
                    "tg recovered op=%s chat_id=%s attempts=%d elapsed=%.2fs",
                    op, chat_id, attempts, time.monotonic() - started,
                )
            return outcome(True, value=task.result())

        kind = classify(error)

        if kind == KIND_FLOOD:
            wait = flood_wait_seconds(error)
            if wait is None:
                return give_up(kind, REASON_FLOOD_UNKNOWN_WAIT, error=error)
            remaining = deadline - time.monotonic()
            if wait + slice_floor > remaining:
                # НЕ спим. Сон дольше остатка бюджета — это выход за дедлайн
                # вызывающего: наверху сработает свой таймаут, готовый ответ
                # будет потерян, а секунды сна потрачены впустую.
                if cooldown is not None:
                    cooldown.block(chat_id, wait)
                return give_up(kind, REASON_FLOOD_OVER_BUDGET, error=error,
                               flood_seconds=wait)
            await asyncio.sleep(wait)
            continue

        if kind == KIND_TRANSIENT:
            transient_seen += 1
            if transient_seen >= max_transient_attempts:
                return give_up(kind, REASON_TRANSIENT_ATTEMPTS, error=error)
            remaining = deadline - time.monotonic()
            backoff = min(backoff_base, max(0.0, remaining))
            if backoff + slice_floor > remaining:
                return give_up(kind, REASON_TRANSIENT_NO_BUDGET, error=error)
            await asyncio.sleep(backoff)
            continue

        return give_up(kind, REASON_TERMINAL, error=error)


# --- Тонкие обёртки под самые частые места правки -------------------------------
# Ровно чтобы adoption был заменой одной строки. Логики здесь нет, вся она в guard.

async def send_message(client, chat_id, text, timeout=DEFAULT_TIMEOUT_SECONDS,
                       op="send_message", logger=None, **kwargs):
    if not text or not (str(text) if text is not None else "").strip():
        log = logger or _LOG
        log.warning("tg_safety.send_message: aborted sending empty message to chat_id=%s op=%s", chat_id, op)
        return TgOutcome(ok=False, chat_id=chat_id, op=op, reason=REASON_TERMINAL, error=ValueError("Empty message text"))
    return await guard(
        lambda: client.send_message(entity=chat_id, message=text, **kwargs),
        op=op, chat_id=chat_id, timeout=timeout, logger=logger,
    )


async def edit_message(client, chat_id, message_id, text,
                       timeout=DEFAULT_TIMEOUT_SECONDS, op="edit_message",
                       logger=None, **kwargs):
    if not text or not (str(text) if text is not None else "").strip():
        log = logger or _LOG
        log.warning("tg_safety.edit_message: aborted editing empty message for chat_id=%s msg_id=%s op=%s", chat_id, message_id, op)
        return TgOutcome(ok=False, chat_id=chat_id, op=op, reason=REASON_TERMINAL, error=ValueError("Empty message text"))
    return await guard(
        lambda: client.edit_message(chat_id, message_id, text, **kwargs),
        op=op, chat_id=chat_id, timeout=timeout, logger=logger,
    )


async def delete_messages(client, chat_id, message_ids,
                          timeout=DEFAULT_TIMEOUT_SECONDS, op="delete_messages",
                          logger=None, **kwargs):
    return await guard(
        lambda: client.delete_messages(chat_id, message_ids, **kwargs),
        op=op, chat_id=chat_id, timeout=timeout, logger=logger,
    )


async def get_messages(client, chat_id, timeout=DEFAULT_TIMEOUT_SECONDS,
                       op="get_messages", logger=None, **kwargs):
    return await guard(
        lambda: client.get_messages(chat_id, **kwargs),
        op=op, chat_id=chat_id, timeout=timeout, logger=logger,
    )
