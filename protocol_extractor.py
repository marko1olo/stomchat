"""
protocol_extractor.py — Автоматический экстрактор и каталогизатор клинических протоколов StomChat.

Выполняет:
1. Интеллектуальный поиск кандидатов в клинические протоколы в сообщениях врачей (эвристика + NLP).
2. Структурированное извлечение пошаговых алгоритмов, показаний, материалов и нюансов через LLM.
3. Сохранение в базу данных (clinical_protocols) с дедупликацией.
4. Интерактивную визуализацию и каталог для команды /protocols и RAG-поиска.
"""

import asyncio
import json
import logging
import re
from typing import Dict, List, Optional, Tuple

import blocking_tools
import database

logger = logging.getLogger(__name__)

generate_gemini_text_async = getattr(blocking_tools, "generate_gemini_text_async", None)

# Ключевые стоматологические термины, указывающие на манипуляцию
_DENTAL_PROCEDURAL_TERMS = {
    "bopt", "препарирован", "фиксаци", "ирригаци", "обтураци", "травлен",
    "силанизац", "адгезив", "слепок", "сканирован", "имплантац", "синус-лифт",
    "кюретаж", "реставрац", "композит", "цемент", "дезобтурац", "штифт",
    "коронка", "винир", "сплинт", "гипохлорит", "гуттаперч", "апекс",
    "коффердам", "рабердам", "бондинг", "протравк", "распломбировк"
}

# Маркеры алгоритмичности / последовательности
_STEP_MARKERS = {
    "протокол", "алгоритм", "последовательность", "пошагово", "методика",
    "этап", "этапы", "сначала", "затем", "далее", "после этого", "в конце",
    "экспозиция", "секунд", "минут", "промываем", "высушиваем"
}


def is_protocol_candidate(text: str) -> bool:
    """
    Быстрый фильтр: определяет, содержит ли текст подробное описание клинического протокола.
    Исключает тривиальные вопросы вида «какой протокол травления?».
    """
    if not text or len(text.strip()) < 130:
        return False

    t_lower = text.lower()

    # Не является кандидатом, если это чисто вопросительное предложение без пояснений
    if t_lower.endswith("?") and len(text.splitlines()) <= 2 and "протокол" in t_lower and not any(c.isdigit() for c in t_lower):
        return False

    has_proc_term = any(term in t_lower for term in _DENTAL_PROCEDURAL_TERMS)
    if not has_proc_term:
        return False

    has_step_marker = any(marker in t_lower for marker in _STEP_MARKERS)
    has_numbered_steps = bool(re.search(r'(?:^|\n|\s)(?:[1-9]\.|\bэтап\s*[1-9]|\bшаг\s*[1-9]|[1-9]\))', t_lower))

    return has_step_marker or has_numbered_steps


def _clean_json_str(text: str) -> Optional[dict]:
    """Извлекает и парсит JSON из ответа нейросети."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            pass
    return None


async def extract_protocol_from_text_async(
    text: str,
    author_doctor: str = "",
    source_msg_id: int = 0
) -> Optional[dict]:
    """
    Извлекает структурированный клинический протокол из клинического сообщения или дискуссии.
    """
    if not is_protocol_candidate(text):
        return None

    prompt = f"""Ты — ведущий эксперт-методолог доказательной стоматологии (Evidence-Based Dentistry).
Врач-стоматолог в профессиональном сообществе поделился клиническим протоколом/методикой.
Твоя задача: структурировать этот клинический опыт в эталонный протокол.

Исходный текст врача:
<text>
{text[:3500]}
</text>

