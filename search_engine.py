import config
import logging
import asyncio
import re
import ipaddress
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Безопасный импорт DDGS
try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

# Попытка импорта Tavily
try:
    from tavily import TavilyClient
    tavily = TavilyClient(api_key=config.TAVILY_API_KEY) if getattr(config, "TAVILY_API_KEY", None) else None
except ImportError:
    tavily = None

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


def _run_ddg_sync(query, max_results=3):
    """Синхронная функция поиска через DuckDuckGo с таймаутом и закрытием контекста."""
    if DDGS is None:
        logger.warning("DDGS package not installed or unavailable")
        return []
    try:
        results = []
        with DDGS(timeout=20) as ddgs:
            try:
                ddg_gen = ddgs.text(query, region='ru-ru', max_results=max_results, backend="api")
            except (TypeError, KeyError, Exception):
                ddg_gen = ddgs.text(query, region='ru-ru', max_results=max_results, backend="auto")
            for r in ddg_gen:
                if r and 'body' in r and 'href' in r:
                    body = _sanitize_text(r['body'])
                    href = r['href'].strip()
                    host = (urlsplit(href).hostname or "").lower()
                    if _is_ssrf_host(host):
                        logger.warning("DDGS SSRF blocked host=%s", host)
                        continue
                    if body:
                        results.append(f"🔹 {body} \n(Источник: {href})")
        return results
    except Exception as e:
        # Логируем ошибку (429/Ratelimit/Timeout/сеть), но не крашим бота
        logger.warning("DDGS search failed: %s", e)
        return []


async def perform_search(query: str, max_results: int = 2) -> str:
    """
    Выполняет поиск. 
    1. Tavily (если есть ключ).
    2. DuckDuckGo (бесплатно, через поток с таймаутом).
    """
    # Очистка запроса с сохранением дозировок, дробей, процентов и дефисов
    clean_query = re.sub(r"[^\w\s./%+-]", " ", query).strip()
    clean_query = re.sub(r"\s+", " ", clean_query)

    try:
        # 1. TAVILY (Приоритет)
        if getattr(config, "SEARCH_PROVIDER", "") == "tavily" and tavily:
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        tavily.search, query=clean_query, search_depth="basic", max_results=max_results
                    ),
                    timeout=25.0,
                )
                results = []
                for r in response.get('results', []):
                    c = _sanitize_text(r.get('content', ''))
                    u = r.get('url', '').strip()
                    h = (urlsplit(u).hostname or "").lower()
                    if _is_ssrf_host(h):
                        continue
                    if c and u:
                        results.append(f"🔹 {c} ({u})")
                return "\n\n".join(results) if results else "Информации не найдено."
            except (asyncio.TimeoutError, Exception) as tav_err:
                logger.warning("Tavily search failed or timed out: %s — falling back to DDGS", tav_err)

        # 2. DUCKDUCKGO (Fallback)
        loop = asyncio.get_running_loop()
        # Запускаем в экзекьюторе под жестким таймаутом, чтобы не блокировать бота
        try:
            results = await asyncio.wait_for(
                loop.run_in_executor(None, _run_ddg_sync, clean_query, max_results),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning("DDGS primary search timed out (30s)")
            results = []

        if not results:
            # Вторая попытка: пробуем искать только первые 4 слова запроса (часто помогает)
            short_query = " ".join(clean_query.split()[:4])
            if short_query != clean_query:
                try:
                    results = await asyncio.wait_for(
                        loop.run_in_executor(None, _run_ddg_sync, short_query, max_results),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("DDGS fallback short search timed out (30s)")
                    results = []
            else:
                try:
                    import web_lookup
                    sci_query = web_lookup.build_scientific_query(clean_query)
                    if sci_query and sci_query != clean_query:
                        results = await asyncio.wait_for(
                            loop.run_in_executor(None, _run_ddg_sync, sci_query, max_results),
                            timeout=30.0,
                        )
                except (asyncio.TimeoutError, Exception) as sci_err:
                    logger.warning("DDGS fallback scientific search failed: %s", sci_err)
                    results = []

        return "\n\n".join(results) if results else "Информации не найдено (поисковик не вернул данных)."

    except Exception as e:
        logger.error(f"❌ Ошибка механизма поиска: {e}")
        return "Ошибка поиска."

