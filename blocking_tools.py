import asyncio
import json
import logging
import os
import re
import signal
import sys
import time

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Хвост ключа, который разрешено печатать. Ровно столько же печатает
# gemini_client (`f"{provider}...{api_key[-5:]}"`, gemini_client.py:475 и :842) —
# держим один формат, иначе журнал нельзя сопоставить по ключу.
_SECRET_TAIL = 5

# Формы секретов, которые могут приехать В ТЕКСТЕ ЧУЖОГО ИСКЛЮЧЕНИЯ.
#
# Замер по всем журналам на диске (bot.log, bot.log.1, bot_test.log,
# distiller.log, bot_supervisor.log — 131 219 строк): ни одного совпадения ни по
# одной из этих форм, то есть сегодня ключи в журнал не текут. Но правки ниже
# впервые начинают печатать str() чужих исключений — от google.genai, openai,
# tavily, telegraph, — а google отдаёт ключ в query string (`?key=...`) и
# показывает URL в тексте некоторых ошибок. Маскируем на входе, чтобы новая
# диагностика не оказалась первым источником утечки.
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\b(?:sk|gsk)_[0-9A-Za-z]{16,}"),
    re.compile(r"tvly-[0-9A-Za-z_\-]{10,}"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}"),
    re.compile(r"(?<=[?&]key=)[0-9A-Za-z_\-]{15,}"),
    re.compile(r"(?<=[Bb]earer )[0-9A-Za-z._\-]{20,}"),
)

# Сколько символов чужого текста вообще пускаем в строку журнала. Ответ модели и
# промпт сводки — десятки КБ; целиком они не диагностика, а заливка журнала,
# который ротируется по 5 МБ.
_ERROR_TEXT_LIMIT = 300


def _redact(text):
    """Замаскировать секретоподобное, оставив хвост для сопоставления с ключом."""
    if not text:
        return text
    result = str(text)
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda m: "***" + m.group(0)[-_SECRET_TAIL:], result)
    return result


def _describe_exception(exc):
    """
    Тип + текст, замаскированные и обрезанные.

    Тип обязателен: замер прямой — `str(ConnectionResetError())`,
    `str(TimeoutError())`, `str(IndexError())` и `str(asyncio.CancelledError())`
    все равны пустой строке. Такое исключение давало в журнале запись с пустым
    полем причины (в bot.log таких 12 из 1393 ERROR/WARNING), и разбирать её
    было нечем.
    """
    text = _redact(str(exc))[:_ERROR_TEXT_LIMIT]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _throttled(level, key, message, *args):
    """
    Строка с ограничителем частоты: правило «одна на минуту на ключ» живёт в
    runtime_guard.throttled_log, дубля здесь нет.

    Импорт ленивый по образцу остальных обращений к runtime_guard в этом файле
    (:383, :416, :471): дочерний процесс поднимается на каждый вызов LLM, а
    бюджет его старта уже дефицитный, см. _SUBPROCESS_STARTUP_SLACK_SECONDS.

    Ограничитель — удобство, а не условие записи: если runtime_guard недоступен
    (в дочернем процессе он может быть не нужен вовсе), строка обязана выйти всё
    равно, иначе правка сама себя обнулит.
    """
    try:
        import runtime_guard

        runtime_guard.throttled_log(level, key, message, *args, logger=logger)
    except Exception:
        logger.log(level, message, *args)


def _json_exit(payload, code=0):
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    raise SystemExit(code)


def _read_stdin_json():
    raw = sys.stdin.buffer.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", errors="replace"))


def _create_telegraph_page_sync(title, html_content):
    import config
    from html_telegraph_poster import TelegraphPoster

    poster = TelegraphPoster(use_api=True, access_token=config.TELEGRAPH_TOKEN)
    if not config.TELEGRAPH_TOKEN:
        poster.create_api_token("StomatBot_Reporter")

    paragraphs = html_content.split('\n\n')
    formatted_body = ''
    for p in paragraphs:
        p = p.strip()
        if p:
            formatted_body += f"<p>{p.replace('\n', '<br>')}</p>"
    page = poster.post(
        title=title,
        author="StomatBot AI",
        text=formatted_body,
    )
    return page["url"]


def _generate_gemini_text_sync(prompt, context, timeout=None):
    """
    None — каскад развалился (ни одна модель не ответила), "" — модель ответила
    пустотой.

    Два разных отказа: в первом случае виноваты ключи, кулдауны и баны моделей и
    повтор тем же путём бессмысленен, во втором ответила живая модель и повтор
    имеет смысл. Раньше оба сводились в один `not response` и уходили наружу
    неотличимыми (ok: False без причины), а именно по этой развилке решают,
    писать врачу «сервис недоступен» или молча переспросить.
    """
    import gemini_client

    response = gemini_client.generate_text(prompt, context, timeout=timeout)
    if response is None:
        return None
    return getattr(response, "text", None) or ""


