# -*- coding: utf-8 -*-
"""
clinical_anatomy_guard.py — Защитный модуль анатомических инвариантов и ориентации корней зубов (FDI 11–48).

Назначение:
Детерминированное предотвращение клинических галлюцинаций в генерациях LLM и Vision-моделей:
1. Запрет «небного корня» (palatal root), «небного канала» и «небной поверхности/стенки»
   для зубов нижней челюсти (Mandible: 31–48, 71–85). На нижней челюсти нет нёба!
   Корни нижних моляров: медиальный (MB, ML, MM) и дистальный (D, DB, DL) + Radix Entomolaris/Paramolaris.
   Поверхность: строго ЯЗЫЧНАЯ (lingual).
2. Запрет «язычного корня» (lingual root), «язычного канала» и «язычной поверхности/стенки»
   для зубов верхней челюсти (Maxilla: 11–28, 51–65).
   Корни верхних моляров: мезиально-щечный (MB), дистально-щечный (DB) и НЕБНЫЙ (P).
   Поверхность: строго НЕБНАЯ (palatal).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Set


@dataclass(frozen=True)
class ToothAnatomyInvariant:
    """Анатомический паспорт зуба по международной двухцифровой системе FDI (ISO 3950)."""
    fdi: int
    name_ru: str
    jaw: str  # "maxilla" (верхняя челюсть) | "mandible" (нижняя челюсть)
    quadrant: int  # 1..4 (постоянные), 5..8 (временные)
    side: str  # "right" | "left" (сторона пациента)
    group: str  # "molar" | "premolar" | "canine" | "incisor"
    permissible_roots: tuple[str, ...]
    permissible_canals: tuple[str, ...]
    permissible_surfaces: tuple[str, ...]


def _build_fdi_invariants() -> Dict[int, ToothAnatomyInvariant]:
    """Строит полную карту анатомических инвариантов постоянных (11–48) и временных (51–85) зубов."""
    invariants: Dict[int, ToothAnatomyInvariant] = {}
    
    # ── Квадрант 1: Верхний правый (18–11) ── Maxilla
    # ── Квадрант 2: Верхний левый (21–28) ── Maxilla
    for fdi in range(11, 29):
        if fdi in (19, 20):
            continue
        quadrant = 1 if fdi <= 18 else 2
        side = "right" if quadrant == 1 else "left"
        tooth_pos = fdi % 10
        
        if tooth_pos in (6, 7, 8):
            group = "molar"
            name_ru = f"Верхний {'правый' if quadrant == 1 else 'левый'} {tooth_pos-5}-й моляр ({fdi})"
            roots = ("мезиально-щечный (MB)", "дистально-щечный (DB)", "небный (P)")
            canals = ("MB1", "MB2", "DB", "P (небный)")
            surfaces = ("вестибулярная (щечная)", "небная (palatal)", "окклюзионная", "мезиальная", "дистальная")
        elif tooth_pos in (4, 5):
            group = "premolar"
            name_ru = f"Верхний {'правый' if quadrant == 1 else 'левый'} {tooth_pos-3}-й премоляр ({fdi})"
            roots = ("щечный (B)", "небный (P)") if tooth_pos == 4 else ("одиночный корень", "щечный", "небный")
            canals = ("щечный", "небный")
            surfaces = ("вестибулярная (щечная)", "небная (palatal)", "окклюзионная", "мезиальная", "дистальная")
        elif tooth_pos == 3:
            group = "canine"
            name_ru = f"Верхний {'правый' if quadrant == 1 else 'левый'} клык ({fdi})"
            roots = ("одиночный массивный корень",)
            canals = ("1 канал",)
            surfaces = ("вестибулярная (губная)", "небная (palatal)", "режущий край", "мезиальная", "дистальная")
        else:  # 1, 2
            group = "incisor"
            name_ru = f"Верхний {'правый' if quadrant == 1 else 'левый'} {'центральный' if tooth_pos == 1 else 'латеральный'} резец ({fdi})"
            roots = ("1 корень",)
            canals = ("1 канал",)
            surfaces = ("вестибулярная (губная)", "небная (palatal)", "режущий край", "мезиальная", "дистальная")
            
        invariants[fdi] = ToothAnatomyInvariant(
            fdi=fdi,
            name_ru=name_ru,
            jaw="maxilla",
            quadrant=quadrant,
            side=side,
            group=group,
            permissible_roots=roots,
            permissible_canals=canals,
            permissible_surfaces=surfaces,
        )

    # ── Квадрант 3: Нижний левый (31–38) ── Mandible
    # ── Квадрант 4: Нижний правый (41–48) ── Mandible
    for fdi in range(31, 49):
        if fdi in (39, 40):
            continue
        quadrant = 3 if fdi <= 38 else 4
        side = "left" if quadrant == 3 else "right"
        tooth_pos = fdi % 10
        
        if tooth_pos in (6, 7, 8):
            group = "molar"
            name_ru = f"Нижний {'левый' if quadrant == 3 else 'правый'} {tooth_pos-5}-й моляр ({fdi})"
            roots = (
                "медиальный (мезиальный / M)",
                "дистальный (D)",
                "Radix Entomolaris (доп. дистолингвальный)",
                "Radix Paramolaris (доп. мезиобуккальный)"
            )
            canals = (
                "MB (медиально-щечный)",
                "ML (медиально-язычный)",
                "MM (средний мезиальный)",
                "D (дистальный)",
                "DB (дистально-щечный)",
                "DL (дистально-язычный)"
            )
            surfaces = ("вестибулярная (щечная)", "язычная (lingual)", "окклюзионная", "мезиальная", "дистальная")
        elif tooth_pos in (4, 5):
            group = "premolar"
            name_ru = f"Нижний {'левый' if quadrant == 3 else 'правый'} {tooth_pos-3}-й премоляр ({fdi})"
            roots = ("1 корень", "щечная и язычная верхушки")
            canals = ("1-2 канала (щечный, язычный)",)
            surfaces = ("вестибулярная (щечная)", "язычная (lingual)", "окклюзионная", "мезиальная", "дистальная")
        elif tooth_pos == 3:
            group = "canine"
            name_ru = f"Нижний {'левый' if quadrant == 3 else 'правый'} клык ({fdi})"
            roots = ("1 корень",)
            canals = ("1-2 канала (губной, язычный)",)
            surfaces = ("вестибулярная (губная)", "язычная (lingual)", "режущий край", "мезиальная", "дистальная")
        else:  # 1, 2
            group = "incisor"
            name_ru = f"Нижний {'левый' if quadrant == 3 else 'правый'} {'центральный' if tooth_pos == 1 else 'латеральный'} резец ({fdi})"
            roots = ("1 корень (плоский)",)
            canals = ("1-2 канала (губной, язычный)",)
            surfaces = ("вестибулярная (губная)", "язычная (lingual)", "режущий край", "мезиальная", "дистальная")
            
        invariants[fdi] = ToothAnatomyInvariant(
            fdi=fdi,
            name_ru=name_ru,
            jaw="mandible",
            quadrant=quadrant,
            side=side,
            group=group,
            permissible_roots=roots,
            permissible_canals=canals,
            permissible_surfaces=surfaces,
        )

    # ── Временные (молочные) зубы 51–85 ──
    for fdi in range(51, 86):
        quad = fdi // 10
        pos = fdi % 10
        if quad in (5, 6, 7, 8) and 1 <= pos <= 5:
            jaw = "maxilla" if quad in (5, 6) else "mandible"
            side = "right" if quad in (5, 8) else "left"
            group = "incisor" if pos in (1, 2) else ("canine" if pos == 3 else "molar")
            surfaces = (
                ("вестибулярная", "небная", "окклюзионная/режущий", "контактные")
                if jaw == "maxilla"
                else ("вестибулярная", "язычная", "окклюзионная/режущий", "контактные")
            )
            invariants[fdi] = ToothAnatomyInvariant(
                fdi=fdi,
                name_ru=f"Временный зуб {fdi}",
                jaw=jaw,
                quadrant=quad,
                side=side,
                group=group,
                permissible_roots=(),
                permissible_canals=(),
                permissible_surfaces=surfaces,
            )
            
    return invariants


FDI_ANATOMY_INVARIANTS: Dict[int, ToothAnatomyInvariant] = _build_fdi_invariants()


# ── Регулярные выражения для топографической и корневой защиты ──

_FDI_EXTRACT_RE = re.compile(
    r'(?:'
    r'(?:\b(?:зуб[а-яёіїєґ]*|fdi|tooth|област[ьи]|сегмент[а-яёіїєґ]*)\s*|#\s*)([1-4][.,\-]?[1-8]|[5-8][.,\-]?[1-5])\b'
    r'|'
    r'\b([1-4][.,\-][1-8]|[5-8][.,\-][1-5]|[1-4][1-8]|[5-8][1-5])\s*(?:зуб[а-яёіїєґ]*|fdi|tooth)\b'
    r'|'
    r'(?<![\d.])\b([1-4][.][1-8]|[5-8][.][1-5])\b(?![\d.])'
    r'|'
    r'(?<![\d,])\b([1-4][,][1-8]|[5-8][,][1-5])\b(?![\d,])'
    r'|'
    r'\b(?:в|на|к|для|у)\s+([1-4][\-][1-8]|[5-8][\-][1-5])\b'
    r'|'
    r'\b(?:в|на|к|для|у)\s+([1-4][1-8]|[5-8][1-5])\b(?!\s*(?:лет|год|года|годика|кг|%|мм|см|мл|мг|мин|сек|час|ч\b|руб|\$|€|:\d))'
    r')',
    re.IGNORECASE
)

# Запрещенные корни/каналы для НИЖНЕЙ челюсти (Mandible: 31-48, 71-85)
# На нижней челюсти НЕТ небного (palatal) корня, канала или стенки/поверхности
_MANDIBLE_FORBIDDEN_ROOT_RE = re.compile(
    r'\b(?:'
    r'(?:н[её]бн\w*|палатинальн\w*|піднебінн\w*|palat\w*)\s+'
    r'(?:кор[еинiі]\w*|канал\w*|root\w*|canal\w*)'
    r'|'
    r'(?:кор[еинiі]\w*|канал\w*|root\w*|canal\w*)\s+'
    r'(?:н[её]бн\w*|палатинальн\w*|піднебінн\w*|palat\w*)'
    r'|'
    r'radix\s+palatinal\w*'
    r')\b',
    re.IGNORECASE
)

_MANDIBLE_FORBIDDEN_SURFACE_RE = re.compile(
    r'\b(?:'
    r'(?:н[её]бн\w*|палатинальн\w*|піднебінн\w*|palat\w*)\s+'
    r'(?:поверхн\w*|ст[еі]нк\w*|кра[ея]\w*|wall\w*|surface\w*)'
    r'|'
    r'(?:поверхн\w*|ст[еі]нк\w*|кра[ея]\w*|wall\w*|surface\w*)\s+'
    r'(?:н[её]бн\w*|палатинальн\w*|піднебінн\w*|palat\w*)'
    r')\b',
    re.IGNORECASE
)

# Запрещенные корни/каналы для ВЕРХНЕЙ челюсти (Maxilla: 11-28, 51-65)
# На верхней челюсти НЕТ язычного (lingual) корня, канала или стенки/поверхности
_MAXILLA_FORBIDDEN_ROOT_RE = re.compile(
    r'\b(?:'
    r'(?:язычн\w*|лингвальн\w*|язиков\w*|lingual)\s+'
    r'(?:кор[еинiі]\w*|канал\w*|root\w*|canal\w*)'
    r'|'
    r'(?:кор[еинiі]\w*|канал\w*|root\w*|canal\w*)\s+'
    r'(?:язычн\w*|лингвальн\w*|язиков\w*|lingual)'
    r'|'
    r'radix\s+lingual\w*'
    r')\b',
    re.IGNORECASE
)

_MAXILLA_FORBIDDEN_SURFACE_RE = re.compile(
    r'\b(?:'
    r'(?:язычн\w*|лингвальн\w*|язиков\w*|lingual)\s+'
    r'(?:поверхн\w*|ст[еі]нк\w*|кра[ея]\w*|wall\w*|surface\w*)'
    r'|'
    r'(?:поверхн\w*|ст[еі]нк\w*|кра[ея]\w*|wall\w*|surface\w*)\s+'
    r'(?:язычн\w*|лингвальн\w*|язиков\w*|lingual)'
    r')\b',
    re.IGNORECASE
)

# Паттерн строгого онтологического отрицания / опровержения (например, "у 36 нет небного корня")
_NEGATION_RE = re.compile(
    r'\b(?:'
    r'не\s+имеет|нет|отсутствует|не\s+бывает|не\s+может\s+быть|исключен[оа]?|'
    r'в\s+отличие\s+от|не\s+путать\s+с|не\s+является|ошибочно\s+называть|'
    r'не\s+существует|немає|відсутній|відсутня'
    r')\b',
    re.IGNORECASE
)


def _is_negated_match(text: str, start_pos: int, end_pos: int, window: int = 50) -> bool:
    """Проверяет, окружено ли вхождение отрицанием (например, 'нет небного корня')."""
    prefix = text[max(0, start_pos - window):start_pos]
    suffix = text[end_pos:min(len(text), end_pos + window)]
    return bool(_NEGATION_RE.search(prefix) or _NEGATION_RE.search(suffix))


def extract_fdi_teeth_from_text(text: str) -> List[int]:
    """Извлекает все упомянутые номера зубов по FDI (11-48, 51-85)."""
    if not text:
        return []
    clean_str = text.strip()
    digits_only = clean_str.replace(".", "").replace(",", "").replace("-", "").replace("#", "").strip()
    if digits_only.isdigit():
        val = int(digits_only)
        if val in FDI_ANATOMY_INVARIANTS:
            return [val]

    teeth: List[int] = []
    seen: Set[int] = set()
    for m in _FDI_EXTRACT_RE.finditer(text):
        for group in m.groups():
            if not group:
                continue
            clean = group.replace(".", "").replace(",", "").replace("-", "").strip()
            if clean.isdigit():
                val = int(clean)
                if val in FDI_ANATOMY_INVARIANTS and val not in seen:
                    seen.add(val)
                    teeth.append(val)
    return teeth


def parse_tooth_identifier(tooth_input: Optional[object]) -> Optional[int]:
    """Безопасно преобразует аргумент (int, str) в номер зуба FDI."""
    if tooth_input is None:
        return None
    if isinstance(tooth_input, int) and tooth_input in FDI_ANATOMY_INVARIANTS:
        return tooth_input
    extracted = extract_fdi_teeth_from_text(str(tooth_input))
    return extracted[0] if extracted else None


def is_mandibular_tooth(fdi: int) -> bool:
    """Проверяет, относится ли зуб к нижней челюсти (Mandible: 31–48, 71–85)."""
    inv = FDI_ANATOMY_INVARIANTS.get(fdi)
    return inv.jaw == "mandible" if inv else False


def is_maxillary_tooth(fdi: int) -> bool:
    """Проверяет, относится ли зуб к верхней челюсти (Maxilla: 11–28, 51–65)."""
    inv = FDI_ANATOMY_INVARIANTS.get(fdi)
    return inv.jaw == "maxilla" if inv else False


def get_tooth_invariants(fdi: int) -> Optional[ToothAnatomyInvariant]:
    """Возвращает анатомический инвариант зуба по номеру FDI (11–48, 51–85)."""
    return FDI_ANATOMY_INVARIANTS.get(fdi)


def verify_and_guard_anatomy(
    text: str,
    tooth_fdi: Optional[int] = None,
    context: Optional[str] = None
) -> Tuple[bool, List[str]]:
    """
    Проверяет текст на анатомические галлюцинации корней и поверхностей по системе FDI.
    
    Аргументы:
      text: Текст для валидации (черновик ответа, описание снимка, реплика).
      tooth_fdi: Опциональный конкретный номер зуба FDI (11–48). Если None, парсится из text/context.
      context: Опциональный контекст беседы/снимка для резолва целевого зуба.
      
    Возвращает:
      (is_valid: bool, violations: list[str])
      is_valid == True, если анатомических нарушений нет.
      is_valid == False, если обнаружены грубые ошибки (например, небный корень у зуба 36).
    """
    if not text or not text.strip():
        return True, []
        
    violations: List[str] = []
    
    # 1. Определяем целевые зубы для анализа
    target_teeth: List[int] = []
    resolved_fdi = parse_tooth_identifier(tooth_fdi)
    if resolved_fdi:
        target_teeth.append(resolved_fdi)
    else:
        text_teeth = extract_fdi_teeth_from_text(text)
        if text_teeth:
            target_teeth.extend(text_teeth)
        elif context:
            ctx_teeth = extract_fdi_teeth_from_text(context)
            if ctx_teeth:
                target_teeth.extend(ctx_teeth)
                
    # 2. Проверяем общие упоминания челюстей в тексте, если точный номер не найден
    mentions_lower_jaw = bool(re.search(r'\b(?:нижн(?:ей|юю|яя|их)?\s+челюст\w*|нижн(?:их|ие|его|ем)?\s+моляр\w*|mandib\w*)\b', text, re.I))
    mentions_upper_jaw = bool(re.search(r'\b(?:верхн(?:ей|юю|яя|их)?\s+челюст\w*|верхн(?:их|ие|его|ем)?\s+моляр\w*|maxill\w*)\b', text, re.I))

    if not target_teeth:
        if mentions_lower_jaw and not mentions_upper_jaw:
            target_teeth.append(36)  # Репрезентативный моляр нижней челюсти
        elif mentions_upper_jaw and not mentions_lower_jaw:
            target_teeth.append(16)  # Репрезентативный моляр верхней челюсти

    # 3. Валидация по целевым зубам (с контекстной изоляцией предложений, если в тексте обе челюсти)
    def _check_segment(seg_text: str, seg_teeth: List[int]) -> None:
        for fdi in seg_teeth:
            inv = FDI_ANATOMY_INVARIANTS.get(fdi)
            if not inv:
                continue

            if inv.jaw == "mandible":
                for m in _MANDIBLE_FORBIDDEN_ROOT_RE.finditer(seg_text):
                    if not _is_negated_match(seg_text, m.start(), m.end()):
                        matched_term = m.group(0).strip()
                        violations.append(
                            f"Зуб {fdi} ({inv.name_ru}): анатомическая галлюцинация '{matched_term}'. "
                            f"У зубов нижней челюсти (31-48) НЕ БЫВАЕТ небных корней/каналов! "
                            f"Корни нижних моляров: медиальный (MB, ML), дистальный (D, DB, DL), либо Radix Entomolaris/Paramolaris."
                        )
                for m in _MANDIBLE_FORBIDDEN_SURFACE_RE.finditer(seg_text):
                    if not _is_negated_match(seg_text, m.start(), m.end()):
                        matched_term = m.group(0).strip()
                        violations.append(
                            f"Зуб {fdi} ({inv.name_ru}): анатомическая галлюцинация '{matched_term}'. "
                            f"У зубов нижней челюсти поверхность ЯЗЫЧНАЯ (lingual), а не нёбная (palatal)."
                        )

            elif inv.jaw == "maxilla":
                for m in _MAXILLA_FORBIDDEN_ROOT_RE.finditer(seg_text):
                    if not _is_negated_match(seg_text, m.start(), m.end()):
                        matched_term = m.group(0).strip()
                        violations.append(
                            f"Зуб {fdi} ({inv.name_ru}): анатомическая галлюцинация '{matched_term}'. "
                            f"У зубов верхней челюсти (11-28) корень/канал НЕБНЫЙ (palatal), язычного корня/канала не существует."
                        )
                for m in _MAXILLA_FORBIDDEN_SURFACE_RE.finditer(seg_text):
                    if not _is_negated_match(seg_text, m.start(), m.end()):
                        matched_term = m.group(0).strip()
                        violations.append(
                            f"Зуб {fdi} ({inv.name_ru}): анатомическая галлюцинация '{matched_term}'. "
                            f"У зубов верхней челюсти поверхность НЕБНАЯ (palatal), а не язычная (lingual)."
                        )

    text_teeth_all = extract_fdi_teeth_from_text(text)
    jaws_in_text = {
        FDI_ANATOMY_INVARIANTS[t].jaw
        for t in text_teeth_all
        if t in FDI_ANATOMY_INVARIANTS
    }
    if len(jaws_in_text) > 1 or (mentions_lower_jaw and mentions_upper_jaw):
        clauses = [c.strip() for c in re.split(r"(?<=[.!?;])\s+|\n+", text) if c.strip()]
        for clause in clauses:
            c_teeth = extract_fdi_teeth_from_text(clause)
            if not c_teeth:
                c_low = bool(re.search(r'\b(?:нижн(?:ей|юю|яя|их)?\s+челюст\w*|нижн(?:их|ие|его|ем)?\s+моляр\w*|mandib\w*)\b', clause, re.I))
                c_up = bool(re.search(r'\b(?:верхн(?:ей|юю|яя|их)?\s+челюст\w*|верхн(?:их|ие|его|ем)?\s+моляр\w*|maxill\w*)\b', clause, re.I))
                if c_low and not c_up:
                    c_teeth = [36]
                elif c_up and not c_low:
                    c_teeth = [16]
                elif resolved_fdi:
                    c_teeth = [resolved_fdi]
                else:
                    target_jaws = {FDI_ANATOMY_INVARIANTS[t].jaw for t in target_teeth if t in FDI_ANATOMY_INVARIANTS}
                    if len(target_jaws) == 1:
                        c_teeth = target_teeth
            if c_teeth:
                _check_segment(clause, c_teeth)
    else:
        _check_segment(text, target_teeth)

    unique_violations = list(dict.fromkeys(violations))
    return (len(unique_violations) == 0, unique_violations)


def get_anatomy_guard_prompt(tooth_str: Optional[object] = None) -> str:
    """
    Генерирует жесткую защитную клиническую директиву по анатомии корней и поверхностей
    для включения в системные vision-промпты и клинические консультации.
    """
    fdi = parse_tooth_identifier(tooth_str)
            
    if fdi and fdi in FDI_ANATOMY_INVARIANTS:
        inv = FDI_ANATOMY_INVARIANTS[fdi]
        if inv.jaw == "mandible":
            return (
                f"\n=== АНАТОМИЧЕСКИЙ ЗАМОК: ЗУБ {fdi} (НИЖНЯЯ ЧЕЛЮСТЬ / MANDIBLE) ===\n"
                f"КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО упоминать «небный корень», «небный канал», «небную поверхность»!\n"
                f"На нижней челюсти нет нёба. Допустимая анатомия для {fdi}:\n"
                f"- Корни: медиальный (каналы MB, ML), дистальный (каналы D, DB, DL), либо Radix Entomolaris (дистолингвальный).\n"
                f"- Поверхности: вестибулярная (щечная), ЯЗЫЧНАЯ (lingual), окклюзионная, контактные (мезиальная/дистальная).\n"
            )
        else:
            return (
                f"\n=== АНАТОМИЧЕСКИЙ ЗАМОК: ЗУБ {fdi} (ВЕРХНЯЯ ЧЕЛЮСТЬ / MAXILLA) ===\n"
                f"КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО упоминать «язычный корень», «язычный канал», «язычную поверхность»!\n"
                f"На верхней челюсти ориентация строго НЕБНАЯ (palatal). Допустимая анатомия для {fdi}:\n"
                f"- Корни моляров: мезиально-щечный (MB1, MB2), дистально-щечный (DB), небный (P).\n"
                f"- Поверхности: вестибулярная (щечная/губная), НЕБНАЯ (palatal), окклюзионная, контактные.\n"
            )

    return (
        "\n=== ИНВАРИАНТ КОРНЕВОЙ И ТОПОГРАФИЧЕСКОЙ АНАТОМИИ (FDI 11–48) ===\n"
        "1. НИЖНЯЯ ЧЕЛЮСТЬ (Зубы 31–48, 71–85):\n"
        "   - ТОТАЛЬНЫЙ ЗАПРЕТ: категорически запрещено упоминать «небный корень», «небный канал», «небную поверхность/стенку»!\n"
        "   - Корни нижних моляров (36, 37, 38, 46, 47, 48): медиальный (MB, ML), дистальный (D, DB, DL). "
        "     Дополнительный корень — Radix Entomolaris (дистолингвальный) или Radix Paramolaris (мезиобуккальный).\n"
        "   - Поверхности: вестибулярная (щечная/губная), ЯЗЫЧНАЯ (lingual), окклюзионная, апроксимальные.\n"
        "2. ВЕРХНЯЯ ЧЕЛЮСТЬ (Зубы 11–28, 51–65):\n"
        "   - ТОТАЛЬНЫЙ ЗАПРЕТ: категорически запрещено упоминать «язычный корень», «язычный канал», «язычную поверхность/стенку»!\n"
        "   - Корни верхних моляров (16, 17, 18, 26, 27, 28): мезиально-щечный (MB), дистально-щечный (DB), НЕБНЫЙ (P).\n"
        "   - Поверхности: вестибулярная (щечная/губная), НЕБНАЯ (palatal), окклюзионная, апроксимальные.\n"
    )
