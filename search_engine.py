import config
import logging
import asyncio
from ddgs import DDGS 
import re

logger = logging.getLogger(__name__)

# Попытка импорта Tavily
try:
    from tavily import TavilyClient
    tavily = TavilyClient(api_key=config.TAVILY_API_KEY) if config.TAVILY_API_KEY else None
except ImportError:
    tavily = None

def _run_ddg_sync(query, max_results=3):
    """Синхронная функция поиска через DuckDuckGo."""
    try:
        results = []
        # Используем менеджер контекста, чтобы не оставлять сессии висеть
        with DDGS() as ddgs:
            # timeout=20 чтобы не висело вечно
            ddg_gen = ddgs.text(query, region='ru-ru', max_results=max_results, backend="api")
            for r in ddg_gen:
                if r and 'body' in r and 'href' in r:
                    results.append(f"🔹 {r['body']} \n(Источник: {r['href']})")
        return results
    except Exception as e:
        # Логируем ошибку, но не крашим бота
        logger.warning("DDGS search failed: %s", e)
        return []

async def perform_search(query: str, max_results: int = 2) -> str:
    """
    Выполняет поиск. 
    1. Tavily (если есть ключ).
    2. DuckDuckGo (бесплатно, через поток).
    """
    # Очистка запроса от мусора (вопросительные знаки, кавычки)
    clean_query = re.sub(r'[^\w\sа-яА-ЯёЁ]', ' ', query).strip()
    
    try:
        # 1. TAVILY (Приоритет)
        if config.SEARCH_PROVIDER == "tavily" and tavily:
            response = await asyncio.to_thread(
                tavily.search, query=clean_query, search_depth="basic", max_results=max_results
            )
            results = [f"🔹 {r['content']} ({r['url']})" for r in response.get('results', [])]
            return "\n\n".join(results) if results else "Информации не найдено."

        # 2. DUCKDUCKGO (Fallback)
        else:
            loop = asyncio.get_running_loop()
            # Запускаем в экзекьюторе, чтобы не блокировать бота
            results = await loop.run_in_executor(None, _run_ddg_sync, clean_query, max_results)
            
            if not results:
                # Вторая попытка: пробуем искать только первые 4 слова запроса (часто помогает)
                short_query = " ".join(clean_query.split()[:4])
                if short_query != clean_query:
                    results = await loop.run_in_executor(None, _run_ddg_sync, short_query, max_results)

            return "\n\n".join(results) if results else "Информации не найдено (поисковик не вернул данных)."
                
    except Exception as e:
        logger.error(f"❌ Ошибка механизма поиска: {e}")
        return "Ошибка поиска."