def _web_search_sync(query, max_results):
    """
    Ищет и ОТДЕЛЬНО возвращает список отказов провайдеров.

    Раньше оба провайдера гасили исключение в `results = []`, а наружу уходило
    ok: True — то есть «tavily не отвечает и ddgs забанил наш IP» выглядело для
    вызывающего ровно как «по запросу ничего нет». Врачу в обоих случаях
    печаталось «Информации не найдено», и отказ поиска не попадал ни в журнал,
    ни в разбор.

    Результат — список {"text", "url"}, а не строк.
    Раньше ссылка клеилась в текст, причём двумя разными способами: tavily
    отдавал "текст (url)", ddgs — "текст\\n(Source: url)". Вынуть её обратно можно
    было только регуляркой, а регулярка режет адрес по первой закрывающей скобке.
    Замер по 556 живым ссылкам из stomat_archive.db и stomat_wiki.db: адрес
    .../Оксид_циркония(IV) в обеих формах терял хвост и перестал открываться —
    врач видит источник под утверждением, а проверить утверждение не может.
    Хуже: если в тексте страницы есть свой маркер «(Источник: url)», регулярка
    берёт ссылку ИЗ ТЕКСТА, и по чужому хосту считается уровень доверия —
    измерено, что обзор с pubmed при этом выбрасывается как реклама клиники, а
    врачу уходит «нашлась только реклама».
    """
    import config

    results = []
    errors = []
    if config.SEARCH_PROVIDER == "tavily" and config.TAVILY_API_KEY:
        try:
            from tavily import TavilyClient

            response = TavilyClient(api_key=config.TAVILY_API_KEY).search(
                query=query,
                search_depth="basic",
                max_results=max_results,
            )
            for item in response.get("results", []):
                content = item.get("content")
                url = item.get("url")
                if content and url:
                    results.append({"text": content, "url": url})
        except Exception as exc:
            results = []
            errors.append(f"tavily: {type(exc).__name__}: {_redact(str(exc))[:200]}")
            # errors доезжает до вызывающего ТОЛЬКО когда пусты оба провайдера
            # (:_json_exit ниже отдаёт ok: True и results, а errors выбрасывает).
            # То есть «tavily не отвечает уже месяц, поиск втихую держится на
            # ddgs» не видно нигде. Запись — единственный канал для этого случая.
            #
            # Ограничитель не берём: это дочерний процесс, он поднимается заново
            # на каждый вызов, состояние окна не переживёт выход, а лишний импорт
            # runtime_guard удлинил бы его старт. Частота здесь и так одна строка
            # на один поиск врача.
            logger.warning("web search: провайдер tavily отказал: %s", _describe_exception(exc))

    if not results:
        try:
            from ddgs import DDGS

            with DDGS() as ddgs:
                for item in ddgs.text(query, region="ru-ru", max_results=max_results, backend="api"):
                    body = item.get("body") if item else None
                    href = item.get("href") if item else None
                    if body and href:
                        results.append({"text": body, "url": href})
        except Exception as exc:
            results = []
            errors.append(f"ddgs: {type(exc).__name__}: {_redact(str(exc))[:200]}")
            logger.warning("web search: провайдер ddgs отказал: %s", _describe_exception(exc))

    return results, errors


# Запас родительского дедлайна на подъём ребёнка.
#
# Замер на этой машине (тёплый кеш, 3 прогона): интерпретатор с импортом
# blocking_tools 0.11-0.33 с, плюс config и gemini_client с клиентом openai
# 0.73-2.48 с — до ~2.8 с уходит ДО первого сетевого запроса, а на холодном кеше
# и под антивирусом это десяток секунд. Родитель ждал ровно тот же бюджет,
# который выдан ребёнку в payload["timeout"], и начало бюджета съедал запуск:
# ребёнок не доживал до последней модели каскада, вызывающий получал "timeout"
# вместо ответа, а вся уже проделанная работа выбрасывалась.
_SUBPROCESS_STARTUP_SLACK_SECONDS = 10.0

# Уровень строки журнала ребёнка, чтобы grep ERROR по bot.log продолжал находить
# его отказы: без разбора всё легло бы в родительский INFO.
_CHILD_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _relay_child_log(action, stderr_text):
    """
    Перелить журнал дочернего процесса в свой logger.

    stderr ребёнка не перехватывался вообще (stderr=None — наследование), а весь
    разбор LLM-вызова живёт именно там: какой ключ взят, кулдаун после 429, бан
    модели на 20 минут, "All AI attempts exhausted". С переходом на подпроцессы
    эти строки перестали попадать в bot.log целиком, и вопрос «почему бот не
    ответил» остался без ответа.

    stdout не смешиваем ни при каких условиях: по нему идёт JSON-протокол, и
    любая строка журнала в фигурных скобках подменила бы ответ — разбор ищет
    последнюю строку вида {...}.
    """
    if not stderr_text:
        return
    for line in stderr_text.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        level = _CHILD_LOG_LEVELS.get(line.split(" ", 1)[0], logging.INFO)
        logger.log(level, "[%s] %s", action, line)


