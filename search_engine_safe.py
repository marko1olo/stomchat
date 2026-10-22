import ipaddress
import logging
import re
from urllib.parse import urlsplit

from blocking_tools import web_search_async


logger = logging.getLogger(__name__)

_PROMPT_INJECTION_RE = re.compile(
    r"(?i)\b(?:"
    r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"disregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"system\s*instruction\s*:|"
    r"system\s*prompt\s*:|"
    r"you\s+are\s+now\s+(?:an?\s+)?(?:unrestricted|evil|dan|new)|"
    r"override\s+(?:all\s+)?(?:system|previous)\s+rules|"
    r"забудь\s+(?:все\s+)?(?:предыдущие\s+)?инструкции|"
    r"отмени\s+(?:все\s+)?(?:предыдущие\s+)?правила|"
    r"ты\s+больше\s+не\s+(?:бот|стоматолог|ассистент)|"
    r"ты\s+теперь\s+(?:нерегулируемый|свободный)"
    r")\b",
    re.UNICODE,
)

_SPECIAL_TOKENS_RE = re.compile(
    r"(?:<\|im_start\|>|<\|im_end\|>|<\|system\|>|\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>|\[SYSTEM\]|\[/SYSTEM\]|<\/?system>|<\/?instructions?>)",
    re.IGNORECASE,
)

_HTML_BLOCKS_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>", re.UNICODE)
_HTML_TAGS_RE = re.compile(r"<[a-zA-Z/][^>]*>", re.UNICODE)


def _is_ssrf_host(host: str) -> bool:
    if not host:
        return False
    host_clean = host.strip().lower()
    if host_clean.startswith("[") and host_clean.endswith("]"):
        host_clean = host_clean[1:-1]
    elif ":" in host_clean:
        parts = host_clean.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            host_clean = parts[0]
    if host_clean in ("localhost", "0.0.0.0", "127.0.0.1", "::1", "::"):
        return True
    if any(host_clean.endswith("." + bad) for bad in ("local", "internal", "lan", "corp", "home.arpa", "localdomain")):
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        return False


def _sanitize_text(text: str) -> str:
    if not text:
        return ""
    text = _HTML_BLOCKS_RE.sub(" ", text)
    text = _SPECIAL_TOKENS_RE.sub(" ", text)
    text = _HTML_TAGS_RE.sub(" ", text)
    text = _PROMPT_INJECTION_RE.sub(" ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


async def perform_search(query: str, max_results: int = 2) -> str:
    # Очистка запроса с сохранением дозировок, дробей, процентов и дефисов
    clean_query = re.sub(r"[^\w\s./%+-]", " ", query).strip()
    clean_query = re.sub(r"\s+", " ", clean_query)

    try:
        results, error = await web_search_async(clean_query, max_results, timeout=45)
        if not results:
            short_query = " ".join(clean_query.split()[:4])
            if short_query != clean_query:
                results, error = await web_search_async(short_query, max_results, timeout=45)
            else:
                try:
                    import web_lookup
                    sci_query = web_lookup.build_scientific_query(clean_query)
                    if sci_query and sci_query != clean_query:
                        results, error = await web_search_async(sci_query, max_results, timeout=45)
                except Exception as sci_err:
                    logger.debug("search_engine_safe scientific fallback failed: %s", sci_err)

        if error:
            logger.warning("search subprocess failed: %s", error)
        if not results:
            return "Информации не найдено."

        formatted = []
        for r in results:
            if isinstance(r, dict):
                txt = _sanitize_text((r.get("text") or "").strip())
                url = (r.get("url") or "").strip()
                host = (urlsplit(url).hostname or "").lower() if url else ""
                if url and _is_ssrf_host(host):
                    logger.warning("search_engine_safe SSRF dropped host=%s", host)
                    continue
                if txt and url:
                    formatted.append(f"🔹 {txt} \n(Источник: {url})")
                elif txt:
                    formatted.append(f"🔹 {txt}")
            elif isinstance(r, str) and r.strip():
                clean_r = _sanitize_text(r.strip())
                if clean_r:
                    formatted.append(clean_r)

        return "\n\n".join(formatted) if formatted else "Информации не найдено."
    except Exception as exc:
        logger.error("search subprocess wrapper failed: %s", exc)
        return "Ошибка поиска."

