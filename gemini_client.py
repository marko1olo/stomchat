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
from openai import OpenAI

logger = logging.getLogger(__name__)

class DummyResponse:
    def __init__(self, text):
        self.text = text

def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
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
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "<think>" in cleaned:
        # Тег открыт и не закрыт: всё от него до конца — незавершённый черновик.
        cleaned = cleaned.split("<think>", 1)[0]
    if "</think>" in cleaned:
        # Закрывающий без открывающего: начало ответа срезано, годен только хвост.
        cleaned = cleaned.split("</think>", 1)[1]
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

def get_openai_client(api_key, base_url, timeout=30.0):
    return OpenAI(
        api_key=api_key if api_key else "dummy_key",
        base_url=base_url,
        timeout=timeout,
        max_retries=0
    )
BANNED_MODELS_FILE = "banned_models.json"
KEY_COOLDOWN_FILE = "key_cooldowns.json"
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
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning(f"Failed to save {path}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


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
MODEL_BAN_SECONDS = 1200  # 20 минут


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
}


PROVIDER_KEY_ATTRS = {"gemini": "GOOGLE_KEYS", "groq": "GROQ_KEYS"}


def provider_pool(provider):
    """Все настроенные ключи провайдера. Нужен, чтобы считать остаток живых."""
    value = getattr(config, PROVIDER_KEY_ATTRS.get(provider, ""), None)
    if isinstance(value, (list, tuple)):
        return [k for k in value if k]
    return []