def _log_tool_failure(action, pid, reason, error):
    """
    Одна запись на КАЖДЫЙ неудавшийся вызов подпроцесса.

    Почему именно здесь, а не у вызывающих: строку ошибки они регулярно
    выбрасывают. assistant.py:104-106 на `error` возвращает врачу готовую фразу
    «Недостаточно сообщений…» и не пишет ничего; assistant.py:135-137 возвращает
    False молча; assistant.py:1213-1214 сворачивает причину в _unavailable. То
    есть отказ доезжает до врача, но не до журнала: «врач говорит, что бот
    молчит» и в bot.log ни одной строки за эту минуту. Одна запись в общей точке
    закрывает все такие вызовы разом, включая те, что появятся позже.

    Ограничитель обязателен. Триаж (`llama_triage`) вызывается на КАЖДОЕ
    сообщение группы, и при постоянном отказе провайдера — а он наблюдался
    постоянным: 45 записей `400 FAILED_PRECONDITION User location is not
    supported` по всем четырём ключам — эта строка выходила бы на каждое
    сообщение 749 врачей и вытеснила бы из bot.log (ротация 5 МБ) всю историю
    работы. Ключ ограничителя включает action и вид отказа, чтобы редкий новый
    отказ не оказался подавлен частым старым.

    WARNING, а не ERROR: одиночный провал вызова закрывается ретраем каскада, и
    ERROR на нём обесценил бы поиск настоящих аварий по журналу. И не DEBUG:
    замер по 131 219 строкам всех журналов на диске — 0 записей DEBUG, корень
    стоит на INFO (runtime_guard.py:72), то есть DEBUG здесь равен молчанию.
    """
    _throttled(
        logging.WARNING,
        f"tool_failure:{action}:{reason}",
        "подпроцесс не дал ответа action=%s pid=%s вид=%s: %s",
        action,
        pid,
        reason,
        _redact(str(error))[:_ERROR_TEXT_LIMIT],
    )


async def _kill_process_tree(proc):
    """
    Убить ребёнка ВМЕСТЕ с внуками.

    proc.kill() снимает только прямой процесс. Ребёнок сам порождает внуков:
    whisper-transcribe запускает ffmpeg (конвертация голосового в 16 кГц моно),
    и после убийства по таймауту ffmpeg оставался жить сиротой — без родителя,
    без таймаута и без того, кто прочитает его результат.

    Порядок важен: на Windows поддерева процессов нет, taskkill /T ищет внуков по
    PID родителя, и у мёртвого родителя он их уже не найдёт. Поэтому режем дерево
    ДО proc.kill(), а proc.kill() оставляем страховкой.
    """
    if proc.returncode is not None:
        return
    # try/finally, а не последовательность: снятие дерева на Windows идёт через
    # await, и отмена задачи посреди него не должна оставить живым хотя бы прямого
    # ребёнка — прежний код звал proc.kill() первым и этим был защищён.
    try:
        if _IS_WINDOWS:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        else:
            # Ребёнок запущен с start_new_session=True, то есть он лидер своей
            # группы процессов, и внуки наследуют её же.
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (asyncio.TimeoutError, Exception) as exc:
        # Провал снятия дерева означает живого внука: ffmpeg от whisper-transcribe
        # остаётся сиротой — без родителя, без таймаута и без читателя результата.
        # Молчание здесь стоило того, что сирот на этой машине никто не считал:
        # искать их приходится в диспетчере задач, а не в журнале.
        # WARNING, а не ERROR: прямой ребёнок ниже всё равно будет убит, отказ
        # частичный. Управление не меняется — finally как был.
        _throttled(
            logging.WARNING,
            "kill_tree_failed",
            "дерево процессов не снято pid=%s: %s — внуки (ffmpeg) могли остаться сиротами",
            proc.pid,
            _describe_exception(exc),
        )
    finally:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            # Процесс уже мёртв — штатный исход, а не отказ: сюда приходят и после
            # успешного taskkill выше. Записи не заслуживает.
            pass


