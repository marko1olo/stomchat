import config
import os
import random
import logging
import time
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
    base = _env_int("STOMCHAT_GEMINI_RETRY_BASE_SECONDS", 10)
    cap = _env_int("STOMCHAT_GEMINI_RETRY_MAX_SECONDS", 60)
    jitter = random.uniform(0, min(5, base))
    return min(cap, base * (2 ** min(attempt, 4)) + jitter)

def _is_retryable_gemini_error(error_text):
    retry_markers = (
        "403", "429", "500", "502", "503", "504",
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

def get_openai_client(api_key, base_url, timeout=60.0):
    return OpenAI(
        api_key=api_key if api_key else "dummy_key",
        base_url=base_url,
        timeout=timeout,
        max_retries=0
    )

def generate_text(prompt, status_context=None):
    """Generate summary text through Gemini with Groq fallback."""
    models_cascade = [
        (config.GEMINI_MODEL, "gemini"),
        (config.GROQ_MODEL, "groq")
    ]
    
    max_attempts = max(1, _env_int("STOMCHAT_GEMINI_MAX_ATTEMPTS", 6))
    
    for model_name, provider in models_cascade:
        if provider == "gemini":
            keys = list(config.GOOGLE_KEYS)
            client_maker = lambda k: get_openai_client(k, "https://generativelanguage.googleapis.com/v1beta/openai/")
        else:
            keys = list(config.GROQ_KEYS)
            client_maker = lambda k: get_openai_client(k, "https://api.groq.com/openai/v1")
            
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

                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=3000,
                    temperature=0.95
                )

                if response.choices and len(response.choices) > 0:
                    text_result = response.choices[0].message.content
                    if text_result:
                        text_result = text_result.strip()
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
                
                if "429" in err_msg or "rate limit" in err_msg or "quota" in err_msg:
                    logger.info(f"{provider.capitalize()} rate limited, switching key without sleeping.")
                    continue
                    
                logger.info(f"{provider.capitalize()} retry in {sleep_time:.1f}s")
                _sleep_with_status(sleep_time, status_context, attempt + 1, max_attempts, key_id)
                continue

    logger.error("All AI attempts exhausted. Summary was not generated.")
    _write_generation_status(status_context, stage="all_exhausted", max_attempts=max_attempts)
    return None
