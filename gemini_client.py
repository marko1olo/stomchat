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


def _release_generation_status(context):
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
        runtime_guard.release_generation_status(context.get("kind"))
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
def generate_text(prompt, status_context=None, timeout=None):
    """Generate summary text through Gemini with Groq fallback."""
    kind = status_context.get("kind") if status_context else None
    is_pm = kind in ("pm_chat", "assistant_media_pm")
    is_triage = kind == "llama_triage"
    thinking_level = status_context.get("thinking_level", "MEDIUM") if status_context else "MEDIUM"
    
    groq_fallback = "openai/gpt-oss-120b" if thinking_level == "HIGH" else config.GROQ_MODEL
    
    # Calculate per-request timeout dynamically
    req_timeout = 30.0
    if timeout:
        req_timeout = max(7.0, float(timeout) / 3.0)
    
    # ROUTING BY TASKS:
    is_chatbot = kind in ("assistant", "assistant_media", "assistant_media_pm", "pm_chat", "llama_triage", "term_explainer", "quiz", "direct_ask")
    
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

    # Filter out models that are currently banned due to 503/504
    now = time.time()
    banned_models = get_banned_models()
    active_cascade = []
    for m_name, prov in models_cascade:
        ban_until = banned_models.get(m_name, 0)
        if ban_until > now:
            logger.info(f"Model {m_name} is temporarily banned due to 503/504 for another {int(ban_until - now)}s. Skipping.")
            continue
        active_cascade.append((m_name, prov))
        
    # If all models in the cascade are banned, fall back to the last one
    if not active_cascade:
        logger.warning("All models in cascade are banned. Forcing fallback to the last model.")
        active_cascade = [models_cascade[-1]]
    max_attempts = _env_int("STOMCHAT_GEMINI_MAX_ATTEMPTS", 3)
    
    for model_name, provider in active_cascade:
        if provider == "gemini":
            keys = list(config.GOOGLE_KEYS)
            client_maker = lambda k: get_openai_client(k, "https://generativelanguage.googleapis.com/v1beta/openai/", timeout=req_timeout)
        else:
            keys = list(config.GROQ_KEYS)
            client_maker = lambda k: get_openai_client(k, "https://api.groq.com/openai/v1", timeout=req_timeout)
            
        if not keys:
            logger.warning(f"No API keys for {provider}. Skipping {model_name}.")
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
        cooldowns = get_key_cooldowns()
        now_ts = time.time()
        available = [k for k in keys if cooldowns.get(_key_fingerprint(provider, k), 0) <= now_ts]
        if not available:
            soonest = min(cooldowns.get(_key_fingerprint(provider, k), 0) for k in keys)
            logger.info(
                "All %s keys are on cooldown (%ss left); skipping %s in cascade.",
                provider, max(0, int(soonest - now_ts)), model_name
            )
            continue
        if len(available) < len(keys):
            logger.info(
                "%s: %s of %s keys available, rest on cooldown.",
                provider, len(available), len(keys)
            )

        # Бюджет попыток — это число РЕАЛЬНЫХ запросов, каждый на своём ключе.
        for attempt in range(min(max_attempts, len(available))):
            api_key = available[attempt]
            key_id = f"{provider}...{api_key[-5:]}" if api_key else f"{provider}_none"

            try:
                client = client_maker(api_key)
                _write_generation_status(
                    status_context, stage=f"{provider}_request",
                    attempt=attempt + 1, max_attempts=max_attempts,
                    key=key_id, model=model_name
                )
                logger.info(f"{provider.capitalize()} request attempt={attempt + 1}/{max_attempts} key={key_id} model={model_name}")

                # Using OpenAI SDK for BOTH Groq and Gemini now
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.95
                )
                text_result = response.choices[0].message.content if (response.choices and len(response.choices) > 0) else None

                if text_result:
                    import re
                    text_result = re.sub(r"<think>.*?</think>", "", text_result, flags=re.DOTALL).strip()
                    logger.info(f"{provider.capitalize()} success key={key_id} chars={len(text_result)}")
                    _write_generation_status(
                        status_context, stage=f"{provider}_success",
                        attempt=attempt + 1, max_attempts=max_attempts,
                        key=key_id, result_chars=len(text_result)
                    )
                    _release_generation_status(status_context)
                    return DummyResponse(text_result)

                logger.warning(f"{provider.capitalize()} returned empty response attempt={attempt + 1}/{max_attempts} key={key_id}")
                _write_generation_status(
                    status_context, stage=f"{provider}_empty_response",
                    attempt=attempt + 1, max_attempts=max_attempts, key=key_id
                )

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
                
                if _SERVER_ERROR_RE.search(err_msg) or "deadline" in err_msg or "unavailable" in err_msg:
                    ban_duration = 1200  # 20 минут в секундах
                    ban_model(model_name, ban_duration)
                    logger.info(f"{provider.capitalize()} server overloaded/unavailable ({err_msg}). Banning model {model_name} for 20 minutes. Skipping in cascade.")
                    break

                if _RATE_LIMIT_RE.search(err_msg):
                    logger.info(
                        f"{provider.capitalize()} rate limited (429/quota). Placing key {key_id} "
                        f"on {KEY_COOLDOWN_SECONDS}s cooldown."
                    )
                    set_key_cooldown(provider, api_key)
                    # Do not sleep, immediately skip to the next key or model
                    continue
                    
                if "403" in err_msg or "permission" in err_msg:
                    logger.info(f"{provider.capitalize()} key denied (403), switching key without sleeping.")
                    continue
                    
                logger.info(f"{provider.capitalize()} retry in {sleep_time:.1f}s")
                _sleep_with_status(sleep_time, status_context, attempt + 1, max_attempts, key_id)
                continue

    logger.error("All AI attempts exhausted. Summary was not generated.")
    _write_generation_status(status_context, stage="all_exhausted", max_attempts=max_attempts)
    # Провал тоже завершает работу: оставлять флаг взведённым после исчерпания
    # каскада — значит держать заряженным сторожевой os._exit.
    _release_generation_status(status_context)
    return None