async def _run_json_tool(action, payload, timeout=None):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-X",
        "utf8",
        os.path.abspath(__file__),
        action,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # На POSIX своя группа процессов — единственный способ снять внуков одним
        # сигналом; на Windows этот флаг означает совсем другое (отдельная группа
        # для Ctrl-Break) и не нужен.
        **({} if _IS_WINDOWS else {"start_new_session": True}),
    )
    request_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")

    # Пайпы дренируем сами, а не через proc.communicate().
    #
    # communicate() накапливает прочитанное в локальной переменной своей корутины,
    # и при отмене по таймауту всё уже прочитанное пропадает вместе с ней: замер
    # показал, что второй communicate() после kill возвращает ПУСТОЙ stderr, то
    # есть журнал убитого ребёнка терялся целиком. А убийство по таймауту — как
    # раз тот случай, который потом и надо разбирать: на какой модели и каком
    # ключе он завис. Читаем в списки снаружи корутины — они переживают отмену.
    stdout_chunks, stderr_lines = [], []

    # Сколько строк журнала ребёнка потеряно на переполнении лимита потока.
    # Копится, а не пишется на месте: см. _read_lines.
    dropped_stderr = [0]

    async def _feed_stdin():
        try:
            proc.stdin.write(request_bytes)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            # Ребёнок умер, не дочитав задание. Дальше родитель не найдёт JSON и
            # вернёт `... failed with code N`, где про недоставленный промпт нет
            # ничего — а это разные отказы: не дошло задание против «модель не
            # ответила». Размер пишем, сам промпт нет: это текст врача.
            _throttled(
                logging.WARNING,
                f"feed_stdin_failed:{action}",
                "задание не доставлено подпроцессу action=%s pid=%s байт=%d: %s",
                action,
                proc.pid,
                len(request_bytes),
                _describe_exception(exc),
            )
        try:
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Закрытие пайпа у мёртвого ребёнка — штатный исход и ничего не
            # говорит о причине; отказ записи уже сделан выше.
            pass

    async def _read_chunks(stream, sink):
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            sink.append(chunk)

    async def _read_lines(stream, sink):
        while True:
            try:
                line = await stream.readline()
            except ValueError:
                # Строка длиннее лимита потока (64 КБ): дочитывать её нечем,
                # но и бросать дренаж нельзя — иначе ребёнок встанет на записи.
                #
                # Пишем НЕ здесь: это тело while True, и на одной переполненной
                # строке ветка срабатывает многократно. Строка журнала на каждый
                # оборот залила бы bot.log (ротация 5 МБ) быстрее, чем оператор
                # успел бы его открыть. Копим счётчик, одна сводная запись — в
                # конце обмена.
                dropped_stderr[0] += 1
                continue
            if not line:
                return
            sink.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))

    async def _exchange():
        # Кормить stdin и читать вывод обязательно одновременно: промпт сводки
        # это десятки КБ, и запись в полный пайп ждала бы, пока ребёнок читает.
        await asyncio.gather(
            _feed_stdin(),
            _read_chunks(proc.stdout, stdout_chunks),
            _read_lines(proc.stderr, stderr_lines),
        )
        await proc.wait()

    def _report_dropped_stderr():
        """Сводная запись о потерянных строках журнала ребёнка (см. _read_lines)."""
        if dropped_stderr[0]:
            _throttled(
                logging.WARNING,
                f"stderr_overflow:{action}",
                "журнал подпроцесса потерян частично action=%s pid=%s:"
                " %d строк длиннее лимита потока 64 КБ",
                action,
                proc.pid,
                dropped_stderr[0],
            )

    async def _reap():
        """Добить процесс с внуками, не зависнув на дренаже его пайпов."""
        await _kill_process_tree(proc)
        tail = None
        try:
            _, tail = await asyncio.wait_for(proc.communicate(), timeout=5)
        except (asyncio.TimeoutError, Exception) as exc:
            # Это последний дренаж убитого ребёнка. Молчание здесь означало, что
            # хвост его журнала — самые последние строки перед зависанием, ровно
            # то, что и нужно разбирать, — исчезал, и оператор не знал даже, что
            # чего-то не хватает. Дренаж дальше не идёт, управление то же.
            _throttled(
                logging.WARNING,
                f"reap_drain_failed:{action}",
                "хвост журнала подпроцесса не дочитан action=%s pid=%s: %s",
                action,
                proc.pid,
                _describe_exception(exc),
            )
        if tail:
            stderr_lines.append(tail.decode("utf-8", errors="replace"))
        _relay_child_log(action, "\n".join(stderr_lines))
        _report_dropped_stderr()

    # Дедлайн родителя = бюджет ребёнка + запас на его запуск, см.
    # _SUBPROCESS_STARTUP_SLACK_SECONDS. timeout=None оставляем None: это
    # сознательное «ждать сколько нужно», а не забытый параметр.
    deadline = timeout + _SUBPROCESS_STARTUP_SLACK_SECONDS if timeout else timeout
    try:
        await asyncio.wait_for(_exchange(), timeout=deadline)
    except asyncio.TimeoutError:
        await _reap()
        error = f"{action} timeout after {deadline}s (бюджет ребёнка {timeout}s)"
        _log_tool_failure(action, proc.pid, "timeout", error)
        return None, error
    except asyncio.CancelledError:
        await _reap()
        raise

    stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_text = "\n".join(stderr_lines).strip()
    _relay_child_log(action, stderr_text)
    _report_dropped_stderr()
    json_line = None
    for line in reversed(stdout_text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_line = stripped
            break

    if not json_line:
        details = stderr_text or stdout_text.strip()
        error = details or f"{action} failed with code {proc.returncode}"
        _log_tool_failure(action, proc.pid, "no_json", error)
        return None, error

    try:
        result = json.loads(json_line)
    except json.JSONDecodeError as exc:
        # Саму строку не печатаем: для gemini-text это ответ модели врачу, то есть
        # payload целиком. Длины и позиции ошибки хватает, чтобы отличить обрезанный
        # вывод от постороннего мусора в stdout.
        error = f"{action} invalid output: {exc}"
        _log_tool_failure(action, proc.pid, "bad_json", f"{error} (длина строки {len(json_line)})")
        return None, error

    if not result.get("ok"):
        error = result.get("error") or f"{action} failed"
        _log_tool_failure(action, proc.pid, result.get("reason") or "not_ok", error)
        return None, error
    return result, None


async def create_telegraph_page_async(title, html_content, timeout):
    payload, error = await _run_json_tool(
        "telegraph-page",
        {"title": title, "html": html_content},
        timeout=timeout,
    )
    if error:
        return None, error
    return payload.get("url"), None


class TextResponse:
    def __init__(self, text):
        self.text = text


_GEMINI_MIN_INTERVAL_SECONDS = 3.0
# Страховка от вечного ожидания: без неё timeout=None превращался в
# asyncio.wait_for(..., None), а дочерний процесс может спать до 60 с между
# ретраями на каждой из 4 моделей каскада.
_GEMINI_DEFAULT_TIMEOUT_SECONDS = 120.0

_LAST_GEMINI_CALL_START = 0.0
# Блокировка удерживается только на время расчёта паузы и самого сна, а не на
# время запроса — иначе все LLM-вызовы выстроились бы в один поток.
_GEMINI_PACE_LOCK = asyncio.Lock()


async def _pace_gemini_calls():
    """
    Разносит СТАРТЫ запросов минимум на _GEMINI_MIN_INTERVAL_SECONDS.

    Прежняя версия читала и писала таймстамп без блокировки: N корутин,
    зашедших одновременно, видели одно и то же значение, спали одинаково и
    просыпались вместе — то есть гейт не разносил ничего и стабильно давал
    залп из N параллельных запросов, ловящий 429 и отправляющий ключи
    в пятиминутный cooldown. Под блокировкой каждый следующий ожидающий
    считает паузу уже от обновлённого времени.
    """
    global _LAST_GEMINI_CALL_START
    async with _GEMINI_PACE_LOCK:
        # monotonic, а не time(): интервал не должен ломаться от перевода часов.
        wait = _GEMINI_MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_GEMINI_CALL_START)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_GEMINI_CALL_START = time.monotonic()


