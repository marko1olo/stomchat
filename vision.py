import base64
import logging
import random
import io
import asyncio
import time
import httpx
from PIL import Image
from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)
GROQ_COOLDOWN_UNTIL = 0

def prepare_image_for_groq(image_bytes):
    """
    Жесткий ресайз картинки, чтобы влезть в лимиты Groq + Base64.
    """
    try:
        Image.MAX_IMAGE_PIXELS = 49_000_000 
        if not image_bytes: return None, "Пустые байты"
        
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.load()
            if img.mode != 'RGB': img = img.convert('RGB')
        except Exception as e:
            return None, f"Невалидный файл изображения: {e}"

        # Лимит Groq жесток. Ужимаем до 800px по большей стороне.
        # Этого достаточно для диагностики, но экономит трафик.
        MAX_SIZE = 1000 
        if max(img.size) > MAX_SIZE:
            img.thumbnail((MAX_SIZE, MAX_SIZE), Image.Resampling.LANCZOS)
        
        buffer = io.BytesIO()
        # Quality=70 - золотая середина для веса/качества
        img.save(buffer, format="JPEG", quality=70, optimize=True)
        return buffer.getvalue(), None

    except Exception as e:
        return None, f"Ошибка CPU обработки: {e}"

async def describe_image(file_path: str, caption: str = None) -> str:
    """Анализирует изображение через Llama Vision на Groq с учетом контекста подписи."""
    global GROQ_COOLDOWN_UNTIL
    
    if time.time() < GROQ_COOLDOWN_UNTIL:
        return None 

    try:
        with open(file_path, 'rb') as f:
            image_bytes = f.read()
        
        loop = asyncio.get_running_loop()
        resized_bytes, error = await loop.run_in_executor(None, prepare_image_for_groq, image_bytes)
        
        if error:
            logger.error(f"Ошибка подготовки фото: {error}")
            return None

        b64_image = base64.b64encode(resized_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{b64_image}"
        
        # ПОДГОТОВКА ПРОМПТА С КОНТЕКСТОМ
        # Если есть подпись, даем её нейронке, чтобы она знала, на что смотреть
        context = f" Context from the author: '{caption}'." if caption else ""
        system_prompt = (
            f"This is a dental image from a professional chat.{context} "
            f"Describe what you see in Russian (pathology, clinical step, or materials). If there is any text, analyze it (make conclusion). If picture is not medical, describe it briefly."
            f"Be professional. (Write up to 4-6 sentences). "
        )

        keys = config.GROQ_KEYS.copy()
        random.shuffle(keys)
        
        for api_key in keys:
            try:
                async with httpx.AsyncClient(verify=False, timeout=30.0) as http_client:
                    client = AsyncOpenAI(
                        api_key=api_key, 
                        base_url="https://api.groq.com/openai/v1", 
                        http_client=http_client
                    )
                    
                    resp = await client.chat.completions.create(
                        model=config.GROQ_VISION_MODEL,
                        messages=[
                            {
                                "role": "user", 
                                "content": [
                                    {"type": "text", "text": system_prompt}, 
                                    {"type": "image_url", "image_url": {"url": image_url}}
                                ]
                            }
                        ],
                        max_tokens=256
                    )
                    
                    content = resp.choices[0].message.content
                    if content:
                        return content.strip()

            except Exception as e:
                err_str = str(e).lower()
                if "413" in err_str: return None 
                if "429" in err_str: continue
        
        GROQ_COOLDOWN_UNTIL = time.time() + 60 
        return None

    except Exception as e:
        logger.error(f"Ошибка в модуле Vision: {e}")
        return None