def convert_to_wav(file_path):
    """Convert any audio file to standard 16kHz mono WAV using ffmpeg."""
    import subprocess
    base, ext = os.path.splitext(file_path)
    wav_path = base + "_converted.wav"
    try:
        cmd = ["ffmpeg", "-y", "-i", file_path, "-ar", "16000", "-ac", "1", wav_path]
        logger.info(f"Converting audio using ffmpeg: {' '.join(cmd)}")
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if os.path.exists(wav_path):
            return wav_path
    except Exception as e:
        logger.warning(f"Audio conversion failed via ffmpeg: {e}")
        if os.path.exists(wav_path):
            try: os.remove(wav_path)
            except Exception: pass
    return file_path


def transcribe_audio_bytes_or_file(file_path):
    """Transcribe audio using Groq Whisper API (whisper-large-v3) with key rotation."""
    keys = list(config.GROQ_KEYS)
    if not keys:
        logger.error("No Groq keys found for transcription.")
        return None

    actual_file_path = convert_to_wav(file_path)
    
    random.shuffle(keys)
    max_attempts = len(keys)

    for attempt in range(max_attempts):
        api_key = keys[attempt]
        key_id = f"groq_whisper...{api_key[-5:]}" if api_key else "groq_none"
        try:
            logger.info(f"Attempting transcription key={key_id} file={actual_file_path}")
            client = get_openai_client(api_key, "https://api.groq.com/openai/v1")
            with open(actual_file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model="whisper-large-v3",
                    file=audio_file,
                    response_format="text"
                )
            if transcription:
                result_text = transcription.strip()
                logger.info(f"Transcription success chars={len(result_text)}")
                
                if actual_file_path != file_path and os.path.exists(actual_file_path):
                    try: os.remove(actual_file_path)
                    except Exception: pass
                    
                return result_text
        except Exception as e:
            logger.warning(f"Whisper transcription failed key={key_id}: {e}")
            if "429" in str(e).lower() or "rate limit" in str(e).lower():
                time.sleep(2)
            continue
            
    if actual_file_path != file_path and os.path.exists(actual_file_path):
        try: os.remove(actual_file_path)
        except Exception: pass
        
    return None
