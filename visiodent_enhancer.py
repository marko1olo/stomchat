# -*- coding: utf-8 -*-
"""
visiodent_enhancer.py - Интеллектуальный модуль обработки дентальных радиовизиограмм (Визиограф HD).

Пайплайн:
1. Фильтрация и валидация: обработка ТОЛЬКО прицельных визиографических снимков
   (отсечение КТ, ОПТГ, цветных клинических фото зубов, скриншотов и мемов).
2. Обработка изображения (OpenCV + Pillow):
   - Bilateral Denoising: подавление дробового зерна сенсора без размытия краев дентина/эмали/кости/металла.
   - Dynamic Range Expansion: нормализация гистограммы (1%-99% процентили) + адаптивная гамма.
   - Квадрант 1: Оригинал (Grayscale 8-bit).
   - Квадрант 2: Адаптивный контраст (CLAHE, clipLimit=2.6).
   - Квадрант 3: Инверсия / Негатив (подсвечивание периапикальных деструкций, резорбций и трещин белым).
   - Квадрант 4: Контурный фильтр (Unsharp Masking: микроструктура периодонтальной щели, отломков NiTi, ступеней).
   - Сборка в 2х2 HD сетку с темными делителями и читаемыми TrueType HUD-плашками (Pillow).
3. LLM/Heuristic Анализ клинических сомнений:
   - Выявление зуба FDI, зоны интереса и предмета сомнения (отломок, периодонтит, латераль, апекс, трещина).
   - Назначение приоритетного квадранта (2, 3 или 4) с прикладным chairside-обоснованием.
4. Telegram UX/UI:
   - ЛС: автоматическая отправка коллажа при наличии сомнений; при отсутствии сомнений — инлайн-кнопка.
   - Группа: вызов по команде /xray или /рентген, при прямом призыве (@bot со снимком),
     либо компактная кнопка ухода в ЛС при пассивном триаже.
"""

import os
import re
import json
import asyncio
import logging
from typing import Optional, Dict, Any, Tuple, Union

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("visiodent_enhancer")

# Пути к системным TrueType шрифтам Windows с полной поддержкой кириллицы
FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]
RESOLVED_FONT_PATH = next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)

# Ключевые маркеры прицельного визиографа (Periapical / Bitewing X-Ray)
PERIAPICAL_KEYWORDS = [
    "прицельн", "визиограф", "радиовизиогр", "радиовизиограмм", "рентгенограмм", "периапикальн",
    "интраоральн", "внутриротов", "снимок зуба", "рентген зуба", "верхушк", "апекс",
    "корень", "корня", "канал", "каналы", "обтураци", "гуттаперч"
]

# Маркеры исключения (НЕ визиограф)
NON_PERIAPICAL_STOPWORDS = [
    "кт", "клкт", "томограф", "оптг", "панорам", "ортопантомограмм",
    "клиническое фото", "внутриротовое фото", "фото полости рта",
    "скриншот", "мем", "интерфейс", "документ", "срез"
]

# Клинические триггеры сомнений и развилок
DOUBT_TRIGGERS = [
    "отломок", "обломок", "сломал", "сломан", "перелом", "сепарац", "трещин", "трещина",
    "очаг", "периодонтит", "разрежени", "деструкци", "кист", "гранулем", "латераль",
    "дельта", "перфораци", "стрип", "ступен", "не пройден", "пройден ли", "где верхушка",
    "апекс", "зажив", "кариес", "нависающ", "прилегани", "сомнева", "посмотри", "видите",
    "кто видит", "подскажите", "что думаете", "артефакт", "наложени", "пустот", "недопломбирован"
]