def _foreign_summary_status(context):
    """
    Отметка ЧУЖОЙ идущей сводки, которую наш вызов вот-вот затрёт.

    Файл статуса один на процесс и одноместный, а пишут в него все: дочерний
    процесс ставит свой kind на каждой попытке, и в конце gemini_client уже
    ВНУТРИ РЕБЁНКА зовёт release_generation_status — тот читает файл, видит там
    запись самого ребёнка (а не отметку дайджеста) и гасит флаг. Итог: дайджест
    считается ещё минуты, а сторож (main.summary_watchdog_task, порог
    GEMINI_GENERATION_TIMEOUT_SECONDS + 600 с) видит active: False и зависание
    сводки больше не поймает — то есть отчёт всему чату врачей теряется молча.

    Ребёнок отличить чужую работу не может: состояние родителя ему неизвестно.
    Поэтому снимок делает родитель и делает его ДО запуска ребёнка.
    """
    try:
        import runtime_guard
        kind = context.get("kind") if isinstance(context, dict) else None
        if kind in runtime_guard.SUMMARY_KINDS:
            return None
        current = runtime_guard.read_summary_status()
        if current.get("active") and current.get("kind") in runtime_guard.SUMMARY_KINDS:
            return current
    except Exception as exc:
        # Без снимка чужую отметку сводки уже не вернуть: файл одноместный, и
        # ребёнок затрёт её своей записью. Итог отказа ровно тот, который описан
        # выше в докстроке — сторож сводки разоружён, зависший дайджест всего чата
        # врачей теряется молча. Возврат None остаётся, управление не меняем.
        _throttled(
            logging.WARNING,
            "foreign_summary_snapshot_failed",
            "снимок чужой отметки сводки не сделан: %s — зависание дайджеста"
            " на время этого вызова сторож не поймает",
            _describe_exception(exc),
        )
    return None


