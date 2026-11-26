import logging
import re
from urllib.parse import urlsplit

from blocking_tools import web_search_async
from web_lookup import _is_ssrf_host, sanitize_excerpt as _sanitize_text


logger = logging.getLogger(__name__)


async def perform_search(query: str, max_results: int = 2) -> str:
    # Очистка запроса с сохранением дозировок, дробей, процентов и дефисов
    clean_query = re.sub(r"[^\w\s./%+-]", " ", query).strip()
    clean_query = re.sub(r"\s+", " ", clean_query)

    try:
        results, error = await web_search_async(clean_query, max_results, timeout=45)
        short_query = " ".join(clean_query.split()[:4])
        if not results and short_query != clean_query:
            results, error = await web_search_async(short_query, max_results, timeout=45)
        if not results:
            try:
                import web_lookup
                sci_query = web_lookup.build_scientific_query(clean_query)
                if sci_query and sci_query not in (clean_query, short_query):
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