def draw_hud_pill_badge(
    pil_img: Image.Image,
    text: str,
    xy: Tuple[int, int],
    tag_color: Tuple[int, int, int] = (0, 200, 255),
    font_size: int = 22
) -> None:
    """Отрисовывает медицинский полупрозрачный темный HUD-бейдж с цветным тегом и шрифтом UTF-8."""
    draw = ImageDraw.Draw(pil_img, "RGBA")
    try:
        font = ImageFont.truetype(RESOLVED_FONT_PATH, font_size) if RESOLVED_FONT_PATH else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad_x = 16
    pad_y = 10
    x, y = xy
    w = tw + pad_x * 2 + 18
    h = th + pad_y * 2

    # Полупрозрачная подложка цвета темного сланца
    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=8,
        fill=(15, 18, 22, 225),
        outline=(60, 68, 76, 255),
        width=1
    )

    # Круглый цветовой индикатор квадранта
    dot_r = 5
    dot_x = x + 14
    dot_y = y + h // 2
    draw.ellipse(
        [dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r],
        fill=tag_color
    )

    # Четкий белый текст
    text_x = dot_x + dot_r + 10
    text_y = y + pad_y - 2
    draw.text((text_x, text_y), text, font=font, fill=(255, 255, 255, 255))


draw_pill_badge = draw_hud_pill_badge