def get_provider_client(provider, api_key, timeout=30.0):
    """Клиент к провайдеру по его имени. Единственная точка создания клиента."""
    base_url = PROVIDER_BASE_URLS.get(provider)
    if not base_url:
        raise ValueError(f"Неизвестный провайдер модели: {provider!r}")
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

    Если забанены все — возвращаем последнюю: попытка по забаненной модели
    дешевле, чем гарантированное молчание бота.
    """
    now = time.time()
    banned = get_banned_models()
    active = []
    for entry in cascade:
        model_name = entry[0] if isinstance(entry, (tuple, list)) else entry
        ban_until = banned.get(model_name, 0)
        if ban_until > now:
            logger.info(
                f"Model {model_name} is temporarily banned due to 503/504 for "
                f"another {int(ban_until - now)}s. Skipping."
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
    if model_name and _clear_expiry_entry(BANNED_MODELS_FILE, model_name):
        logger.info("Model %s answered while banned; ban lifted.", model_name)


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
    if _RATE_LIMIT_RE.search(err_msg):
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

    if _SERVER_ERROR_RE.search(err_msg) or "deadline" in err_msg or "unavailable" in err_msg:
        if model_name:
            ban_model(model_name, MODEL_BAN_SECONDS)
            logger.info(
                f"{provider.capitalize()} server overloaded/unavailable ({err_msg}). "
                f"Banning model {model_name} for {MODEL_BAN_SECONDS // 60} minutes. Skipping in cascade."
            )
        else:
            logger.info(
                f"{provider.capitalize()} server overloaded/unavailable ({err_msg}). "
                f"Модель не банится: замены у этого пути нет, бан = отказ врачу целиком."
            )
        _record_failure("model_overloaded", error_text, api_key)
        return "model_overloaded"

    if "403" in err_msg or "permission" in err_msg:
        set_key_cooldown(provider, api_key, 31536000) # 365 days
        logger.warning(f"{provider.capitalize()} key permanently denied (403). Banned for 365 days.")
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
})
# Всё остальное — daily, weekly, group_summary и любой незнакомый вид — идёт в
# тяжёлый каскад: там качество важнее задержки, и бюджет там 2100 с.

# Ниже этого одна попытка бессмысленна: запрос рвётся на генерации. Значение
# унаследовано от прежнего max(7.0, ...) — нижнюю границу автор уже выбрал.
MIN_REQUEST_SECONDS = 7.0
# Дробить долю модели на несколько попыток по разным ключам стоит только если
# каждой достанется хотя бы столько: иначе ротация ключей превращается в серию
# запросов, убитых по таймауту на середине ответа.
COMFORT_REQUEST_SECONDS = 20.0
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
    for attr in ("GOOGLE_KEYS", "GROQ_KEYS", "OPENROUTER_KEYS"):
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
    thinking_level = status_context.get("thinking_level", "MEDIUM") if status_context else "MEDIUM"

    groq_fallback = "openai/gpt-oss-120b" if thinking_level == "HIGH" else config.GROQ_MODEL

    # Таймаут одного запроса считается ниже, когда известен каскад: он зависит от
    # числа попыток, а число попыток — от числа моделей и живых ключей. Здесь
    # стояло req_timeout = timeout/3, и это была ошибка арифметики, а не оценки;
    # разбор — у расчёта бюджета после сборки каскада.
    req_timeout = 30.0

    # ROUTING BY TASKS: списки видов — TRIAGE_KINDS / CHAT_KINDS выше.
    is_chatbot = kind in CHAT_KINDS

    if is_triage:
        models_cascade = [
            ("llama-3.3-70b-versatile", "groq"),
            ("gemini-3.5-flash-lite", "gemini"),
            ("gemini-3.1-flash-lite", "gemini")
        ]
    elif is_chatbot:
        models_cascade = [
            ("gemini-3.5-flash-lite", "gemini"),
            ("gemini-3.1-flash-lite", "gemini"),
            ("qwen/qwen3.6-27b", "groq"),
            (groq_fallback, "groq")
        ]
    else:
        # Complex tasks (Summaries, analytics, etc)
        models_cascade = [
            (config.GEMINI_MODEL, "gemini"), # gemini-3.6-flash
            ("gemini-3.5-flash", "gemini"),
            ("gemini-3.5-flash-lite", "gemini"),
            (groq_fallback, "groq")
        ]

    # Отсев забаненных за 503/504 — через общий учёт (active_models), а не своей
    # копией цикла: вторая копия проверки бана уже начинала расходиться с первой.
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
        usable = max(min(budget, MIN_REQUEST_SECONDS), budget * BUDGET_RESERVE_SHARE)
        models_fit = max(1, min(len(active_cascade), int(usable // MIN_REQUEST_SECONDS)))
        if models_fit < len(active_cascade):
            logger.info(
                "Budget %.0fs fits %s of %s cascade models; dropping the tail (it was never reached anyway).",
                budget, models_fit, len(active_cascade)
            )
            active_cascade = active_cascade[:models_fit]
        model_share = usable / len(active_cascade)
        max_attempts = max(1, min(max_attempts, int(model_share // COMFORT_REQUEST_SECONDS)))
        req_timeout = model_share / max_attempts
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
    for model_index, (model_name, provider) in enumerate(active_cascade):
        keys = list(config.GOOGLE_KEYS if provider == "gemini" else config.GROQ_KEYS)
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
        is_last_model = model_index == len(active_cascade) - 1
        for attempt in range(attempts_planned):
            api_key = available[attempt]
            key_id = f"{provider}...{api_key[-5:]}" if api_key else f"{provider}_none"

            # Первый запрос делаем всегда: бюджет уже нарезан так, чтобы он в
            # него влез, а ноль запросов — это гарантированное молчание бота.
            if deadline is not None and requests_made:
                remaining = deadline - time.monotonic()
                if remaining < MIN_REQUEST_SECONDS:
                    # Запрос, который не успеет закончиться до убийства процесса,
                    # начинать нечего: его ответ никто не прочитает.
                    logger.warning(
                        "Budget spent (%.1fs left) before %s; stopping cascade.",
                        max(0.0, remaining), model_name
                    )
                    _record_failure(
                        "budget_exhausted",
                        f"бюджет {float(timeout):.0f}с израсходован до модели {model_name}"
                    )
                    out_of_budget = True
                    break
                # Последнему запросу отдаём ровно остаток бюджета, а не полную
                # долю: сумма таймаутов запросов не должна вылезать за timeout.
                req_timeout = min(req_timeout, remaining)

            try:
                client = client_maker(api_key)
                _write_generation_status(
                    status_context, stage=f"{provider}_request",
                    attempt=attempt + 1, max_attempts=max_attempts,
                    key=key_id, model=model_name
                )
                logger.info(f"{provider.capitalize()} request attempt={attempt + 1}/{max_attempts} key={key_id} model={model_name}")

                requests_made += 1
                # Using OpenAI SDK for BOTH Groq and Gemini now
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.95
                )
                text_result = response.choices[0].message.content if (response.choices and len(response.choices) > 0) else None

                # Срезаем размышления ДО проверки на непустоту: раньше проверка
                # стояла до срезки, и ответ из одних размышлений уходил наружу
                # пустым, но с признаком успеха — каскад обрывался.
                text_result = strip_reasoning(text_result)

                if text_result:
                    # Удача тоже событие учёта, не только отказ: см. note_success.
                    note_success(provider, api_key, model_name=model_name)
                    logger.info(f"{provider.capitalize()} success key={key_id} chars={len(text_result)}")
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
                    # Не спим: следующий ключ свежий, врач ждёт.
                    continue

                if failure_reason == "model_overloaded":
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

    # Причина провала уходит и в журнал, и в файл статуса. Раньше здесь была
    # одна строка «All AI attempts exhausted» на все случаи: по журналу нельзя
    # было отличить «все ключи остывают» (само пройдёт через 5 минут) от
    # «промпт длиннее контекста» (не пройдёт никогда и лечится обрезкой), а
    # вызывающему возвращался просто None.
    failure = LAST_FAILURE or {
        "reason": "no_attempts_made",
        "detail": "каскад не сделал ни одного запроса",
    }
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
    ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".mp4", ".mpeg", ".mpga", ".webm", ".wav", ".flac",
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
_FFMPEG_ENV = "STOMCHAT_FFMPEG_PATH"
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
        except Exception:
            pass

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
        except Exception:
            pass
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
                            ["-ar", "16000", "-ac", "1", "-c:a", "libopus", "-b:a", "24k"])
        if packed:
            logger.info("Audio recompressed: %s -> %s bytes", size, os.path.getsize(packed))
            return packed

    wav = _ffmpeg_to(file_path, base + "_converted.wav", ["-ar", "16000", "-ac", "1"])
    return wav or file_path


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
            with open(actual_file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model="whisper-large-v3",
                    file=audio_file,
                    response_format="text"
                )
            if transcription:
                result_text = transcription.strip()
                # Ключ ответил — снимаем с него пометку и для текстового каскада.
                note_success("groq", api_key)
                logger.info(f"Transcription success chars={len(result_text)}")
                
                if actual_file_path != file_path and os.path.exists(actual_file_path):
                    try: os.remove(actual_file_path)
                    except Exception: pass
                    
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
                # Пауза не длиннее остатка бюджета: иначе она съедает время,
                # которого хватило бы на следующий, свежий ключ.
                left = (usable - (time.monotonic() - started)) if usable else 2.0
                time.sleep(max(0.0, min(2.0, left)))
            continue
            
    if actual_file_path != file_path and os.path.exists(actual_file_path):
        try: os.remove(actual_file_path)
        except Exception: pass
        
    return None
