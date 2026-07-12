import config
import os
import random
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

def _is_retryable_gemini_error(error_text):
    retry_markers = (
        "429", "500", "502", "503", "504",
        "deadline", "timeout", "timed out", "temporarily",
        "unavailable", "rate", "quota", "failed_precondition",
        "connection", "transport"
    )
    return any(marker in error_text for marker in retry_markers)

def _write_generation_status(context, **updates):
    if not context: return
    payload = dict(context)
    payload.update(updates)
    payload["active"] = True
    runtime_guard.write_summary_status(payload)

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
import json

BANNED_MODELS_FILE = "banned_models.json"

def get_banned_models():
    if not os.path.exists(BANNED_MODELS_FILE):
        return {}
    try:
        with open(BANNED_MODELS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def ban_model(model_name, duration_seconds):
    models = get_banned_models()
    models[model_name] = time.time() + duration_seconds
    try:
        with open(BANNED_MODELS_FILE, "w") as f:
            json.dump(models, f)
    except Exception as e:
        logger.warning(f"Failed to save banned models: {e}")
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
    
    if is_triage:
        models_cascade = [
            ("llama-3.3-70b-versatile", "groq"),
            ("qwen/qwen3.6-27b", "groq"),
            ("gemini-3.1-flash-lite", "gemini")
        ]
    elif is_pm:
        models_cascade = [
            ("gemini-3.1-flash-lite", "gemini"),
            ("gemini-3-flash-preview", "gemini"),
            (groq_fallback, "groq"),
            ("qwen/qwen3.6-27b", "groq")
        ]
    else:
        models_cascade = [
            (config.GEMINI_MODEL, "gemini"),
            ("gemini-3-flash-preview", "gemini"),
            ("gemini-3.1-flash-lite", "gemini"),
            (groq_fallback, "groq"),
            ("qwen/qwen3.6-27b", "groq")
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
            from google import genai
            from google.genai import types
            keys = list(config.GOOGLE_KEYS)
            ms_timeout = int(req_timeout * 1000)
            client_maker = lambda k: genai.Client(api_key=k, http_options=types.HttpOptions(timeout=ms_timeout))
        else:
            keys = list(config.GROQ_KEYS)
            client_maker = lambda k: get_openai_client(k, "https://api.groq.com/openai/v1", timeout=req_timeout)
            
        if not keys:
            logger.warning(f"No API keys for {provider}. Skipping {model_name}.")
            continue
            
        random.shuffle(keys)
        
        for attempt in range(max_attempts):
            api_key = keys[attempt % len(keys)]
            key_id = f"{provider}...{api_key[-5:]}" if api_key else f"{provider}_none"
            
            try:
                client = client_maker(api_key)
                _write_generation_status(
                    status_context, stage=f"{provider}_request",
                    attempt=attempt + 1, max_attempts=max_attempts,
                    key=key_id, model=model_name
                )
                logger.info(f"{provider.capitalize()} request attempt={attempt + 1}/{max_attempts} key={key_id} model={model_name}")

                if provider == "gemini":
                    from google.genai import types
                    thinking_config = None
                    is_gemini_3 = any(v in model_name for v in ["gemini-3", "gemini-3.1", "gemini-3.5", "gemini-omni"])
                    if is_gemini_3:
                        lvl = status_context.get("thinking_level") if status_context else None
                        if not lvl:
                            lvl = os.getenv("STOMCHAT_GEMINI_THINKING_LEVEL", "HIGH")
                        lvl = lvl.upper()
                        if lvl in ["MINIMAL", "LOW", "MEDIUM", "HIGH"]:
                            thinking_config = types.ThinkingConfig(thinking_level=lvl)
                    else:
                        bgt_str = os.getenv("STOMCHAT_GEMINI_THINKING_BUDGET", "1024")
                        try:
                            bgt = int(bgt_str)
                            thinking_config = types.ThinkingConfig(thinking_budget=bgt)
                        except ValueError:
                            pass
                    
                    gen_config = types.GenerateContentConfig(thinking_config=thinking_config) if thinking_config else None
                    response = client.models.generate_content_stream(
                        model=model_name,
                        contents=prompt,
                        config=gen_config
                    )
                    text_result_parts = []
                    for chunk in response:
                        if chunk.text:
                            text_result_parts.append(chunk.text)
                    text_result = "".join(text_result_parts)
                else:
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
                
                if "503" in err_msg or "504" in err_msg or "deadline" in err_msg or "unavailable" in err_msg or "500" in err_msg:
                    ban_duration = 1200  # 20 минут в секундах
                    ban_model(model_name, ban_duration)
                    logger.info(f"{provider.capitalize()} server overloaded/unavailable ({err_msg}). Banning model {model_name} for 20 minutes. Skipping in cascade.")
                    break

                if "429" in err_msg or "rate limit" in err_msg or "quota" in err_msg:
                    logger.info(f"{provider.capitalize()} rate limited, waiting 2.5s cooldown before next attempt...")
                    time.sleep(2.5)
                    continue
                    
                if "403" in err_msg or "permission" in err_msg:
                    logger.info(f"{provider.capitalize()} key denied (403), switching key without sleeping.")
                    continue
                    
                logger.info(f"{provider.capitalize()} retry in {sleep_time:.1f}s")
                _sleep_with_status(sleep_time, status_context, attempt + 1, max_attempts, key_id)
                continue

    logger.error("All AI attempts exhausted. Summary was not generated.")
    _write_generation_status(status_context, stage="all_exhausted", max_attempts=max_attempts)
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