def generate_visiodent_collage(
    input_source: Union[str, bytes],
    output_path: Optional[str] = None,
    target_quadrant_width: int = 950
) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """
    Создает диагностический 2x2 HD коллаж из прицельного рентгеновского снимка (визиографа).

    Квадранты:
    1. Оригинал (Grayscale 8-bit)
    2. Адаптивный контраст (CLAHE: детали каналов и кости)
    3. Негатив (Инверсия: подсвечивание очагов разрежения, кист и трещин)
    4. Контурный фильтр (Unsharp Mask: грани отломков, периодонтальная щель, ступени)

    Возвращает (output_path, (width, height)).
    """
    try:
        if isinstance(input_source, bytes):
            nparr = np.frombuffer(input_source, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        elif isinstance(input_source, str) and os.path.exists(input_source):
            img = cv2.imread(input_source, cv2.IMREAD_COLOR)
        else:
            logger.error("generate_visiodent_collage: невалидный источник изображения: %s", type(input_source))
            return None, None

        if img is None or img.size == 0:
            logger.error("generate_visiodent_collage: не удалось декодировать изображение")
            return None, None

        # 1. Grayscale
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img.copy()

        orig_h, orig_w = gray.shape
        if orig_h < 40 or orig_w < 40:
            logger.warning("Слишком маленькое разрешение для визиографа: %dx%d", orig_w, orig_h)
            return None, None

        # 2. Bilateral Denoising: устранение зерна сенсора при сохранении резких контуров
        denoised = cv2.bilateralFilter(gray, d=7, sigmaColor=30, sigmaSpace=30)

        # 3. Dynamic Range Expansion (1%-99% процентили гистограммы)
        p1, p99 = np.percentile(denoised, (1, 99))
        if p99 > p1:
            norm = np.clip((denoised.astype(np.float32) - p1) * (255.0 / (p99 - p1)), 0, 255).astype(np.uint8)
        else:
            norm = denoised.copy()

        # Адаптивная гамма-коррекция для недоэкспонированных затемненных снимков
        mean_val = np.mean(norm)
        if mean_val < 90:
            gamma = 0.85
            inv_gamma = 1.0 / gamma
            lut = np.array([((i / 255.0) ** inv_gamma) * 255 for i in range(256)]).astype("uint8")
            norm = cv2.LUT(norm, lut)

        # Квадрант 1: Оригинал
        q1 = gray.copy()

        # Квадрант 2: CLAHE (Contrast-Limited Adaptive Histogram Equalization)
        clahe = cv2.createCLAHE(clipLimit=2.6, tileGridSize=(8, 8))
        q2 = clahe.apply(norm)

        # Квадрант 3: Инверсия / Негатив
        q3 = cv2.bitwise_not(q2)

        # Квадрант 4: Контурный фильтр (Unsharp Masking сбалансированный)
        blurred = cv2.GaussianBlur(q2, (0, 0), sigmaX=1.8)
        q4 = cv2.addWeighted(q2, 1.45, blurred, -0.45, 0)

        # Масштабирование в HD размер с сохранением оригинальных пропорций
        target_w = target_quadrant_width
        target_h = int(orig_h * (target_w / orig_w))

        interp = cv2.INTER_AREA if target_w < orig_w else cv2.INTER_LANCZOS4
        q1_res = cv2.resize(q1, (target_w, target_h), interpolation=interp)
        q2_res = cv2.resize(q2, (target_w, target_h), interpolation=interp)
        q3_res = cv2.resize(q3, (target_w, target_h), interpolation=interp)
        q4_res = cv2.resize(q4, (target_w, target_h), interpolation=interp)

        q1_bgr = cv2.cvtColor(q1_res, cv2.COLOR_GRAY2BGR)
        q2_bgr = cv2.cvtColor(q2_res, cv2.COLOR_GRAY2BGR)
        q3_bgr = cv2.cvtColor(q3_res, cv2.COLOR_GRAY2BGR)
        q4_bgr = cv2.cvtColor(q4_res, cv2.COLOR_GRAY2BGR)

        # Сборка 2x2 с темными разделителями
        divider_thickness = 6
        divider_color = (25, 28, 32)

        divider_v = np.full((target_h, divider_thickness, 3), divider_color, dtype=np.uint8)
        top_row = np.hstack([q1_bgr, divider_v, q2_bgr])
        bot_row = np.hstack([q3_bgr, divider_v, q4_bgr])

        total_w = top_row.shape[1]
        divider_h = np.full((divider_thickness, total_w, 3), divider_color, dtype=np.uint8)
        collage_bgr = np.vstack([top_row, divider_h, bot_row])

        # Переход в Pillow для качественного нанесения UTF-8 HUD-плашек
        collage_rgb = cv2.cvtColor(collage_bgr, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(collage_rgb)

        margin_x, margin_y = 24, 24
        draw_pill_badge(pil_img, "1. ОРИГИНАЛ (GRAYSCALE)", (margin_x, margin_y), tag_color=(180, 190, 205), font_size=22)
        draw_pill_badge(pil_img, "2. АДАПТИВНЫЙ КОНТРАСТ (CLAHE)", (target_w + divider_thickness + margin_x, margin_y), tag_color=(0, 215, 255), font_size=22)
        draw_pill_badge(pil_img, "3. НЕГАТИВ (РЕЗОРБЦИЯ & ТРЕЩИНЫ)", (margin_x, target_h + divider_thickness + margin_y), tag_color=(255, 90, 90), font_size=22)
        draw_pill_badge(pil_img, "4. КОНТУРНЫЙ ФИЛЬТР (PDL & ОБЛОМКИ)", (target_w + divider_thickness + margin_x, target_h + divider_thickness + margin_y), tag_color=(75, 225, 120), font_size=22)

        # Определение пути сохранения
        if not output_path:
            from media_tools import MEDIA_TEMP_DIR
            os.makedirs(MEDIA_TEMP_DIR, exist_ok=True)
            output_path = os.path.join(MEDIA_TEMP_DIR, f"visio_collage_{os.getpid()}_{np.random.randint(1000, 9999)}.jpg")

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        pil_img.save(output_path, "JPEG", quality=95)
        return output_path, pil_img.size
    except Exception as exc:
        logger.exception("Ошибка при генерации визиографического коллажа: %s", exc)
        return None, None


def is_periapical_xray_heuristic(text: str = "", vision_description: str = "") -> bool:
    """Быстрый эвристический чекер: относится ли снимок к прицельному радиовизиографу."""
    combined = f"{text or ''} {vision_description or ''}".lower()
    
    # Жесткий фильтр исключений
    if any(stop in combined for stop in NON_PERIAPICAL_STOPWORDS):
        # Если есть слово "кт" или "оптг" без явного "прицельный", отсекаем
        if not any(per in combined for per in ["прицельн", "визиограф", "радиовизиограф"]):
            return False

    has_periapical = any(kw in combined for kw in PERIAPICAL_KEYWORDS)
    return has_periapical


def has_clinical_doubt_heuristic(text: str = "") -> bool:
    """Проверяет, содержит ли вопрос врача клинические сомнения или просьбу проверить снимок."""
    if not text:
        return False
    t_lower = text.lower()
    return any(trig in t_lower for trig in DOUBT_TRIGGERS)


async def analyze_xray_context_and_doubt_async(
    user_text: str = "",
    vision_description: str = ""
) -> Dict[str, Any]:
    """
    Анализирует контекст запроса врача и описание снимка.
    Возвращает структурированный вердикт с фокусом внимания и рекомендованным квадрантом.
    """
    user_t = (user_text or "").strip()
    vis_desc = (vision_description or "").strip()

    # 1. Быстрая эвристическая проверка
    is_xray = is_periapical_xray_heuristic(user_t, vis_desc)
    has_doubt = has_clinical_doubt_heuristic(user_t)

    # Извлечение номера зуба (FDI)
    tooth_match = re.search(r'(?<!\d)([1-4][1-8])(?!\d)', f"{user_t} {vis_desc}")
    target_tooth = tooth_match.group(1) if tooth_match else None

    # Определение рекомендованного квадранта по паттернам патологии
    t_lower = user_t.lower()
    if any(k in t_lower for k in ["отломок", "обломок", "сломал", "сепарац", "ступен", "нависающ", "край"]):
        rec_quad = 4
        rec_text = "Используйте Квадрант 4 (Контурная резкость) для детальной оценки краев объекта, границ отломка и просвета канала."
        focus = f"Зуб {target_tooth or ''}: поиск отломка инструмента / микроструктурных дефектов канала".strip()
    elif any(k in t_lower for k in ["периодонтит", "очаг", "кист", "разрежени", "трещин", "перелом", "зажив"]):
        rec_quad = 3
        rec_text = "Используйте Квадрант 3 (Негатив), где плотная кость темнеет, а очаги разрежения и трещины светятся яркими белыми контурами."
        focus = f"Зуб {target_tooth or ''}: ревизия периапикальной зоны и плотности костной ткани".strip()
    else:
        rec_quad = 2
        rec_text = "Используйте Квадрант 2 (CLAHE) для выравнивания локального контраста и четкой визуализации анатомии каналов."
        focus = f"Зуб {target_tooth or ''}: рентгено-анатомический анализ корневых каналов".strip()

    result = {
        "is_periapical_xray": is_xray,
        "has_clinical_doubt": has_doubt,
        "should_generate_collage": is_xray,
        "target_tooth": target_tooth,
        "focus_area": focus,
        "recommended_quadrant": rec_quad,
        "recommendation_text": rec_text
    }

    return result


def format_xray_collage_caption(analysis: Dict[str, Any]) -> str:
    """Формирует профессиональную медицинскую подпись под 2x2 HD коллажем."""
    focus = analysis.get("focus_area") or "Комплексный рентгенологический аудит"
    rec_quad = analysis.get("recommended_quadrant", 2)
    rec_text = analysis.get("recommendation_text") or "Сверьтесь с квадрантами 2 и 4 для уточнения диагноза."

    quad_names = {
        1: "Квадрант 1 (Оригинал)",
        2: "Квадрант 2 (Адаптивный контраст CLAHE)",
        3: "Квадрант 3 (Негатив / Инверсия)",
        4: "Квадрант 4 (Контурный фильтр / Шарп)"
    }
    target_q_name = quad_names.get(rec_quad, f"Квадрант {rec_quad}")

    caption = (
        "🔬 <b>Рентген-фильтры 2х2 HD (Визиограф)</b>\n\n"
        f"🎯 <b>Фокус внимания:</b> {focus}\n"
        f"💡 <b>Рекомендация:</b> обратите внимание на <b>{target_q_name}</b> — {rec_text}\n\n"
        "<i>Сетка: 1 — Исходный | 2 — CLAHE | 3 — Негатив | 4 — Контурный шарп</i>"
    )
    return caption