def _restore_foreign_summary_status(snapshot):
    """
    Вернуть отметку сводки, затёртую нашим вызовом. Правило снятия флага не
    дублируется: оно живёт в runtime_guard.release_generation_status.

    Восстанавливаем только если от сводки в файле ничего не осталось И последним
    писал не наш процесс: свои записи (summarizer, сброс на старте) ставят pid
    родителя, записи подпроцессов — чужой. Проверка pid нужна, чтобы не воскресить
    флаг сводки, которая за время нашего вызова успела нормально закончиться:
    воскрешённую отметку снимать некому, кроме summarizer, и через полчаса сторож
    перезапустил бы процесс впустую.

    utc при записи обновляется (write_summary_status ставит своё), то есть возраст
    отметки сбрасывается на длительность нашего вызова — не больше 120 с при
    пороге сторожа в GEMINI_GENERATION_TIMEOUT_SECONDS + 600 с. Исходное время
    кладём отдельным полем: сторож печатает status целиком, и в журнале остаётся
    настоящий возраст.
    """
    if not snapshot:
        return False
    try:
        import runtime_guard
        current = runtime_guard.read_summary_status()
        if current.get("active") and current.get("kind") in runtime_guard.SUMMARY_KINDS:
            return False
        if current.get("pid") == os.getpid():
            return False
        restored = dict(snapshot)
        restored["restored_from_utc"] = snapshot.get("utc")
        restored["restored_by"] = "blocking_tools"
        runtime_guard.write_summary_status(restored)
        logger.warning(
            "восстановлена затёртая отметка сводки kind=%s stage=%s utc=%s",
            snapshot.get("kind"), snapshot.get("stage"), snapshot.get("utc"),
        )
        return True
    except Exception as exc:
        # Отметка сводки НЕ восстановлена: сторож сводки с этого момента слеп до
        # конца дайджеста. Успешное восстановление уже пишется строкой выше —
        # молчащим оставался ровно отказ, то есть в журнале был виден только
        # хороший исход. Возврат False сохраняем.
        _throttled(
            logging.WARNING,
            "restore_summary_failed",
            "затёртая отметка сводки НЕ восстановлена kind=%s stage=%s: %s",
            snapshot.get("kind"),
            snapshot.get("stage"),
            _describe_exception(exc),
        )
        return False


async def generate_gemini_text_async(prompt, context, timeout=None):
    await _pace_gemini_calls()

    effective_timeout = float(timeout) if timeout else _GEMINI_DEFAULT_TIMEOUT_SECONDS

    # Снимок делается ДО запуска ребёнка: после его первой записи чужую отметку
    # от своей уже не отличить, файл одноместный.
    foreign_summary = _foreign_summary_status(context)

    try:
        payload, error = await _run_json_tool(
            "gemini-text",
            {"prompt": prompt, "context": context, "timeout": effective_timeout},
            timeout=effective_timeout,
        )
        if error:
            return None, error

        text = payload.get("text")
        if not text:
            return None, "gemini-text returned empty text"
        return TextResponse(text), None
    finally:
        # Флаг взводит дочерний процесс, снимать его обязан родитель: ребёнок
        # мог не дожить до конца. Раньше здесь стоял безусловный active: False,
        # который гасил отметку дайджеста, если ответ ассистента завершался
        # посреди его генерации, — и зависший дайджест переставал быть виден
        # сторожу. Снимаем только свой флаг.
        #
        # Одной этой охраны мало: она смотрит в файл ПОСЛЕ ребёнка, а ребёнок уже
        # затёр там чужую отметку своей. Поэтому СНАЧАЛА возвращаем то, что было
        # до нас, и только потом зовём охрану — она увидит отметку сводки и сама
        # откажется гасить. В обратном порядке не работает: clear ставит в файл
        # НАШ pid и стирает признак «последним писал подпроцесс», по которому
        # только и отличается затёртая отметка от честно законченной.
        try:
            import runtime_guard
            _restore_foreign_summary_status(foreign_summary)
            runtime_guard.release_generation_status(
                context.get("kind") if isinstance(context, dict) else None
            )
        except Exception as exc:
            # Здесь гасился и отказ записи файла статуса: write_summary_status
            # (runtime_guard.py:120) после пяти ретраев поднимает OSError, и
            # единственным её получателем был этот pass. Незакрытый флаг генерации
            # оставляет сторожу сводки повод перезапустить бота через полчаса
            # впустую, а незакрытый и невидимый — ещё и без объяснения в журнале.
            # finally остаётся finally: отказ уборки не имеет права подменить
            # результат вызова, ради которого врач ждал ответа.
            _throttled(
                logging.WARNING,
                "release_status_failed",
                "флаг генерации не снят kind=%s: %s — сторож сводки может"
                " перезапустить процесс по устаревшей отметке",
                context.get("kind") if isinstance(context, dict) else None,
                _describe_exception(exc),
            )


