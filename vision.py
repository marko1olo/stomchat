import base64
import logging
import random
import re
import asyncio
import time
import os
import json
import hashlib
import httpx
from openai import AsyncOpenAI

import clinical_cognitive_core
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

# Сколько моделей в пуле каскада зрения. Держим рядом с константами, потому что
# от этого числа зависит внешний потолок разбора снимка в main.py, а сам пул
# объявлен внутри describe_image.
VISION_MODEL_POOL_SIZE = 3

# Сколько ключей пробовать на ОДНУ модель. Отказ конкретного ключа почти всегда
# означает квоту или 429, и от этого спасает следующая модель, а не двадцатый
# ключ той же. Перебор всех ключей давал 27 попыток и до 891 с на снимок.
VISION_KEYS_PER_MODEL = max(1, _env_int("STOMCHAT_VISION_KEYS_PER_MODEL", 2))


def vision_cascade_budget_seconds():
    """
    Худший случай каскада зрения в секундах.

    Нужна вызывающему (main.py), чтобы внешний потолок разбора снимка вмещал
    внутренний бюджет. Раньше это были два независимых числа: потолок 180 с при
    фактическом бюджете до 891 с — резервные модели каскада не пробовались
    никогда, а снимок после внешнего таймаута получал в базе отметку
    «разобрано» и терялся навсегда.

    Попыток = число моделей пула на число ключей, пробуемых на модель. На попытку
    уходит запрос плюс пауза троттлинга. Подготовка снимка (до 45 с) считается
    отдельно вызывающим, потому что делается один раз на файл, а не на попытку.
    """
    attempts = VISION_MODEL_POOL_SIZE * VISION_KEYS_PER_MODEL
    per_attempt = GROQ_HTTP_TIMEOUT_SECONDS + VISION_MIN_CALL_INTERVAL_SECONDS
    return int(attempts * per_attempt)

# Проверка TLS-сертификата. Здесь стояло жёсткое verify=False, то есть API-ключ
# уходил провайдеру по соединению, подлинность которого не проверялась: любой,
# кто способен вклиниться в трафик, получал ключ в заголовке Authorization.
# По умолчанию теперь проверяем. Если в вашей сети стоит перехватывающий прокси
# с собственным корневым сертификатом и зрение начнёт падать с SSL-ошибкой —
# STOMCHAT_VISION_TLS_VERIFY=0 возвращает прежнее поведение осознанно.
VISION_TLS_VERIFY = _env_flag("STOMCHAT_VISION_TLS_VERIFY", True)


# «Снимок больше лимита провайдера» — по границе слова, как _SERVER_ERROR_RE в
# gemini_client. Здесь стоял подстрочный поиск "413", а он находит эти цифры в
# любом посторонним числе: "max_tokens 4130", "413000 tokens", "req_8413ac".
# Цена ошибки тут выше, чем у остальных классификаторов: ветка 413 не переходит
# к следующему ключу и не к следующей модели, а делает return из describe_image
# — то есть один посторонний отказ рвал весь каскад из трёх моделей и выбрасывал
# описание, уже полученное от предыдущей. Текстовая формулировка нужна отдельно:
# Groq и прокси отвечают "Request Entity Too Large" вообще без кода.
_PAYLOAD_TOO_LARGE_RE = re.compile(r"\b413\b|payload too large|request entity too large")


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


class VisionDescription(str):
    """
    Строковое описание снимка с сохранением подготовленных image_urls (data:image/jpeg;base64,...).
    Полностью совместимо с обычным str для базы данных, логирования и строковых операций,
    но позволяет вызывающему коду извлечь оригинальные изображения для мультимодального вызова Gemini,
    а также краткую chairside подпись к иллюстрации (short_caption).
    """
    image_urls: list[str]
    short_caption: str | None

    def __new__(cls, content, image_urls=None, short_caption=None):
        instance = super().__new__(cls, content)
        instance.image_urls = list(image_urls) if image_urls else []
        instance.short_caption = short_caption
        return instance


