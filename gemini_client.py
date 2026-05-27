from google import genai
import config
import os
import random
import logging
import time
import runtime_guard

logger = logging.getLogger(__name__)


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _retry_sleep_seconds(attempt):
    base = _env_int("STOMCHAT_GEMINI_RETRY_BASE_SECONDS", 30)
    cap = _env_int("STOMCHAT_GEMINI_RETRY_MAX_SECONDS", 180)
    jitter = random.uniform(0, min(5, base))
    return min(cap, base * (2 ** min(attempt, 4)) + jitter)


def _is_retryable_gemini_error(error_text):
    retry_markers = (
        "403",
        "429",
        "500",
        "502",
        "503",
        "504",
        "deadline",
        "timeout",
        "timed out",
        "temporarily",
        "unavailable",
        "rate",
        "quota",
        "failed_precondition",
        "location is not supported",
        "connection",
        "transport",
    )
    return any(marker in error_text for marker in retry_markers)


def _write_generation_status(context, **updates):
    if not context:
        return
    payload = dict(context)
    payload.update(updates)
    payload["active"] = True
    runtime_guard.write_summary_status(payload)


def _sleep_with_status(seconds, context, attempt, max_attempts, key_id):
    end_time = time.monotonic() + seconds
    while True:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            return
        _write_generation_status(
            context,
            stage="gemini_retry_sleep",
            attempt=attempt,
            max_attempts=max_attempts,
            key=key_id,
            retry_sleep_remaining_seconds=round(remaining, 1),
        )
        time.sleep(min(15, remaining))


def generate_text(prompt, status_context=None):
    """Generate summary text through Gemini only."""
    if not config.GOOGLE_KEYS:
        logger.error("No Google API keys configured. Summary generation is Gemini-only; fallback is disabled.")
        _write_generation_status(status_context, stage="gemini_no_keys")
        return None

    keys = list(config.GOOGLE_KEYS)
    random.shuffle(keys)
    max_attempts = max(1, _env_int("STOMCHAT_GEMINI_MAX_ATTEMPTS", 12))

    for attempt in range(max_attempts):
        api_key = keys[attempt % len(keys)]
        key_id = f"...{api_key[-5:]}"

        try:
            client = genai.Client(api_key=api_key)
            _write_generation_status(
                status_context,
                stage="gemini_request",
                attempt=attempt + 1,
                max_attempts=max_attempts,
                key=key_id,
                model=config.GEMINI_MODEL,
            )
            logger.info("Gemini request attempt=%s/%s key=%s model=%s", attempt + 1, max_attempts, key_id, config.GEMINI_MODEL)

            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config={'temperature': 0.95}
            )

            if response and response.text:
                logger.info("Gemini success key=%s chars=%s", key_id, len(response.text))
                _write_generation_status(
                    status_context,
                    stage="gemini_success",
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    key=key_id,
                    result_chars=len(response.text),
                )
                return response

            logger.warning("Gemini returned empty response attempt=%s/%s key=%s", attempt + 1, max_attempts, key_id)
            _write_generation_status(
                status_context,
                stage="gemini_empty_response",
                attempt=attempt + 1,
                max_attempts=max_attempts,
                key=key_id,
            )

        except Exception as exc:
            err_msg = str(exc).lower()
            logger.warning("Gemini failed attempt=%s/%s key=%s: %s", attempt + 1, max_attempts, key_id, exc)
            if _is_retryable_gemini_error(err_msg):
                sleep_time = _retry_sleep_seconds(attempt)
            else:
                sleep_time = 5
            _write_generation_status(
                status_context,
                stage="gemini_error",
                attempt=attempt + 1,
                max_attempts=max_attempts,
                key=key_id,
                error=str(exc)[:500],
            )
            logger.info("Gemini-only retry in %.1fs; fallback is disabled.", sleep_time)
            _sleep_with_status(sleep_time, status_context, attempt + 1, max_attempts, key_id)
            continue

    logger.error("Gemini attempts exhausted. Fallback is disabled; summary was not generated.")
    _write_generation_status(status_context, stage="gemini_exhausted", max_attempts=max_attempts)
    return None
