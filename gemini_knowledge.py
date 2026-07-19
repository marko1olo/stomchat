from google import genai
from google.genai import types
import config
import random
import logging
import time

logger = logging.getLogger(__name__)

def generate_fact_json(prompt):
    """
    Генерирует JSON через Gemma (без JSON-mode) или Gemini (с JSON-mode).
    Добавлена пауза при 429 для обхода лимитов.
    """
    if not config.GOOGLE_KEYS:
        logger.error("No Google API keys found.")
        return None

    keys = list(config.GOOGLE_KEYS)
    random.shuffle(keys)

    models_to_try = [
        "models/gemma-3-27b-it"
    ]

    for model_id in models_to_try:
        for api_key in keys:
            try:
                client = genai.Client(api_key=api_key)
                
                # ДИНАМИЧЕСКИЙ КОНФИГ
                # Gemma не поддерживает response_mime_type, Gemini - поддерживает.
                is_gemma = "gemma" in model_id
                
                config_params = {
                    "temperature": 0.0,
                    "max_output_tokens": 8192,
                    "safety_settings": [
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE")
                    ]
                }
                
                if not is_gemma:
                    config_params["response_mime_type"] = "application/json"
                
                response = client.models.generate_content(
                    model=model_id,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_params)
                )

                if response and response.text:
                    return response.text

            except Exception as e:
                err = str(e).lower()
                
                if "429" in err or "resource_exhausted" in err:
                    # ВАЖНО: Делаем паузу, чтобы не долбить API
                    logger.warning(f"Rate limit for {model_id}. Sleeping 10s...")
                    time.sleep(10)
                    continue
                
                if "400" in err and "json mode" in err:
                    # Это страховка, если логика is_gemma не сработала
                    continue

                if "404" not in err:
                    logger.error(f"Error {model_id}: {err[:100]}")
                continue
                
    return None