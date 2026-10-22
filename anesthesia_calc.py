# -*- coding: utf-8 -*-
"""
anesthesia_calc.py — Клинический калькулятор местных анестетиков и неотложной реанимации у кресла врача.

Функционал:
1. Расчет максимальных безопасных доз (мг/кг и абсолютные потолки) для Артикаина, Мепивакаина, Лидокаина, Бупивакаина.
2. Строгий контроль дозы вазоконстриктора (эпинефрина/адреналина): 0.2 мг (здоровые) vs 0.04 мг (кардиопатология).
3. Педиатрическая безопасность (Rule 12.1): артикаин противопоказан детям <4 лет (<15 кг); округление карпул строго ВНИЗ.
4. Устойчивый парсинг входных данных: поддержка чисел с запятой («18,5»), текстовых числительных («двадцать кг»), защита от 0/отрицательных значений.
5. Библиотека эталонных протоколов реанимации у кресла: анафилактический шок, LAST (интралипид 20%), коллапс, гипогликемия.
"""

from dataclasses import dataclass, field
import html
import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("anesthesia_calc")


# ==============================================================================
# 1. МОДЕЛИ ДАННЫХ
# ==============================================================================

@dataclass(frozen=True)
class LocalAnesthetic:
    key: str
    generic_name: str
    trade_names: List[str]
    concentration_percent: float
    vasoconstrictor: Optional[str]
    epinephrine_ratio: Optional[int]
    carpule_volume_ml: float
    mg_per_carpule: float
    mg_epinephrine_per_carpule: float
    max_mg_per_kg_adult: float
    max_mg_per_kg_child: float
    absolute_max_mg_adult: float
    min_age_years: float
    min_weight_kg: float
    contraindications: List[str]
    clinical_notes: str


@dataclass
class AnesthesiaCalculationResult:
    is_valid: bool
    error_message: Optional[str] = None
    is_pediatric: bool = False
    is_cardiac: bool = False
    contraindicated: bool = False
    contraindication_reason: Optional[str] = None
    weight_kg: float = 0.0
    drug: Optional[LocalAnesthetic] = None
    mg_per_kg_applied: float = 0.0
    raw_calc_mg: float = 0.0
    effective_max_mg: float = 0.0
    hit_mg_ceiling: bool = False
    ceiling_weight_threshold: float = 0.0
    max_carpules_exact: float = 0.0
    safe_carpules_by_drug: int = 0
    safe_carpules_by_epi: Optional[int] = None
    final_safe_carpules: int = 0
    epinephrine_total_mg: float = 0.0
    epinephrine_limit_mg: float = 0.20
    hit_epinephrine_ceiling: bool = False
    html_report: str = ""


@dataclass(frozen=True)
class EmergencyProtocol:
    key: str
    title: str
    short_indication: str
    primary_drugs: List[Tuple[str, str, str]]  # (Препарат, Дозировка, Путь введения)
    immediate_steps: List[str]
    contraindicated_actions: List[str]
    card_text: str


# ==============================================================================
# 2. КАТАЛОГ МЕСТНЫХ АНЕСТЕТИКОВ
# ==============================================================================

EPINEPHRINE_MAX_HEALTHY_MG: float = 0.20  # 200 мкг для соматически здоровых взрослых
EPINEPHRINE_MAX_CARDIAC_MG: float = 0.04  # 40 мкг строгий предел при кардиопатологии (ИБС, гипертензия, аритмия)

