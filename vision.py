import base64
import logging
import random
import asyncio
import time
import os
import httpx
from openai import AsyncOpenAI

import config
import gemini_client
from media_tools import prepare_image_for_analysis

# Подготовка картинки живёт в media_tools и запускается подпроцессом.
#
# Здесь лежала inline-копия с комментарием «media_tools не существует» — модуль
# существует, и его версия всё это время была мёртвым кодом, который никто не
# вызывал. Копия отличалась по существу: 1024px и quality=85 против 1000px,
# quality=70, optimize=True — на реальном снимке это 74 КБ против 45 КБ, а после
# base64 разница ещё вырастает на треть. При альбоме в десяток снимков запрос
# подходил к лимиту провайдера, и обработчик 413 ниже просто отказывался от
# анализа целиком.
#
# Кроме размера, версия в media_tools выставляет Image.MAX_IMAGE_PIXELS (защита
# от decompression bomb) и реально соблюдает timeout: у inline-копии параметр
# timeout принимался и игнорировался, так что распаковка «бомбы» вешала поток
# исполнителя без ограничения по времени. Падение подпроцесса при этом не
# задевает бота.

logger = logging.getLogger(__name__)
_VISION_SEMAPHORE = None


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_flag(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


VISION_CONCURRENCY = max(1, _env_int("STOMCHAT_VISION_CONCURRENCY", 1))
GROQ_HTTP_TIMEOUT_SECONDS = max(5, _env_int("STOMCHAT_GROQ_HTTP_TIMEOUT_SECONDS", 30))
VISION_IMAGE_PREP_TIMEOUT_SECONDS = max(5, _env_int("STOMCHAT_VISION_IMAGE_PREP_TIMEOUT_SECONDS", 45))
VISION_MIN_CALL_INTERVAL_SECONDS = 3.0

# Проверка TLS-сертификата. Здесь стояло жёсткое verify=False, то есть API-ключ
# уходил провайдеру по соединению, подлинность которого не проверялась: любой,
# кто способен вклиниться в трафик, получал ключ в заголовке Authorization.
# По умолчанию теперь проверяем. Если в вашей сети стоит перехватывающий прокси
# с собственным корневым сертификатом и зрение начнёт падать с SSL-ошибкой —
# STOMCHAT_VISION_TLS_VERIFY=0 возвращает прежнее поведение осознанно.
VISION_TLS_VERIFY = _env_flag("STOMCHAT_VISION_TLS_VERIFY", True)


# Какая доля букв должна быть кириллицей, чтобы считать описание русским.
# 0.3, а не больше: в нормальном русском описании хватает латиницы — «e.max»,
# «BOPT», «CAD/CAM», названия материалов.
VISION_MIN_CYRILLIC_RATIO = 0.3


# Доля букв вне кириллицы и латиницы, после которой текст считается мусором.
# Модель зрения изредка выдаёт кашу из случайных токенов на разных письменностях
# — в базе есть «=`ື່ອ picojax expandingື່ອ associative romatРанее MAL». Такая
# строка проходила порог по кириллице за счёт нескольких русских слов внутри.
VISION_MAX_FOREIGN_SCRIPT_RATIO = 0.1


def _is_mostly_cyrillic(text):
    letters = [ch for ch in (text or "") if ch.isalpha()]
    if not letters:
        return False

    cyrillic = sum(1 for ch in letters if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    latin = sum(1 for ch in letters if "a" <= ch.lower() <= "z")
    foreign = len(letters) - cyrillic - latin
    if foreign / len(letters) > VISION_MAX_FOREIGN_SCRIPT_RATIO:
        return False

    return cyrillic / len(letters) >= VISION_MIN_CYRILLIC_RATIO


def _get_vision_semaphore():
    global _VISION_SEMAPHORE
    if _VISION_SEMAPHORE is None:
        _VISION_SEMAPHORE = asyncio.Semaphore(VISION_CONCURRENCY)
    return _VISION_SEMAPHORE


_LAST_VISION_CALL_TIME = 0.0
_VISION_PACE_LOCK = None


def _get_pace_lock():
    global _VISION_PACE_LOCK
    if _VISION_PACE_LOCK is None:
        _VISION_PACE_LOCK = asyncio.Lock()
    return _VISION_PACE_LOCK


async def _pace_vision_calls():
    """
    Трёхсекундный интервал между запросами.

    Раньше это был голый глобал: при STOMCHAT_VISION_CONCURRENCY > 1 все
    сопрограммы читали одно и то же устаревшее значение, решали, что ждать не
    нужно, и уходили в провайдера одновременно — ровно то, против чего интервал
    и ставили. Метку времени ставим на входе, под замком.
    """
    global _LAST_VISION_CALL_TIME
    async with _get_pace_lock():
        wait = VISION_MIN_CALL_INTERVAL_SECONDS - (time.time() - _LAST_VISION_CALL_TIME)
        if wait > 0:
            await asyncio.sleep(wait)
        _LAST_VISION_CALL_TIME = time.time()


async def describe_image(file_paths, caption: str = None, is_passive: bool = False) -> str:
    """Анализирует изображение(я) через каскад Vision (Gemini 3.5 -> Qwen 3.6 -> Llama 4 Scout)."""
    if isinstance(file_paths, str):
        file_paths = [file_paths]

    async with _get_vision_semaphore():
        try:
            # Английское описание — запасной вариант: если ни одна модель
            # каскада не ответит по-русски, отдадим его, а не пустоту.
            english_fallback = None
            image_urls = []
            for fp in file_paths:
                resized_bytes, error = await prepare_image_for_analysis(
                    fp,
                    timeout=VISION_IMAGE_PREP_TIMEOUT_SECONDS,
                )
                if error:
                    logger.warning("Vision image prep failed path=%s: %s", fp, error)
                if not error and resized_bytes:
                    image_urls.append(f"data:image/jpeg;base64,{base64.b64encode(resized_bytes).decode('utf-8')}")

            if not image_urls:
                logger.error("Ошибка подготовки фото: ни одно фото не удалось обработать.")
                return None

            context = f" Контекст от автора: '{caption}'." if caption else ""
            # Описание снимка уходит в отвечающий промпт и становится основанием
            # клинического комментария. Прежняя формулировка прямо приглашала
            # называть патологию, не ограничивая домысливание: модель зрения
            # уверенно «видит» на рентгене очаг, которого там нет, и дальше по
            # этой выдумке строится совет коллеге. Отсюда требования ниже —
            # отделять увиденное от истолкованного и признавать пределы снимка.
            system_prompt = (
                f"Это стоматологическое изображение из профессионального врачебного чата.{context} "
                f"Опиши на русском языке, что НА САМОМ ДЕЛЕ видно: анатомия, этап лечения, материалы, инструменты, видимые изменения. "
                f"Если на картинке есть текст — разбери его смысл. Если картинка не медицинская (мем, котик, бытовая), опиши её кратко. "
                f"ПРАВИЛА ДОСТОВЕРНОСТИ (важнее краткости): "
                f"1) Отделяй наблюдение от истолкования: сначала что видно, затем «похоже на…», «нельзя исключить…». "
                f"2) НЕ ставь диагноз по одному снимку и не утверждай патологию, признаки которой не различимы. "
                f"3) Если ракурс, резкость, засветка или обрезка не дают оценить область — скажи об этом прямо, а не достраивай. "
                f"4) Не называй числовых величин (миллиметры убыли кости, длина канала, размер дефекта), если они не читаются на снимке или в подписи. "
                f"5) Не выдумывай отсутствующие детали ради полноты описания: «не видно» — полноценный ответ. "
                f"Будь профессионалом. (Напиши 3-5 предложений). "
                f"ОТВЕЧАЙ СТРОГО НА РУССКОМ ЯЗЫКЕ. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать на английском языке, выводить черновики, шаги размышления (Reasoning/Thinking) или теги <think>."
            )
            
            # 33% / 33% / 33% load balancing pool between Gemini 3.5 Flash Lite, Gemini 3.1 Flash Lite, Qwen 3.6 27B
            models_pool = [
                ("gemini-3.5-flash-lite", "gemini"),
                ("gemini-3.1-flash-lite", "gemini"),
                ("qwen/qwen3.6-27b", "groq")
            ]
            start_idx = random.randint(0, 2)
            models_cascade = models_pool[start_idx:] + models_pool[:start_idx]

            timeout = httpx.Timeout(
                GROQ_HTTP_TIMEOUT_SECONDS,
                connect=min(10.0, GROQ_HTTP_TIMEOUT_SECONDS),
                read=GROQ_HTTP_TIMEOUT_SECONDS,
                write=min(15.0, GROQ_HTTP_TIMEOUT_SECONDS),
                pool=5.0,
            )

            # Кулдауны ключей общие с текстовым каскадом (gemini_client): пул
            # ключей один и тот же, и ключ, выбитый в 429 генерацией текста, не
            # должен тут же получать запрос от зрения. Раньше vision про
            # кулдауны не знал вовсе и продолжал бить в исчерпанные ключи,
            # отсчитывая по 2.5 секунды на каждый отказ.
            cooldowns = gemini_client.get_key_cooldowns()

            async with httpx.AsyncClient(verify=VISION_TLS_VERIFY, trust_env=False, timeout=timeout) as http_client:
                for model_name, provider in models_cascade:
                    if provider == "gemini":
                        raw_keys = os.getenv("GOOGLE_API_KEYS", "") or os.getenv("GOOGLE_KEYS", "") or getattr(config, "GOOGLE_KEYS", [])
                        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
                    else:
                        raw_keys = os.getenv("GROQ_API_KEYS", "") or os.getenv("GROQ_KEYS", "") or getattr(config, "GROQ_KEYS", [])
                        base_url = "https://api.groq.com/openai/v1"

                    if isinstance(raw_keys, list):
                        keys = [k for k in raw_keys if k]
                    else:
                        keys = [k.strip() for k in str(raw_keys).split(",") if k.strip()]
                        
                    if not keys:
                        continue

                    random.shuffle(keys)

                    now_ts = time.time()
                    available = [
                        k for k in keys
                        if cooldowns.get(gemini_client._key_fingerprint(provider, k), 0) <= now_ts
                    ]
                    if not available:
                        logger.info(
                            "Vision: all %s keys are on cooldown; skipping %s.", provider, model_name
                        )
                        continue

                    for api_key in available:
                        try:
                            await _pace_vision_calls()

                            client = AsyncOpenAI(
                                api_key=api_key,
                                base_url=base_url,
                                http_client=http_client,
                                max_retries=0,
                                timeout=GROQ_HTTP_TIMEOUT_SECONDS,
                            )
                            content_arr = [{"type": "text", "text": system_prompt}]
                            for iu in image_urls:
                                content_arr.append({"type": "image_url", "image_url": {"url": iu}})
                            
                            resp = await client.chat.completions.create(
                                model=model_name,
                                messages=[
                                    {
                                        "role": "user",
                                        "content": content_arr
                                    }
                                ],
                                max_tokens=600
                            )
                            content = resp.choices[0].message.content
                            if content:
                                # Общий помощник вместо местной копии срезки. В
                                # прежней ветке незакрытого тега при пустом начале
                                # выбиралось parts2[1] — то есть САМИ размышления
                                # модели уходили как клиническое описание снимка.
                                text = gemini_client.strip_reasoning(content)
                                if text:
                                    # Инструкции «отвечай строго по-русски» мало:
                                    # замер по живой базе показал 1285 английских
                                    # описаний из 3375 — 38%. Они уходят в промпт
                                    # русского чата, и отвечающая модель вынуждена
                                    # переводить чужой текст. Проверяем результат,
                                    # а не надеемся на послушание модели.
                                    if _is_mostly_cyrillic(text):
                                        logger.info(f"Vision success via {provider} ({model_name})")
                                        return text
                                    if english_fallback is None:
                                        english_fallback = text
                                    logger.warning(
                                        "Vision answered not in Russian via %s (%s); trying next model",
                                        provider, model_name,
                                    )
                                    break

                        except Exception as e:
                            err_str = str(e).lower()
                            if "413" in err_str:
                                logger.warning(
                                    "Vision payload too large (413) for %s images; giving up.",
                                    len(image_urls),
                                )
                                return None
                            # Коды статусов — по границе слова. Подстрочный поиск
                            # "500" находил его в "1500 tokens" и "500000 tokens",
                            # то есть обычная ошибка запроса выбрасывала модель
                            # из каскада как перегруженную.
                            if gemini_client._SERVER_ERROR_RE.search(err_str) or "unavailable" in err_str:
                                logger.warning(f"Vision {provider} server overloaded ({err_str}). Skipping model {model_name}.")
                                break
                            if gemini_client._RATE_LIMIT_RE.search(err_str):
                                # Кулдаун записываем в общий с текстовым каскадом
                                # файл: иначе следующий же снимок снова уходит в
                                # тот же исчерпанный ключ.
                                gemini_client.set_key_cooldown(provider, api_key)
                                logger.info(
                                    "Vision key rate limited (429); key placed on %ss cooldown.",
                                    gemini_client.KEY_COOLDOWN_SECONDS,
                                )
                                continue
                            logger.warning(f"Vision {provider} key failed ({model_name}): {e}")

            if english_fallback:
                # Ни одна модель каскада не ответила по-русски. Английское
                # описание всё же лучше, чем ничего: без него врач получит
                # ответ, в котором снимок вообще не упомянут.
                logger.warning("Vision: no Russian answer from cascade, using non-Russian description")
                return english_fallback
            return None

        except Exception as e:
            logger.error(f"Ошибка в модуле Vision: {e}")
            return None
