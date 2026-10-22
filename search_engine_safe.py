import logging
import re

from blocking_tools import web_search_async


logger = logging.getLogger(__name__)


async def perform_search(query: str, max_results: int = 2) -> str:
    clean_query = re.sub(r"[^\w\sа-яА-ЯёЁ]", " ", query).strip()

    try:
        results, error = await web_search_async(clean_query, max_results, timeout=45)
        if not results:
            short_query = " ".join(clean_query.split()[:4])
            if short_query != clean_query:
                results, error = await web_search_async(short_query, max_results, timeout=45)

        if error:
            logger.warning("search subprocess failed: %s", error)
        if not results:
            return "Информации не найдено."

        formatted = []
        for r in results:
            if isinstance(r, dict):
                txt = (r.get("text") or "").strip()
                url = (r.get("url") or "").strip()
                if txt and url:
                    formatted.append(f"🔹 {txt} \n(Источник: {url})")
                elif txt:
                    formatted.append(f"🔹 {txt}")
            elif isinstance(r, str) and r.strip():
                formatted.append(r.strip())

        return "\n\n".join(formatted) if formatted else "Информации не найдено."
    except Exception as exc:
        logger.error("search subprocess wrapper failed: %s", exc)
        return "Ошибка поиска."