DRUG_CATALOG: Dict[str, LocalAnesthetic] = {
    "articaine_100": LocalAnesthetic(
        key="articaine_100",
        generic_name="Артикаин 4% с эпинефрином 1:100 000",
        trade_names=["Ультракаин Д-С форте", "Септонест 1:100 000", "Убистезин форте", "Брилокаин"],
        concentration_percent=4.0,
        vasoconstrictor="Эпинефрин 1:100 000",
        epinephrine_ratio=100000,
        carpule_volume_ml=1.7,
        mg_per_carpule=68.0,
        mg_epinephrine_per_carpule=0.017,
        max_mg_per_kg_adult=7.0,
        max_mg_per_kg_child=5.0,
        absolute_max_mg_adult=500.0,
        min_age_years=4.0,
        min_weight_kg=15.0,
        contraindications=[
            "Детский возраст до 4 лет (масса тела менее 15 кг)",
            "Тяжелые нарушения ритма сердца (пароксизмальная тахикардия, мерцательная аритмия)",
            "Неконтролируемая артериальная гипертензия",
            "Закрытоугольная глаукома",
            "Бронхиальная астма при гиперчувствительности к сульфитам"
        ],
        clinical_notes="Золотой стандарт обезболивания в стоматологии. Высокая диффузионная способность в кости. При кардиопатологии лимит эпинефрина (0.04 мг) ограничивает объем строго 2 карпулами!"
    ),
    "articaine_200": LocalAnesthetic(
        key="articaine_200",
        generic_name="Артикаин 4% с эпинефрином 1:200 000",
        trade_names=["Ультракаин Д-С", "Септонест 1:200 000", "Убистезин 1:200 000"],
        concentration_percent=4.0,
        vasoconstrictor="Эпинефрин 1:200 000",
        epinephrine_ratio=200000,
        carpule_volume_ml=1.7,
        mg_per_carpule=68.0,
        mg_epinephrine_per_carpule=0.0085,
        max_mg_per_kg_adult=7.0,
        max_mg_per_kg_child=5.0,
        absolute_max_mg_adult=500.0,
        min_age_years=4.0,
        min_weight_kg=15.0,
        contraindications=[
            "Детский возраст до 4 лет (масса тела менее 15 кг)",
            "Аллергия на сульфиты (антиоксидант вазоконстриктора)",
            "Тяжелая декомпенсированная сердечная недостаточность"
        ],
        clinical_notes="Препарат выбора артикаинового ряда для пациентов группы риска. Меньшая кардионагрузка: кардио-лимит эпинефрина (0.04 мг) допускает до 4 карпул."
    ),
    "mepivacaine_3": LocalAnesthetic(
        key="mepivacaine_3",
        generic_name="Мепивакаин 3% без вазоконстриктора",
        trade_names=["Скандонест 3%", "Мепивастезин", "Мепидонт", "Изокаин"],
        concentration_percent=3.0,
        vasoconstrictor=None,
        epinephrine_ratio=0,
        carpule_volume_ml=1.8,
        mg_per_carpule=54.0,
        mg_epinephrine_per_carpule=0.0,
        max_mg_per_kg_adult=4.4,
        max_mg_per_kg_child=4.4,
        absolute_max_mg_adult=400.0,
        min_age_years=0.0,
        min_weight_kg=3.0,
        contraindications=[
            "Тяжелая печеночная недостаточность (печеночный метаболизм)",
            "Миастения гравис",
            "Атриовентрикулярная блокада II-III степени без кардиостимулятора"
        ],
        clinical_notes="Абсолютный препарат выбора для кардиологических больных, пациентов с тиреотоксикозом, а также при аллергии на сульфиты. Не содержит эпинефрина и парабенов. Абсолютный максимум 400 мг (~7 карпул)."
    ),
    "lidocaine_2_epi": LocalAnesthetic(
        key="lidocaine_2_epi",
        generic_name="Лидокаин 2% с эпинефрином 1:100 000",
        trade_names=["Ксилокаин с адреналином", "Ксилонор", "Лидокаин-адреналин"],
        concentration_percent=2.0,
        vasoconstrictor="Эпинефрин 1:100 000",
        epinephrine_ratio=100000,
        carpule_volume_ml=1.8,
        mg_per_carpule=36.0,
        mg_epinephrine_per_carpule=0.018,
        max_mg_per_kg_adult=7.0,
        max_mg_per_kg_child=4.4,
        absolute_max_mg_adult=500.0,
        min_age_years=1.0,
        min_weight_kg=10.0,
        contraindications=[
            "Синдром слабости синусового узла, выраженная брадикардия",
            "АВ-блокада II-III степени",
            "Кардиогенный шок"
        ],
        clinical_notes="Классический амидный анестетик. За счет вазоконстриктора максимальная доза возрастает с 4.4 до 7.0 мг/кг (абсолютный максимум 500 мг). У кардиопациентов лимит эпинефрина — максимум 2 карпулы."
    ),
    "lidocaine_2_plain": LocalAnesthetic(
        key="lidocaine_2_plain",
        generic_name="Лидокаин 2% без вазоконстриктора (чистый)",
        trade_names=["Лидокаин 2%", "Ксилокаин"],
        concentration_percent=2.0,
        vasoconstrictor=None,
        epinephrine_ratio=0,
        carpule_volume_ml=1.8,
        mg_per_carpule=36.0,
        mg_epinephrine_per_carpule=0.0,
        max_mg_per_kg_adult=4.4,
        max_mg_per_kg_child=4.4,
        absolute_max_mg_adult=300.0,
        min_age_years=1.0,
        min_weight_kg=10.0,
        contraindications=[
            "АВ-блокада II-III степени",
            "Тяжелая брадикардия",
            "Острая сердечная недостаточность"
        ],
        clinical_notes="Без вазоконстриктора вызывает быструю вазодилатацию и всасывание в кровоток. Максимальная доза строго 4.4 мг/кг, абсолютный потолок 300 мг (≈ 8 карпул по 1.8 мл)."
    ),
    "bupivacaine_05": LocalAnesthetic(
        key="bupivacaine_05",
        generic_name="Бупивакаин 0.5% с эпинефрином 1:200 000",
        trade_names=["Маркаин", "Анекаин"],
        concentration_percent=0.5,
        vasoconstrictor="Эпинефрин 1:200 000",
        epinephrine_ratio=200000,
        carpule_volume_ml=1.8,
        mg_per_carpule=9.0,
        mg_epinephrine_per_carpule=0.009,
        max_mg_per_kg_adult=2.0,
        max_mg_per_kg_child=2.0,
        absolute_max_mg_adult=90.0,
        min_age_years=12.0,
        min_weight_kg=40.0,
        contraindications=[
            "Детский возраст до 12 лет (строго запрещен!)",
            "Высокая кардиотоксичность — риск фибрилляции желудочков и рефрактерной асистолии",
            "Внутривенное попадание категорически недопустимо"
        ],
        clinical_notes="Анестетик сверхдлительного действия (до 6-8 часов) для длительных хирургических вмешательств и постоперационной анальгезии. Выраженная кардиотоксичность: при случайном в/в введении антидот — Интралипид 20%."
    )
}


# ==============================================================================
# 3. ПАРСИНГ ТЕКСТА И ВХОДНЫХ ДАННЫХ
# ==============================================================================

_RUSSIAN_NUM_WORDS: Dict[str, int] = {
    "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "двести": 200
}