def _as_search_entry(item):
    """
    Один результат поиска -> {"text", "url"}, независимо от того, что пришло.

    Зачем нормализация здесь, а не у вызывающего: ребёнок отдаёт структуру, но
    строковая форма из старой версии может ещё приехать (сохранённая нагрузка,
    неперезапущенный процесс на боевой машине). Вызывающий, который потянется за
    entry["url"] у строки, получит TypeError по всему поиску — то есть врач вместо
    ответа со ссылками не получит ничего. Разбор строк не дублируем: регулярки
    живут в web_lookup, иначе две копии разъедутся и разойдутся молча.

    Ключи content/body/href — имена, которыми отдают сами провайдеры (tavily и
    ddgs соответственно): принимаем и их, чтобы результат провайдера, переданный
    как есть, не остался без ссылки.
    """
    if isinstance(item, dict):
        return {
            "text": (item.get("text") or item.get("content") or item.get("body") or "").strip(),
            "url": (item.get("url") or item.get("href") or "").strip(),
        }
    if not isinstance(item, str):
        # Ни структура, ни строка — порченая нагрузка, а не находка: показывать
        # врачу str(None) как выдержку из источника нельзя. Но и молчать нельзя,
        # иначе «поиск нашёл три, показал одну» останется без объяснения.
        _throttled(
            logging.WARNING,
            "search_entry_unknown_type",
            "результат поиска неизвестного вида пропущен: %s",
            type(item).__name__,
        )
        return {"text": "", "url": ""}
    text = item
    parsed = None
    try:
        import web_lookup

        parsed = web_lookup.parse_result(text)
    except Exception as exc:
        # Отказ слоя качества не имеет права похоронить саму находку: выдержка без
        # ссылки — половина ответа, а пустой ответ — ноль.
        _throttled(
            logging.WARNING,
            "search_entry_parse_failed",
            "результат поиска не разобран (%s) — выдержка уйдёт врачу без ссылки",
            _describe_exception(exc),
        )
    if parsed:
        return {"text": parsed["text"], "url": parsed["url"]}
    return {"text": text.strip(), "url": ""}


async def web_search_async(query, max_results, timeout):
    payload, error = await _run_json_tool(
        "web-search",
        {"query": query, "max_results": max_results},
        timeout=timeout,
    )
    if error:
        return [], error
    entries = [_as_search_entry(item) for item in (payload.get("results") or [])]
    # Пустышка (ни текста, ни ссылки) врачу не показывается и в счёт находок не
    # идёт: иначе поиск отчитается «нашлось 3», а показать будет нечего.
    return [entry for entry in entries if entry["text"] or entry["url"]], None


def _remove_converted_wav(file_path):
    """
    Снести ffmpeg-конвертат, который ребёнок не успел убрать за собой.

    Убитый по таймауту процесс до своей уборки не доходит (TerminateProcess не
    даёт выполнить finally), и wav 16 кГц моно остаётся лежать рядом с исходником
    навсегда: минута голосового — около 2 МБ. Имя задаётся в
    gemini_client.convert_to_wav; правило продублировано здесь осознанно, потому
    что убрать может только тот, кто пережил убийство, — то есть родитель.
    """
    if not file_path:
        return
    base, _ext = os.path.splitext(file_path)
    wav_path = base + "_converted.wav"
    try:
        if os.path.exists(wav_path):
            os.remove(wav_path)
    except OSError as exc:
        # Утечка диска, а не отказ распознавания: минута голосового — около 2 МБ
        # wav, и невывезенный конвертат остаётся лежать навсегда. Молча это
        # заканчивается заполненным диском, у которого в журнале нет ни одной
        # причины. Путь печатать можно: имя файла — не содержимое голосового.
        _throttled(
            logging.WARNING,
            "converted_wav_not_removed",
            "конвертат голосового не удалён path=%s: %s",
            wav_path,
            _describe_exception(exc),
        )


async def transcribe_audio_async(file_path, timeout):
    payload, error = await _run_json_tool(
        "whisper-transcribe",
        # Бюджет уходит и в payload: ребёнок раскладывает его по попыткам ключей,
        # иначе перебор складывался в 222 с против 70 с родительского дедлайна.
        {"file_path": file_path, "timeout": timeout},
        timeout=timeout,
    )
    if error:
        _remove_converted_wav(file_path)
        return None, error
    return payload.get("text"), None


async def correct_dental_transcription_async(raw_text, timeout=20):
    if not raw_text or len(raw_text) < 4:
        return raw_text
        
    prompt = f"""
Ты — специализированный стоматологический редактор. Твоя задача — исправить возможные ошибки распознавания речи (опечатки, ослышки) в стоматологических и медицинских терминах.
Исправь текст, сохранив исходный смысл. Заменяй только искаженные термины (например, 'верти преп' -> 'вертипреп', 'бэо пт' -> 'BOPT', 'гипохлорид' -> 'гипохлорит', 'кафердам' -> 'коффердам' и т.д.).

Исходный распознанный текст:
"{raw_text}"

Правило: выведи ИСКЛЮЧИТЕЛЬНО исправленный текст, без каких-либо комментариев, кавычек или пояснений. Если исправлений не требуется, выведи исходный текст без изменений.
"""
    response, error = await generate_gemini_text_async(prompt, {"kind": "transcription_corrector"}, timeout=timeout)
    if response and getattr(response, "text", None):
        corrected = response.text.strip().strip('"').strip("'")
        if corrected:
            return corrected
    return raw_text