def extract_fallback_short_caption(text: str, max_len: int = 160) -> str:
    """Извлекает лаконичную подпись chairside (1-2 предложения) из сплошного текста."""
    if not text:
        return ""
    cleaned = re.sub(
        r"^[\[\]#*\s\d.)-]*(?:ПОДРОБНОЕ\s+ОПИСАНИЕ|КРАТКАЯ\s+ПОДПИСЬ|ОПИСАНИЕ\s+СНИМКА|СНИМОК|НА\s+СНИМКЕ)[\[\]#*\s:\-]*",
        "",
        text,
        flags=re.IGNORECASE
    )
    cleaned = re.sub(r"^#+\s*", "", cleaned)
    cleaned = re.sub(r"[\r\n]+", " ", cleaned).strip()

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    candidate = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if not candidate:
            candidate = s
        elif len(candidate) + len(s) + 1 <= max_len:
            candidate = f"{candidate} {s}"
        else:
            break

    if not candidate:
        candidate = cleaned[:max_len]
    if len(candidate) > max_len:
        truncated = candidate[:max_len - 3]
        last_space = truncated.rfind(" ")
        if last_space > 40:
            candidate = truncated[:last_space] + "..."
        else:
            candidate = truncated + "..."
    return candidate.strip()


def parse_dual_vision_description(raw_text: str) -> tuple[str, str]:
    """
    Разбирает ответ модели зрения на два компонента:
    1. Подробное клиническое описание (для LLM контекста, логов и промпта)
    2. Краткая chairside подпись (для figcaption в Telegraph и PDF)
    """
    if not raw_text:
        return "", ""

    text = raw_text.strip()

    detailed_re = re.compile(
        r"(?:^|\n)[\[\]#*\s\d.)-]*(?:ПОДРОБНОЕ\s+ОПИСАНИЕ|КЛИНИЧЕСКОЕ\s+ОПИСАНИЕ|ПОДРОБНЫЙ\s+РАЗБОР)[\[\]#*\s:\-]*",
        re.IGNORECASE
    )
    short_re = re.compile(
        r"(?:^|\n)[\[\]#*\s\d.)-]*(?:КРАТКАЯ\s+ПОДПИСЬ|ПОДПИСЬ\s+К\s+ИЛЛЮСТРАЦИИ|CHAIRSIDE\s+ПОДПИСЬ|КРАТКОЕ\s+ОПИСАНИЕ)[\[\]#*\s:\-]*",
        re.IGNORECASE
    )

    d_match = detailed_re.search(text)
    s_match = short_re.search(text)

    if d_match and s_match:
        if d_match.start() < s_match.start():
            detailed_part = text[d_match.end():s_match.start()].strip()
            short_part = text[s_match.end():].strip()
        else:
            short_part = text[s_match.end():d_match.start()].strip()
            detailed_part = text[d_match.end():].strip()

        short_part = re.sub(r"^[\"«']\s*|\s*[\"»']$", "", short_part).strip()
        if len(short_part) > 180:
            short_part = extract_fallback_short_caption(short_part, max_len=180)
        if not short_part:
            short_part = extract_fallback_short_caption(detailed_part, max_len=180)

        return detailed_part, short_part

    if d_match and not s_match:
        detailed_part = text[d_match.end():].strip()
        short_part = extract_fallback_short_caption(detailed_part, max_len=180)
        return detailed_part, short_part

    if s_match and not d_match:
        short_part = text[s_match.end():].strip()
        short_part = re.sub(r"^[\"«']\s*|\s*[\"»']$", "", short_part).strip()
        return text, short_part

    detailed = text
    short = extract_fallback_short_caption(detailed)
    return detailed, short



_RECENT_IMAGE_URLS: dict[str, list[str]] = {}

VISION_CACHE_FILE = os.getenv("STOMCHAT_VISION_CACHE_FILE", "vision_cache.json")
VISION_CACHE_MAX_ENTRIES = 1000