def parse_weight_from_text(text: str) -> Optional[float]:
    """
    Извлекает вес пациента из текста сообщения с поддержкой:
    - Десятичных дробей с запятой или точкой («18,5», «70.5 кг»).
    - Текстовых числительных («двадцать килограмм», «семьдесят пять кг»).
    - Контекстных конструкций («на 80», «вес 65», «весом 18.5»).
    - Защиты от отрицательных чисел и деления на ноль.
    """
    if not text:
        return None

    cleaned = text.lower().replace(',', '.')

    # 1. Точное совпадение: вес от 1 до 3 знаков и до 2 знаков дробной части (ReDoS-защита)
    m = re.search(r'(?<!\w)(-\d{1,3}(?:\.\d{1,2})?|\d{1,3}(?:\.\d{1,2})?)\s*(?:кг|kg|килограмм[а-яё]*|кило\b)\b', cleaned)
    if m:
        try:
            val = float(m.group(1))
            return val
        except ValueError:
            pass

    # 2. Контекстный предлог/маркер: «на 70», «для 80», «вес 65», «весом 18.5» (с поддержкой буквы «ё»)
    m_ctx = re.search(r'\b(?:вес[а-яё]*|на|для|масс[а-яё]*)\s+(-\d{1,3}(?:\.\d{1,2})?|\d{1,3}(?:\.\d{1,2})?)\b', cleaned)
    if m_ctx:
        try:
            val = float(m_ctx.group(1))
            return val
        except ValueError:
            pass

    # 3. Числительные словами перед единицами веса (с поддержкой буквы «ё»)
    tokens = re.findall(r'[а-яёa-z]+', cleaned)
    for i, tok in enumerate(tokens):
        if tok in ("кг", "kg", "килограмм", "килограмма", "килограммов", "кило"):
            acc = 0
            for j in range(max(0, i - 3), i):
                w = tokens[j]
                if w in _RUSSIAN_NUM_WORDS:
                    acc += _RUSSIAN_NUM_WORDS[w]
            if 3 <= acc <= 300:
                return float(acc)

    # 4. Фоллбек: изолированное число в разумных пределах веса [5, 250]
    for num_str in re.findall(r'\b\d+(?:\.\d+)?\b', cleaned):
        try:
            val = float(num_str)
            if 5.0 <= val <= 250.0:
                return val
        except ValueError:
            pass

    return None


def detect_anesthetic_drug(text: str) -> str:
    """Определяет запрошенный препарат анестетика (по умолчанию 'articaine_100')."""
    if not text:
        return "articaine_100"
    low = text.lower()

    if re.search(r'\b(?:мепивакаин|скандонест|мепивастезин|мепидонт|изокаин|mepivacaine)\w*', low):
        return "mepivacaine_3"
    elif re.search(r'\b(?:лидокаин|ксилокаин|ксилонор|lidocaine)\w*', low):
        if re.search(r'\b(?:чист\w*|без\s+адреналин\w*|без\s+эпинефрин\w*|без\s+вазо\w*)\b', low):
            return "lidocaine_2_plain"
        return "lidocaine_2_epi"
    elif re.search(r'\b(?:бупивакаин|маркаин|анекаин|bupivacaine)\w*', low):
        return "bupivacaine_05"
    elif re.search(r'\b(?:200|1:200|1:200000|1/200)\b', low):
        return "articaine_200"

    return "articaine_100"


def detect_cardiac_risk(text: str) -> bool:
    """Детектирует указания на сердечно-сосудистую патологию и необходимость кардио-лимита адреналина."""
    if not text:
        return False
    low = text.lower()
    return bool(re.search(
        r'\b(?:кардио\w*|сердечн\w*|гипертон\w*|давлен\w*|ибс\b|аритми\w*|стенокарди\w*|инфаркт\w*|'
        r'стент\w*|тахикарди\w*|брадикарди\w*|бета-блокатор\w*|кардиопатолог\w*|сердц\w*)\b',
        low
    ))


def detect_pediatric_context(text: str, weight: Optional[float] = None) -> bool:
    """Определяет, относится ли запрос к ребенку (по маркерам или весу < 40 кг)."""
    if weight is not None and weight < 40.0:
        return True
    if not text:
        return False
    low = text.lower()
    return bool(re.search(
        r'\b(?:ребен\w*|ребёнк\w*|детям\w*|детск\w*|малыш\w*|мальчик\w*|девочк\w*|педиатр\w*|года|лет)\b',
        low
    ))


# ==============================================================================
# 4. ЯДРО КЛИНИЧЕСКОГО РАСЧЕТА
# ==============================================================================