def _transcribe_audio_sync(file_path, timeout=None):
    import gemini_client
    # Бюджет передаём внутрь: без него перебор ключей складывался в 222 с
    # против 70 с родительского дедлайна, и пять ключей оставались нетронутыми.
    return gemini_client.transcribe_audio_bytes_or_file(file_path, timeout=timeout)


def _main():
    # Журнал ребёнка идёт в stderr, откуда его забирает родитель (_relay_child_log).
    #
    # Без этой настройки корневой logger в дочернем процессе остаётся вообще без
    # обработчиков: срабатывает logging.lastResort, который глотает всё ниже
    # WARNING и печатает голое сообщение без уровня и имени. То есть весь разбор
    # решений — какой ключ взят, какая модель забанена, почему пропущена, сколько
    # символов вернулось — исчезал полностью, а он весь идёт через logger.info.
    #
    # Своего RotatingFileHandler в ребёнке быть не должно: bot.log пишет и
    # ротирует родитель, а два процесса, ротирующих один файл, на Windows рвут
    # ротацию — открытый файл не переименовать. Секретов в этих строках нет,
    # gemini_client печатает только последние 5 символов ключа.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    if len(sys.argv) != 2:
        _json_exit({"ok": False, "error": "usage: blocking_tools.py <action>"}, 2)

    action = sys.argv[1]
    try:
        payload = _read_stdin_json()
        if action == "telegraph-page":
            url = _create_telegraph_page_sync(payload.get("title") or "", payload.get("html") or "")
            _json_exit({"ok": bool(url), "url": url})

        if action == "gemini-text":
            text = _generate_gemini_text_sync(
                payload.get("prompt") or "",
                payload.get("context") or {},
                payload.get("timeout")
            )
            # ok: bool(text) склеивал два разных отказа в один. reason —
            # машиночитаемая развилка для вызывающего, error — то же словами.
            if text is None:
                _json_exit({
                    "ok": False,
                    "reason": "cascade_exhausted",
                    "error": "gemini cascade exhausted: ни одна модель не ответила",
                })
            if not text:
                _json_exit({
                    "ok": False,
                    "reason": "empty_text",
                    "error": "gemini model returned empty text",
                })
            _json_exit({"ok": True, "text": text})

        if action == "web-search":
            results, errors = _web_search_sync(
                payload.get("query") or "",
                int(payload.get("max_results") or 2),
            )
            # Пусто без ошибок — это честное «ничего не найдено», ok: True.
            # Пусто, когда все провайдеры упали, — отказ поиска, и он обязан
            # доехать до вызывающего с причиной.
            if not results and errors:
                _json_exit({
                    "ok": False,
                    "reason": "providers_failed",
                    "error": "web search failed: " + "; ".join(errors),
                    "results": [],
                })
            _json_exit({"ok": True, "results": results})

        if action == "whisper-transcribe":
            # Бюджет из payload передаём внутрь: без него перебор ключей
            # складывался в 222 с против 70 с родительского дедлайна, и пять
            # ключей оставались нетронутыми.
            text = _transcribe_audio_sync(
                payload.get("file_path") or "", timeout=payload.get("timeout")
            )
            _json_exit({"ok": bool(text), "text": text})

        _json_exit({"ok": False, "error": f"unknown action: {action}"}, 2)
    except SystemExit:
        # _json_exit — это штатный выход, а не отказ: он бросает SystemExit из
        # каждой ветки выше. Без этой ветки `except Exception` его не поймал бы
        # (SystemExit наследует BaseException), но полагаться на это молча нельзя:
        # ветка объявлена явно, чтобы правка ниже не начала писать «ребёнок упал»
        # на каждый УСПЕШНЫЙ ответ.
        raise
    except Exception as exc:
        # Трейсбек падения ребёнка не попадал НИКУДА.
        #
        # _json_exit пишет только в stdout, а stdout здесь — JSON-протокол, куда
        # трейсбек положить нельзя (родитель ищет последнюю строку вида {...}).
        # Журнал ребёнка идёт в stderr (:567) и именно stderr родитель переливает
        # в bot.log через _relay_child_log. То есть до этой правки писать было
        # некуда, и не писали: `error: str(exc)` — всё, что доезжало.
        #
        # Чем это стоило: str() у ConnectionResetError, TimeoutError, IndexError и
        # CancelledError пуст, поэтому родитель на :315 подставлял
        # `f"{action} failed"`, и оператор читал `gemini-text failed` — без типа,
        # файла, строки, модели и ключа. Замер по bot.log: 12 записей ERROR/WARNING
        # с пустым полем причины.
        #
        # logger.exception уходит в stderr, откуда родитель заберёт его сам.
        # Локальных переменных в трейсбеке нет (стандартный traceback их не
        # печатает), то есть промпт врача и ключи в него не попадают.
        logger.exception("дочерний процесс упал action=%s", action)
        _json_exit({"ok": False, "error": _describe_exception(exc)}, 1)


if __name__ == "__main__":
    _main()