def _load_vision_cache() -> dict:
    if not os.path.exists(VISION_CACHE_FILE):
        return {}
    try:
        with open(VISION_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning("Failed to load vision cache from %s: %s", VISION_CACHE_FILE, e)
    return {}


def _save_vision_cache(cache_data: dict) -> None:
    tmp_path = None
    try:
        if len(cache_data) > VISION_CACHE_MAX_ENTRIES:
            sorted_items = sorted(
                cache_data.items(),
                key=lambda item: item[1].get("ts", 0) if isinstance(item[1], dict) else 0,
                reverse=True,
            )
            cache_data = dict(sorted_items[:VISION_CACHE_MAX_ENTRIES])

        # Unique tmp_path prevents collision between concurrent processes / threads
        tmp_path = f"{VISION_CACHE_FILE}.{os.getpid()}_{time.time_ns()}_{random.randint(1000, 9999)}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        # Atomic replace with retry for Windows filesystem concurrency locks
        for attempt in range(4):
            try:
                os.replace(tmp_path, VISION_CACHE_FILE)
                break
            except (PermissionError, OSError) as os_err:
                if attempt == 3:
                    raise os_err
                time.sleep(0.02 * (attempt + 1))
    except Exception as e:
        logger.warning("Failed to persist vision cache to %s: %s", VISION_CACHE_FILE, e)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def clear_vision_cache():
    """Сбрасывает кэш разбора изображений в памяти и на диске."""
    global _VISION_CACHE
    _VISION_CACHE.clear()
    if os.path.exists(VISION_CACHE_FILE):
        try:
            os.remove(VISION_CACHE_FILE)
        except OSError:
            pass


def get_vision_cache() -> dict:
    """Возвращает текущий словарь кэша разбора изображений."""
    return _VISION_CACHE


_VISION_CACHE: dict = _load_vision_cache()


def _compute_vision_cache_key(image_bytes_list: list[bytes], caption: str = None) -> str:
    hasher = hashlib.sha256()
    for b in image_bytes_list:
        hasher.update(b)
    norm_caption = (caption or "").strip()[:500]
    hasher.update(norm_caption.encode("utf-8"))
    return hasher.hexdigest()


def get_recent_image_urls(key=None) -> list[str]:
    """Возвращает последние сохраненные image_urls."""
    if key and key in _RECENT_IMAGE_URLS:
        return _RECENT_IMAGE_URLS[key]
    if _RECENT_IMAGE_URLS:
        return next(reversed(_RECENT_IMAGE_URLS.values()))
    return []


async def _translate_description_to_russian(text: str, client: AsyncOpenAI = None, provider: str = None) -> str:
    """
    Быстрый легковесный перевод англоязычного описания на русский язык.
    Исключает выброс готового описания снимка и повторный запуск тяжелого мультимодального каскада.
    """
    if not text:
        return ""
    translate_prompt = (
        "Переведи следующее клиническое стоматологическое описание на профессиональный русский язык. "
        "Сохраняй медицинскую терминологию (FDI номера зубов, анатомические структуры, рентгенологические термины). "
        "Выведи ТОЛЬКО русский перевод без каких-либо комментариев, предисловий и тегов <think>:\n\n"
        f"{text}"
    )
    if client and provider:
        try:
            trans_model = "gemini-3.5-flash-lite" if provider == "gemini" else "qwen/qwen3.8-27b"
            resp = await client.chat.completions.create(
                model=trans_model,
                messages=[{"role": "user", "content": translate_prompt}],
                max_tokens=1000,
            )
            raw = resp.choices[0].message.content if (resp.choices and len(resp.choices) > 0) else None
            cleaned = gemini_client.strip_reasoning(raw)
            if cleaned and _is_mostly_cyrillic(cleaned):
                return cleaned
        except Exception as exc:
            logger.warning("Direct client translation failed: %s; falling back to blocking_tools", exc)

    try:
        import blocking_tools
        res, err = await blocking_tools.generate_gemini_text_async(
            translate_prompt,
            {"kind": "llama_triage", "thinking_level": "LOW"},
            timeout=15.0,
        )
        if res and getattr(res, "text", None):
            cleaned = gemini_client.strip_reasoning(res.text)
            if cleaned and _is_mostly_cyrillic(cleaned):
                return cleaned
    except Exception as exc:
        logger.warning("blocking_tools translation failed: %s", exc)

    return ""


async def describe_image(file_paths, caption: str = None, is_passive: bool = False, use_cache: bool = True) -> str:
    """Анализирует изображение(я) через каскад Vision (Gemini 3.5 -> Qwen 3.6 -> Llama 4 Scout)."""
    if isinstance(file_paths, str):
        file_paths = [file_paths]

    skip_cache = (not use_cache) or any(not os.path.exists(fp) for fp in file_paths)

    async with _get_vision_semaphore():
        try:
            # Английское описание — запасной вариант: если ни одна модель
            # каскада не ответит по-русски, отдадим его, а не пустоту.
            english_fallback = None
            image_urls = []
            image_bytes_list = []
            for fp in file_paths:
                resized_bytes, error = await prepare_image_for_analysis(
                    fp,
                    timeout=VISION_IMAGE_PREP_TIMEOUT_SECONDS,
                )
                if error:
                    logger.warning("Vision image prep failed path=%s: %s", fp, error)
                if not error and resized_bytes:
                    image_bytes_list.append(resized_bytes)
                    image_urls.append(f"data:image/jpeg;base64,{base64.b64encode(resized_bytes).decode('utf-8')}")

            if not image_urls:
                logger.error("Ошибка подготовки фото: ни одно фото не удалось обработать.")
                return None

            cache_key = None
            if not skip_cache and image_bytes_list:
                cache_key = _compute_vision_cache_key(image_bytes_list, caption)
                if cache_key in _VISION_CACHE:
                    cached_entry = _VISION_CACHE[cache_key]
                    cached_text = cached_entry.get("text") if isinstance(cached_entry, dict) else str(cached_entry)
                    cached_short = cached_entry.get("short_caption") if isinstance(cached_entry, dict) else None
                    if cached_text:
                        logger.info("Vision cache hit for key=%s (%s images)", cache_key[:12], len(image_urls))
                        _RECENT_IMAGE_URLS[cached_text[:60]] = image_urls
                        if len(_RECENT_IMAGE_URLS) > 50:
                            _RECENT_IMAGE_URLS.pop(next(iter(_RECENT_IMAGE_URLS)))
                        return VisionDescription(cached_text, image_urls=image_urls, short_caption=cached_short)

            context = f" Контекст от автора: '{caption}'." if caption else ""
            # Описание снимка уходит в отвечающий промпт и становится основанием
            # клинического комментария. Прежняя формулировка прямо приглашала
            # называть патологию, не ограничивая домысливание: модель зрения
            # уверенно «видит» на рентгене очаг, которого там нет, и дальше по
            # этой выдумке строится совет коллеге. Отсюда требования ниже —
            # отделять увиденное от истолкованного и признавать пределы снимка.
            album_notice = ""
            if len(image_urls) > 1:
                album_notice = (
                    f" ВНИМАНИЕ: Загружена серия из {len(image_urls)} снимков (клинический фотопротокол/серия RG/сканы). "
                    f"Ограничение длины в 3-6 предложений СНИМАЕТСЯ! "
                    f"Дай последовательный разбор каждого снимка (Снимок 1, Снимок 2...), затем сформируй структурированный синтез. "
                    f"Синтез строй по анатомическим доменам, реально представленным в серии: "
                    f"выдели те аспекты (макроархитектура, окклюзия, боковые опоры, локальные дефекты, эндо/пародонт, лабораторные этапы и т.д.), "
                    f"которые читаются на снимках. Не перечисляй домены ради перечисления — включай только те, по которым серия даёт достаточно информации."
                )
            system_prompt = (
                f"Это стоматологическое изображение из профессионального врачебного сообщества.{context}{album_notice} "
                f"Оценивай любые снимки: внутриротовые фото (фронт, боковые окклюзии, окклюзионный вид ВЧ/НЧ с нёба/языка, зеркала, ретракторы, этапы препарирования, временные коронки, раббердам); "
                f"рентгенографию (прицельные/периапикальные, bitewing, ОПТГ панорамные, срезы КЛКТ: аксиальные, корональные, 3D-реконструкции); лабораторные сканы, wax-up, схемы CAD/CAM, фото гипсовых моделей. Не требуй другой снимок вместо анализа. "
                f"НЕМЕДИЦИНСКИЕ ИЗОБРАЖЕНИЯ: если это мем, котик, еда, скриншот мессенджера/интерфейса, бытовое фото — КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выдумывать зубы, кариес или рентген! Кратко и точно опиши суть («Скриншот переписки...», «Мем с собакой...»). Если на картинке есть текст или подписи — разбери их смысл. "
                f"Опиши клинико-рентгенологический статус (Macro + Micro): "
                f"1. Архитектура и прикус: положение челюстей, окклюзионная плоскость, перекрёстный/глубокий/открытый прикус, потеря межальвеолярной высоты (снижение прикуса), скелетная/зубоальвеолярная асимметрия. "
                f"2. Зубные ряды: дефекты (включенные, концевые — потеря жевательной опоры в боковых отделах, отсутствие моляров/премоляров), мостовидные протезы, имплантаты, композитные шины фронтальной группы. "
                f"3. Локальный статус (FDI): номера зубов (1.1-4.8), твёрдые ткани, кариес, вторичный кариес, уступы, краевое прилегание коронок/реставраций. "
                f"4. Скрытые контрасты (фасад vs нёбо/язык): сопоставление вестибулярного вида (коронки/виниры) с окклюзионным/нёбным/язычным (кариозный распад, разрушенные стенки). "
                f"5. Эндодонтия и кость (RG/КТ): кривизна каналов (Шнайдер), пропущенная анатомия (MB2, Vertucci), качество обтурации (апекс, недо-/перепломбировка, пафф), штифты (СВШ/вкладка), ятрогении (отломки файлов, ступенька/байпас, перфорации), резорбции, периодонтальная щель, маргинальная кость, PAI 1-5, признаки VRF (halo/J-дефект). "
                f"ПРАВИЛА ДОСТОВЕРНОСТИ: "
                f"1) Строгая наблюдаемость: описывай структуры только в прямом фокусе; при помехах констатируй недоступность обзора («дефектов не дифференцируется», «граница вне зоны обзора» — полноценный ответ). "
                f"2) Отделяй наблюдение от истолкования: сначала морфология, затем гипотезы; не утверждай патологию без четких границ; НЕ ставь диагноз по одному снимку. "
                f"3) Дисциплинарная консистентность: не смешивай терапию, хирургию, ортопедию. "
                f"4) Без фантомных величин: никаких вымышленных чисел (миллиметры, длина, дефект), если они не читаются на снимке. "
                f"Для одиночного снимка: 3-6 емких профессиональных предложений (для альбома — зональный синтез). "
                f"{clinical_cognitive_core.VISION_COGNITIVE_ARCHITECTURE} "
                f"ФОРМАТ ОТВЕТА — СТРОГО ДВЕ СЕКЦИИ:\n"
                f"[ПОДРОБНОЕ ОПИСАНИЕ]\n"
                f"<подробный клинический разбор снимка для LLM-контекста: 3-6 предложений>\n\n"
                f"[КРАТКАЯ ПОДПИСЬ]\n"
                f"<1-2 коротких chairside-фразы для подписи к фото в статье, макс 120 символов>\n\n"
                f"ОТВЕЧАЙ СТРОГО НА РУССКОМ ЯЗЫКЕ без английских фраз, черновиков и тегов <think>."
            )

            
            # Load balancing pool (fallback: gemini-3.8-flash, gemini-3.7-flash): Gemini 3.5 Flash Lite, Gemini 3.1 Flash Lite, Qwen 3.8 27B
            models_pool = [
                ("gemini-3.5-flash-lite", "gemini"),
                ("gemini-3.1-flash-lite", "gemini"),
                ("qwen/qwen3.8-27b", "groq"),
            ]
            # Исключаем временно забаненные модели (по 503/404)
            banned_map = gemini_client.get_banned_models()
            unbanned_pool = [entry for entry in models_pool if entry[0] not in banned_map]
            active_pool = unbanned_pool if unbanned_pool else models_pool
            # Gemini-модели (3.5 и 3.1) всегда идут первыми — они нативно видят снимки и анатомию.
            # Groq (Qwen) остаётся строго резервом на случай отказа или бана Google.
            gemini_active = [m for m in active_pool if m[1] == "gemini"]
            fallback_active = [m for m in active_pool if m[1] != "gemini"]
            if is_passive and len(gemini_active) > 1:
                s_idx = random.randint(0, len(gemini_active) - 1)
                gemini_active = gemini_active[s_idx:] + gemini_active[:s_idx]
            models_cascade = gemini_active + fallback_active
            if not models_cascade:
                models_cascade = active_pool

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

                    # Ключей на модель берём ОГРАНИЧЕННОЕ число, а не все.
                    #
                    # Перебор всех ключей давал 2 x 10 + 1 x 7 = 27 попыток по 33 с
                    # (запрос 30 плюс пауза троттлинга 3) — до 891 с на один
                    # снимок. Внешний потолок разбора стоял 180 с, то есть
                    # резервные модели каскада не пробовались никогда, а снимок
                    # после срабатывания внешнего таймаута получал в базе отметку
                    # «разобрано» и терялся навсегда.
                    #
                    # Поднимать внешний потолок до 951 с было бы хуже: воркер
                    # один, очередь на 128 снимков, и одно зависшее фото
                    # остановило бы разбор на четверть часа. Отказ конкретного
                    # ключа почти всегда означает квоту или 429 — а от этого
                    # спасает СЛЕДУЮЩАЯ МОДЕЛЬ, не двадцатый ключ той же.
                    # Ключи перемешаны выше, поэтому квота размазывается по пулу
                    # между вызовами.
                    for api_key in available[:VISION_KEYS_PER_MODEL]:
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
                            max_images = 3 if provider == "groq" else min(len(image_urls), 6)
                            for iu in image_urls[:max_images]:
                                content_arr.append({"type": "image_url", "image_url": {"url": iu}})
                            
                            resp = await client.chat.completions.create(
                                model=model_name,
                                messages=[
                                    {
                                        "role": "user",
                                        "content": content_arr
                                    }
                                ],
                                max_tokens=800 if provider == "groq" else 1200
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
                                    russian_text = text
                                    if not _is_mostly_cyrillic(text):
                                        logger.info(
                                            "Vision answered in non-Russian via %s (%s). Translating to Russian via lightweight call...",
                                            provider, model_name,
                                        )
                                        translated = await _translate_description_to_russian(text, client, provider)
                                        if translated:
                                            russian_text = translated

                                    if _is_mostly_cyrillic(russian_text):
                                        logger.info(f"Vision success via {provider} ({model_name})")
                                        gemini_client.note_success(provider, api_key, model_name=model_name)
                                        # Разбор двойного формата [ПОДРОБНОЕ ОПИСАНИЕ] / [КРАТКАЯ ПОДПИСЬ]
                                        detailed_ctx, short_cap = parse_dual_vision_description(russian_text)
                                        if not detailed_ctx:
                                            detailed_ctx = russian_text
                                        if not short_cap:
                                            short_cap = extract_fallback_short_caption(detailed_ctx, max_len=160)
                                        _RECENT_IMAGE_URLS[detailed_ctx[:60]] = image_urls
                                        if len(_RECENT_IMAGE_URLS) > 50:
                                            _RECENT_IMAGE_URLS.pop(next(iter(_RECENT_IMAGE_URLS)))
                                        if not skip_cache and cache_key:
                                            _VISION_CACHE[cache_key] = {"text": detailed_ctx, "short_caption": short_cap, "ts": time.time()}
                                            _save_vision_cache(_VISION_CACHE)
                                        return VisionDescription(detailed_ctx, image_urls=image_urls, short_caption=short_cap)


                                    if english_fallback is None:
                                        english_fallback = text
                                    logger.warning(
                                        "Vision answered not in Russian and translation failed via %s (%s); trying next model",
                                        provider, model_name,
                                    )
                                    break

                        except Exception as e:
                            err_str = str(e).lower()
                            if _PAYLOAD_TOO_LARGE_RE.search(err_str):
                                logger.warning(
                                    "Vision payload too large (413) for %s images. Stopping attempts.",
                                    len(image_urls),
                                )
                                if english_fallback:
                                    _RECENT_IMAGE_URLS[english_fallback[:60]] = image_urls
                                    if not skip_cache and cache_key:
                                        _VISION_CACHE[cache_key] = {"text": english_fallback, "ts": time.time()}
                                        _save_vision_cache(_VISION_CACHE)
                                    return VisionDescription(english_fallback, image_urls=image_urls)
                                return None
                            if any(s in err_str for s in ("404", "not_found", "does not exist", "not found")):
                                gemini_client.ban_model(model_name, 86400)
                                logger.warning(f"Vision {provider} model {model_name} not found (404). Banned for 24h.")
                                break
                            # Коды статусов — по границе слова. Подстрочный поиск
                            # "500" находил его в "1500 tokens" и "500000 tokens",
                            # то есть обычная ошибка запроса выбрасывала модель
                            # из каскада как перегруженную.
                            if gemini_client._SERVER_ERROR_RE.search(err_str) or any(s in err_str for s in ("unavailable", "503", "504", "overload")):
                                ban_duration = getattr(gemini_client, "MODEL_BAN_SECONDS", 1200)
                                gemini_client.ban_model(model_name, ban_duration)
                                logger.warning(
                                    f"Vision {provider} server overloaded ({err_str}). "
                                    f"Banning model {model_name} for {ban_duration}s."
                                )
                                break
                            if gemini_client._RATE_LIMIT_RE.search(err_str):
                                # Кулдаун записываем в общий с текстовым каскадом
                                # файл: иначе следующий же снимок снова уходит в
                                # тот же исчерпанный ключ.
                                gemini_client.note_key_failure(provider, api_key, str(e), model_name=model_name)
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
                _RECENT_IMAGE_URLS[english_fallback[:60]] = image_urls
                if not skip_cache and cache_key:
                    _VISION_CACHE[cache_key] = {"text": english_fallback, "ts": time.time()}
                    _save_vision_cache(_VISION_CACHE)
                return VisionDescription(english_fallback, image_urls=image_urls)
            return None

        except Exception as e:
            logger.error(f"Ошибка в модуле Vision: {e}")
            return None