def calculate_anesthesia_dose(
    text_or_weight: Any,
    drug_key: Optional[str] = None,
    is_cardiac: Optional[bool] = None,
    is_child: Optional[bool] = None
) -> AnesthesiaCalculationResult:
    """
    Основная расчетная функция: выполняет клинический расчет безопасных доз местных анестетиков.
    Обеспечивает соблюдение строгих двойных потолков (мг/кг и абсолютный максимум в мг),
    кардиологических ограничений адреналина и педиатрической безопасности (округление строго ВНИЗ).
    """
    raw_text = str(text_or_weight) if isinstance(text_or_weight, str) else ""

    # Извлечение веса
    if isinstance(text_or_weight, (int, float)):
        weight = float(text_or_weight)
    else:
        weight = parse_weight_from_text(raw_text)

    if weight is None:
        return AnesthesiaCalculationResult(
            is_valid=False,
            error_message="⚠️ <i>Не удалось определить вес пациента. Укажите вес в кг (например: «артикаин на 70 кг» или «/anes 20»).</i>"
        )

    # Валидация веса (краевые случаи, 0, отрицательный, экстремальный)
    if weight <= 0:
        return AnesthesiaCalculationResult(
            is_valid=False,
            error_message="⚠️ <i>Клиническая ошибка: вес пациента должен быть строго положительным числом (> 0 кг).</i>"
        )
    if weight < 3.0:
        return AnesthesiaCalculationResult(
            is_valid=False,
            error_message=f"⛔ <i>Масса тела {weight:g} кг недопустима для амбулаторной стоматологической анестезии (минимальный вес 3 кг).</i>"
        )
    if weight > 300.0:
        return AnesthesiaCalculationResult(
            is_valid=False,
            error_message=f"⚠️ <i>Указан нереалистичный вес ({weight:g} кг). Допустимый диапазон: 3–300 кг.</i>"
        )

    # Определение соматического статуса
    cardiac = is_cardiac if is_cardiac is not None else detect_cardiac_risk(raw_text)
    pediatric = is_child if is_child is not None else detect_pediatric_context(raw_text, weight)

    # Определение препарата
    selected_key = drug_key or detect_anesthetic_drug(raw_text)
    drug = DRUG_CATALOG.get(selected_key, DRUG_CATALOG["articaine_100"])

    # ── ПЕДИАТРИЧЕСКИЙ СТОП-ФАКТОР (МЗ РФ) ──
    # Артикаин строго противопоказан детям < 4 лет / < 15 кг
    if "articaine" in drug.key and weight < 15.0:
        contra_msg = (
            f"⛔ <b>ПЕДИАТРИЧЕСКАЯ БЕЗОПАСНОСТЬ: АРТИКАИН КАТЕГОРИЧЕСКИ ПРОТИВОПОКАЗАН!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Пациент:</b> Вес {weight:g} кг (возраст < 4 лет).\n"
            f"• Согласно официальной инструкции МЗ РФ, 4% артикаин (Ультракаин, Септонест, Убистезин) "
            f"<b>противопоказан детям до 4 лет (масса тела менее 15 кг)</b>!\n"
            f"• Даже 1 стандартная карпула 1.7 мл (68 мг) превышает допустимый предел ({weight * 5.0:.1f} мг)!\n\n"
            f"💊 <b>Препараты выбора у детей раннего возраста:</b>\n"
            f"• <b>Мепивакаин 3% без вазоконстриктора:</b> норма 4.4 мг/кг → предел <b>{weight * 4.4:.1f} мг</b> "
            f"(максимум <b>{math.floor((weight * 4.4) / 54.0)} карпул</b>).\n"
            f"⚠️ <i>Округление доз у детей — строго ВНИЗ к меньшему целому!</i>"
        )
        return AnesthesiaCalculationResult(
            is_valid=True,
            contraindicated=True,
            contraindication_reason="Артикаин противопоказан детям до 4 лет (< 15 кг)",
            is_pediatric=True,
            weight_kg=weight,
            drug=drug,
            html_report=contra_msg
        )

    # Бупивакаин противопоказан детям < 12 лет / < 40 кг
    if drug.key == "bupivacaine_05" and (weight < 40.0 or pediatric):
        contra_msg = (
            f"⛔ <b>ПРОТИВОПОКАЗАНИЕ: БУПИВАКАИН (МАРКАИН) ДЕТЯМ ЗАПРЕЩЕН!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Бупивакаин строго противопоказан детям до 12 лет из-за высокой кардиотоксичности.\n"
            f"• Препарат выбора: Мепивакаин 3% или Лидокаин 2%."
        )
        return AnesthesiaCalculationResult(
            is_valid=True,
            contraindicated=True,
            contraindication_reason="Бупивакаин противопоказан детям до 12 лет",
            is_pediatric=True,
            weight_kg=weight,
            drug=drug,
            html_report=contra_msg
        )

    # ── МАТЕМАТИЧЕСКИЙ РАСЧЕТ ДОЗЫ АНЕСТЕТИКА ──
    mg_per_kg = drug.max_mg_per_kg_child if pediatric else drug.max_mg_per_kg_adult
    raw_calc_mg = weight * mg_per_kg
    effective_max_mg = min(raw_calc_mg, drug.absolute_max_mg_adult)
    hit_ceiling = (raw_calc_mg >= drug.absolute_max_mg_adult)
    ceiling_weight_threshold = drug.absolute_max_mg_adult / mg_per_kg

    max_carpules_exact = effective_max_mg / drug.mg_per_carpule
    safe_carpules_by_drug = math.floor(max_carpules_exact)

    # ── РАСЧЕТ ВАЗОКОНСТРИКТОРА (ЭПИНЕФРИНА) ──
    epi_limit_mg = EPINEPHRINE_MAX_CARDIAC_MG if cardiac else EPINEPHRINE_MAX_HEALTHY_MG
    safe_carpules_by_epi = None
    hit_epi_ceiling = False

    if drug.mg_epinephrine_per_carpule > 0:
        safe_carpules_by_epi = math.floor(epi_limit_mg / drug.mg_epinephrine_per_carpule)
        final_safe_carpules = min(safe_carpules_by_drug, safe_carpules_by_epi)
        if safe_carpules_by_epi < safe_carpules_by_drug:
            hit_epi_ceiling = True
    else:
        final_safe_carpules = safe_carpules_by_drug

    epinephrine_total_mg = final_safe_carpules * drug.mg_epinephrine_per_carpule

    # Формирование отчета HTML
    report_lines = []
    category_title = "Ребёнок" if pediatric else "Взрослый"
    cardio_badge = " ❤️ <b>[КАРДИОПАТОЛОГИЯ: лимит адреналина 0.04 мг]</b>" if cardiac else ""

    report_lines.append(f"🧮 <b>Клинический расчет анестезии: {drug.generic_name}</b>{cardio_badge}")
    report_lines.append("━━━━━━━━━━━━━━━━━━━━━")
    report_lines.append(f"👤 <b>Пациент:</b> {category_title}, масса тела <b>{weight:g} кг</b>")
    report_lines.append(f"📏 <b>Норма расчета:</b> {mg_per_kg:g} мг/кг (абсолютный потолок: {drug.absolute_max_mg_adult:g} мг)\n")

    report_lines.append("📊 <b>Расчет действующего вещества:</b>")
    report_lines.append(f"• По массе тела ({weight:g} кг × {mg_per_kg:g} мг/кг) = <b>{raw_calc_mg:g} мг</b>")

    if hit_ceiling:
        report_lines.append(
            f"• ⚠️ <b>Сработал абсолютный потолок {drug.absolute_max_mg_adult:g} мг</b> "
            f"(для веса ≥ {ceiling_weight_threshold:.1f} кг)."
        )
    report_lines.append(f"• <b>Предельно допустимая доза:</b> <b>{effective_max_mg:g} мг</b>\n")

    # Склонение слова карпула после предлога "до"
    rem100 = final_safe_carpules % 100
    rem10 = final_safe_carpules % 10
    if rem100 in (11, 12, 13, 14):
        carp_declension = f"{final_safe_carpules} карпул"
    elif rem10 == 1:
        carp_declension = f"{final_safe_carpules} карпулы"
    else:
        carp_declension = f"{final_safe_carpules} карпул"

    report_lines.append(f"💉 <b>Допустимое количество карпул (по {drug.carpule_volume_ml:g} мл = {drug.mg_per_carpule:g} мг):</b>")
    report_lines.append(f"• Математический максимум: <code>{effective_max_mg:g} / {drug.mg_per_carpule:g}</code> = <b>{max_carpules_exact:.2f} карпул</b>")
    report_lines.append(f"• 🛡 <b>Безопасный клинический максимум:</b> <b>до {carp_declension}</b> ({final_safe_carpules * drug.mg_per_carpule:g} мг)")

    if drug.mg_epinephrine_per_carpule > 0:
        report_lines.append("")
        report_lines.append("⚡ <b>Контроль вазоконстриктора (Адреналин / Эпинефрин):</b>")
        report_lines.append(f"• В 1 карпуле: <b>{drug.mg_epinephrine_per_carpule * 1000:g} мкг</b> ({drug.vasoconstrictor})")
        report_lines.append(f"• Потолок адреналина: <b>{epi_limit_mg * 1000:g} мкг</b> ({'Кардио-пациент' if cardiac else 'Здоровый взрослый'})")
        if hit_epi_ceiling:
            report_lines.append(
                f"• ⚠️ <b>ВНИМАНИЕ!</b> Доза ограничена вазоконстриктором: по веществу разрешено "
                f"{safe_carpules_by_drug} карп., но по адреналину — <b>СТРОГО НЕ БОЛЕЕ {final_safe_carpules} КАРПУЛ!</b>"
            )
        else:
            report_lines.append(f"• Суммарно в {final_safe_carpules} карп.: <b>{epinephrine_total_mg * 1000:g} мкг</b> (в пределах нормы)")

    report_lines.append("")
    if pediatric:
        report_lines.append("⚠️ <i>В педиатрии округление карпул строго ВНИЗ к меньшему целому!</i>")
    elif cardiac:
        report_lines.append("⚠️ <i>У кардиологических пациентов препарат первого выбора — Мепивакаин 3% без вазоконстриктора!</i>")
    else:
        report_lines.append("💡 <i>При длительных вмешательствах или неэффективности блокады не превышайте рассчитанный потолок.</i>")

    html_report = "\n".join(report_lines)

    return AnesthesiaCalculationResult(
        is_valid=True,
        is_pediatric=pediatric,
        is_cardiac=cardiac,
        weight_kg=weight,
        drug=drug,
        mg_per_kg_applied=mg_per_kg,
        raw_calc_mg=raw_calc_mg,
        effective_max_mg=effective_max_mg,
        hit_mg_ceiling=hit_ceiling,
        ceiling_weight_threshold=ceiling_weight_threshold,
        max_carpules_exact=max_carpules_exact,
        safe_carpules_by_drug=safe_carpules_by_drug,
        safe_carpules_by_epi=safe_carpules_by_epi,
        final_safe_carpules=final_safe_carpules,
        epinephrine_total_mg=epinephrine_total_mg,
        epinephrine_limit_mg=epi_limit_mg,
        hit_epinephrine_ceiling=hit_epi_ceiling,
        html_report=html_report
    )


