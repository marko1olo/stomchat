import config
import hashlib
import json
import os
import random
import re
import logging
import time

# Force lowercase proxy environment variables for httpx / requests compatibility on Windows
for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "PROXY_URL"]:
    val = os.getenv(proxy_var)
    if val:
        os.environ[proxy_var.lower()] = val
        os.environ[proxy_var.upper()] = val
import runtime_guard
import httpx as _httpx
from openai import OpenAI

logger = logging.getLogger(__name__)

class DummyResponse:
    def __init__(self, text):
        self.text = text

def _env_int(name, default):
    alt_name = name.replace("STOMCHAT_", "ENDOCHAT_") if name.startswith("STOMCHAT_") else name.replace("ENDOCHAT_", "STOMCHAT_")
    for var in (name, alt_name):
        val = os.getenv(var)
        if val is not None:
            try:
                return int(val)
            except ValueError:
                pass
    return default

def strip_reasoning(text):
    """
    Убирает из ответа модели поток размышлений и возвращает то, что осталось.

    Рассуждающие модели (qwen и подобные) заворачивают черновик в <think>…</think>.
    Обработка этого была в двух местах и в обоих неверна по-разному:

      * gemini_client проверял НЕПУСТОТУ ДО срезки: `if text_result:` стоял до
        re.sub. Модель, вернувшая только размышления, давала после срезки пустую
        строку — и она уходила наружу как УСПЕХ. Каскад останавливался на первой
        же такой попытке, вызывающий получал пустой ответ, резервный провайдер не
        пробовался вообще;

      * незакрытый <think> (ответ оборвался по лимиту токенов) шаблон
        <think>.*?</think> не срезает совсем — черновик модели уходил врачу как
        ответ бота дословно;

      * vision срезку делал аккуратнее, но в ветке незакрытого тега при пустом
        начале брал `parts2[1]`, то есть отдавал САМИ РАЗМЫШЛЕНИЯ как клиническое
        описание снимка.

    Правило здесь одно: наружу идёт только текст ВНЕ размышлений. Если такого
    текста нет — возвращается пустая строка, и вызывающий обязан считать это
    неудачей попытки, а не ответом. Пустой ответ дешевле, чем черновик модели,
    поданный врачу как заключение по снимку.
    """
    if not text:
        return ""
    # 1. Удаляем закрытые теги think / thought / reasoning без учета регистра
    cleaned = re.sub(r"(?is)<(think|thought|reasoning)>.*?</\1>", "", text)
    cleaned = re.sub(r"(?is)```(?:thought|reasoning|think).*?```", "", cleaned)
    # 2. Срезаем незакрытые теги (обрыв по лимиту токенов)
    m_open = re.search(r"(?i)<(think|thought|reasoning)>", cleaned)
    if m_open:
        cleaned = cleaned[:m_open.start()]
    m_open_code = re.search(r"(?is)```(?:thought|reasoning|think)\b", cleaned)
    if m_open_code:
        cleaned = cleaned[:m_open_code.start()]
    # 3. Срезаем осиротевшие закрывающие теги
    m_close = re.search(r"(?i)</(think|thought|reasoning)>", cleaned)
    if m_close:
        cleaned = cleaned[m_close.end():]
    return cleaned.strip()


def _retry_sleep_seconds(attempt):
    base = _env_int("STOMCHAT_GEMINI_RETRY_BASE_SECONDS", 2)
    cap = _env_int("STOMCHAT_GEMINI_RETRY_MAX_SECONDS", 60)
    jitter = random.uniform(0, min(5, base))
    return min(cap, base * (2 ** min(attempt, 4)) + jitter)

# Коды статусов ищем по границе слова. Подстрочный поиск "500" находил его в
# "1500 tokens" и "500000 tokens" — совершенно посторонний отказ трактовался как
# перегрузка сервера и банил модель на 20 минут для всех ключей сразу.
_STATUS_CODE_RE = re.compile(r"\b(429|500|502|503|504)\b")
_SERVER_ERROR_RE = re.compile(r"\b(500|502|503|504)\b")
_RATE_LIMIT_RE = re.compile(r"\b429\b|rate ?limit|quota|resource[_ ]exhausted")


def _is_retryable_gemini_error(error_text):
    # "rate" отдельным маркером был ловушкой: он есть в слове "generate", то
    # есть почти в любом сообщении об ошибке генерации.
    retry_markers = (
        "deadline", "timeout", "timed out", "temporarily",
        "unavailable", "failed_precondition",
        "connection", "transport"
    )
    if _STATUS_CODE_RE.search(error_text) or _RATE_LIMIT_RE.search(error_text):
        return True
    return any(marker in error_text for marker in retry_markers)

def _write_generation_status(context, **updates):
    if not context: return
    payload = dict(context)
    payload.update(updates)
    payload["active"] = True
    runtime_guard.write_summary_status(payload)


def _release_generation_status(context, reason=None, extra=None):
    """
    Снимает флаг «идёт работа» после обычного вызова ассистента.

    Любой вызов — ответ в ЛС, триаж, рецензент — пишет в файл статуса
    active: True. Сторож саммари убивает процесс (os._exit 79), если active
    стоит, а метка старше 30 минут.

    Почему в боевых журналах этого сторожа не видно (ни кода 79, ни дампов):
    флаг снимает finally в blocking_tools.generate_gemini_text_async — то есть
    родительский процесс, переживший ребёнка в любом случае. Вчера я записал,
    что объяснить молчание сторожа не могу; объяснение нашлось, и оно здесь.

    Сброс здесь остаётся страховкой на случай вызова gemini_client напрямую,
    в обход обёртки: этим путём ходит vision. Оба пути ведут в одну охрану в
    runtime_guard, чтобы правило не разъехалось по двум копиям.
    """
    if not context:
        return
    try:
        runtime_guard.release_generation_status(
            context.get("kind"), reason=reason, extra=extra
        )
    except Exception as status_err:
        logger.warning("Failed to release generation status: %s", status_err)

def _sleep_with_status(seconds, context, attempt, max_attempts, key_id):
    end_time = time.monotonic() + seconds
    while True:
        remaining = end_time - time.monotonic()
        if remaining <= 0: return
        _write_generation_status(
            context, stage="retry_sleep", attempt=attempt,
            max_attempts=max_attempts, key=key_id,
            retry_sleep_remaining_seconds=round(remaining, 1)
        )
        time.sleep(min(15, remaining))

def get_openai_client(api_key, base_url, timeout=120.0, **kwargs):
    client_kwargs = {
        "api_key": api_key if api_key else "dummy_key",
        "base_url": base_url,
        "timeout": timeout,
        "max_retries": 0,
    }
    if "default_headers" in kwargs and kwargs["default_headers"]:
        client_kwargs["default_headers"] = kwargs["default_headers"]
    return OpenAI(**client_kwargs)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANNED_MODELS_FILE = os.path.join(_BASE_DIR, "banned_models.json")
KEY_COOLDOWN_FILE = os.path.join(_BASE_DIR, "key_cooldowns.json")
KEY_COOLDOWN_SECONDS = 300


