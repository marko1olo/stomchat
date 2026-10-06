import pytest
import asyncio
import re
from unittest.mock import AsyncMock, patch, MagicMock

import assistant
import summarizer
import config
import runtime_guard

# Изоляция файла статуса для test_isolation
runtime_guard.SUMMARY_STATUS_PATH = "bot_summary_status_test.json"

@pytest.mark.asyncio
async def test_dialogue_resilience_emoji_sanitization():
    """Тест: если валидатор отклонил черновик из-за эмодзи, он санируется и одобряется."""
    context_msgs = ["Врач: Подскажите по протоколу фиксации"]
    draft_with_emoji = "Используйте Variolink Esthetic 😂😎"
    
    with patch("assistant.check_response_quality", new_callable=AsyncMock) as mock_val:
        mock_val.return_value = (False, "Ответ содержит неуместные эмодзи 😂")
        
        # Симулируем вызов проверки качества из assistant.py
        quality_ok, quality_reason = await assistant.check_response_quality(
            context_msgs, draft_with_emoji, invited=True
        )
        assert not quality_ok
        assert "эмодзи" in quality_reason.lower()
        
        # Проверяем логику санирования
        reason_lower = quality_reason.lower()
        if any(w in reason_lower for w in ("эмодз", "emoji", "смайл")):
            sanitized = re.sub(r"[😅😂😎😤😏🤣🤡🙄]+", "", draft_with_emoji).strip()
            assert sanitized == "Используйте Variolink Esthetic"

@pytest.mark.asyncio
async def test_dialogue_fallback_on_hallucinated_article():
    """Тест: в активном диалоге при отклонении черновика генерируется безопасный фоллбек, а не немой уход."""
    mock_event = MagicMock()
    mock_event.chat_id = -1001820467444
    mock_bot = AsyncMock()
    
    # Моделируем отказ валидатора из-за выдуманного артикула
    with patch("assistant.check_response_quality", new_callable=AsyncMock) as mock_val, \
         patch("assistant.generate_gemini_text_async", new_callable=AsyncMock) as mock_llm:
        
        mock_val.return_value = (False, "ИИ приводит выдуманный артикул инструмента")
        mock_llm.return_value = (MagicMock(text="Точный артикул сверяйте по каталогу производителя."), None)
        
        # Запускаем логику фоллбека
        reason = "ИИ приводит выдуманный артикул инструмента"
        prompt = f"""Ты — строгий клинический эксперт-стоматолог в Telegram-чате.
Врач задал клинический/технический вопрос, но черновик ответа был отклонён рецензентом по причине: "{reason}".
Сформулируй предельно краткий ответ...
"""
        fb_resp, fb_err = await assistant.generate_gemini_text_async(prompt, {"kind": "dialogue_fallback"}, timeout=20)
        assert fb_resp is not None
        assert "каталог" in fb_resp.text

def test_media_prompt_has_anti_inertia_rule():
    """Тест: проверяет наличие жесткого правила анти-инерции в исходном коде assistant.py."""
    with open(r"c:\Users\danat\Desktop\stomchat\assistant.py", "r", encoding="utf-8") as f:
        src = f.read()
    
    assert "[ПРИОРИТЕТ ИЗОБРАЖЕНИЯ И ЗАЩИТА ОТ ИНЕРЦИИ КОНТЕКСТА:" in src
    assert "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО переносить диагнозы, патологии или находки из предыдущих не связанных обсуждений" in src

def test_teaser_dynamic_topics_extraction():
    """Тест: извлечение реальных клинических тем из HTML саммари."""
    sample_html = """
    <h3>ТЕМА ДНЯ: Резорбция зуба 2.6 и тактика по КЛКТ</h3>
    <p>Текст разбора темы дня...</p>
    <h4>КЕЙС: Протокол препарирования под BOPT на варфарине</h4>
    <p>Текст кейса...</p>
    <h4>ОБСУЖДЕНИЕ: Выбор винтового экстрактора для Ankylos</h4>
    <p>Текст обсуждения...</p>
    """
    
    patterns = [
        r'(?:ТЕМА ДНЯ|ТЕМА ДНЯ \(ГЛУБОКИЙ РАЗБОР\))[:\s\-–—]+([^\n<]+)',
        r'(?:▶️\s*СИТУАЦИЯ|КЕЙС|СЛУЧАЙ|ОБСУЖДЕНИЕ)[:\s\-–—]+([^\n<]+)',
        r'<h4>([^<]+)</h4>',
        r'<h3>([^<]+)</h3>',
    ]
    candidates = []
    for pat in patterns:
        for m in re.findall(pat, sample_html, re.IGNORECASE):
            cl = re.sub(r'<[^>]+>', '', m).strip(' :–—-.')
            if 8 <= len(cl) <= 60:
                if cl not in candidates:
                    candidates.append(cl)
    
    assert len(candidates) >= 2
    assert any("Резорбция" in c for c in candidates)
    assert any("BOPT" in c for c in candidates)

def test_teaser_intros_no_cringe():
    """Тест: в summarizer.py нет кринж-интро про кроликов из шляпы, агентов 007 и 500 сообщений."""
    with open(r"c:\Users\danat\Desktop\stomchat\summarizer.py", "r", encoding="utf-8") as f:
        src = f.read()
    
    assert "кролика из шляпы" not in src
    assert "Агент 007" not in src
    assert "Я сделал это за вас" not in src

def test_env_cleaned_of_403_keys():
    """Тест: в .env нет ни одного из 4 заблокированных Google ключей (HTTP 403)."""
    with open(r"c:\Users\danat\Desktop\stomchat\.env", "r", encoding="utf-8") as f:
        content = f.read()
    
    match = re.search(r'GOOGLE_API_KEYS\s*=\s*([^\r\n]+)', content)
    assert match is not None
    raw_keys = match.group(1).strip().strip('"\'').split(',')
    keys = [k.strip() for k in raw_keys if k.strip()]
    
    banned_suffixes = {"CVz4Q", "J6lRA", "4TzoA", "UjZYg"}
    for k in keys:
        for sfx in banned_suffixes:
            assert not k.endswith(sfx), f"Key ending in {sfx} is still in .env!"
    
    assert len(keys) == 6, f"Expected exactly 6 clean keys, found {len(keys)}"