def calculate_anesthesia_quick_card(weight_kg: float, is_cardiac: bool = False) -> str:
    """
    Формирует сводную сравнительную карточку по всем трем основным анестетикам
    (Артикаин 4%, Мепивакаин 3%, Лидокаин 2%) для команды /anes [вес].
    """
    res_art = calculate_anesthesia_dose(weight_kg, "articaine_100", is_cardiac=is_cardiac)
    if res_art.contraindicated:
        return res_art.html_report

    res_mepi = calculate_anesthesia_dose(weight_kg, "mepivacaine_3", is_cardiac=is_cardiac)
    res_lido = calculate_anesthesia_dose(weight_kg, "lidocaine_2_epi", is_cardiac=is_cardiac)

    lines = [
        f"💉 <b>Сводный расчет предельных доз анестезии (Вес: {weight_kg:.1f} кг)</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        "• <b>4% Артикаин (Ультракаин / Септонест / Убистезин 1:100 000):</b>",
        f"  — Потолок дозы: <b>{int(res_art.effective_max_mg)} мг</b> ({res_art.mg_per_kg_applied:g} мг/кг, макс 500 мг)",
        f"  — Максимум карпул 1.7 мл: <b>{res_art.final_safe_carpules} карп.</b>" + (" (лимит адреналина!)" if res_art.hit_epinephrine_ceiling else ""),
        "",
        "• <b>3% Мепивакаин БЕЗ адреналина (Скандонест 3%):</b>",
        f"  — Потолок дозы: <b>{int(res_mepi.effective_max_mg)} мг</b> (4.4 мг/кг, макс 400 мг)",
        f"  — Максимум карпул 1.8 мл: <b>{res_mepi.final_safe_carpules} карп.</b> (54 мг/карп)",
        "  — <i>Идеален для кардиопатологии, пожилых и аллергии на сульфиты</i>",
        "",
        "• <b>2% Лидокаин с адреналином 1:100 000:</b>",
        f"  — Потолок дозы: <b>{int(res_lido.effective_max_mg)} мг</b> ({res_lido.mg_per_kg_applied:g} мг/кг, макс 500 мг)",
        f"  — Максимум карпул 1.8 мл: <b>{res_lido.final_safe_carpules} карп.</b>" + (" (лимит адреналина!)" if res_lido.hit_epinephrine_ceiling else ""),
        "",
        "⚠️ <b>Лимит адреналина при кардиопатологии (ИБС, аритмия, гипертензия II-III ст):</b>",
        "• Кардио-лимит эпинефрина: строго <b>0.04 мг</b> (40 мкг)!",
        "  — Адреналин 1:100 000 (0.017-0.018 мг/карп) → <b>СТРОГО ДО 2 КАРПУЛ</b>",
        "  — Адреналин 1:200 000 (0.0085-0.009 мг/карп) → <b>СТРОГО ДО 4 КАРПУЛ</b>",
        "",
        "💡 <i>Если анестезия не берет при горячем пульпите — выполните буферизацию 8.4% NaHCO3 (/anes сода) или блокаду Гоу-Гейтса (/anes гоу-гейтс).</i>"
    ]
    return "\n".join(lines)


