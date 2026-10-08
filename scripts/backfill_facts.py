#!/usr/bin/env python3
"""
Скрипт ретроспективного наполнения (backfill) долговременной клинической памяти врачей StomChat.
Извлекает структурированные факты (facts_json) из текстовых полей specialty, clinical_summary и group_summary.

Запуск:
    python backfill_facts.py [--db stomat_bot.db] [--dry-run]
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from typing import List, Optional, Set, Tuple

# Настройка кодировки вывода для Windows консолей
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("backfill_facts")

# ---------------------------------------------------------------------------
# База стоматологических сущностей, протоколов, брендов и географии
# ---------------------------------------------------------------------------

CITIES_EXACT = [
    "Москва", "Санкт-Петербург", "СПб", "Питер", "Казань", "Краснодар",
    "Новосибирск", "Екатеринбург", "Уфа", "Самара", "Нижний Новгород",
    "Ростов-на-Дону", "Воронеж", "Челябинск", "Красноярск", "Пермь",
    "Волгоград", "Саратов", "Тюмень", "Тольятти", "Ижевск", "Барнаул",
    "Иркутск", "Ульяновск", "Хабаровск", "Владивосток", "Ярославль",
    "Махачкала", "Томск", "Оренбург", "Кемерово", "Рязань", "Астрахань",
    "Пенза", "Липецк", "Тула", "Киров", "Чебоксары", "Калининград",
    "Брянск", "Курск", "Иваново", "Тверь", "Ставрополь", "Сочи",
    "Белгород", "Ташкент", "Минск", "Бишкек", "Алматы", "Астана",
    "Баку", "Ереван", "Тбилиси", "Симферополь", "Севастополь", "Грозный", "Дубай"
]

CLINICAL_VERBS = (
    "интересуется|придерживается|применяет|использует|рекомендует|отвергает|"
    "отказался|отказалась|выступает|стремится|демонстрирует|акцентирует|уделяет|"
    "считает|поддерживает|проводит|практикует|оценивает|контролирует|учитывает|"
    "сталкивается|занимается|ведет|работает|выбирает|предпочитает"
)


def clean_sentence(s: str) -> str:
    """
    Очищает предложение от разговорных и описательных вводных конструкций чата,
    превращая его в компактное клиническое правило/предпочтение врача.
    """
    s = s.strip()
    # Удаляем телеграм юзернеймы (@username) и пустые маркеры (@)
    s = re.sub(r'\(@[a-zA-Z0-9_]*\)', '', s)
    s = re.sub(r'\(@\)', '', s)

    # Удаляем конструкции вида "Иван Иванов — практикующий ортопед, "
    s = re.sub(r'^(?:Доктор|Врач|Коллега)\s+[A-Za-zА-ЯЁ][a-zа-яёA-Z0-9_\s]{1,30}\s+[—\-]\s*', '', s)
    s = re.sub(
        r'^[A-Za-zА-ЯЁ][a-zа-яёA-Z0-9_\s]{1,30}\s+[—\-]\s+(?:практикующий|опытный|начинающий|ведущий|высококвалифицированный)\s+[^,]+,\s*',
        '', s
    )
    s = re.sub(r'^[A-Za-zА-ЯЁ][a-zа-яёA-Z0-9_\s]{1,30}\s+[—\-]\s*', '', s)

    # Удаляем префикс с именем доктора перед клиническим глаголом
    s = re.sub(
        rf'^(?:(?:Доктор|Врач|Коллега)\s+)?[A-Za-zА-ЯЁ][a-zа-яёA-Z0-9_]{{1,20}}(?:\s+[A-Za-zА-ЯЁ][a-zа-яёA-Z0-9_]{{1,20}})?\s+(?:(?:\w+)\s+){{0,2}}(?=(?:{CLINICAL_VERBS}))',
        '', s, flags=re.IGNORECASE
    )

    # Удаляем вводные слова
    s = re.sub(r'^(?:Также|Кроме того|При этом|В свою очередь|Однако|Вместе с тем)\s*,\s*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+', ' ', s).strip()

    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    s = re.sub(r'[.,;!?]+$', '', s).strip()
    return s


def extract_clinical_facts(
    specialty: str,
    clinical_summary: str,
    group_summary: str
) -> List[str]:
    """
    Извлекает от 2 до 6 структурированных клинических фактов для профиля врача
    на основе анализа полей specialty, clinical_summary и group_summary.
    """
    facts: List[str] = []
    seen: Set[str] = set()

    def add_fact(f: str) -> None:
        f = f.strip().strip('"\'')
        f = re.sub(r'[.,;]+$', '', f).strip()
        f = re.sub(r'\s+', ' ', f)
        norm = f.lower()
        if f and norm not in seen and len(f) >= 8:
            if "специализация:" in norm and any("специализация:" in s for s in seen):
                return
            seen.add(norm)
            facts.append(f)

    full_text = f"{specialty}\n{clinical_summary}\n{group_summary}".strip()
    if not full_text:
        return []

    # Отсекаем явные системные заглушки без клинической информации
    if "недостаточно сообщений в общей группе" in full_text.lower() and len(full_text) < 120 and not specialty.strip():
        return []

    # -----------------------------------------------------------------------
    # 1. СПЕЦИАЛИЗАЦИЯ И СТАТУС
    # -----------------------------------------------------------------------
    clean_spec = specialty.strip()
    clinic_extracted: Optional[str] = None
    edu_extracted: Optional[str] = None

    if clean_spec:
        # Извлечение клиники из скобок (например, DDS, Forest Hills Dental)
        m_clinic = re.search(r'\((?:DDS,\s*)?([A-Za-z\s]+(?:Dental|Clinic|Practice|Center))\)', clean_spec)
        if m_clinic:
            clinic_extracted = m_clinic.group(1).strip()
            clean_spec = clean_spec[:m_clinic.start()].strip() + clean_spec[m_clinic.end():].strip()
            clean_spec = clean_spec.strip(' ,/')

        # Извлечение ВУЗа / академического статуса из скобок
        m_edu = re.search(
            r'\(([^)]*(?:университет|институт|академи|МГМСУ|ПСПбГМУ|ординатор|студент)[^)]*)\)',
            clean_spec, re.IGNORECASE
        )
        if m_edu:
            edu_extracted = m_edu.group(1).strip()
            clean_spec = clean_spec[:m_edu.start()].strip() + clean_spec[m_edu.end():].strip()
            clean_spec = clean_spec.strip(' ,/')

        clean_spec = clean_spec.strip(" ,/")
        if clean_spec.count('(') > clean_spec.count(')'):
            clean_spec += ')'

        if clean_spec.lower() in ("не определена", "не указана", "уточняется", ""):
            clean_spec = ""
        elif clean_spec.lower() == "стоматолог":
            clean_spec = "Стоматолог общей практики"

        if clean_spec:
            if not clean_spec.lower().startswith("специализация"):
                add_fact(f"Специализация: {clean_spec}")
            else:
                add_fact(clean_spec)

    # Если специализация не была заполнена в колонке specialty
    if not any("специализация" in f.lower() for f in facts):
        m_spec1 = re.search(
            r'(?:врач[а-яё\s\-]*|стоматолог[а-яё\s\-]*)?(ортопед[а-яё\-]*|терапевт[а-яё\-]*|хирург[а-яё\-]*|эндодонтист[а-яё\-]*|ортодонт[а-яё\-]*|гнатолог[а-яё\-]*|детск\w+\s+стоматолог\w*)',
            full_text, re.IGNORECASE
        )
        if m_spec1:
            matched_spec = m_spec1.group(1).capitalize()
            add_fact(f"Специализация: Стоматолог-{matched_spec.lower()}")
        elif re.search(r'студент\w*\s+\d+\s+курс\w*|обучени\w+\s+в\s+вузе', full_text, re.IGNORECASE):
            add_fact("Статус: Студент стоматологического факультета (старшие курсы)")
        else:
            add_fact("Специализация: Стоматолог общей практики (уточняется в диалоге)")

    # -----------------------------------------------------------------------
    # 2. КЛИНИКА / ГОРОД / ВУЗ
    # -----------------------------------------------------------------------
    if clinic_extracted:
        add_fact(f"Клиника: {clinic_extracted}")
    else:
        m_cl = re.search(r'(?:клиник[аеы]|стоматологи[иея])\s+[«"“]([^»"”]{3,40})[»"”]', full_text, re.IGNORECASE)
        if m_cl:
            add_fact(f"Клиника: «{m_cl.group(1).strip()}»")
        else:
            m_cl_en = re.search(r'\b([A-Z][a-zA-Z\s]{2,30}(?:Dental|Clinic|Center|Practice))\b', full_text)
            if m_cl_en and m_cl_en.group(1).strip() not in ("Study Club", "Dental"):
                add_fact(f"Клиника: {m_cl_en.group(1).strip()}")

    if edu_extracted:
        add_fact(f"Образование / статус: {edu_extracted}")
    else:
        if re.search(r'Сеченовск\w+|Перв\w+\s+МГМУ', full_text, re.IGNORECASE):
            add_fact("Учебное заведение: Сеченовский университет (Первый МГМУ)")
        elif re.search(r'МГМСУ|Евдокимов\w*', full_text, re.IGNORECASE):
            add_fact("Учебное заведение: МГМСУ им. А.И. Евдокимова")
        elif re.search(r'ПСПбГМУ|Павлов\w*', full_text, re.IGNORECASE):
            add_fact("Учебное заведение: ПСПбГМУ им. акад. И.П. Павлова")
        elif re.search(r'\bРУДН\b', full_text):
            add_fact("Учебное заведение: РУДН")
        elif re.search(r'преподавател\w+\s+(?:ортопедическ\w+|терапевтическ\w+|хирургическ\w+|стоматологи\w+)[^,.;()]*', full_text, re.IGNORECASE):
            m_prep = re.search(r'преподавател\w+\s+(?:ортопедическ\w+|терапевтическ\w+|хирургическ\w+|стоматологи\w+)[^,.;()]*', full_text, re.IGNORECASE)
            add_fact(f"Академическая деятельность: {m_prep.group(0).strip()}")
        elif re.search(r'студент\w*\s+(\d+)\s+курс\w*', full_text, re.IGNORECASE):
            m_st = re.search(r'студент\w*\s+(\d+)\s+курс\w*', full_text, re.IGNORECASE)
            add_fact(f"Статус: Студент {m_st.group(1)}-го курса стоматологического факультета")

    # Город / Локация (с фильтром отсечения омонимичных авторов окклюзии вроде Питера Доусона)
    text_without_dawson = re.sub(r'Питер\w*\s+Доусон\w*', '', full_text, flags=re.IGNORECASE)
    m_city_paren = re.search(r'\(([^)]{2,30})\)', text_without_dawson)
    city_found: Optional[str] = None
    if m_city_paren:
        paren_text = m_city_paren.group(1).strip()
        for c in CITIES_EXACT:
            if re.search(r'\b' + re.escape(c) + r'\b', paren_text, re.IGNORECASE):
                city_found = c
                break

    if not city_found:
        for c in CITIES_EXACT:
            if re.search(r'(?:г\.|город[а-е]?|в|из)\s+' + re.escape(c) + r'\b', text_without_dawson, re.IGNORECASE):
                city_found = c
                break

    if city_found:
        if city_found.lower() in ("питер", "спб"):
            city_found = "Санкт-Петербург"
        add_fact(f"Локация: {city_found}")

    # -----------------------------------------------------------------------
    # 3. МАТЕРИАЛЫ, СИСТЕМЫ, ОБОРУДОВАНИЕ И ПРОТОКОЛЫ
    # -----------------------------------------------------------------------
    # Цифра и внутриротовые сканеры
    if re.search(r'Primescan|Праймскан', full_text, re.IGNORECASE):
        add_fact("Оборудование: внутриротовой сканер Dentsply Sirona Primescan")
    elif re.search(r'Trios|3Shape', full_text, re.IGNORECASE):
        add_fact("Оборудование: внутриротовой сканер 3Shape Trios")
    elif re.search(r'iTero|Айтеро', full_text, re.IGNORECASE):
        add_fact("Оборудование: интраоральный сканер iTero")
    elif re.search(r'Medit|Медит', full_text, re.IGNORECASE):
        add_fact("Оборудование: интраоральный сканер Medit")
    elif re.search(r'внутриротов\w+\s+сканер\w*|интраоральн\w+\s+сканер\w*', full_text, re.IGNORECASE):
        add_fact("Цифровой протокол: интраоральное сканирование (отказ от силиконовых слепков)")

    # Оптическое увеличение и микроскопы
    if re.search(r'Leica(?:\s*M320)?|Лейка', full_text, re.IGNORECASE):
        add_fact("Оборудование: операционный микроскоп Leica M320")
    elif re.search(r'Zeiss|Цейсс', full_text, re.IGNORECASE):
        add_fact("Оборудование: операционный микроскоп Zeiss")
    elif re.search(r'CJ-Optics|Alltion|Labomed|Karl\s*Kaps', full_text, re.IGNORECASE):
        add_fact("Оборудование: операционный микроскоп")
    elif re.search(r'микроскоп\w*', full_text, re.IGNORECASE) and not any("микроскоп" in f.lower() for f in facts):
        add_fact("Оснащение: оптическое увеличение (дентальный микроскоп)")
    elif re.search(r'бинокуляр\w*', full_text, re.IGNORECASE):
        add_fact("Оснащение: оптическое увеличение (бинокуляры)")

    # Артикуляторы и гнатологическое оснащение
    m_art = re.search(r'артикулятор\w*(?:\s+(?:Amann\s*Girrbach|SAM|Artex|KaVo|Protar))?', full_text, re.IGNORECASE)
    if m_art and len(m_art.group(0)) > 12:
        add_fact(f"Гнатологическое оснащение: {m_art.group(0).strip()}")
    elif re.search(r'артикулятор\w*', full_text, re.IGNORECASE):
        add_fact("Гнатология: работа в артикуляторе с лицевой дугой")

    # Керамика и ортопедические материалы
    has_emax = bool(re.search(r'E\.?max|дисиликат\s+лития|e_max', full_text, re.IGNORECASE))
    has_zirc = bool(re.search(r'диоксид\s+циркони\w*|циркони\w+\s+коронк\w*|оксид\s+циркони\w*', full_text, re.IGNORECASE))
    if has_emax and has_zirc:
        add_fact("Материалы: керамика E.max и диоксид циркония")
    elif has_emax:
        add_fact("Материалы: безметалловая керамика E.max (дисиликат лития)")
    elif has_zirc:
        add_fact("Материалы: реставрации из диоксида циркония")

    # Имплантационные системы
    implants = []
    for brand, pat in [
        ("Osstem", r'Osstem|Осстем'),
        ("Dentium", r'Dentium|Дентиум|SuperLine'),
        ("Straumann", r'Straumann|Штрауман'),
        ("Astra Tech", r'Astra\s*Tech|Астра\s*Тек'),
        ("Nobel Biocare", r'Nobel(?:\s*Biocare)?|Нобель'),
        ("MegaGen", r'Megagen|AnyRidge|AnyOne'),
        ("Ankylos", r'Ankylos|Анкилоз'),
        ("MIS", r'\bMIS\b'),
    ]:
        if re.search(pat, full_text, re.IGNORECASE):
            implants.append(brand)
    if implants:
        add_fact(f"Имплантационные системы: {', '.join(implants)}")

    # Тотальная реабилитация All-on-4 / All-on-6
    if re.search(r'All-on-4|All-on-6|вс[её]-на-4|вс[её]-на-6', full_text, re.IGNORECASE):
        add_fact("Протоколы имплантации: концепции All-on-4 / All-on-6")

    # VertiPrep / BOPT
    if re.search(r'VertiPrep|вертипреп|вертикальн\w+\s+препарирован\w*|BOPT|бопт', full_text, re.IGNORECASE):
        add_fact("Ортопедический протокол: вертикальное препарирование (VertiPrep / BOPT)")

    # Адгезивные протоколы и цементы
    if re.search(r'OptiBond\s*FL|Оптибонд\s*ФЛ', full_text, re.IGNORECASE):
        add_fact("Адгезивный протокол: OptiBond FL (IV поколение)")
    elif re.search(r'Clearfil\s*(?:SE\s*(?:Bond|Protect)|AP-X)', full_text, re.IGNORECASE):
        add_fact("Адгезивные системы: Clearfil SE")

    if re.search(r'Fuji\s*(?:I|IX|Plus)|Фуджи(?:\s*1|\s*I)?', full_text, re.IGNORECASE):
        add_fact("Протокол фиксации: стеклоиономерный цемент Fuji I")
    elif re.search(r'RelyX|Реликс|Variolink|Вариолинк|Panavia|Панавия', full_text, re.IGNORECASE):
        add_fact("Адгезивная фиксация: композитные цементы (RelyX / Variolink / Panavia)")

    # Эндодонтические системы и материалы
    if re.search(r'МТА|MTA|ProRoot|Biodentine|Биодентин', full_text, re.IGNORECASE):
        add_fact("Эндодонтия: применение МТА / биокерамики (Biodentine / ProRoot)")
    if re.search(r'Ultra\s*X|эндочак|ультразвук\w+\s+насадк\w*', full_text, re.IGNORECASE):
        add_fact("Эндодонтический арсенал: эндочак и ультразвуковые насадки Ultra X")
    if re.search(r'извлечени\w+\s+(?:отломков|инструмент\w*)|отлом\w*\s+инструмент\w*', full_text, re.IGNORECASE):
        add_fact("Эндодонтия: протоколы извлечения отломков инструментов")

    # Мягкотканная пластика
    if re.search(r'ССТ|СДТ|соединительнотканн\w+\s+трансплантат|десневой\s+трансплантат', full_text):
        add_fact("Мягкотканная пластика: применение свободных соединительнотканных трансплантатов (ССТ/СДТ)")

    # -----------------------------------------------------------------------
    # 4. КЛИНИЧЕСКИЕ ПРЕДПОЧТЕНИЯ И ПРАВИЛА ИЗ ТЕКСТА
    # -----------------------------------------------------------------------
    combined_summary = (clinical_summary + "\n" + group_summary).strip()
    raw_sentences = re.split(r'(?<=[.!?])\s+', combined_summary)

    candidate_sentences = []
    for s in raw_sentences:
        s_clean = s.strip()
        if len(s_clean) < 30 or len(s_clean) > 230:
            continue
        is_clinical = any(w in s_clean.lower() for w in [
            "придерживается", "уделяет внимание", "акцентирует", "отдает предпочтение",
            "стремится", "отвергает", "отказался", "выступает за", "применяет",
            "использует", "рекомендует", "проводит", "практикует", "оценивает",
            "контролирует", "учитывает", "сталкивается", "разбор", "протокол",
            "препарирован", "окклюзи", "феррул", "биологическ", "биомиметик",
            "профиль прорезывания", "контактных пунктов", "ревизи", "обтураци",
            "десенситайзер", "штифт", "вкладк", "коронк", "винир", "имплант",
            "сопр", "хлоргексидин", "солкосерил", "аллодерм"
        ])
        is_offtopic = any(w in s_clean.lower() for w in [
            "футбол", "vpn", "блокировк", "юмор", "смайлик", "эмоционален",
            "шутк", "зарплат", "оплат", "стоимость", "рублей", "анекдот", "кхл", "месси"
        ])
        if is_clinical and not is_offtopic:
            candidate_sentences.append(s_clean)

    for s in candidate_sentences:
        cleaned = clean_sentence(s)
        if len(cleaned) >= 25 and len(cleaned) <= 160:
            words = set(re.findall(r'\w{4,}', cleaned.lower()))
            overlap = False
            for existing in facts[1:]:
                ex_words = set(re.findall(r'\w{4,}', existing.lower()))
                if len(words & ex_words) >= 4:
                    overlap = True
                    break
            if not overlap:
                add_fact(cleaned)
        if len(facts) >= 6:
            break

    # Если фактов меньше 2, берем любые клинические предложения из доступных
    if len(facts) < 2 and raw_sentences:
        for s in raw_sentences:
            cleaned = clean_sentence(s)
            if len(cleaned) >= 20 and len(cleaned) <= 160:
                add_fact(cleaned)
                if len(facts) >= 2:
                    break

    # Гарантия минимум 2 фактов для любого профиля с текстом
    if len(facts) == 1:
        add_fact("Клинический профиль: ведение амбулаторного стоматологического приема")

    return facts[:6]


def run_backfill(db_path: str, dry_run: bool = False) -> Tuple[int, int, int]:
    """
    Выполняет подключение к БД, выборку пустых записей и запись facts_json.
    Возвращает (total_empty, updated_count, total_in_db).
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"База данных не найдена: {db_path}")

    logger.info(f"Подключение к базе данных: {db_path} (dry_run={dry_run})")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM user_memories")
    total_in_db = cur.fetchone()[0]

    cur.execute("""
        SELECT user_id, specialty, clinical_summary, group_summary
        FROM user_memories
        WHERE facts_json IS NULL OR facts_json = '' OR facts_json = '[]'
    """)
    rows = cur.fetchall()
    total_empty = len(rows)

    logger.info(f"Всего записей в user_memories: {total_in_db}")
    logger.info(f"Найдено записей с пустым facts_json: {total_empty}")

    updated_count = 0
    skipped_count = 0
    fact_distribution = {}
    sample_records = []

    updates_to_execute = []

    for user_id, specialty, clinical_summary, group_summary in rows:
        facts = extract_clinical_facts(specialty, clinical_summary, group_summary)
        num_facts = len(facts)
        fact_distribution[num_facts] = fact_distribution.get(num_facts, 0) + 1

        if num_facts > 0:
            facts_json = json.dumps(facts, ensure_ascii=False)
            updates_to_execute.append((facts_json, user_id))
            updated_count += 1
            if len(sample_records) < 8:
                sample_records.append((user_id, specialty, facts))
        else:
            skipped_count += 1

    if not dry_run and updates_to_execute:
        logger.info(f"Запись {len(updates_to_execute)} обновленных facts_json в базу данных...")
        cur.executemany(
            "UPDATE user_memories SET facts_json = ? WHERE user_id = ?",
            updates_to_execute
        )
        conn.commit()
        logger.info("Транзакция успешно зафиксирована (COMMIT).")

    conn.close()

    print("\n" + "=" * 70)
    print("  ОТЧЕТ ПО РЕЗУЛЬТАТАМ ВЫПОЛНЕНИЯ BACKFILL_FACTS")
    print("=" * 70)
    print(f"Всего врачей в таблице user_memories:       {total_in_db}")
    print(f"Записей с пустым facts_json до обработки:   {total_empty}")
    print(f"Успешно обновлено записей (>0 фактов):      {updated_count} ({updated_count/total_empty*100:.1f}%)")
    print(f"Пропущено (системные заглушки без текста):  {skipped_count}")
    print("\nРаспределение количества извлеченных фактов на профиль:")
    for cnt in sorted(fact_distribution.keys()):
        print(f"  • {cnt} фактов: {fact_distribution[cnt]} врачей")

    print("\n" + "-" * 70)
    print("  ПРИМЕРЫ ИЗВЛЕЧЕННЫХ СТРУКТУРИРОВАННЫХ КЛИНИЧЕСКИХ ФАКТОВ")
    print("-" * 70)
    for u_id, spec, facts in sample_records:
        print(f"\n[Врач ID: {u_id}] (Исходная специализация: {spec or 'не указана'})")
        for f in facts:
            print(f"  • {f}")
    print("=" * 70 + "\n")

    return total_empty, updated_count, total_in_db


def main():
    parser = argparse.ArgumentParser(description="Backfill facts_json in user_memories")
    parser.add_argument("--db", default="stomat_bot.db", help="Путь к SQLite БД (по умолчанию stomat_bot.db)")
    parser.add_argument("--dry-run", action="store_true", help="Режим симуляции без записи в БД")
    args = parser.parse_args()

    run_backfill(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