КРИТИЧЕСКИЕ ТРЕБОВАНИЯ:
1. Выдели точное клиническое название протокола (title), например: «Адгезивная фиксация керамических виниров E.max» или «Протокол распломбировки каналов, обтурированных резорцин-формалином».
2. Определи категорию (category): строго одна из: "Ортопедия", "Терапия", "Эндодонтия", "Хирургия", "Пародонтология", "Гнатология", "Общая стоматология".
3. Выдели четкие клинические показания (indication) и противопоказания/ограничения (contraindications).
4. Разбей методику на строгие последовательные этапы (steps), каждый этап должен иметь номер, краткое название ("name") и подробное клиническое описание ("description") с экспозициями, концентрациями и движениями инструмента.
5. Выпиши конкретный список материалов и инструментов (materials).
6. Выдели критические клинические нюансы и подводные камни (key_nuances) — на что обратить особое внимание, чтобы избежать осложнений.
7. Верни результат СТРОГО в формате JSON:
{{
  "title": "Название протокола",
  "category": "Ортопедия",
  "indication": "Показания к применению",
  "contraindications": "Противопоказания или анатомические ограничения",
  "steps": [
    {{"step": 1, "name": "Название этапа", "description": "Подробности выполнения..."}},
    {{"step": 2, "name": "...", "description": "..."}}
  ],
  "materials": "Материалы, препараты, боры, аппараты с концентрациями",
  "key_nuances": "Клинические акценты и профилактика ошибок"
}}
"""

    try:
        status_ctx = {"kind": "protocol_extraction", "thinking_level": "LOW", "max_tokens": 2048}
        response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=45)
        if error or not response or not getattr(response, "text", None):
            logger.warning(f"Protocol extraction LLM error: {error}")
            return None

        parsed = _clean_json_str(response.text)
        if not parsed or not parsed.get("title") or not parsed.get("steps"):
            logger.debug(f"Protocol parsing returned incomplete JSON: {response.text[:100]}")
            return None

        parsed["author_doctor"] = author_doctor
        parsed["source_msg_id"] = source_msg_id
        return parsed
    except Exception as e:
        logger.error(f"Error during protocol extraction: {e}")
        return None


async def extract_and_save_protocol_async(
    text: str,
    author_doctor: str = "",
    source_msg_id: int = 0
) -> Optional[int]:
    """
    Извлекает и сохраняет протокол в базу данных stomat_bot.db.
    Возвращает ID сохраненного протокола либо None.
    """
    proto = await extract_protocol_from_text_async(text, author_doctor=author_doctor, source_msg_id=source_msg_id)
    if not proto:
        return None

    steps_json = json.dumps(proto.get("steps", []), ensure_ascii=False)
    proto_id = await database.save_clinical_protocol(
        title=proto.get("title", "Клинический протокол").strip(),
        category=proto.get("category", "Общая стоматология").strip(),
        indication=proto.get("indication", "").strip(),
        contraindications=proto.get("contraindications", "").strip(),
        steps_json=steps_json,
        materials=proto.get("materials", "").strip(),
        key_nuances=proto.get("key_nuances", "").strip(),
        author_doctor=author_doctor,
        source_msg_id=source_msg_id,
    )
    logger.info(f"Successfully extracted & persisted clinical protocol #{proto_id}: '{proto.get('title')}'")
    return proto_id


# Базовые золотые стандарты сообщества для автоматического посева (seed)
_DEFAULT_COMMUNITY_PROTOCOLS = [
    {
        "title": "BOPT (Biologically Oriented Preparation Technique)",
        "category": "Ортопедия",
        "indication": "Протезирование зубов с поддесневыми дефектами, ревизия старых коронок с уступом, необходимость управляемого утолщения десневого края и формирования нового соединительнотканного прикрепления.",
        "contraindications": "Тонкий биотип десны с выраженным риском рецессии при агрессивном кюретаже, отсутствие зоны прикрепленной кератинизированной десны (<2 мм).",
        "steps": [
            {"step": 1, "name": "Ориентировочное сошлифовывание", "description": "Редукция окклюзионной/режущей поверхности на 1.5–2.0 мм и аксиальных стенок бором торпедовидной формы с формированием легкой конусности (4–6 градусов)."},
            {"step": 2, "name": "Поддесневой бесуступный препаринг", "description": "Введение пламевидного бора с зернистостью 40–50 мкм в зубодесневую борозду с контролируемым микрокюретажем эпителия борозды без повреждения соединительнотканного прикрепления."},
            {"step": 3, "name": "Формирование финишной зоны адаптации", "description": "Сглаживание пришеечной границы желтым/белым пламевидным бором до зеркальной гладкости, устранение поднутрений."},
            {"step": 4, "name": "Временная реставрация и созревание", "description": "Фиксация высокополированной временной коронки с овоидной пришеечной геометрией для стабилизации кровяного сгустка и управляемой миграции десневого контура (экспозиция 4–8 недель)."}
        ],
        "materials": "Пламевидные боры 862/863 (красные и желтые), бис-акрил (Protemp 4 / Structur 3), полировочные пасты и диски, безевгенольный временный цемент.",
        "key_nuances": "Критически важно не заходить за биологическую ширину (не глубже 0.5–1.0 мм в борозду). Идеальная полировка шейки временной коронки обязательна — шероховатость вызывает хроническое воспаление вместо прикрепления.",
        "author_doctor": "Ignazio Loi / Доктора сообщества Docendo Discimus",
        "source_msg_id": 0
    },
    {
        "title": "Адгезивная фиксация керамики на основе дисиликата лития (E.max)",
        "category": "Ортопедия",
        "indication": "Фиксация виниров, вкладок (inlay/onlay) и коронок из дисиликата лития (E.max CAD / Press).",
        "contraindications": "Невозможность создания абсолютной изоляции рабочего поля (коффердам), поддесневые края реставрации глубже 2 мм без предварительного хирургического удлинения коронки или DME.",
        "steps": [
            {"step": 1, "name": "Подготовка реставрации (Травление)", "description": "Нанесение 4.5–9% плавиковой кислоты (HF) на внутреннюю поверхность керамики строго на 20 секунд (для дисиликата лития)."},
            {"step": 2, "name": "Нейтрализация и ультразвуковая очистка", "description": "Смывание кислоты обильной струей воды 60 сек, нейтрализация содовым раствором, очистка в ультразвуковой ванне в 96% спирте 3–5 минут для удаления нерастворимых солей фторсиликатов."},
            {"step": 3, "name": "Силанизация керамики", "description": "Нанесение моносилана (Silane) на просушенную поверхность с выдержкой 60 сек и подогревом теплым воздухом (50–60°C) для завершения конденсации силоксановых связей."},
            {"step": 4, "name": "Подготовка твердых тканей зуба", "description": "Пескоструйная очистка оксидом алюминия 27–50 мкм (Rondoflex, 1.5–2 бар). Селективное или тотальное травление 37% ортофосфорной кислотой (эмаль 30 сек, дентин 15 сек)."},
            {"step": 5, "name": "Внесение адгезива и композитного цемента", "description": "Нанесение адгезива без полимеризации (OptiBond FL / Clearfil SE Protect), внесение подогретого композита или светоотверждаемого цемента (Variolink Esthetic LC), посадка реставрации, предварительная фотополимеризация 2 сек для срезания излишков, затем финишная полимеризация через глицериновый гель 40 сек на поверхность."}
        ],
        "materials": "Плавиковая кислота 5-9% (Porcelain Etch), Силан (Monobond Plus / Silane), Ортофосфорная кислота 37% (Ultra-Etch), Оксид алюминия 27 мкм, Адгезивная система 4-го поколения, Световой цемент (Variolink / Choice 2), Глицериновый гель (Air Block).",
        "key_nuances": "Не перетравливать E.max плавиковой кислотой дольше 20 секунд — деструкция кристаллической решетки снижает адгезию. Глицериновый гель в конце предотвращает ингибированный кислородом слой по краю реставрации.",
        "author_doctor": "Pascal Magne / Доктора сообщества Docendo Discimus",
        "source_msg_id": 0
    },
    {
        "title": "Эндодонтическая ирригация и ультразвуковая активация",
        "category": "Эндодонтия",
        "indication": "Первичное и повторное эндодонтическое лечение пульпитов и апикальных периодонтитов.",
        "contraindications": "Несформированная верхушка корня с широким апикальным отверстием (риск выведения гипохлорита в периапикальные ткани — гипохлоритовая авария).",
        "steps": [
            {"step": 1, "name": "Изоляция и первичное формирование резервуара", "description": "Коффердам обязателен. Создание прямого эндодонтического доступа и стенок для поддержания постоянного пула ирриганта в пульпарной камере."},
            {"step": 2, "name": "Основная ирригация гипохлоритом натрия (NaOCl)", "description": "Постоянная ирригация 3–5.25% NaOCl эндодонтической иглой с боковым срезом (Side-vented) на 2–3 мм короче рабочей длины (WL). Общий объем не менее 15–20 мл на канал."},
            {"step": 3, "name": "Удаление смазанного слоя (ЭДТА 17%)", "description": "Промывание каналов 17% раствором ЭДТА в течение 1–2 минут для растворения неорганической составляющей смазанного слоя и раскрытия дентинных трубочек."},
            {"step": 4, "name": "Финальная активация и промывание", "description": "Повторное внесение подогретого NaOCl (50–60°C) и ультразвуковая активация (PUI) неагрессивной гладкой насадкой (IrriSafe / Ultra X) циклами по 20–30 секунд. Финишная ирригация дистиллированной водой или физраствором."}
        ],
        "materials": "Гипохлорит натрия 3–5%, ЭДТА 17%, эндодонтические иглы 30G с боковым срезом, ультразвуковой активатор каналов (IrriSafe, Eighteeth Ultra X), бумажные пины.",
        "key_nuances": "Никогда не заклинивать иглу в канале — подача ирриганта строго без давления! Не смешивать NaOCl с хлоргексидином напрямую (образуется токсичный парахлоранилин PCA).",
        "author_doctor": "Domenico Ricucci / Доктора сообщества Docendo Discimus",
        "source_msg_id": 0
    },
    {
        "title": "Трехмерная обтурация корневых каналов горячей гуттаперчей (Continuous Wave)",
        "category": "Эндодонтия",
        "indication": "Качественная обтурация сформированной корневой системы после полноценной хемомеханической обработки и высушивания.",
        "contraindications": "Продолжающийся активный экссудат из канала, выраженная апикальная резорбция без создания апикального упора.",
        "steps": [
            {"step": 1, "name": "Припасовка мастер-штифта", "description": "Подбор гуттаперчевого штифта соответствующей конусности (04/06). Проверка tug-back (апикального заклинивания) на рабочей длине (WL). Рентгенологический контроль."},
            {"step": 2, "name": "Внесение силера", "description": "Канал высушивается бумажными штифтами. Силер (эпоксидный AH Plus или биокерамический BioRoot RCS) вносится мастер-штифтом тонким слоем на стенки."},
            {"step": 3, "name": "Downpack (Апикальная конденсация волной тепла)", "description": "Нагретый плаггер (System B / Elements) погружается на глубину WL минус 3–5 мм, срезая корональную гуттаперчу за 2–3 секунды. Холодным ручным плаггером проводится вертикальная конденсация апикальной пробки."},
            {"step": 4, "name": "Backfill (Корональное пломбирование)", "description": "Инжектором разогретой термопластифицированной гуттаперчи (200°C) порционно заполняется средняя и устьевая треть канала с послойным уплотнением плаггерами Buchanan."}
        ],
        "materials": "Аппарат трехмерной обтурации (Fast-Pack / Fast-Fill / Elements), плаггеры Buchanan, эпоксидный силер AH Plus / биокерамика, гуттаперчевые мастер-штифты.",
        "key_nuances": "Контролировать температуру и время активации нагревательного плаггера (не более 3–4 сек в апексе во избежание термического ожога периодонта).",
        "author_doctor": "Stephen Buchanan / Доктора сообщества Docendo Discimus",
        "source_msg_id": 0
    },
    {
        "title": "Вертикальное препарирование под диоксид циркония (VertiPrep)",
        "category": "Ортопедия",
        "indication": "Разрушение коронковой части зуба с переходом дефекта за пределы биологической ширины, перелечивание после старых штампованных или металлокерамических коронок, стираемость зубов.",
        "contraindications": "Невозможность соблюдения гигиены пациентом, неконтролируемый активный пародонтит с костными карманами >6 мм.",
        "steps": [
            {"step": 1, "name": "Окклюзионная редукция", "description": "Равномерное сошлифовывание окклюзионной поверхности на 1.5–2.0 мм с сохранением анатомической формы бугров."},
            {"step": 2, "name": "Аксиальная сепарация и устранение выпуклостей", "description": "Снятие экватора зуба длинным тонким бором с созданием параллельности/легкой конусности аксиальных стенок."},
            {"step": 3, "name": "Периферический бесуступный финишинг", "description": "Погружение в борозду кончика тонкого бора с формированием плавного вертикального перехода в корень («knife-edge» / feather-edge)."},
            {"step": 4, "name": "Снятие прецизионного оттиска или интраоральное сканирование", "description": "Двухниточная ретракция (нити 000 и 0) или гидрофобный скан-спрей при оптическом сканировании для четкой фиксации границы перехода эмаль-цемент."}
        ],
        "materials": "Алмазные боры Komet / NTI VertiPrep серии 862F, 863EF, ретракционные нити Ultrapack, А-силикон или интраоральный сканер (Medit/iTero).",
        "key_nuances": "Минимальная толщина циркониевой коронки на границе knife-edge должна составлять не менее 0.3–0.4 мм во избежание сколов края при жевательной нагрузке.",
        "author_doctor": "Пауло Кано / Доктора сообщества Docendo Discimus",
        "source_msg_id": 0
    }
]


async def seed_default_protocols_async():
    """Заполняет базу данных эталонными клиническими протоколами, если таблица пуста."""
    try:
        existing = await database.get_clinical_protocols(limit=1)
        if not existing:
            logger.info("Seeding default clinical protocols into database...")
            for proto in _DEFAULT_COMMUNITY_PROTOCOLS:
                steps_json = json.dumps(proto["steps"], ensure_ascii=False)
                await database.save_clinical_protocol(
                    title=proto["title"],
                    category=proto["category"],
                    indication=proto["indication"],
                    contraindications=proto["contraindications"],
                    steps_json=steps_json,
                    materials=proto["materials"],
                    key_nuances=proto["key_nuances"],
                    author_doctor=proto["author_doctor"],
                    source_msg_id=proto["source_msg_id"],
                )
            logger.info("Successfully seeded 5 core clinical protocols.")
    except Exception as e:
        logger.error(f"Failed to seed default protocols: {e}")


def format_protocol_view(proto: dict) -> Tuple[str, list]:
    """
    Форматирует один протокол в структурированную медицинскую карточку с кнопками навигации.
    """
    from telethon import Button

    title = proto.get("title", "Клинический протокол")
    category = proto.get("category", "Общая стоматология")
    author = proto.get("author_doctor", "")
    indication = proto.get("indication", "")
    contra = proto.get("contraindications", "")
    materials = proto.get("materials", "")
    if isinstance(materials, (list, tuple)):
        materials = ", ".join(str(m) for m in materials)
    nuances = proto.get("key_nuances", "")
    
    steps_raw = proto.get("steps") or proto.get("steps_json", "[]")
    steps = []
    if isinstance(steps_raw, (list, tuple)):
        steps = list(steps_raw)
    elif isinstance(steps_raw, str):
        try:
            steps = json.loads(steps_raw)
            if not isinstance(steps, list):
                steps = []
        except Exception:
            steps = []

    out = [
        f"📋 <b>ПРОТОКОЛ: {title.upper()}</b>",
        f"🏷 <i>Раздел: {category}</i>" + (f" | 👨‍⚕️ <i>Эксперт: {author}</i>" if author else ""),
        ""
    ]

    if indication:
        out.append(f"🎯 <b>Показания:</b>\n{indication}\n")
    if contra:
        out.append(f"🚫 <b>Противопоказания и ограничения:</b>\n{contra}\n")

    if steps:
        out.append("🔢 <b>Пошаговый алгоритм действий:</b>")
        for idx, st in enumerate(steps, 1):
            if isinstance(st, str):
                out.append(f"<b>{idx}.</b> {st}")
            elif isinstance(st, dict):
                s_num = st.get("step", idx)
                s_name = st.get("name", "")
                s_desc = st.get("description", "")
                if s_name and s_desc:
                    out.append(f"<b>{s_num}. {s_name}</b>\n{s_desc}\n")
                elif s_desc:
                    out.append(f"<b>{s_num}.</b> {s_desc}\n")
                elif s_name:
                    out.append(f"<b>{s_num}. {s_name}</b>\n")
            else:
                out.append(f"<b>{idx}.</b> {str(st)}")

    if materials:
        out.append(f"\n🛠 <b>Необходимые материалы и оборудование:</b>\n{materials}\n")
    if nuances:
        out.append(f"⚠️ <b>Клинические нюансы и подводные камни:</b>\n{nuances}\n")

    out.append("🏛 <i>Сообщество доказательной стоматологии «Docendo Discimus»</i>")

    text = "\n".join(out)

    buttons = [
        [Button.inline("⬅️ К списку протоколов", data="proto:list"), Button.inline("🏠 Главное меню", data="nav:main")]
    ]
    return text, buttons


def format_protocol_catalog(
    protocols: list,
    category_filter: Optional[str] = None
) -> Tuple[str, list]:
    """
    Форматирует каталог протоколов с фильтрами по направлениям и инлайн-кнопками.
    """
    from telethon import Button

    header_title = f"категории «{category_filter}»" if category_filter else "Базы Знаний"
    out = [
        f"📚 <b>Клинические протоколы {header_title}:</b>",
        "Ниже представлены проверенные алгоритмы доказательной стоматологии (EBM), сформированные на основе клинических разборов сообщества.\n"
    ]

    buttons = []
    cat_buttons = [
        Button.inline("🦷 Ортопедия", data="proto:cat:Ортопедия"),
        Button.inline("🩸 Эндодонтия", data="proto:cat:Эндодонтия"),
        Button.inline("🔪 Хирургия", data="proto:cat:Хирургия"),
    ]
    buttons.append(cat_buttons)

    row = []
    for idx, p in enumerate(protocols[:10], 1):
        p_id = p["id"]
        p_title = p["title"]
        p_cat = p.get("category", "")
        out.append(f"<b>{idx}.</b> {p_title} <i>({p_cat})</i>")
        btn = Button.inline(f"📖 #{idx} {p_title[:18]}...", data=f"proto:view:{p_id}")
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    out.append("\n👇 <i>Нажмите на кнопку с протоколом для открытия полной пошаговой карты.</i>")
    buttons.append([Button.inline("🔄 Все протоколы", data="proto:list"), Button.inline("🏠 Меню", data="nav:main")])

    return "\n".join(out), buttons
