import config
import logging
import asyncio
import re
from urllib.parse import urlsplit
from web_lookup import _is_ssrf_host, sanitize_excerpt as _sanitize_text

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
        if not results:
            try:
                import web_lookup
                sci_query = web_lookup.build_scientific_query(clean_query)
                if sci_query and sci_query not in (clean_query, short_query):
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