# ==============================================================================
# 5. ЭКСТРЕННАЯ РЕАНИМАЦИЯ У КРЕСЛА (SOS RESUSCITATION)
# ==============================================================================

EMERGENCY_PROTOCOLS: Dict[str, EmergencyProtocol] = {
    "anaphylaxis": EmergencyProtocol(
        key="anaphylaxis",
        title="🚨 SOS-Протокол: Анафилактический шок в стоматологическом кресле",
        short_indication="Острая генерализованная реакция: падение АД, стридор, ларингоспазм, крапивница, отек Квинке после инъекции.",
        primary_drugs=[
            ("Адреналин (Эпинефрин 0.1% 1:1000, 1 мг/мл)", "0.3–0.5 мг (0.3–0.5 мл)", "В/м строго в среднюю треть переднебоковой поверхности бедра"),
            ("Кислород 100%", "10–15 л/мин", "Лицевая маска с резервуаром"),
            ("Дексаметазон (препарат 2-й линии)", "8–16 мг", "В/в струйно или в/м (не заменяет адреналин!)"),
        ],
        immediate_steps=[
            "1. СТОП: Немедленно прекратить введение любого препарата!",
            "2. Вызвать СМП / реанимационную бригаду (112 / 103).",
            "3. АДРЕНАЛИН 0.3–0.5 мл 0.1% раствора внутримышечно в среднюю треть переднебоковой поверхности бедра (детям 0.01 мг/кг, макс 0.3 мг).",
            "4. Уложить пациента на спину с приподнятыми ногами (положение Тренделенбурга на 20-30°). При одышке/стридоре — полусидячее.",
            "5. Ингаляция 100% кислорода через маску с резервуаром (10–15 л/мин).",
            "6. При отсутствии эффекта через 5–10 минут: ПОВТОРИТЬ адреналин 0.3–0.5 мл в/м в другое бедро!",
            "7. Мониторинг: АД, ЧСС, SpO2 каждые 2–3 минуты до приезда СМП."
        ],
        contraindicated_actions=[
            "⛔ ЗАПРЕЩЕНО сажать или ставить пациента на ноги (риск синдрома пустой полой вены и мгновенной асистолии)!",
            "⛔ ЗАПРЕЩЕНО вводить адреналин подкожно или в десну (медленное и непредсказуемое всасывание)!",
            "⛔ ЗАПРЕЩЕНО начинать помощь с антигистаминных или гормонов (они действуют медленно и не спасают от асфиксии и коллапса)!"
        ],
        card_text=(
            "🚨 <b>SOS-Протокол: Анафилактический шок у кресла врача</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚡ Протокол 1-й минуты (Алгоритм спасения жизни):</b>\n"
            "1. <b>СТОП:</b> Немедленно прекратить введение анестетика/препарата! Позвать ассистента, вызвать СМП (112).\n"
            "2. 💉 <b>ПРЕПАРАТ №1 — АДРЕНАЛИН (Эпинефрин 0.1% 1:1000):</b>\n"
            "   • Взрослым: <b>0.3 – 0.5 мл (0.3–0.5 мг) внутримышечно</b> в среднюю треть переднебоковой поверхности бедра.\n"
            "   • Детям: <b>0.01 мг/кг</b> (максимум 0.3 мг) в/м в бедро.\n"
            "   • <i>При сохранении шока через 5–10 мин — повторить инъекцию в другое бедро!</i>\n"
            "3. <b>Положение пациента:</b> Горизонтально на спину, ноги приподнять на 20–30° (аутотрансфузия 500 мл крови). При стридоре/удушье — полусидя.\n"
            "4. <b>Оксигенотерапия:</b> 100% кислород через маску с мешком 10–15 л/мин.\n"
            "5. <b>Вторая линия (после адреналина):</b> Дексаметазон 8–16 мг в/м или Преднизолон 90–120 мг в/в.\n\n"
            "⛔ <b>КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО:</b> Сажать пациента, колоть адреналин подкожно в руку/десну, терять время на Супрастин!"
        )
    ),
    "last": EmergencyProtocol(
        key="last",
        title="🚨 SOS-Протокол: LAST — Системная токсичность местных анестетиков (ERC / ASRA)",
        short_indication="Внутрисосудистое попадание или передозировка анестетика: онемение губ, металл во рту, судороги, блокада, асистолия.",
        primary_drugs=[
            ("Липидная эмульсия 20% (Intralipid / Lipofundin 20%)", "Болюс 1.5 мл/кг за 2-3 мин, затем инфузия 0.25 мл/кг/мин", "В/в капельно/струйно"),
            ("Диазепам (при судорогах)", "5–10 мг", "В/в медленно"),
            ("Адреналин (только при СЛР)", "Малые титрованные дозы < 1 мкг/кг", "В/в строго титрованно"),
        ],
        immediate_steps=[
            "1. Прекратить инъекцию анестетика при первых же симптомах (звон в ушах, металл во рту, диплопия).",
            "2. Вызвать реанимационную бригаду СМП (112 / 103).",
            "3. Вентиляция 100% кислородом (маска/мешок Амбу). Предотвратить гиперкапнию и ацидоз!",
            "4. Купирование судорог: Диазепам 5–10 мг в/в или Мидазолам 2.5–5 мг в/м.",
            "5. АНТИДОТ: Немедленное введение Липидной эмульсии 20% (Intralipid):",
            "   • Начальный болюс: 1.5 мл/кг за 2–3 минуты (~100 мл на пациента 70 кг).",
            "   • Постоянная инфузия: 0.25 мл/кг/мин (~1000 мл/час) до стабилизации.",
            "   • При сохранении нестабильности гемодинамики: повторить болюс через 3–5 минут."
        ],
        contraindicated_actions=[
            "⛔ ЛИДОКАИН И ДРУГИЕ АНТИАРИТМИКИ IB КЛАССА КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ (усугубляют блокаду)!",
            "⛔ БЛОКАТОРЫ КАЛЬЦИЕВЫХ КАНАЛОВ И БЕТА-БЛОКАТОРЫ ЗАПРЕЩЕНЫ (вызывают фатальный коллапс)!",
            "⛔ СТАНДАРТНЫЕ БОЛЬШИЕ ДОЗЫ АДРЕНАЛИНА (1 мг) ЗАПРЕЩЕНЫ — только малые титрованные дозы < 1 мкг/кг!"
        ],
        card_text=(
            "🚨 <b>SOS: LAST — Системная токсичность местных анестетиков (ERC / ASRA)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚡ 1. Продромальные симптомы (первые 30–60 сек):</b>\n"
            "• Металлический привкус, онемение языка/губ, шум в ушах, тревога, нистагм.\n"
            "• ЦНС-фаза: мышечные подергивания → генерализованные судороги → кома.\n"
            "• Сердечно-сосудистая фаза: брадикардия → расширение QRS → фибрилляция / асистолия.\n\n"
            "<b>💉 2. Неотложный протокол у кресла:</b>\n"
            "1. <b>СТОП инъекция!</b> Вызвать реанимацию СМП (112).\n"
            "2. <b>100% O2</b> через маску, мешок Амбу, гипервентиляция (ацидоз усиливает токсичность!).\n"
            "3. <b>Судороги:</b> Диазепам 5–10 мг в/в или Мидазолам 2.5–5 мг в/м.\n"
            "4. 🧪 <b>ЛИПИДНАЯ РЕАНИМАЦИЯ (Lipid Rescue — Интралипид 20%):</b>\n"
            "   • Болюс: <b>1.5 мл/кг</b> в/в струйно за 2–3 мин (~100 мл на 70 кг).\n"
            "   • Инфузия: <b>0.25 мл/кг/мин</b> (до восстановления стабильного ритма, макс 10–12 мл/кг).\n"
            "   • Повторный болюс через 3–5 мин при рецидиве аритмии/коллапса.\n"
            "5. <b>СЛР при остановке сердца:</b> Адреналин только малыми дозами (< 1 мкг/кг).\n"
            "⛔ <b>СТРОГО ЗАПРЕЩЕНЫ:</b> Лидокаин, амиодарон, пропофол при шоке, бета-блокаторы!"
        )
    ),
    "syncope_collapse": EmergencyProtocol(
        key="syncope_collapse",
        title="🚨 SOS-Протокол: Вазовагальный обморок и сосудистый коллапс",
        short_indication="Внезапная бледность, липкий холодный пот, головокружение, брадикардия, кратковременная потеря сознания.",
        primary_drugs=[
            ("Раствор аммиака 10% (Нашатырный спирт)", "Пары на марле", "Ингаляционно на расстоянии 5 см"),
            ("Кислород", "4–6 л/мин", "Лицевая маска"),
            ("Атропин 0.1% (при стойкой брадикардии < 40 уд/мин)", "0.5–1.0 мл", "В/в или п/к"),
        ],
        immediate_steps=[
            "1. Немедленно опустить кресло в горизонтальное положение с приподнятыми ногами (Тренделенбург).",
            "2. Расстегнуть сдавливающую одежду (галстук, воротник, ремень), обеспечить приток свежего воздуха.",
            "3. Поднести к носу ватку с парами нашатырного спирта на расстояние 5–7 см (рефлекторная стимуляция сосудодвигательного центра).",
            "4. Оценить пульс на сонной артерии и артериальное давление.",
            "5. Сбрызнуть лицо холодной водой, положить холодное влажное полотенце на лоб.",
            "6. При отсутствии восстановления сознания более 2–3 минут — дифференцировать с инсультом, гипогликемией, вызвать СМП (112)."
        ],
        contraindicated_actions=[
            "⛔ ЗАПРЕЩЕНО удерживать пациента в вертикальном или сидячем положении (усугубляет ишемию мозга)!",
            "⛔ ЗАПРЕЩЕНО подносить концентрированный нашатырный спирт вплотную к ноздрям (риск химического ожога и рефлекторного апноэ)!"
        ],
        card_text=(
            "🚨 <b>SOS: Вазовагальный обморок и сосудистый коллапс у кресла</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚡ Алгоритм неотложных действий:</b>\n"
            "1. <b>Кресло горизонтально:</b> Положение Тренделенбурга (голова ниже уровня ног, ноги приподнять на 20–30°).\n"
            "2. <b>Дыхание:</b> Расстегнуть воротник, освободить дыхательные пути, подача 100% O2.\n"
            "3. <b>Стимуляция:</b> Ватка с парами нашатырного спирта (раствор аммиака 10%) на расстоянии 5 см от носа.\n"
            "4. <b>Контроль витальных функций:</b> Пульс на сонной артерии, АД. При брадикардии < 40 уд/мин — вызвать СМП.\n"
            "5. При выходе из обморока не поднимать пациента резко минимум 15–20 минут."
        )
    ),
    "hypoglycemia": EmergencyProtocol(
        key="hypoglycemia",
        title="🚨 SOS-Протокол: Гипогликемический криз / кома у кресла",
        short_indication="Холодный липкий пот, мелкий тремор рук, чувство волчьего голода, спутанность сознания, судороги у пациента с диабетом.",
        primary_drugs=[
            ("Быстрые углеводы (при сохраненном сознании)", "15–20 г глюкозы / сахара", "Per os (теплый сладкий чай, сок)"),
            ("Глюкоза 40% (при угнетении сознания)", "40–60 мл", "В/в струйно"),
            ("Глюкагон 1 мг", "1 мг", "В/м или п/к"),
        ],
        immediate_steps=[
            "1. Прекратить лечение, извлечь все инструменты и изоляцию изо рта.",
            "2. Оценить уровень сознания и гликемию (глюкометром, если имеется).",
            "3. Пациент В СОЗНАНИИ: дать выпить 15–20 г легкоусвояемых углеводов (теплый чай с 4 ложками сахара, 200 мл фруктового сока).",
            "4. Пациент БЕЗ СОЗНАНИЯ: КАТЕГОРИЧЕСКИ НЕ ПОИТЬ! Повернуть на бок (профилактика аспирации рвотных масс).",
            "5. Ввести 40% раствор глюкозы 40–60 мл внутривенно струйно (или Глюкагон 1 мг внутримышечно).",
            "6. Вызвать скорую медицинскую помощь (112)."
        ],
        contraindicated_actions=[
            "⛔ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО вливать жидкости и сладкий чай в рот пациенту без сознания (смертельный риск аспирации и асфиксии)!",
            "⛔ ЗАПРЕЩЕНО вводить инсулин без точного подтверждения гипергликемии глюкометром!"
        ],
        card_text=(
            "🚨 <b>SOS: Гипогликемический криз / кома у пациента в кресле</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚡ Алгоритм действий:</b>\n"
            "1. <b>Если пациент В СОЗНАНИИ:</b>\n"
            "   • Дать 15–20 г быстрых углеводов: 200 мл фруктового сока или теплый чай с 4 ложками сахара.\n"
            "   • Ожидать улучшения 10–15 минут, повторить при необходимости.\n\n"
            "2. <b>Если пациент БЕЗ СОЗНАНИЯ:</b>\n"
            "   • ⛔ <b>В РОТ НИЧЕГО НЕ ЛИТЬ!</b> Риск фатальной аспирации.\n"
            "   • Повернуть голову/пациента на бок, очистить ротоглотку.\n"
            "   • Внутривенно струйно ввести <b>40% глюкозу 40–60 мл</b> (или Глюкагон 1 мг в/м).\n"
            "   • Немедленный вызов СМП (112)."
        )
    ),
    "hypertensive_crisis": EmergencyProtocol(
        key="hypertensive_crisis",
        title="🚨 SOS-Протокол: Гипертонический криз у кресла (АД ≥ 180/110 мм рт. ст.)",
        short_indication="Резкий подъем АД, распирающая головная боль в затылке, мелькание «мушек» перед глазами, шум в ушах, боль за грудиной.",
        primary_drugs=[
            ("Моксонидин (Физиотенз 0.2–0.4 мг)", "0.2 мг", "Сублингвально (под язык)"),
            ("Каптоприл (Капотен 25 мг)", "25 мг", "Сублингвально (под язык)"),
            ("Нитроглицерин 0.5 мг (при болях за грудиной)", "1 таб или 1 доза спрея", "Сублингвально при САД > 100 мм рт. ст."),
        ],
        immediate_steps=[
            "1. Немедленно прекратить вмешательство, аспирировать слюну, извлечь коффердам/инструменты.",
            "2. Придать пациенту полусидячее положение (уменьшает венозный приток к сердцу и снижает давление в малом круге).",
            "3. Измерить артериальное давление и пульс на обеих руках.",
            "4. Дать под язык таблетку Моксонидина 0.2 мг (или Каптоприл 25 мг) для рассасывания.",
            "5. При давящих болях за грудиной или одышке — дать Нитроглицерин 0.5 мг под язык (только при САД > 100 мм рт. ст.) и вызвать СМП.",
            "6. Контроль АД через 15–20 минут (снижение должно быть плавным, не более чем на 20-25% от исходного за первый час!)."
        ],
        contraindicated_actions=[
            "⛔ ЗАПРЕЩЕНО резко снижать АД внутривенными гипотензивными препаратами (риск ишемического инсульта головного мозга)!",
            "⛔ ЗАПРЕЩЕНО укладывать пациента горизонтально (усиливает отек мозга и левожелудочковую недостаточность)!"
        ],
        card_text=(
            "🚨 <b>SOS: Гипертонический криз в кресле (АД > 180/110)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚡ Алгоритм купирования:</b>\n"
            "1. <b>СТОП лечение:</b> Полусидячее положение в кресле (ноги опущены).\n"
            "2. <b>Фармакотерапия у кресла:</b>\n"
            "   • <b>Моксонидин 0.2 мг</b> или <b>Каптоприл 25 мг</b> — под язык до полного рассасывания.\n"
            "   • При давящей боли за грудиной: <b>Нитроглицерин 0.5 мг</b> сублингвально (при САД > 100).\n"
            "3. <b>Контроль:</b> Измерение АД каждые 10 мин. Плавное снижение (не более чем на 20% за первый час).\n"
            "4. При отсутствии эффекта, загрудинной боли или очаговой неврологии — экстренный вызов СМП (112)."
        )
    )
}


def get_emergency_protocol(key: str) -> Optional[EmergencyProtocol]:
    """Возвращает протокол неотложной помощи по ключу."""
    return EMERGENCY_PROTOCOLS.get(key.lower().strip())