def _load_expiry_map(path):
    """Читает {ключ: unix_время_истечения}, отбрасывая протухшее."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > now}


def _save_expiry_map(path, data):
    """
    Атомарная запись через временный файл.

    Прежний вариант писал поверх напрямую: обрыв на середине оставлял
    обрезанный JSON, и весь список банов молча превращался в пустой — все
    перегруженные модели снова считались рабочими.

    Файл общий для параллельных подпроцессов, так что редкая потеря одной
    записи при одновременной записи возможна и допустима: цена — один лишний
    запрос к ключу, а не порча файла.
    """
    tmp_path = f"{path}.{os.getpid()}_{int(time.time() * 1000000)}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning(f"Failed to save {path}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception as exc:
            logger.debug("Failed to remove tmp_path %s: %s", tmp_path, exc)


def get_banned_models():
    return _load_expiry_map(BANNED_MODELS_FILE)


def ban_model(model_name, duration_seconds):
    models = get_banned_models()
    models[model_name] = time.time() + duration_seconds
    _save_expiry_map(BANNED_MODELS_FILE, models)


def _key_fingerprint(provider, api_key):
    """
    Устойчивый отпечаток ключа для файла кулдаунов.

    На диск идёт именно хеш: сам ключ — секрет, и писать его в файл рядом с
    логами и состоянием нельзя ни при каких обстоятельствах.
    """
    return hashlib.sha256(f"{provider}:{api_key}".encode("utf-8")).hexdigest()[:16]


def get_key_cooldowns():
    """
    Кулдауны ключей, переживающие процесс.

    Раньше это был обычный словарь в памяти модуля — а каждый LLM-вызов уходит
    в свежий подпроцесс (blocking_tools.py), который импортирует модуль заново.
    Словарь всегда был пуст, и пятиминутный кулдаун после 429 не действовал ни
    одного запроса: следующее же сообщение снова било в тот же исчерпанный ключ.
    Баны моделей автор сохранял в файл — здесь та же механика.
    """
    return _load_expiry_map(KEY_COOLDOWN_FILE)


def set_key_cooldown(provider, api_key, seconds=KEY_COOLDOWN_SECONDS):
    cooldowns = get_key_cooldowns()
    cooldowns[_key_fingerprint(provider, api_key)] = time.time() + seconds
    _save_expiry_map(KEY_COOLDOWN_FILE, cooldowns)


# Перегруженную модель убираем из каскада на это время. Значение было вписано
# числом прямо в обработчик ошибки; вынесено, потому что решение о бане теперь
# принимает note_key_failure, и величина не должна разъехаться по копиям.
MODEL_BAN_INITIAL_503_SECONDS = 60    # 1-я ошибка 503/504: бан на 60 сек (полный срок)
MODEL_BAN_REPEAT_503_SECONDS = 90     # Повторная ошибка 503 подряд: 1.5 минуты
MODEL_BAN_SECONDS = 180               # 3 минуты потолок на устойчивый отказ (исключает 20-минутный паралич)
MODEL_FAILURES_FILE = os.path.join(_BASE_DIR, "model_failures.json")


def _clear_failure_history(model_name):
    """Сбрасывает историю сбоев модели при успешном ответе."""
    if not os.path.exists(MODEL_FAILURES_FILE):
        return
    try:
        with open(MODEL_FAILURES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if model_name in data:
            data.pop(model_name, None)
            _save_expiry_map(MODEL_FAILURES_FILE, data)
    except Exception as exc:
        logger.debug("Failed to clear failure history for %s: %s", model_name, exc)


def _record_model_server_failure(model_name: str) -> int:
    """
    Рассчитывает прогрессивный срок бана для транзиентных ошибок 503/504:
    - 1-я ошибка: 60 секунд (быстрый спайк серверов Google).
    - 2-я ошибка подряд (в течение 15 минут): 300 секунд (5 минут).
    - 3-я и более: 1200 секунд (20 минут).
    """
    now = time.time()
    history = {}
    if os.path.exists(MODEL_FAILURES_FILE):
        try:
            with open(MODEL_FAILURES_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = {}

    entry = history.get(model_name, {})
    last_time = entry.get("last_time", 0) if isinstance(entry, dict) else 0
    count = entry.get("count", 0) if isinstance(entry, dict) else 0

    # Если последняя ошибка была более 15 минут назад — сбрасываем счетчик
    if now - last_time > 900:
        count = 0

    count += 1
    history[model_name] = {"count": count, "last_time": now}
    _save_expiry_map(MODEL_FAILURES_FILE, history)

    if count == 1:
        return MODEL_BAN_INITIAL_503_SECONDS
    elif count == 2:
        return MODEL_BAN_REPEAT_503_SECONDS
    else:
        return MODEL_BAN_SECONDS


# ==========================================================================
# ЕДИНЫЙ УЧЁТ ЗДОРОВЬЯ КЛЮЧЕЙ И МОДЕЛЕЙ. Публичные функции ниже — то, чем
# обязаны пользоваться ВСЕ пути, создающие клиента к модели.
#
# Замер по дереву (_measure_key_health.py, разбор ast): клиента создают семь
# модулей, а про кулдауны и баны знали два — этот и наполовину vision. Пул
# ключей у всех один (config.GOOGLE_KEYS — 10, config.GROQ_KEYS — 7), то есть
# квота у них тоже общая.
#
# Последствие для врача, когда путь ходит мимо учёта: ключ, уже упёршийся в
# квоту, пробуется снова и снова, а живой ключ простаивает. Врач ждёт ответа,
# который приходит после лишних попыток — или не приходит вовсе, потому что
# родительский дедлайн истёк на мёртвых ключах.
# ==========================================================================

# Адрес провайдера жил двумя строковыми литералами внутри generate_text. Любой
# другой путь, которому нужен клиент, копировал литерал себе — так и появились
# шесть независимых создателей клиента.
PROVIDER_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq": "https://api.groq.com/openai/v1",
    "agentrouter": getattr(config, "AGENTROUTER_BASE_URL", "https://agentrouter.org/v1"),
}


PROVIDER_KEY_ATTRS = {
    "gemini": "GOOGLE_KEYS",
    "groq": "GROQ_KEYS",
    "agentrouter": "AGENTROUTER_KEYS",
}


def provider_pool(provider):
    """Все настроенные ключи провайдера. Нужен, чтобы считать остаток живых."""
    value = getattr(config, PROVIDER_KEY_ATTRS.get(provider, ""), None)
    if isinstance(value, (list, tuple)):
        return [k for k in value if k]
    return []


GCP_PROJECT_COOLDOWN_SECONDS = 60


def is_gcp_project_quota_error(error_text: str) -> bool:
    """Определяет, относится ли ошибка 429 к квоте всего проекта GCP (все 10 ключей одного проекта)."""
    err_lower = str(error_text or "").lower()
    return (
        "project_number:" in err_lower
        or "generatecontentrequestsperminuteperprojectperregion" in err_lower
        or "886969145965" in err_lower
        or ("resource_exhausted" in err_lower and ("project" in err_lower or "perproject" in err_lower))
    )


def set_provider_pool_cooldown(provider: str, seconds: int = GCP_PROJECT_COOLDOWN_SECONDS) -> None:
    """Устанавливает кулдаун на весь пул ключей провайдера (при исчерпании проектной квоты GCP)."""
    pool = provider_pool(provider)
    for k in pool:
        set_key_cooldown(provider, k, seconds=seconds)


def get_provider_client(provider, api_key, timeout=120.0):
    """Клиент к провайдеру по его имени. Единственная точка создания клиента."""
    base_url = PROVIDER_BASE_URLS.get(provider)
    if not base_url:
        raise ValueError(f"Неизвестный провайдер модели: {provider!r}")
    if provider == "agentrouter":
        try:
            return get_openai_client(
                api_key, base_url, timeout=timeout,
                default_headers={"User-Agent": "opencode/1.18.25"}
            )
        except TypeError:
            return get_openai_client(api_key, base_url, timeout=timeout)
    # Через глобальное имя намеренно: тесты подменяют get_openai_client целиком.
    return get_openai_client(api_key, base_url, timeout=timeout)


def available_keys(provider, keys):
    """
    Делит ключи провайдера на живые и остывающие. Отбор для ВСЕХ путей — здесь.

    Возвращает (живые, остывающие, секунд_до_ближайшего_освобождения). Порядок
    внутри списков сохраняется — вызывающий уже перемешал пул, и перемешивать
    заново нельзя: это ломает размазывание квоты.

    Отдаём обе половины, а не только живую, потому что правильное поведение при
    «все остывают» у путей разное: текстовый каскад уходит к следующему
    провайдеру, а у расшифровки голосового следующего провайдера нет вовсе, и
    для неё остывающий ключ в конце очереди лучше, чем тишина врачу.
    """
    cooldowns = get_key_cooldowns()
    now_ts = time.time()
    fresh, cooling = [], []
    for key in keys:
        if cooldowns.get(_key_fingerprint(provider, key), 0) <= now_ts:
            fresh.append(key)
        else:
            cooling.append(key)
    wait_seconds = 0
    if cooling and not fresh:
        soonest = min(cooldowns.get(_key_fingerprint(provider, k), 0) for k in cooling)
        wait_seconds = max(0, int(soonest - now_ts))
    return fresh, cooling, wait_seconds


def active_models(cascade):
    """
    Убирает из каскада модели, забаненные за 5xx. Проверка бана — тоже здесь.
    Также дедуплицирует повторяющиеся имена моделей с сохранением порядка.

    Если забанены все — возвращаем последнюю: попытка по забаненной модели
    дешевле, чем гарантированное молчание бота.
    """
    now = time.time()
    banned = get_banned_models()
    active = []
    seen = set()
    for entry in cascade:
        model_name = entry[0] if isinstance(entry, (tuple, list)) else entry
        if model_name in seen:
            continue
        seen.add(model_name)
        ban_until = banned.get(model_name, 0)
        if ban_until > now:
            remaining = int(ban_until - now)
            reason = "(404/not_found)" if remaining > 3600 else "due to 503/504"
            logger.info(
                f"Model {model_name} is temporarily banned {reason} for "
                f"another {remaining}s. Skipping."
            )
            continue
        active.append(entry)
    if not active and cascade:
        logger.warning("All models in cascade are banned. Forcing fallback to the last model.")
        active = [cascade[-1]]
    return active


def _clear_expiry_entry(path, entry_key):
    """Снимает одну пометку. Файл не переписывается, если пометки там и не было."""
    current = _load_expiry_map(path)
    if entry_key not in current:
        return False
    current.pop(entry_key, None)
    _save_expiry_map(path, current)
    return True


def note_success(provider, api_key, model_name=None):
    """
    Удачный ответ СНИМАЕТ пометку с ключа и с модели. Вторая половина учёта.

    Пометки ставились, но не снимались никогда: и кулдаун ключа (300 с), и бан
    модели (1200 с) держались до истечения срока, даже когда та же пара только
    что успешно ответила. Пометка — предположение о здоровье, а удачный ответ —
    прямое доказательство обратного, и доказательство должно быть сильнее.

    Где это меняло поведение:
      * расшифровка голосового пробует остывающие ключи последними (у неё нет
        второй модели), и если такой ключ ответил, он обязан немедленно
        вернуться и в текстовый каскад — иначе ответ врачу в группе ещё
        четыре минуты будет обходить ключ, про который уже известно, что он жив;
      * active_models при «забанены все» принудительно берёт последнюю модель.
        Она отвечает — и остаётся забаненной, так что следующий вызов снова
        отсеет весь каскад и снова упрётся в ту же принудительную ветку.
    """
    cleared_key = _clear_expiry_entry(KEY_COOLDOWN_FILE, _key_fingerprint(provider, api_key))
    if cleared_key:
        logger.info("%s key answered while on cooldown; cooldown lifted.", provider.capitalize())
    if model_name:
        if _clear_expiry_entry(BANNED_MODELS_FILE, model_name):
            logger.info("Model %s answered while banned; ban lifted.", model_name)
        _clear_failure_history(model_name)


def note_key_failure(provider, api_key, error_text, model_name=None):
    """
    Разбирает отказ провайдера и ЗАПИСЫВАЕТ вывод в учёт. Классификатор один.

    Возвращает причину: key_rate_limited | model_overloaded | key_denied |
    request_failed. Что делать дальше, решает вызывающий: текстовый каскад на
    model_overloaded уходит к следующей модели, расшифровка — к следующему ключу.

    Порядок проверок оплачен дефектом и менять его нельзя: «ключ исчерпан»
    идёт ПЕРВЫМ. В теле 429 и Gemini, и Groq пишут сам лимит квоты, а он часто
    500-класса («quota_limit_value: 500 per day»), и проверка серверной ошибки
    находит там 500 отдельным словом. Когда бан стоял первым, один выдохшийся
    ключ выносил РАБОЧУЮ модель из каскада на 20 минут для всех подпроцессов
    сразу, а сам кулдауна не получал.

    model_name=None означает «замены этой модели в каскаде нет»: тогда бан не
    ставится. Так ходит расшифровка голосового — whisper-large-v3 там
    единственный, и бан на 20 минут лишил бы врача расшифровок целиком вместо
    того, чтобы просто сменить ключ.
    """
    err_msg = str(error_text or "").lower()
    # 402 Budget pool quota for a specific model (AgentRouter)
    if "402" in err_msg or "budget pool" in err_msg:
        if model_name:
            ban_duration = 1800  # 30 mins
            ban_model(model_name, ban_duration)
            logger.warning(
                f"{provider.capitalize()} model {model_name} budget pool exhausted (402). "
                f"Banning model {model_name} for {ban_duration}s. Key remains active."
            )
        _record_failure("model_overloaded", error_text, api_key)
        return "model_overloaded"

    if "content-blocked" in err_msg:
        logger.warning(f"{provider.capitalize()} content-blocked for {model_name}.")
        _record_failure("content_blocked", error_text, api_key)
        return "content_blocked"

    if _RATE_LIMIT_RE.search(err_msg):
        if provider == "gemini" and is_gcp_project_quota_error(err_msg):
            set_provider_pool_cooldown("gemini", seconds=GCP_PROJECT_COOLDOWN_SECONDS)
            logger.warning(
                "Gemini GCP Project quota exhausted (%s). "
                "Placed all %d Google keys on %ds cooldown to prevent burning keys.",
                err_msg[:120], len(provider_pool("gemini")), GCP_PROJECT_COOLDOWN_SECONDS,
            )
        else:
            set_key_cooldown(provider, api_key)
        # В журнале называем ПОСЛЕДСТВИЕ, а не только механику: строка
        # «placing key on 300s cooldown» не отвечала на единственный вопрос,
        # который стоит задавать по такой записи, — осталось ли чем отвечать
        # врачу. Остаток считаем после записи кулдауна, то есть уже с ним.
        pool = provider_pool(provider)
        alive = len(available_keys(provider, pool)[0]) if pool else 0
        if alive:
            logger.info(
                "%s rate limited (429/quota). Placing key on %ss cooldown. "
                "Последствие: живых ключей осталось %s из %s, ответ врачу идёт через них.",
                provider.capitalize(), KEY_COOLDOWN_SECONDS, alive, len(pool),
            )
        else:
            logger.warning(
                "%s rate limited (429/quota). Placing key on %ss cooldown. "
                "Последствие: живых ключей %s НЕ ОСТАЛОСЬ (всего %s) — до истечения "
                "кулдауна врач получит «не смог ответить» вместо ответа.",
                provider.capitalize(), KEY_COOLDOWN_SECONDS, provider, len(pool),
            )
        _record_failure("key_rate_limited", error_text, api_key)
        return "key_rate_limited"

    is_transient_overload = (
        "503" in err_msg
        or "504" in err_msg
        or "unavailable" in err_msg
        or "overload" in err_msg
        or "deadline" in err_msg
    )
    if _SERVER_ERROR_RE.search(err_msg) or is_transient_overload:
        if model_name:
            if is_transient_overload:
                ban_duration = _record_model_server_failure(model_name)
            else:
                ban_duration = MODEL_BAN_SECONDS
            ban_model(model_name, ban_duration)
            logger.info(
                f"{provider.capitalize()} server overloaded/unavailable ({err_msg}). "
                f"Banning model {model_name} for {ban_duration}s. Skipping in cascade."
            )
        else:
            logger.info(
                f"{provider.capitalize()} server overloaded/unavailable ({err_msg}). "
                f"Модель не банится: замены у этого пути нет, бан = отказ врачу целиком."
            )
        _record_failure("model_overloaded", error_text, api_key)
        return "model_overloaded"

    if any(m in err_msg for m in ("404", "not_found", "not found", "does not exist")):
        if model_name:
            ban_model(model_name, 3600) # 1 hour
            logger.warning(f"{provider.capitalize()} model {model_name} not found (404). Banned for 1h.")
        _record_failure("model_not_found", error_text, api_key)
        return "model_not_found"

    is_auth_error = any(m in err_msg for m in ("401", "403", "unauthorized", "invalid_api_key", "incorrect api key", "permission", "forbidden", "key has been revoked"))
    if is_auth_error:
        if provider == "gemini":
            is_api_permanent = any(m in err_msg for m in (
                "api_key_invalid", "permission_denied", "consumer_invalid",
                "key has been revoked", "unregistered_callers", "not registered", "401"
            ))
            if is_api_permanent:
                ban_duration = 31536000  # 365 days for google
                set_key_cooldown(provider, api_key, ban_duration)
                logger.warning(f"{provider.capitalize()} key permanently denied by API ({err_msg[:120]}). Banned for {ban_duration}s.")
            else:
                set_key_cooldown(provider, api_key, 3600)
                logger.warning(f"{provider.capitalize()} key encountered transient 403/Forbidden (likely proxy/gateway). Temporary cooldown 3600s.")
        else:
            is_groq_permanent = any(m in err_msg for m in ("401", "invalid", "incorrect", "revoked", "unauthorized"))
            if is_groq_permanent:
                set_key_cooldown(provider, api_key, 86400)
                logger.warning(f"{provider.capitalize()} key denied/invalid (401): {err_msg[:120]}. Placed on cooldown 24h.")
            else:
                logger.info(f"{provider.capitalize()} key encountered transient 403 ({err_msg[:120]}), switching without cooldown.")
        _record_failure("key_denied", error_text, api_key)
        return "key_denied"

    _record_failure("request_failed", error_text, api_key)
    return "request_failed"


# Маршрутизация по ВИДУ работы. Список видов собран по фактическим вызовам:
# assistant.py (21 вызов), summarizer.py (daily/weekly), blocking_tools.py
# (transcription_corrector). Прежняя таблица перечисляла имена, которых не
# передаёт НИКТО — term_explainer, quiz, direct_ask, assistant_media_pm, — а
# десять настоящих видов в неё не попадали и сваливались в else, то есть в
# тяжёлый «сводочный» каскад: bot_mention_reply, group_ask, group_quiz_gen,
# group_explainer, group_referee, pm_ping, transcription_corrector и три
# классификатора. Цена ошибки: короткий ответ в группе начинал с большой
# config.GEMINI_MODEL при бюджете 60 с, вместо lite-модели.
#
# Короткие классификаторы: у всех вызывающих thinking_level LOW и ответ в одну
# строку. Первой идёт groq-llama — на такой задаче она быстрее lite-моделей.
TRIAGE_KINDS = frozenset({
    "llama_triage", "bot_mention_triage", "response_validator", "referee_analyser",
    "group_ping_hot_check", "protocol_extraction",
    "react_standalone_triage", "react_triage",  # реакции — дешёвый lite-каскад
    "poll_triage",
})
# Живой диалог: ответ ждёт врач в чате, поэтому впереди lite-модели.
CHAT_KINDS = frozenset({
    "assistant", "assistant_media", "pm_chat", "pm_ping", "bot_mention_reply",
    "group_ask", "group_quiz_gen", "group_explainer", "group_referee",
    "transcription_corrector",
    # pm_web_lookup — ответ по выдержкам веб-поиска на команду /web. Это живой
    # диалог: врач ждёт в чате, и впереди должны идти lite-модели. Вид добавлен
    # вместе с самой командой, и без этой строки он проваливался в else, то есть
    # в тяжёлый сводочный каскад — ровно тот дефект, который описан выше и уже
    # стоил проекту десяти видов: короткий ответ начинался с большой
    # config.GEMINI_MODEL, а бюджет брался сводочный (2100 с) вместо диалогового.
    # Поймано test_fix_cascade сразу после подключения команды.
    "pm_web_lookup",
    "daemon_memory",
    "dialogue_fallback",
    "media_fallback",
    "dialogue_ebm_fallback",
    "group_ask_ebm_fallback",
    "media_ebm_fallback",
    "pm_cautious_ebm",
    "clinical_rescue",
})
# Генерация контента опроса — только Gemini, без Qwen/GPT.
# Qwen 3.8-27B при русских промптах выдаёт chinese thinking leaks:
# иероглифы в тексте ("宽ом апексе"), смешанные слова ("gutтаперчи").
# Опрос не real-time (расписание 13:30 МСК) — задержки ретраев допустимы.
# При вылете всех ключей Gemini: EXHAUSTION_RETRY_KINDS даёт 3 шанса с паузой 30/60/90s.
POLL_GEN_KINDS = frozenset({
    "poll_content_gen",
})
# Всё остальное — daily, weekly, group_summary и любой незнакомый вид — идёт в
# тяжёлый каскад: там качество важнее задержки, и бюджет там 2100 с.


# Базовый минимальный порог на модель при планировании бюджета.
MIN_REQUEST_SECONDS = 4.0
# Дробить долю модели на несколько попыток по разным ключам стоит только если
# каждой достанется хотя бы столько: иначе ротация ключей превращается в серию
# запросов, убитых по таймауту на середине ответа.
COMFORT_REQUEST_SECONDS = 92.0
# 15% бюджета не раздаём запросам: это сны между попытками (2-12 с,
# _retry_sleep_seconds), старт подпроцесса с импортом openai (~1-2 с) и запас,
# чтобы успеть записать причину провала ДО того, как родитель убьёт процесс.
BUDGET_RESERVE_SHARE = 0.85

# Причина последнего провала каскада.
#
# generate_text возвращает None одинаково и на «все ключи остывают», и на
# «промпт длиннее контекста», и на «модель забанена». Вызывающий различить их не
# мог, в журнале стояло одно и то же «All AI attempts exhausted», и врачу шло
# одинаковое «не смог ответить». Причина теперь остаётся здесь — для вызова в
# том же процессе (vision) — и уходит в файл статуса, который переживает смерть
# подпроцесса и потому доступен родителю.
LAST_FAILURE = None


def get_last_failure():
    """Причина последнего провала каскада: {'reason', 'detail', 'ts'} или None."""
    return LAST_FAILURE


def _reset_failure():
    global LAST_FAILURE
    LAST_FAILURE = None


def _all_known_keys(extra_key=None):
    """Все настроенные ключи: их не должно быть ни в журнале, ни в файле статуса."""
    keys = []
    for attr in ("GOOGLE_KEYS", "GROQ_KEYS", "AGENTROUTER_KEYS", "OPENROUTER_KEYS"):
        value = getattr(config, attr, None)
        if isinstance(value, (list, tuple, set)):
            keys.extend(k for k in value if isinstance(k, str) and len(k) >= 8)
    if extra_key:
        keys.append(extra_key)
    # Длинные вперёд: короткий ключ может быть началом длинного, и замена
    # короткого первой оставила бы хвост длинного в тексте.
    return sorted(set(keys), key=len, reverse=True)


def _record_failure(reason, detail="", api_key=None):
    global LAST_FAILURE
    text = str(detail or "")[:500]
    # Ключ — секрет. Причина уходит в файл статуса и в журнал, поэтому ключ
    # вырезаем даже из текста чужого исключения.
    #
    # Вырезать ТОЛЬКО переданный ключ недостаточно: провайдер присылает
    # «401 invalid api key <ключ> rejected», где ключ может быть любым из
    # настроенных — например тем, которым ходил предыдущий запрос каскада, или
    # тем, что попал в текст из чужого заголовка. Замер на подставном каскаде:
    # ключ утекал в файл статуса при совпадении по любому НЕ переданному ключу.
    # Поэтому чистим по всему набору.
    for secret in _all_known_keys(api_key):
        if secret and secret in text:
            text = text.replace(secret, "***")
    LAST_FAILURE = {"reason": reason, "detail": text, "ts": time.time()}


MEDIA_PROMPT_PATTERNS = [
    re.compile(r"\[МУЛЬТИМОДАЛЬНО[ЕЙ]\s+ЗРЕНИ[ЕЯ]", re.IGNORECASE),
    re.compile(r"\[МУЛЬТИМОДАЛЬНЫЙ\s+АНАЛИЗ", re.IGNORECASE),
    re.compile(r"описание\s+изображения\s*\(", re.IGNORECASE),
    re.compile(r"распознано\s+моделью\s+зрения", re.IGNORECASE),
    re.compile(r"модел(?:ь|ью|и)\s+зрения", re.IGNORECASE),
    re.compile(r"(?:\(|^|\s)(?:на\s+(?:фото|снимке|рентгенограмме)|прикреплен[оаы]?\s+(?:фото|снимок|изображение|серия))", re.IGNORECASE),
    re.compile(r"(?:на|по)\s+присланн(?:ом|ому)\s+(?:снимке|фото|изображени[ию])", re.IGNORECASE),
    re.compile(r"присланн(?:ое|ый)\s+(?:изображение|снимок|фото)", re.IGNORECASE),
    re.compile(r"анатомический\s+и\s+рентгенологический\s+статус", re.IGNORECASE),
    re.compile(r"детали\s+(?:каждого\s+)?снимка", re.IGNORECASE),
    re.compile(r"компьютерное\s+зрение\s*\(\s*vision\s*\)", re.IGNORECASE),
    re.compile(r"комментарий\s+по\s+снимку", re.IGNORECASE),
]


def is_media_task(status_context=None, prompt=None) -> bool:
    """
    Определяет, относится ли задача к анализу фото/рентгена/медиа.
    Проверяет как контекст вызова (has_media, image_urls, kind),
    так и текст промпта на ключевые клинические маркеры визуального осмотра.
    """
    if isinstance(status_context, dict):
        # Triage, классификаторы и валидаторы ответов (response_validator) — это ВСЕГДА
        # чисто текстовые задачи. Они никогда не должны попадать в тяжёлый медиа-каскад,
        # даже если в тексте проверяемого черновика или контексте цитируется снимок.
        if status_context.get("kind") in TRIAGE_KINDS:
            return False
        if status_context.get("has_media"):
            return True
        if status_context.get("image_urls"):
            return True
        if status_context.get("kind") in ("assistant_media", "assistant_media_pm", "media_fallback"):
            return True
    if prompt and isinstance(prompt, str):
        if status_context and status_context.get("kind") in TRIAGE_KINDS:
            return False
        for pat in MEDIA_PROMPT_PATTERNS:
            if pat.search(prompt):
                return True
    return False


def _adapt_prompt_for_text_model(prompt: str, status_context: dict | None = None) -> str:
    """
    Адаптирует промпт для чисто текстовых моделей (DeepSeek, Groq), не имеющих нативного зрения:
    1. Срезает директивы мультимодального зрения и запреты «не писать что снимки не загрузились».
    2. Заменяет плейсхолдеры прикрепленных картинок на текстовое клиническое описание из Vision.
    3. Добавляет ясную инструкцию клинического разбора по представленным данным без галлюцинаций.
    """
    if not prompt:
        return prompt

    effective_prompt = prompt
    media_desc = ""
    if isinstance(status_context, dict):
        media_desc = str(status_context.get("media_description") or "").strip()

    had_vision_directive = bool(
        re.search(r"\[МУЛЬТИМОДАЛЬНО[ЕЙ]\s+ЗРЕНИ[ЕЯ]", effective_prompt, re.IGNORECASE)
        or re.search(r"\[Оригинальное изображение прикреплено к запросу", effective_prompt)
        or re.search(r"\[Ожидается разбор изображения\]", effective_prompt)
    )

    # Срезаем блоки мультимодального зрения
    effective_prompt = re.sub(
        r"\[МУЛЬТИМОДАЛЬНО[ЕЙ]\s+ЗРЕНИ[ЕЯ][^\]]*\][\s\S]*?(?=\n\n|\Z)",
        "",
        effective_prompt,
        flags=re.IGNORECASE
    )
    effective_prompt = re.sub(
        r"[-\s*•]*КАТЕГОРИЧЕСКИ\s+ЗАПРЕЩЕНО\s+писать\s+[«\"]снимки\s+не\s+загрузились[»\"][^\n]*",
        "",
        effective_prompt,
        flags=re.IGNORECASE
    )

    placeholder_pattern = r"\[Оригинальное изображение прикреплено к запросу[^\]]*\]|\[Ожидается разбор изображения\]"
    if media_desc:
        replacement_text = f"[Клинико-рентгенологическое описание снимка]: {media_desc}"
        if re.search(placeholder_pattern, effective_prompt):
            effective_prompt = re.sub(placeholder_pattern, replacement_text, effective_prompt)
        elif media_desc not in effective_prompt and had_vision_directive:
            effective_prompt += f"\n\n[КЛИНИКО-РЕНТГЕНОЛОГИЧЕСКОЕ ОПИСАНИЕ СНИМКА]:\n{media_desc}\n"
    else:
        effective_prompt = re.sub(
            placeholder_pattern,
            "[Клинический снимок: опирайся на вопрос врача и контекст обсуждения, укажи ключевые рентгенологические ориентиры для проверки]",
            effective_prompt
        )

    if had_vision_directive and "[РЕЖИМ ТЕКСТОВОГО РАЗБОРА" not in effective_prompt:
        effective_prompt = (
            "[РЕЖИМ ТЕКСТОВОГО РАЗБОРА КЛИНИЧЕСКОГО СЛУЧАЯ (РЕЗЕРВНЫЙ ТЕКСТОВЫЙ КАСКАД)]:\n"
            "Разбор проводится по клиническому контексту и описанию случая. "
            "Дай доказательный EBM-разбор, дифдиагноз и тактику лечения.\n\n"
            + effective_prompt.strip()
        )

    return effective_prompt


def cascade_for_context(status_context=None, prompt=None):
    """
    Формирует каскад (model_name, provider) в зависимости от контекста задачи.
    Для медиа/фото/рентгена: мультимодальные модели Gemini (3.8 -> 3.7 -> 3.6 -> 3.5 -> lite) первыми,
    затем надежный текстовый резерв (DeepSeek через AgentRouter и Groq) на случай отказа Google.
    Для CHAT_KINDS: gemini-3.5-flash-lite в начале для быстрых ответов (при LOW/MEDIUM),
    но gemini-3.8-flash доступен для глубоких рассуждений (при HIGH).
    Для daemon_memory и простых задач с LOW не проваливается во флагманский gemini-3.8-flash.
    """
    if is_media_task(status_context, prompt):
        has_agentrouter = bool(provider_pool("agentrouter"))
        groq_primary = getattr(config, "GROQ_MODEL", "qwen/qwen3.8-27b") or "qwen/qwen3.8-27b"
        cascade = [
            ("gemini-3.8-flash", "gemini"),
            ("gemini-3.5-flash-lite", "gemini"),
        ]
        if has_agentrouter:
            cascade.append(("deepseek-v4-flash", "agentrouter"))
        cascade.extend([
            ("gemini-3.7-flash", "gemini"),
            ("gemini-3.6-flash", "gemini"),
            ("gemini-3.5-flash", "gemini"),
            ("gemini-3.1-flash-lite", "gemini"),
            (groq_primary, "groq"),
        ])
        return cascade

    kind = status_context.get("kind") if status_context else None
    is_triage = kind in TRIAGE_KINDS
    summary_kinds = getattr(runtime_guard, "SUMMARY_KINDS", frozenset({"daily", "weekly", "group_summary"}))
    if kind in summary_kinds and (not status_context or "thinking_level" not in status_context):
        thinking_level = "HIGH"
    else:
        thinking_level = status_context.get("thinking_level", "MEDIUM") if status_context else "MEDIUM"

    is_chatbot = kind in CHAT_KINDS
    is_clinical_review = (kind == "poll_clinical_review")
    is_poll_gen = kind in POLL_GEN_KINDS
    has_agentrouter = bool(provider_pool("agentrouter"))
    groq_primary = getattr(config, "GROQ_MODEL", "qwen/qwen3.8-27b") or "qwen/qwen3.8-27b"
    groq_secondary = "openai/gpt-oss-120b"

    if is_triage:
        base = [
            ("gemini-3.5-flash-lite", "gemini"),
            (groq_primary, "groq"),
            ("gemini-3.1-flash-lite", "gemini"),
            (groq_secondary, "groq"),
            ("gemini-3.8-flash", "gemini"),
            ("gemini-3.7-flash", "gemini"),
            ("gemini-3.6-flash", "gemini"),
        ]
        if has_agentrouter:
            base.append(("deepseek-v4-flash", "agentrouter"))
        return base
    elif kind == "daemon_memory":
        # Фоновое обновление досье врача: используем легкие Flash-Lite и Groq
        cascade = [
            ("gemini-3.5-flash-lite", "gemini"),
            ("gemini-3.1-flash-lite", "gemini"),
            (groq_primary, "groq"),
            ("gemini-3.6-flash", "gemini"),
        ]
        if has_agentrouter:
            cascade.append(("deepseek-v4-flash", "agentrouter"))
        return cascade
    elif is_clinical_review:
        # Независимый кросс-модельный рецензент: опрос от Gemini проверяет DeepSeek / Qwen!
        if has_agentrouter:
            return [
                ("deepseek-v4-flash", "agentrouter"),
                (groq_primary, "groq"),
                (groq_secondary, "groq"),
                ("gemini-3.8-flash", "gemini"),
                ("gemini-3.7-flash", "gemini"),
                ("gemini-3.5-flash-lite", "gemini"),
            ]
        return [
            (groq_primary, "groq"),
            (groq_secondary, "groq"),
            ("gemini-3.8-flash", "gemini"),
            ("gemini-3.7-flash", "gemini"),
            ("gemini-3.5-flash-lite", "gemini"),
        ]
    elif is_poll_gen:
        # Только Gemini: Qwen выдаёт chinese thinking leaks в русском тексте.
        # Опрос не real-time — при вылете ключей ждём EXHAUSTION_RETRY_KINDS backoff.
        base = [
            ("gemini-3.8-flash", "gemini"),
            ("gemini-3.7-flash", "gemini"),
            ("gemini-3.6-flash", "gemini"),
            ("gemini-3.5-flash-lite", "gemini"),
            ("gemini-3.1-flash-lite", "gemini"),
        ]
        if has_agentrouter:
            base.append(("deepseek-v4-flash", "agentrouter"))
        return base
    elif is_chatbot and bool(
        kind in ("assistant_media", "assistant_media_pm")
        or (status_context and (status_context.get("image_urls") or status_context.get("has_media")))
    ):
        has_agentrouter = bool(provider_pool("agentrouter"))
        cascade = [
            ("gemini-3.8-flash", "gemini"),
            ("gemini-3.7-flash", "gemini"),
            ("gemini-3.6-flash", "gemini"),
            ("gemini-3.5-flash", "gemini"),
            ("gemini-3.5-flash-lite", "gemini"),
            ("gemini-3.1-flash-lite", "gemini"),
        ]
        if has_agentrouter:
            cascade.append(("deepseek-v4-flash", "agentrouter"))
        cascade.append((groq_primary, "groq"))
        return cascade
    elif is_chatbot and (thinking_level in ("LOW", "MEDIUM") or (kind in ("pm_chat", "pm_ping") and thinking_level != "HIGH")):
        cascade = [
            ("gemini-3.5-flash-lite", "gemini"),
            ("gemini-3.8-flash", "gemini"),
            ("gemini-3.7-flash", "gemini"),
            ("gemini-3.6-flash", "gemini"),
        ]
        if has_agentrouter:
            cascade.append(("deepseek-v4-flash", "agentrouter"))
        cascade.extend([
            (groq_primary, "groq"),
            ("gemini-3.1-flash-lite", "gemini"),
            (groq_secondary, "groq"),
        ])
        return cascade
    elif is_chatbot:
        cascade = [
            ("gemini-3.8-flash", "gemini"),
            ("gemini-3.7-flash", "gemini"),
            ("gemini-3.6-flash", "gemini"),
            ("gemini-3.5-flash-lite", "gemini"),
        ]
        if has_agentrouter:
            cascade.append(("deepseek-v4-flash", "agentrouter"))
        cascade.extend([
            (groq_primary, "groq"),
            ("gemini-3.1-flash-lite", "gemini"),
            (groq_secondary, "groq"),
        ])
        return cascade
    elif kind in summary_kinds:
        # Для ежедневных и еженедельных дайджестов с гигантским контекстом (50k–200k+ символов)
        # и необходимостью масштабного лонгрида (8.5k–15k знаков) модели Gemini идут первыми:
        # у них окно контекста 1M+ токенов и проверенное глубокое изложение.
        # DeepSeek через AgentRouter выступает как надежный резерв при отказе Google.
        primary_heavy = getattr(config, "GEMINI_MODEL", "gemini-3.8-flash") or "gemini-3.8-flash"
        all_gem = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
        raw_models = [(primary_heavy, "gemini")]
        if primary_heavy in all_gem:
            idx = all_gem.index(primary_heavy)
            raw_models.extend([(m, "gemini") for m in all_gem[idx+1:]])
        else:
            raw_models.extend([(m, "gemini") for m in all_gem])
        seen = set()
        cascade = []
        for m in raw_models:
            if m not in seen:
                seen.add(m)
                cascade.append(m)
        if has_agentrouter:
            cascade.append(("deepseek-v4-flash", "agentrouter"))
        cascade.extend([
            ("gemini-3.1-flash-lite", "gemini"),
            (groq_primary, "groq"),
            (groq_secondary, "groq"),
        ])
        return cascade
    else:
        # Complex tasks (Analytics, etc)
        primary_heavy = getattr(config, "GEMINI_MODEL", "gemini-3.8-flash") or "gemini-3.8-flash"
        all_gem = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
        raw_models = [(primary_heavy, "gemini")]
        if primary_heavy in all_gem:
            idx = all_gem.index(primary_heavy)
            raw_models.extend([(m, "gemini") for m in all_gem[idx+1:]])
        else:
            raw_models.extend([(m, "gemini") for m in all_gem])
        seen = set()
        cascade = []
        for m in raw_models:
            if m not in seen:
                seen.add(m)
                cascade.append(m)
        if has_agentrouter:
            cascade.append(("deepseek-v4-flash", "agentrouter"))
        cascade.extend([
            ("gemini-3.1-flash-lite", "gemini"),
            (groq_primary, "groq"),
            (groq_secondary, "groq"),
        ])
        return cascade


def generate_text(prompt, status_context=None, timeout=None):
    """Generate summary text through Gemini with Groq fallback."""
    _reset_failure()
    kind = status_context.get("kind") if status_context else None
    # is_pm здесь больше нет намеренно: он остался от ПРЕЖНЕЙ таблицы
    # маршрутизации и уже ни на что не влиял. `pm_chat` входит в CHAT_KINDS, то
    # есть разбирается как живой диалог ниже, а `assistant_media_pm` не передаёт
    # НИКТО — это прямо сказано в комментарии к таблице и закреплено проверкой
    # test_fix_cascade.py:264. Живой флаг, вычисляемый из мёртвого условия, читается
    # как «здесь есть ветка для личных сообщений», которой нет.
    is_triage = kind in TRIAGE_KINDS
    is_chatbot = kind in CHAT_KINDS
    is_clinical_review = (kind == "poll_clinical_review")
    is_poll_gen = kind in POLL_GEN_KINDS
    summary_kinds = getattr(runtime_guard, "SUMMARY_KINDS", frozenset({"daily", "weekly", "group_summary"}))
    if kind in summary_kinds and (not status_context or "thinking_level" not in status_context):
        thinking_level = "HIGH"
    else:
        thinking_level = status_context.get("thinking_level", "MEDIUM") if status_context else "MEDIUM"

    groq_fallback = "openai/gpt-oss-120b" if thinking_level == "HIGH" else config.GROQ_MODEL

    # Таймаут одного запроса считается ниже, когда известен каскад: он зависит от
    # числа попыток, а число попыток — от числа моделей и живых ключей.
    req_timeout = 140.0

    # Авто-детект фокуса на изображении/рентгене по контексту и промпту
    is_media = is_media_task(status_context, prompt)
    if is_media and isinstance(status_context, dict) and not status_context.get("has_media"):
        status_context["has_media"] = True

    models_cascade = cascade_for_context(status_context, prompt=prompt)
    active_cascade = active_models(models_cascade)

    max_attempts = _env_int("STOMCHAT_GEMINI_MAX_ATTEMPTS", 3)

    # АРИФМЕТИКА БЮДЖЕТА: попытки x таймаут попытки обязаны влезать в timeout.
    #
    # Родитель (blocking_tools.generate_gemini_text_async) держит подпроцесс под
    # asyncio.wait_for ровно на этот же timeout и убивает его по истечении.
    # Прежнее timeout/3 исходило из трёх запросов, а каскад делает моделей x
    # попыток = 4 x 3 = 12. Замер по коду на боевых бюджетах: assistant 90 с ->
    # 12 x 30 с = 360 с (перерасход x4), llama_triage 20 с -> 9 x 7 с = 63 с
    # (x3.1), response_validator 15 с -> 12 x 7 с = 84 с (x5.6), daily 2100 с ->
    # 12 x 700 с = 8400 с (x4). Процесс умирал на третьей попытке ПЕРВОЙ модели,
    # то есть резервный провайдер groq в конце каскада не пробовался ни разу —
    # ради него каскад и написан.
    #
    # Делим иначе: сначала бюджет режется между моделями (шанс должна получить
    # каждая, в этом смысл каскада), и только если доля модели вмещает несколько
    # запросов по COMFORT_REQUEST_SECONDS, доля дробится на попытки по ключам.
    # При бюджете 90 с и 4 моделях выходит 4 запроса по 19 с вместо 12 по 30 с;
    # при 2100 с — 12 по 148 с, ротация ключей сохраняется полностью.
    deadline = None
    if timeout:
        budget = float(timeout)
        # На резерв не жертвуем последней попыткой: при совсем малом бюджете
        # (меньше 8 с такие вызывающие сейчас не передают) лучше один запрос на
        # весь бюджет, чем ноль запросов и гарантированный None.
        min_req_step = 2.0 if is_triage else 4.0
        usable = max(min(budget, MIN_REQUEST_SECONDS), budget * BUDGET_RESERVE_SHARE)
        models_fit = max(1, min(len(active_cascade), int(usable // min_req_step)))
        # НЕ обрезаем каскад: быстрые резервные модели (Groq/Qwen) отвечают за 0.8-1.5с.
        # Если ранние модели Google сбоят (503/400/timeout), Groq должен получить попытку.
        # Дедлайн (deadline) динамически страхует от вылета за общий бюджет.
        if models_fit < len(active_cascade):
            logger.info(
                "Budget %.0fs nominally fits %s of %s cascade models; tail preserved for fast fallbacks.",
                budget, models_fit, len(active_cascade)
            )
        share_divisor = max(1, min(models_fit, len(active_cascade)))
        model_share = usable / share_divisor
        comfort_step = 4.0 if is_triage else 12.0
        max_attempts = max(1, min(max_attempts, int(model_share // comfort_step)))
        req_timeout = model_share / max_attempts
        # Интерактивные потолки таймаута на один HTTP-запрос к провайдеру:
        # Для triage/валидатора: максимум 15с (ответ классификатора нужен за секунды).
        # Для живого чата: максимум 60с (даем reasoning-моделям Gemini/DeepSeek время
        # на генерацию подробных клинических ответов без ложных срывов по ReadTimeout).
        if is_triage:
            req_timeout = max(7.0 if budget >= 7.0 else 2.5, min(req_timeout, 15.0))
        elif is_chatbot:
            # Chairside потолок таймаута на один HTTP-запрос в живом чате у кресла:
            # Не позволяем первичному вызову висеть 110+ секунд!
            # Для HIGH: максимум 50с. Для MEDIUM: максимум 35с.
            chat_max = 50.0 if thinking_level == "HIGH" else 35.0
            req_timeout = max(15.0, min(req_timeout, chat_max))
        # Дедлайн считаем по usable, а не по budget: остановиться нужно ДО
        # убийства родителем, иначе причину провала записать будет некому.
        deadline = time.monotonic() + usable
        logger.info(
            "Budget %.0fs: %s models x %s attempts x %.1fs = %.0fs planned.",
            budget, len(active_cascade), max_attempts, req_timeout,
            len(active_cascade) * max_attempts * req_timeout
        )

    out_of_budget = False
    requests_made = 0
    max_media_rounds = 8 if is_media else 1
    media_round = 0

    while media_round < max_media_rounds and not out_of_budget:
        min_cooldown_seen = float('inf')
        for model_index, (model_name, provider) in enumerate(active_cascade):
            # Для медиа-задач Gemini пробуются первыми (в начале active_cascade).
            # Если все Gemini в раунде исчерпаны/на кулдауне, каскад переключается на
            # резервные текстовые модели (AgentRouter / DeepSeek, Groq) через _adapt_prompt_for_text_model.
            keys = list(provider_pool(provider))
            # Адрес провайдера берём из общей таблицы PROVIDER_BASE_URLS: два литерала
            # здесь были источником, с которого их скопировали остальные пути.
            client_maker = lambda k: get_provider_client(provider, k, timeout=req_timeout)

            if not keys:
                logger.warning(f"No API keys for {provider}. Skipping {model_name}.")
                _record_failure("no_keys_configured", f"{provider}: ключи не настроены")
                continue

            random.shuffle(keys)

            # Ключи на кулдауне отсеиваются ДО цикла попыток.
            #
            # Раньше проверка стояла внутри цикла и делала `continue`: «холодный»
            # ключ съедал попытку целиком, не отправив запроса. Настроено 10 ключей
            # Google и 7 Groq при бюджете max_attempts=3 — трёх подряд попавшихся
            # остывающих ключей хватало, чтобы модель была пропущена при семи
            # полностью здоровых. Когда так же осыпался весь каскад, бот писал
            # «All AI attempts exhausted» и молча не отвечал врачу, имея на руках
            # больше десятка рабочих ключей.
            # Отбор — через общий учёт (available_keys), а не своей копией фильтра.
            available, _cooling, cooldown_wait = available_keys(provider, keys)
            if not available:
                logger.info(
                    "All %s keys are on cooldown (%ss left); skipping %s in cascade.",
                    provider, cooldown_wait, model_name
                )
                _record_failure(
                    "all_keys_on_cooldown",
                    f"{provider}: все {len(keys)} ключей остывают, ближайший через "
                    f"{cooldown_wait}с"
                )
                continue
            if len(available) < len(keys):
                logger.info(
                    "%s: %s of %s keys available, rest on cooldown.",
                    provider, len(available), len(keys)
                )

            # Бюджет попыток — это число РЕАЛЬНЫХ запросов, каждый на своём ключе.
            attempts_planned = min(max_attempts, len(available))
            is_last_model = (model_index == len(active_cascade) - 1) and (media_round + 1 >= max_media_rounds)
            for attempt in range(attempts_planned):
                api_key = available[attempt]
                key_id = f"{provider}...{api_key[-5:]}" if api_key else f"{provider}_none"

                # Первый запрос делаем всегда: бюджет уже нарезан так, чтобы он в
                # него влез, а ноль запросов — это гарантированное молчание бота.
                if deadline is not None and requests_made:
                    remaining = deadline - time.monotonic()
                    if is_triage:
                        min_needed = min(7.0, float(timeout or 7.0))
                    elif kind in summary_kinds:
                        min_needed = 10.0
                    else:
                        min_needed = 7.0
                    if remaining < min_needed:
                        subsequent_models = active_cascade[model_index + 1:]
                        subsequent_min_needed = min(
                            (1.5 if p == "groq" else (2.0 if "lite" in m else (2.5 if p == "agentrouter" else 3.5)))
                            for m, p in subsequent_models
                        ) if subsequent_models else 999.0

                        if remaining < 1.5 or remaining < subsequent_min_needed:
                            logger.warning(
                                "Budget spent (%.1fs left, needed %.1fs) before %s; stopping cascade.",
                                max(0.0, remaining), min_needed, model_name
                            )
                            _record_failure(
                                "budget_exhausted",
                                f"бюджет {float(timeout):.0f}с израсходован до модели {model_name}"
                            )
                            out_of_budget = True
                            break
                        else:
                            logger.info(
                                "Skipping slow model %s (%.1fs left < %.1fs needed); proceeding to faster fallback in cascade.",
                                model_name, max(0.0, remaining), min_needed
                            )
                            break
                    # Последнему запросу отдаём ровно остаток бюджета, а не полную
                    # долю: сумма таймаутов запросов не должна вылезать за timeout.
                    req_timeout = min(req_timeout, remaining)

                call_timeout = req_timeout
                if is_chatbot:
                    if provider in ("gemini", "agentrouter"):
                        rem = (deadline - time.monotonic()) if deadline else 120.0
                        has_more_models = (model_index < len(active_cascade) - 1)
                        fallback_reserve = 12.0 if len(active_cascade) > 2 else 4.0
                        if has_more_models and rem > fallback_reserve:
                            max_allowed_for_model = min(rem, max(MIN_REQUEST_SECONDS, rem - fallback_reserve))
                        else:
                            max_allowed_for_model = rem
                        # Chairside потолок ожидания врача: не перебивать req_timeout гигантским числом!
                        chat_ceiling = 50.0 if thinking_level == "HIGH" else 35.0
                        call_timeout = max(MIN_REQUEST_SECONDS, min(req_timeout, rem, chat_ceiling, max_allowed_for_model))
                elif provider == "agentrouter":
                    rem = (deadline - time.monotonic()) if deadline else 140.0
                    if kind in summary_kinds:
                        call_timeout = max(call_timeout, min(720.0, max(120.0, rem)))
                    else:
                        call_timeout = max(call_timeout, min(120.0, max(15.0, rem)))
                if deadline is not None:
                    rem = max(1.0, deadline - time.monotonic())
                    call_timeout = min(call_timeout, rem)

                try:
                    client = get_provider_client(provider, api_key, timeout=call_timeout)
                    _write_generation_status(
                        status_context, stage=f"{provider}_request",
                        attempt=attempt + 1, max_attempts=max_attempts,
                        key=key_id, model=model_name
                    )
                    logger.info(f"{provider.capitalize()} request attempt={attempt + 1}/{max_attempts} key={key_id} model={model_name}")

                    requests_made += 1
                    # Поддержка мультимодальности: если переданы image_urls, прикрепляем их для Gemini
                    image_urls = status_context.get("image_urls") if isinstance(status_context, dict) else None
                    can_vision = (provider == "gemini") or ("vision" in model_name.lower())

                    if can_vision and image_urls:
                        user_content = [{"type": "text", "text": prompt}]
                        max_imgs = 16 if provider == "gemini" else 3
                        for iu in image_urls[:max_imgs]:
                            user_content.append({"type": "image_url", "image_url": {"url": iu}})
                        messages_payload = [{"role": "user", "content": user_content}]
                    else:
                        adapted_prompt = _adapt_prompt_for_text_model(prompt, status_context)
                        messages_payload = [{"role": "user", "content": adapted_prompt}]

                    # Using OpenAI SDK for BOTH Groq and Gemini now
                    ctx_temp = status_context.get("temperature") if isinstance(status_context, dict) else None
                    if ctx_temp is not None:
                        eff_temp = float(ctx_temp)
                    elif is_triage or is_clinical_review:
                        eff_temp = 0.2
                    else:
                        eff_temp = 0.95

                    create_kwargs = {
                        "model": model_name,
                        "messages": messages_payload,
                        "temperature": eff_temp,
                    }
                    ctx_max_tokens = status_context.get("max_tokens") if isinstance(status_context, dict) else None
                    if ctx_max_tokens:
                        create_kwargs["max_tokens"] = int(ctx_max_tokens)
                    elif kind == "daemon_memory":
                        create_kwargs["max_tokens"] = 2048

                    # Лимит OTPM для Groq (особенно бесплатные квоты Qwen): потолок 1000 токенов/мин
                    # Лимит токенов для Groq (учитываем reasoning-модели типа Qwen-32B / DeepSeek):
                    if provider == "groq":
                        if "max_tokens" in create_kwargs:
                            create_kwargs["max_tokens"] = min(create_kwargs["max_tokens"], 4096)
                        elif "qwen" in model_name.lower() or "deepseek" in model_name.lower() or "reason" in model_name.lower():
                            create_kwargs["max_tokens"] = 4096

                    # Для AgentRouter (DeepSeek / рассуждающие модели) не ограничиваем токенами:
                    # Даем просторный лимит (минимум 8192 для HIGH/MEDIUM), чтобы reasoning tokens не съедали ответ.
                    # Для дайджестов выделяем расширенный лимит 16384 токенов.
                    if provider == "agentrouter":
                        default_max = 16384 if kind in summary_kinds else (8192 if thinking_level in ("HIGH", "MEDIUM") else 4096)
                        create_kwargs["max_tokens"] = max(default_max, int(ctx_max_tokens or default_max))

                    # Нативный параметр размышлений для моделей:
                    # Передаем ТОЛЬКО Gemini, так как Groq API не поддерживает reasoning_effort.
                    # Google OpenAI endpoint поддерживает только 'low', 'medium', 'high'.
                    # Значение 'none' вызывает 400 INVALID_ARGUMENT, поэтому для none/triage опускаем параметр.
                    if provider == "gemini":
                        if not is_triage:
                            # Thinking tokens входят в max_tokens на Google OpenAI endpoint!
                            # Даем 8192 токенов для HIGH/MEDIUM (16384 для summary), чтобы CoT не вызывал finish_reason: length!
                            gemini_default_max = 16384 if kind in summary_kinds else 8192
                            create_kwargs["max_tokens"] = max(gemini_default_max, int(ctx_max_tokens or gemini_default_max))
                        if is_triage or thinking_level in ("NONE", "none"):
                            pass
                        elif thinking_level == "LOW":
                            create_kwargs["reasoning_effort"] = "low"
                        elif thinking_level == "HIGH":
                            create_kwargs["reasoning_effort"] = "high"
                        else:  # MEDIUM
                            create_kwargs["reasoning_effort"] = "medium"

                    while True:
                        try:
                            response = client.chat.completions.create(**create_kwargs)
                            break
                        except TypeError as err:
                            err_str = str(err).lower()
                            if "unexpected keyword" in err_str:
                                popped = False
                                for bad_kw in ("reasoning_effort", "max_tokens", "temperature"):
                                    if bad_kw in create_kwargs and bad_kw in err_str:
                                        create_kwargs.pop(bad_kw, None)
                                        popped = True
                                if popped:
                                    continue
                            raise
                        except Exception as err:
                            err_str = str(err).lower()
                            if ("reasoning_effort" in err_str or "invalid argument" in err_str) and "reasoning_effort" in create_kwargs:
                                create_kwargs.pop("reasoning_effort", None)
                                continue
                            if any(kw in err_str for kw in ("image_url", "image input", "not support image", "unsupported content")):
                                logger.warning("Model %s rejected multimodal payload (%s); retrying with text only", model_name, err_str[:120])
                                adapted_prompt = _adapt_prompt_for_text_model(prompt, status_context)
                                create_kwargs["messages"] = [{"role": "user", "content": adapted_prompt}]
                                continue
                            if any(kw in err_str for kw in ("payload too large", "413", "entity too large", "request too large")):
                                logger.warning("Model %s payload too large for provider limit (%s); falling back to next model", model_name, err_str[:120])
                                break
                            raise
                    text_result = response.choices[0].message.content if (response.choices and len(response.choices) > 0) else None
                    if response.choices and len(response.choices) > 0:
                        choice = response.choices[0]
                        finish_reason = getattr(choice, "finish_reason", "")
                        if finish_reason == "length":
                            logger.warning(
                                "%s model %s hit finish_reason=length (token limit reached, potential truncation)",
                                provider, model_name
                            )

                    # Срезаем размышления ДО проверки на непустоту: раньше проверка
                    # стояла до срезки, и ответ из одних размышлений уходил наружу
                    # пустым, но с признаком успеха — каскад обрывался.
                    text_result = strip_reasoning(text_result)

                    if text_result:
                        # Удача тоже событие учёта, не только отказ: см. note_success.
                        note_success(provider, api_key, model_name=model_name)
                        logger.info(f"{provider.capitalize()} success key={key_id} model={model_name} chars={len(text_result)}")
                        _write_generation_status(
                            status_context, stage=f"{provider}_success",
                            attempt=attempt + 1, max_attempts=max_attempts,
                            key=key_id, result_chars=len(text_result)
                        )
                        _release_generation_status(status_context)
                        _reset_failure()
                        return DummyResponse(text_result)

                    logger.warning(f"{provider.capitalize()} returned empty response attempt={attempt + 1}/{max_attempts} key={key_id}")
                    _write_generation_status(
                        status_context, stage=f"{provider}_empty_response",
                        attempt=attempt + 1, max_attempts=max_attempts, key=key_id
                    )
                    _record_failure("empty_response", f"{model_name}: модель вернула пустой текст")

                except Exception as exc:
                    err_msg = str(exc).lower()
                    logger.warning(f"{provider.capitalize()} failed attempt={attempt + 1}/{max_attempts} key={key_id}: {exc}")

                    is_status_400 = (
                        getattr(exc, "status_code", None) == 400
                        or "400" in err_msg
                        or "bad request" in err_msg
                    )
                    is_fatal_400 = is_status_400 and (
                        "invalid_argument" in err_msg
                        or "invalid argument" in err_msg
                        or "context_length_exceeded" in err_msg
                        or "token count exceeds" in err_msg
                        or "maximum context length" in err_msg
                        or "context_window_exceeded" in err_msg
                    )
                    if is_fatal_400:
                        logger.warning(
                            f"{provider.capitalize()} deterministic 400 Bad Request for {model_name} ({err_msg[:120]}). "
                            "Skipping this model and moving to next in cascade."
                        )
                        _record_failure("model_invalid_request", str(exc), api_key)
                        break

                    if _is_retryable_gemini_error(err_msg):
                        sleep_time = _retry_sleep_seconds(attempt)
                    else:
                        sleep_time = 5

                    _write_generation_status(
                        status_context, stage=f"{provider}_error",
                        attempt=attempt + 1, max_attempts=max_attempts,
                        key=key_id, error=str(exc)[:500]
                    )

                    # Разбор отказа и запись в учёт — в note_key_failure, здесь только
                    # решение, куда идти дальше. Порядок проверок («ключ исчерпан»
                    # ПЕРВЫМ, бан модели вторым) оплачен дефектом и описан там же:
                    # держать его в одном месте — весь смысл выноса, потому что
                    # копия в vision этот порядок уже воспроизводит вручную.
                    failure_reason = note_key_failure(
                        provider, api_key, str(exc), model_name=model_name
                    )
                    if failure_reason == "key_rate_limited":
                        if provider == "gemini" and is_gcp_project_quota_error(err_msg):
                            logger.warning(
                                "GCP project quota exhausted: skipping remaining Gemini keys, moving to fallback provider."
                            )
                            break
                        # Ликвидация 429 stampede: обязательная пауза не менее 2.5-3.0 сек перед переходом к следующему ключу
                        rate_sleep = 2.5
                        if deadline is not None:
                            rate_sleep = min(rate_sleep, max(0.0, deadline - time.monotonic() - MIN_REQUEST_SECONDS))
                        if rate_sleep > 0:
                            logger.info(
                                f"{provider.capitalize()} key rate limited (429); pausing {rate_sleep:.1f}s before trying next key."
                            )
                            _sleep_with_status(rate_sleep, status_context, attempt + 1, max_attempts, key_id)
                        continue

                    if failure_reason in ("model_overloaded", "model_not_found", "content_blocked"):
                        break

                    if failure_reason == "key_denied":
                        logger.info(f"{provider.capitalize()} key denied (403), switching key without sleeping.")
                        continue

                    # Сон нужен только тому, кто ещё будет повторять. На последней
                    # попытке последней модели дальше стоит return None — 8-10 с сна
                    # перед ним врач ждал впустую. Остаток бюджета тоже режем: спать
                    # дольше, чем осталось на сам запрос, бессмысленно.
                    if is_last_model and attempt + 1 >= attempts_planned:
                        logger.info(
                            f"{provider.capitalize()} last chance in cascade failed; "
                            f"skipping the {sleep_time:.1f}s backoff before giving up."
                        )
                        continue
                    if deadline is not None:
                        sleep_time = min(
                            sleep_time,
                            max(0.0, deadline - time.monotonic() - MIN_REQUEST_SECONDS)
                        )
                    if sleep_time > 0:
                        logger.info(f"{provider.capitalize()} retry in {sleep_time:.1f}s")
                        _sleep_with_status(sleep_time, status_context, attempt + 1, max_attempts, key_id)
                    continue

        if out_of_budget:
            break

        if is_media and (media_round + 1 < max_media_rounds):
            remaining = (deadline - time.monotonic()) if deadline else 120.0
            if remaining >= (MIN_REQUEST_SECONDS + 2.0):
                wait_s = min(8.0, max(2.5, min_cooldown_seen if min_cooldown_seen < float('inf') else 3.0))
                if deadline is not None:
                    wait_s = min(wait_s, max(1.0, remaining - MIN_REQUEST_SECONDS))
                logger.info(
                    'Media task: Gemini models unready in round %s/%s; '
                    'pausing %.1fs before next round (remaining budget %.1fs)...',
                    media_round + 1, max_media_rounds, wait_s, remaining
                )
                _sleep_with_status(wait_s, status_context, media_round + 1, max_media_rounds, 'gemini_backoff')
                media_round += 1
                continue
            else:
                logger.warning(
                    'Media task: insufficient budget left (%.1fs < %.1fs) for another round; stopping.',
                    remaining, MIN_REQUEST_SECONDS + 2.0
                )
                break
        else:
            break

    # Причина провала уходит и в журнал, и в файл статуса. Раньше здесь была
    # одна строка «All AI attempts exhausted» на все случаи: по журналу нельзя
    # было отличить «все ключи остывают» (само пройдёт через 5 минут) от
    # «промпт длиннее контекста» (не пройдёт никогда и лечится обрезкой), а
    # вызывающему возвращался просто None.
    failure = LAST_FAILURE or {
        "reason": "no_attempts_made",
        "detail": "каскад не сделал ни одного запроса",
    }
    is_quiet_kind = isinstance(status_context, dict) and status_context.get("kind") in ("batch_reactions", "reaction", "light_check", "spellcheck")
    if is_quiet_kind:
        logger.info(
            "All AI attempts exhausted: reason=%s detail=%s (quiet background task %s)",
            failure["reason"], failure["detail"], status_context.get("kind")
        )
    else:
        logger.error(
            "All AI attempts exhausted: reason=%s detail=%s",
            failure["reason"], failure["detail"]
        )
    _write_generation_status(
        status_context, stage="all_exhausted", max_attempts=max_attempts,
        failure_reason=failure["reason"], error=failure["detail"]
    )
    # Провал тоже завершает работу: оставлять флаг взведённым после исчерпания
    # каскада — значит держать заряженным сторожевой os._exit. Но гасить флаг
    # и одновременно СТИРАТЬ разбор нельзя: оператор, открывший файл статуса
    # после того как бот не ответил, не находил там ни причины, ни ошибки —
    # только «stage: pm_chat_done». Разбор переносим вместе со снятием.
    _release_generation_status(
        status_context,
        reason="all_exhausted",
        extra={"failure_reason": failure["reason"], "error": failure["detail"]},
    )
    return None


# Форматы, которые Whisper-API принимает как есть. Голосовые Telegram — это
# ogg/opus, то есть штатный вход, и перегонять их в PCM незачем.
_WHISPER_NATIVE_EXTENSIONS = frozenset({
    ".ogg", ".mp3", ".m4a", ".mp4", ".mpeg", ".mpga", ".webm", ".wav", ".flac",
})

# Потолок размера файла у провайдера. Значение внешнее (документация Groq), и я
# его не проверял запросом — поэтому оно ниже фактического: лучше сжать лишний
# раз, чем получить 413. Что провайдер режет по размеру, журнал подтверждает:
# в bot.log есть "HTTP/1.1 413 Payload Too Large" от api.groq.com.
_WHISPER_MAX_UPLOAD_BYTES = _env_int("STOMCHAT_WHISPER_MAX_UPLOAD_BYTES", 24 * 1024 * 1024)


# Путь к ffmpeg. Голый "ffmpeg" из PATH — не гарантия: на этой машине первым в
# PATH лежит 108-килобайтный шим (Python/Scripts/ffmpeg.exe), который на
# "-version" отдаёт код 1 и пустой вывод. Без проверки каждый нештатный файл
# платил бы за обречённый subprocess и получал одну обезличенную строку
# "Audio conversion failed" — ни причины, ни того, что дело в отсутствии бинаря.
_FFMPEG_ENV = "ENDOCHAT_FFMPEG_PATH" if os.getenv("ENDOCHAT_FFMPEG_PATH") else "STOMCHAT_FFMPEG_PATH"
_FFMPEG_RESOLVED = []  # пусто — ещё не искали; [None] — рабочего нет; [путь] — есть


def _probe_ffmpeg(path):
    """True, если бинарь действительно запускается и печатает свою версию."""
    import subprocess

    try:
        done = subprocess.run([path, "-version"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30)
    except Exception as exc:
        logger.debug("ffmpeg candidate rejected %s: %s", path, exc)
        return False
    ok = done.returncode == 0 and b"ffmpeg version" in (done.stdout or b"")
    if not ok:
        logger.debug("ffmpeg candidate rejected %s: rc=%s out=%r",
                     path, done.returncode, (done.stdout or b"")[:80])
    return ok


def ffmpeg_binary():
    """Рабочий ffmpeg или None. Ищет и проверяет один раз за процесс."""
    if _FFMPEG_RESOLVED:
        return _FFMPEG_RESOLVED[0]

    import shutil as _shutil

    candidates = []
    override = os.getenv(_FFMPEG_ENV, "").strip().strip('"')
    if override:
        candidates.append(override)
    on_path = _shutil.which("ffmpeg")
    if on_path:
        candidates.append(on_path)
    # Пакеты, которые таскают собственный бинарь: если они стоят, шим из PATH
    # можно обойти без правки окружения.
    for module_name, getter in (("imageio_ffmpeg", "get_ffmpeg_exe"),
                                ("static_ffmpeg", None)):
        try:
            module = __import__(module_name)
            if getter:
                candidates.append(getattr(module, getter)())
            else:
                candidates.append(os.path.join(
                    os.path.dirname(module.__file__), "bin", "ffmpeg.exe"))
        except Exception as exc:
            logger.debug("Module %s ffmpeg import failed: %s", module_name, exc)

    for candidate in candidates:
        if candidate and _probe_ffmpeg(candidate):
            _FFMPEG_RESOLVED.append(candidate)
            logger.info("ffmpeg resolved: %s", candidate)
            return candidate

    _FFMPEG_RESOLVED.append(None)
    logger.warning(
        "ffmpeg не найден или не запускается (проверено кандидатов: %s). "
        "Нештатные контейнеры и файлы больше %s Б расшифрованы НЕ БУДУТ; "
        "штатные ogg/opus/mp3/m4a идут в API как есть и не затронуты. "
        "Путь к рабочему бинарю задаётся переменной %s.",
        len(candidates), _WHISPER_MAX_UPLOAD_BYTES, _FFMPEG_ENV)
    return None


def _ffmpeg_to(file_path, out_path, args):
    """Прогон через ffmpeg с ограничением по времени. None, если не вышло."""
    import subprocess

    binary = ffmpeg_binary()
    if not binary:
        # Спавнить обречённый процесс на каждый файл незачем: причина уже в журнале.
        return None

    cmd = [binary, "-y", "-i", file_path] + args + [out_path]
    try:
        logger.info("Converting audio: %s", " ".join(cmd))
        # timeout обязателен: без него ffmpeg на битом файле висит бесконечно, а
        # родитель убивает дерево процессов по своему дедлайну и оставляет
        # недописанный файл.
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       check=True, timeout=120)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as exc:
        logger.warning("Audio conversion failed: %s", exc)
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except Exception as exc:
            logger.debug("Failed to remove failed audio conversion file %s: %s", out_path, exc)
    return None


def convert_to_wav(file_path):
    """
    Готовит аудио к отправке в Whisper. Имя историческое: WAV теперь крайний случай.

    Раньше ЛЮБОЙ вход безусловно перегонялся в 16 кГц моно PCM. Размер такого
    выхода задан форматом точно: 16 000 отсчётов × 2 байта × 1 канал = 32 000 Б/с,
    то есть 1.92 МБ на минуту речи, сколько бы ни весил вход. Голосовое Telegram
    идёт opus'ом около 31 кб/с — это 3.9 кБ/с, в 8.2 раза меньше. При потолке
    загрузки 24 МБ в лимит упирается диктовка длиной 13 минут, тогда как исходный
    ogg упёрся бы только на 105-й.

    Сценарий отказа: врач диктует подробный разбор случая на 15-20 минут или
    пересылает аудиозапись лекции (приём аудиодокументов размера не проверяет).
    Конвертация делает из 5-7 МБ ogg около 30-40 МБ wav, все ключи получают отказ
    по размеру, расшифровки нет, врачу — тишина. Побочно тот же восьмикратный
    файл лежит в temp_media всё время работы.

    Теперь: штатный для API контейнер отдаём КАК ЕСТЬ. Слишком большой файл
    сжимаем в opus (речь при 24 кб/с моно разборчива и весит на порядок меньше
    PCM), и только если и это не вышло — прежний путь в wav.
    """
    ext = os.path.splitext(file_path)[1].lower()
    try:
        size = os.path.getsize(file_path)
    except OSError:
        size = 0

    if ext in _WHISPER_NATIVE_EXTENSIONS and 0 < size <= _WHISPER_MAX_UPLOAD_BYTES:
        logger.info("Audio kept as-is: ext=%s size=%s (no PCM inflation)", ext, size)
        return file_path

    base = os.path.splitext(file_path)[0]
    if size > _WHISPER_MAX_UPLOAD_BYTES:
        # Сжимаем, а не раздуваем: цель — влезть в потолок провайдера.
        packed = _ffmpeg_to(file_path, base + "_converted.ogg",
                            ["-vn", "-ar", "16000", "-ac", "1", "-c:a", "libopus", "-b:a", "24k"])
        if packed:
            logger.info("Audio recompressed: %s -> %s bytes", size, os.path.getsize(packed))
            return packed

    wav = _ffmpeg_to(file_path, base + "_converted.wav", ["-vn", "-ar", "16000", "-ac", "1"])
    return wav or file_path


def is_audio_silent_or_empty(file_path: str) -> bool:
    """
    Проверяет аудиофайл на тишину/пустоту через ffmpeg volumedetect.
    Возвращает True, если max_volume <= -40.0 dB или -inf, либо если в файле нет аудиопотока.
    При отсутствии ffmpeg, исключениях или ошибках декодирования (включая синтетические
    байты в тестах) возвращает False (безопасный fallback, чтобы не отсечь реальное аудио).
    """
    if not getattr(config, "ENABLE_AUDIO_VOLUMEDETECT", True):
        return False
    if not file_path or not os.path.exists(file_path):
        return False
    try:
        size = os.path.getsize(file_path)
        if size < 32:
            return True
    except OSError:
        return False

    binary = ffmpeg_binary()
    if not binary:
        return False

    import subprocess
    cmd = [binary, "-y", "-i", file_path, "-af", "volumedetect", "-f", "null", "-"]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8.0,
        )
        if proc.returncode != 0:
            return False

        err_out = (proc.stderr or b"").decode("utf-8", errors="ignore")
        if "misdetection possible!" in err_out:
            return False
        if "does not contain any audio stream" in err_out or "Output file is empty" in err_out:
            return True

        m_max = re.search(r'max_volume:\s*([-\d\.]+|-inf)\s*dB', err_out)
        m_mean = re.search(r'mean_volume:\s*([-\d\.]+|-inf)\s*dB', err_out)
        if m_max:
            val_str = m_max.group(1)
            if val_str == "-inf":
                return True
            try:
                max_vol = float(val_str)
                if max_vol <= -40.0:
                    return True
                if m_mean:
                    mean_str = m_mean.group(1)
                    if mean_str == "-inf":
                        return True
                    mean_vol = float(mean_str)
                    if max_vol <= -35.0 and mean_vol <= -45.0:
                        return True
                    if mean_vol <= -50.0:
                        return True
            except ValueError:
                pass
    except Exception as e:
        logger.debug("volumedetect check failed: %s", e)
        return False

    return False


async def transcribe_audio_gemini_multimodal(
    file_path: str,
    duration_secs: float = 0.0,
    is_video_note: bool = False,
) -> tuple:
    """
    Первичный путь STT: Gemini принимает аудио/видео как base64 inlineData и
    возвращает дословную транскрипцию за один мультимодальный вызов.

    Возвращает (text, None) при успехе (text='' означает тишину) или
    (None, error_str) при полном отказе. Вызывающий при (None, ...) переходит
    к Groq Whisper как запасному пути.

    429 -> следующий ключ; иная ошибка HTTP -> следующая модель.
    Модели: от самой лёгкой к более тяжёлой (flash-lite справляется на расшифровке).
    """
    import base64

    keys = list(getattr(config, "GOOGLE_KEYS", []))
    if not keys:
        return None, "no Google keys"

    if not file_path or not os.path.exists(file_path):
        return None, f"file not found: {file_path}"

    try:
        with open(file_path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError as exc:
        return None, f"file read error: {exc}"

    if len(raw_bytes) < 32:
        return None, "file too small"

    b64_audio = base64.b64encode(raw_bytes).decode("utf-8")
    _ext = os.path.splitext(file_path)[1].lower()
    _MIME_MAP = {
        ".mp3": "audio/mp3",
        ".wav": "audio/wav",
        ".m4a": "audio/m4a",
        ".aac": "audio/aac",
        ".webm": "audio/webm",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".mp4": "video/mp4",
    }
    mime_type = "video/mp4" if is_video_note else _MIME_MAP.get(_ext, "audio/ogg")

    dental_prompt = (
        "Ты — медицинский транскрибатор для врача-стоматолога.\n"
        "Расшифруй ДОСЛОВНО всю речь на языке оригинала: русский, английский или смешанный.\n"
        "Клинические термины: зубы, каналы, имплантаты, коронки, эндодонтия, "
        "дентальные препараты, протоколы лечения — пиши точно как произнесено.\n"
        "Если в записи тишина, шум, нечленораздельные звуки или пустота — "
        "пиши только: [тишина]\n"
        "ВЫВЕДИ ТОЛЬКО расшифровку, без комментариев, пояснений и предисловий."
    )

    gemini_timeout = max(120.0, min(480.0, duration_secs * 0.6 + 80.0))
    models_to_try = [
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
    ]
    proxy_url = os.getenv("PROXY_URL") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or None
    proxies_to_try = [None, proxy_url] if proxy_url else [None]

    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": mime_type, "data": b64_audio}},
                {"text": dental_prompt},
            ]
        }],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1024},
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }

    last_error = "all attempts failed"
    for gkey in keys:
        for model_name in models_to_try:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent?key={gkey}"
            )
            for proxy in proxies_to_try:
                try:
                    async with _httpx.AsyncClient(proxy=proxy, verify=False, timeout=gemini_timeout) as hc:
                        resp = await hc.post(url, json=payload)
                        if resp.status_code == 200:
                            gdata = resp.json()
                            parts = (
                                gdata.get("candidates", [{}])[0]
                                .get("content", {})
                                .get("parts", [])
                            )
                            if parts and "text" in parts[0]:
                                raw = parts[0]["text"].strip()
                                if not raw or re.match(
                                    r"^\[?(тишина|silence|тишина\.+)\]?\.?$",
                                    raw.strip().lower(),
                                ):
                                    logger.info(
                                        "Gemini multimodal STT: silence via %s", model_name
                                    )
                                    return "", None
                                logger.info(
                                    "Gemini multimodal STT: success via %s (%d chars)",
                                    model_name, len(raw),
                                )
                                return raw, None
                            last_error = f"{model_name}: empty response"
                        elif resp.status_code == 429:
                            logger.info(
                                "Gemini multimodal STT: 429 key=...%s model=%s. Placing key on %ds cooldown.",
                                gkey[-4:], model_name, KEY_COOLDOWN_SECONDS,
                            )
                            set_key_cooldown("gemini", gkey, KEY_COOLDOWN_SECONDS)
                            last_error = f"429 on {model_name}"
                            # Обязательная пауза >= 2.5с перед следующим ключом (Iron Rule)
                            import asyncio as _asyncio
                            await _asyncio.sleep(2.5)
                            break  # следующий ключ
                        else:
                            last_error = f"{model_name}: HTTP {resp.status_code}"
                            logger.debug(
                                "Gemini multimodal STT HTTP %s model=%s",
                                resp.status_code, model_name,
                            )
                except Exception as exc:
                    last_error = f"{model_name}: {exc}"
                    logger.debug(
                        "Gemini multimodal STT exception model=%s: %s", model_name, exc
                    )
                    continue

    return None, last_error


def transcribe_audio_bytes_or_file(file_path, timeout=None):
    """
    Расшифровка голосового через Groq Whisper с ротацией ключей.

    Бюджет разложен по попыткам, а не взят с потолка на каждую. Раньше каждый
    ключ получал таймаут клиента по умолчанию (30 с), и перебор всех ключей
    складывался в бюджет БОЛЬШЕ внешнего: замер на живом наборе — 7 ключей по
    30 с плюс 6 пауз по 2 с на 429 = 222 секунды внутреннего против 70 секунд
    родительского дедлайна (60 внешних плюс 10 на подъём подпроцесса).

    Сценарий отказа: Groq тормозит или отдаёт 429 по первым двум ключам — для
    квоты whisper это норма при залпе голосовых, — остальные пять свежие. На
    70-й секунде родитель убивает дерево процессов, врач не получает ничего ПРИ
    ПЯТИ НЕИСПОЛЬЗОВАННЫХ КЛЮЧАХ. Та же арифметика для текстового каскада
    сделана давно, в whisper-путь её не перенесли.

    Теперь: на попытку отводится доля общего бюджета, и перебор прекращается по
    ИСТЕЧЕНИЮ остатка, а не по числу ключей. Так до последнего ключа доходит
    любой залп, а не только идеальный.

    ВТОРОЕ: этот путь ходил мимо учёта здоровья ключей, хотя живёт в модуле,
    которому этот учёт принадлежит. Замер: get_key_cooldowns здесь не вызывался,
    set_key_cooldown тоже. Пул ключей общий с текстовым каскадом
    (config.GROQ_KEYS, 7 ключей), то есть общая и квота.

    Обе половины стоили врачу ответа. Не читал: ключ, выбитый в 429 ответом в
    группе минуту назад, пробовался снова и съедал полную долю попытки —
    при трёх остывающих из семи это 21 с из 48 (44% бюджета 60 с) и 3 живые
    попытки вместо 7; при шести — 88% бюджета и НОЛЬ живых попыток. Не писал:
    429, найденный расшифровкой, выбрасывался, и следующий текстовый ответ
    начинал с того же мёртвого ключа.

    Остывающие ключи здесь СДВИГАЮТСЯ В КОНЕЦ ОЧЕРЕДИ, а не выбрасываются, — в
    отличие от текстового каскада, который такую модель пропускает. Причина:
    у whisper нет ни второй модели, ни второго провайдера, а лимиты Groq
    выставлены на модель, поэтому ключ, упёршийся в квоту llama, может ещё иметь
    квоту whisper. Проверить это без сети нельзя, и цена ошибки несимметрична:
    лишняя попытка стоит секунд, а отказ от попытки — расшифровки целиком.
    """
    keys = list(config.GROQ_KEYS)
    if not keys:
        logger.error("No Groq keys found for transcription.")
        return None

    started = time.monotonic()
    # Запас на конвертацию в wav и на выгрузку файла провайдеру: они идут внутри
    # того же родительского дедлайна.
    budget = float(timeout) if timeout else 0.0
    reserve = 12.0
    usable = max(0.0, budget - reserve) if budget else 0.0

    actual_file_path = convert_to_wav(file_path)

    # Pre-flight проверка тишины: если звук пустой или тишина, не тратим квоты Groq Whisper
    if is_audio_silent_or_empty(actual_file_path):
        logger.info("Audio detected as silence/empty via volumedetect: %s", actual_file_path)
        if actual_file_path != file_path and os.path.exists(actual_file_path):
            try:
                os.remove(actual_file_path)
            except Exception as exc:
                logger.debug("Failed to remove silent temp audio %s: %s", actual_file_path, exc)
        return ""

    random.shuffle(keys)
    # Живые ключи вперёд, остывающие в хвост (порядок внутри половин уже случаен).
    # Так бюджет тратится на ключи, у которых есть шанс, и при истечении бюджета
    # непробованными остаются именно остывающие, а не свежие.
    fresh, cooling, cooldown_wait = available_keys("groq", keys)
    if cooling:
        logger.info(
            "Whisper: %s из %s ключей остывают (ближайший через %s с) — пробуем их последними",
            len(cooling), len(keys), cooldown_wait,
        )
    keys = fresh + cooling
    max_attempts = len(keys)
    # Доля на попытку: минимум 7 с, иначе запрос не успеет даже соединиться.
    per_attempt = max(7.0, usable / max_attempts) if usable else 30.0

    for attempt in range(max_attempts):
        if usable and (time.monotonic() - started) >= usable:
            logger.warning(
                "Whisper: бюджет %.0f с исчерпан после %s попыток из %s — остальные ключи не пробуем",
                usable, attempt, max_attempts,
            )
            break
        api_key = keys[attempt]
        key_id = f"groq_whisper...{api_key[-5:]}" if api_key else "groq_none"
        try:
            logger.info(f"Attempting transcription key={key_id} file={actual_file_path}")
            client = get_provider_client("groq", api_key, timeout=per_attempt)
            whisper_model = getattr(config, "GROQ_WHISPER_MODEL", None) or "whisper-large-v3-turbo"
            whisper_prompt = getattr(config, "WHISPER_PROMPT", None) or "Клиническая диктовка врача-стоматолога: зуб, каналы, кариес, пульпит, периодонтит, коффердам, раббердам, гипохлорит натрия, хлоргексидин, ЭДТА, МТА, апекслокатор, эндомотор, слюноотсос, эндочак, депофорез, ProTaper, Reciproc, OptiBond FL, AH Plus, визиограф, КЛКТ, анестезия, ультракаин, перкуссия, холодовая проба."
            create_kwargs = {
                "model": whisper_model,
                "response_format": "text",
            }
            if whisper_prompt:
                create_kwargs["prompt"] = whisper_prompt

            with open(actual_file_path, "rb") as audio_file:
                create_kwargs["file"] = audio_file
                try:
                    transcription = client.audio.transcriptions.create(**create_kwargs)
                except TypeError as te:
                    # Fallback для тестовых моков (FakeTranscriptions), не принимающих prompt
                    if "prompt" in str(te):
                        create_kwargs.pop("prompt", None)
                        transcription = client.audio.transcriptions.create(**create_kwargs)
                    else:
                        raise
            if transcription:
                result_text = transcription.strip()
                # Ключ ответил — снимаем с него пометку и для текстового каскада.
                note_success("groq", api_key)
                logger.info(f"Transcription success chars={len(result_text)}")

                if actual_file_path != file_path and os.path.exists(actual_file_path):
                    try:
                        os.remove(actual_file_path)
                    except Exception as exc:
                        logger.debug("Failed to remove temp audio %s after transcription: %s", actual_file_path, exc)

                return result_text
        except Exception as e:
            logger.warning(f"Whisper transcription failed key={key_id}: {e}")
            # Отказ уходит в ОБЩИЙ учёт: 429, найденный расшифровкой, снимает
            # этот ключ и с текстового каскада — иначе следующий ответ врачу в
            # группе начнёт с ключа, про который здесь уже известно, что он мёртв.
            # model_name не передаём: whisper-large-v3 в этом пути единственный,
            # и бан модели на 20 минут оставил бы врача вообще без расшифровок.
            failure_reason = note_key_failure("groq", api_key, str(e))
            if failure_reason == "key_rate_limited":
                # Пауза строго >= 2.5-3.0 сек согласно Iron Law для защиты от блокировок
                rate_delay = 2.5
                if usable:
                    left = usable - (time.monotonic() - started)
                    rate_delay = max(2.5, min(3.0, left))
                time.sleep(rate_delay)
            continue

    if actual_file_path != file_path and os.path.exists(actual_file_path):
        try:
            os.remove(actual_file_path)
        except Exception as exc:
            logger.debug("Failed to remove temp audio %s after exhaustion: %s", actual_file_path, exc)

    return None


def sanitize_supplement_output(text):
    """Очищает ответ 120b от HTML-документных оберток, мета-тегов и преамбул."""
    if not text:
        return ""
    text = strip_reasoning(text)
    text = re.sub(r"<(head|title|meta|style|script)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<!DOCTYPE.*?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:html|body|head|title|meta)[^>]*>", "", text, flags=re.IGNORECASE)
    text = text.strip()
    text = re.sub(r"(?i)^(?:конечно[,\s]+дополняю|конечно|в дополнение[,\s]+к сказанному|в дополнение|дополняю|дополнение)[^:\n]*:?\s*", "", text).strip()
    return text


def generate_pm_supplement(user_question, initial_answer, timeout=140.0):
    """
    Генерирует лаконичное клиническое дополнение (дельту) к первичному ответу через Groq (openai/gpt-oss-120b)
    с автоматическим фоллбеком на Gemini (gemini-3.5-flash).
    Если первичный ответ полон, возвращает 'NONE'.
    """
    _reset_failure()
    system_prompt = (
        "Ты — опытный коллега-стоматолог, рецензент клинических протоколов.\n"
        "Твоя задача — критически оценить входящий запрос и первичный ответ на вопрос врача и выдать ТОЛЬКО упущенные важные клинические детали (дельту).\n\n"
        "КРИТИЧЕСКИЕ ИНСТРУКЦИИ:\n"
        "1. СТРОГИЙ ФИЛЬТР НЕКЛИНИЧЕСКИХ СООБЩЕНИЙ:\n"
        "   - Если сообщение врача — это приветствие («привет», «здравствуй», «добрый день»), смолл-ток («как дела», «что делаешь»), благодарность («спасибо», «ок»), мета-вопрос о боте («кто ты», «что умеешь») или не содержит конкретного клинического кейса/вопроса — верни СТРОГО ОДНО СЛОВО: NONE\n"
        "2. СТРОГИЙ ФИЛЬТР ПОЛНОТЫ:\n"
        "   - Если первичный ответ уже исчерпывающий, точен и существенных клинических пробелов нет — верни СТРОГО ОДНО СЛОВО: NONE\n"
        "3. КЛИНИЧЕСКАЯ ДЕЛЬТА (когда дополнение РЕАЛЬНО нужно):\n"
        "   - Выдавай дополнение ТОЛЬКО если упущены критические риски осложнений, анатомические ориентиры, точные дозировки или обязательные протокольные этапы (изоляция, экспозиция, выбор цемента/файлов).\n"
        "4. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:\n"
        "   - Педиатрическая токсичность: для детей (<12 лет) артикаин строго не более 5.0 мг/кг (до 4 лет / <15 кг противопоказан). Никогда не указывать 7 мг/кг для ребенка!\n"
        "   - Перфорация дна гайморовой пазухи: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО советовать электрокоагуляцию верхушки/пазухи или адреналиновые тампоны в пазуху! Допустима только бережная коллагеновая губка без проталкивания в синус, герметичное ушивание, антибиотики, сосудосуживающие капли и ЛОР.\n"
        "   - Топографическая подмена: строго следовать зубу и возрасту из запроса врача, не выдумывать другой зуб или возраст.\n"
        "   - Вступать в диалог при смолл-токе, здороваться или объяснять причину отсутствия дополнений.\n"
        "   - Повторять или пересказывать то, что уже сказано в первом ответе.\n"
        "   - Писать вводные фразы («Конечно, дополняю:», «В дополнение к сказанному...»).\n"
        "   - Генерировать обертки <html>, <head>, <body>, <!DOCTYPE>.\n"
        "5. ФОРМАТ: 1–3 емких пункта (список <ul><li>...</li></ul>). Объем: до 600–900 символов. Разметка: только <b> и <ul><li>."
    )
    user_prompt = (
        f"Вопрос врача:\n{user_question}\n\n"
        f"Первичный ответ:\n{initial_answer}\n\n"
        "Выдай клинические дополнения (или строго NONE, если дополнять нечего):"
    )

    groq_primary = getattr(config, "GROQ_MODEL", "qwen/qwen3.8-27b") or "qwen/qwen3.8-27b"
    groq_secondary = "openai/gpt-oss-120b"
    cascade = [
        (groq_primary, "groq"),
    ]
    if provider_pool("agentrouter"):
        cascade.append(("deepseek-v4-flash", "agentrouter"))
    cascade.extend([
        ("gemini-3.5-flash-lite", "gemini"),
        (groq_secondary, "groq"),
        ("gemini-3.8-flash", "gemini"),
    ])

    for model_name, provider in cascade:
        keys = config.GROQ_KEYS if provider == "groq" else config.GOOGLE_KEYS
        live_keys, cooldown_keys, _ = available_keys(provider, keys)
        candidates = live_keys or cooldown_keys
        if not candidates:
            continue

        for api_key in candidates[:2]:
            key_id = f"{provider}...{api_key[-5:]}" if api_key else f"{provider}_none"
            try:
                client = get_provider_client(provider, api_key, timeout=min(25.0, timeout or 25.0))
                supp_kwargs = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.5,
                    "max_tokens": 800 if provider == "groq" else 2048,
                }
                if provider == "gemini" and "lite" not in model_name:
                    supp_kwargs["reasoning_effort"] = "medium"
                try:
                    response = client.chat.completions.create(**supp_kwargs)
                except (TypeError, Exception) as err:
                    if "reasoning_effort" in str(err).lower():
                        supp_kwargs.pop("reasoning_effort", None)
                        response = client.chat.completions.create(**supp_kwargs)
                    else:
                        raise
                if response and response.choices and len(response.choices) > 0:
                    raw = response.choices[0].message.content or ""
                    cleaned = sanitize_supplement_output(raw)
                    if not cleaned:
                        continue
                    note_success(provider, api_key, model_name=model_name)
                    logger.info(f"PM supplement success model={model_name} chars={len(cleaned)}")
                    return DummyResponse(cleaned)
            except Exception as e:
                logger.warning(f"PM supplement failed model={model_name} key={key_id}: {e}")
                supp_fail = note_key_failure(provider, api_key, str(e), model_name=model_name)
                if supp_fail == "key_rate_limited":
                    time.sleep(2.5)
                continue

    return None


async def generate_pm_supplement_async(user_question, initial_answer, timeout=140.0):
    """Асинхронная обертка для вызова генерации дополнения через изолированный подпроцесс."""
    import blocking_tools
    return await blocking_tools.generate_pm_supplement_async(user_question, initial_answer, timeout=timeout)


def generate_google_grounding(prompt_or_query, timeout=160.0):
    """
    Генерация клинического ответа с заземлением на живой веб-поиск через Google Search Grounding.
    Модель: gemini-2.0-flash с tools=[google_search].
    Возвращает (result_dict, error_str).
    """
    keys = list(config.GOOGLE_KEYS)
    if not keys:
        return None, "no_google_keys"

    available, _cooling, cooldown_wait = available_keys("gemini", keys)
    candidates = available or keys
    if not candidates:
        return None, f"all_keys_cooling:{cooldown_wait}"

    try:
        from google import genai
        from google.genai import types
    except ImportError as imp_err:
        logger.warning("google.genai SDK not available: %s", imp_err)
        return None, f"sdk_import_error: {imp_err}"

    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    is_endo = "endo" in getattr(config, "DB_PATH", "").lower() or "endo" in os.path.basename(os.path.dirname(os.path.abspath(__file__))).lower()
    community_name = "EndoChat" if is_endo else "StomChat"
    system_instruction = (
        f"Ты — опытный стоматолог-эксперт сообщества {community_name}. "
        "Твоя задача — дать четкий, доказательный медицинский ответ на основе актуальных "
        "научных публикаций, исследований (PubMed, Cochrane, ADA) и клинических протоколов.\n\n"
        "КРИТИЧЕСКИЕ ПРАВИЛА:\n"
        "- Начинай ответ СРАЗУ с сути и клинических данных. Категорически ЗАПРЕЩЕНО представляться («Как старший клинический эксперт...»).\n"
        "- Никаких технических префиксов и саморекламы.\n\n"
        "ФОРМАТ ОТВЕТА:\n"
        "1. Структурируй ответ четкими клиническими тезисами: суть, доказательная база, тактика/протокол, дозировки/сроки при наличии.\n"
        "2. Выделяй ключевые термины тегом <b>жирный</b> (разметка только HTML).\n"
        "3. После каждого утверждения ставь номер источника [1], [2].\n"
        "4. Пиши профессионально и строго по делу, как коллега коллеге, без пустых вводных слов и без рекламы."
    )

    cfg_model = getattr(config, "GEMINI_MODEL", "gemini-3.8-flash")
    raw_models = [m for m in [cfg_model, "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"] if m]
    seen_models = set()
    models_to_try = [m for m in raw_models if not (m in seen_models or seen_models.add(m))]
    last_err = None

    for model_name in models_to_try:
        if model_name in get_banned_models():
            continue
        for api_key in candidates[:3]:
            key_id = f"gemini...{api_key[-5:]}" if api_key else "none"
            try:
                client = genai.Client(api_key=api_key)
                cfg = types.GenerateContentConfig(
                    tools=[grounding_tool],
                    temperature=0.3,
                    system_instruction=system_instruction,
                )
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt_or_query,
                    config=cfg,
                )
                text = (response.text or "").strip()
                text = strip_reasoning(text)
                if not text:
                    continue

                sources = []
                queries = []
                if response.candidates and response.candidates[0].grounding_metadata:
                    meta = response.candidates[0].grounding_metadata
                    if hasattr(meta, "web_search_queries") and meta.web_search_queries:
                        queries = list(meta.web_search_queries)
                    if hasattr(meta, "grounding_chunks") and meta.grounding_chunks:
                        for chunk in meta.grounding_chunks:
                            if chunk.web and chunk.web.uri:
                                uri = chunk.web.uri
                                title = chunk.web.title or ""
                                domain = chunk.web.domain or ""
                                if not domain:
                                    try:
                                        from urllib.parse import urlsplit
                                        domain = (urlsplit(uri).hostname or "").lower()
                                        if domain.startswith("www."):
                                            domain = domain[4:]
                                    except Exception:
                                        domain = ""
                                sources.append({
                                    "text": title or uri,
                                    "url": uri,
                                    "host": domain,
                                    "title": title,
                                })

                note_success("gemini", api_key, model_name=model_name)
                logger.info(
                    "Google Search Grounding success model=%s key=%s chars=%d sources=%d",
                    model_name, key_id, len(text), len(sources),
                )
                return {
                    "text": text,
                    "sources": sources,
                    "queries": queries,
                }, None

            except Exception as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
                logger.warning(
                    "Google Search Grounding failed model=%s key=%s err=%s",
                    model_name, key_id, last_err,
                )
                g_fail = note_key_failure("gemini", api_key, str(exc), model_name=model_name)
                if g_fail == "key_rate_limited":
                    time.sleep(2.5)

    return None, last_err or "all_grounding_attempts_failed"



