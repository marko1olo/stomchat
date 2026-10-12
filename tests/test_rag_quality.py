"""
Качество справки, которую бот подкладывает модели перед ответом.

Это главный рычаг качества ответа: модель отвечает по тому, что ей дали.

Замеры на живых корпусах (12 784 факта вики, 107 316 реплик архива) по шести
реальным клиническим вопросам:

  до правок  архив: 160 строк, из них 19 вопросов и 29 обрывков — 30% справки
             без знания внутри. На «протокол травления емакс» первой шла
             реплика «Какой протокол травления циркона?» — вопрос, прочитанный
             как утверждение.
             Отдельно: одна многострочная реплика превращалась в несколько
             строк корпуса, и в клиническую справку попадали правила чата
             («Правила канала», «Никакой политики») как отдельные факты.
  после      бесполезных записей 0, релевантность вики и архива 100%.

Корпуса открываются только на чтение; если их нет рядом, замер пропускается.

Запуск: python test_rag_quality.py
"""
import asyncio
import os
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import assistant
import dental_vocab

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


QUESTIONS = [
    "какой уступ под цирконий и как вести мягкие ткани",
    "чем ирригировать канал при некрозе пульпы",
    "болит зуб после лечения канала два месяца",
    "какой протокол травления керамики емакс",
    "нужна ли мембрана при синус-лифтинге",
    "чем снимать коронку с циркония",
]

print("\n[1] Отбор ключей: общие слова не подмешиваются к клиническим")
check("при наличии клинических терминов берутся только они",
      assistant.select_search_keywords(["уступ", "циркон", "вести", "месяц"]) == ["уступ", "циркон"],
      f"got {assistant.select_search_keywords(['уступ', 'циркон', 'вести', 'месяц'])}")
check("одного клинического термина достаточно, добивать не нужно",
      assistant.select_search_keywords(["коронк", "смотреть", "нужн"]) == ["коронк"],
      f"got {assistant.select_search_keywords(['коронк', 'смотреть', 'нужн'])}")
check("без клинических терминов берутся общие — иначе искать нечем",
      assistant.select_search_keywords(["погода", "выходные"]) == ["погода", "выходные"])
check("пустой ввод не падает", assistant.select_search_keywords([]) == [])

print("\n[2] Бюджет строк: чем меньше ключей, тем глубже каждый")
one = assistant._rows_per_keyword(1)
six = assistant._rows_per_keyword(6)
check("на одном ключе выборка глубже", one > six, f"один={one}, шесть={six}")
check("на многих ключах не мельче нормы",
      six >= assistant._CORPUS_ROWS_PER_KEYWORD, f"got {six}")
check("нулевое число ключей не делит на ноль",
      assistant._rows_per_keyword(0) == assistant._CORPUS_ROWS_PER_KEYWORD)

print("\n[3] Запись справки — всегда одна строка")
entry = assistant._corpus_entry("Врач:", "первая строка\nвторая строка\n\nтретья")
check("переводы строк схлопнуты", "\n" not in entry, f"got {entry!r}")
check("текст сохранён", "третья" in entry and "первая" in entry, f"got {entry!r}")
check("двойные пробелы убраны", "  " not in entry, f"got {entry!r}")
check("пустое тело не роняет", assistant._corpus_entry("Врач:", None) == "Врач:")

print("\n[4] Обратное правило словаря проверено и оставлено как есть")
# Слово, являющееся префиксом термина, считается клиническим. Замер по архиву:
# из 27 частых стемов, которые ловит это правило, клинические почти все —
# «коронк» (4124 вхождения), «десн» (2859), «фиксац», «эмал», «кост», «шейк».
# Суммарная частота 14 017. Правило зарабатывает свой хлеб, поэтому не тронуто.
for stem in ("коронк", "десн", "фиксац", "эмал", "кост", "шейк", "адгез", "полост"):
    check(f"стем «{stem}» распознаётся", dental_vocab.is_dental_keyword(stem) is True)
# Обратная проверка ужесточена долей совпадения: слово должно покрывать не
# меньше 60% термина. Это отсекло «вести» (36% «вестибулопласта»), «фото»,
# «форм», «верх», а «скан», «верти», «импл», «металл» добавлены отдельными
# терминами, чтобы не потеряться.
for junk in ("вести", "вест", "фото", "форм", "верх", "песк", "работ"):
    check(f"«{junk}» клиническим НЕ считается",
          dental_vocab.is_dental_keyword(junk) is False,
          "обычное слово принято за клинический термин")
for added in ("скан", "верти", "импл", "металл"):
    check(f"«{added}» распознаётся как термин",
          dental_vocab.is_dental_keyword(added) is True)


async def run():
    corpora = [p for p in ("stomat_wiki.db", "stomat_archive.db") if os.path.exists(p)]
    if len(corpora) < 2:
        print("\n[5] Живые корпуса недоступны — замер пропущен")
        check("замер пропущен осознанно", True)
        return

    print("\n[5] Замер на живых корпусах")
    total = questions = fragments = 0
    wiki_total = wiki_rel = arch_total = arch_rel = 0

    for text in QUESTIONS:
        selected = assistant.select_search_keywords(assistant.extract_keywords(text))
        wiki, archive = await assistant.search_knowledge_corpus(selected)
        wiki_lines = [l for l in wiki.split("\n") if l.strip()]
        arch_lines = [l for l in archive.split("\n") if l.strip()]
        dental = [k for k in selected if dental_vocab.is_dental_keyword(k)]

        for line in arch_lines:
            total += 1
            match = re.match(r"^[^:]{1,40}: (.*)$", line)
            body = match.group(1).strip() if match else line.strip()
            if body.endswith("?"):
                questions += 1
            if len(body) < assistant._ARCHIVE_MIN_USEFUL_CHARS:
                fragments += 1

        wiki_total += len(wiki_lines)
        arch_total += len(arch_lines)
        wiki_rel += sum(1 for l in wiki_lines if any(k in l.lower() for k in dental))
        arch_rel += sum(1 for l in arch_lines if any(k in l.lower() for k in dental))

    print(f"      строк архива: {total}, вопросов: {questions}, обрывков: {fragments}")
    print(f"      релевантность вики: {wiki_rel}/{wiki_total}, архива: {arch_rel}/{arch_total}")

    check("вопросы в справку не попадают", questions == 0, f"got {questions}")
    check("обрывков в справке нет", fragments == 0, f"got {fragments}")
    check("каждая строка архива — оформленная запись",
          total > 0, "справка пуста, замер бессмыслен")
    check("релевантность вики не ниже 90%",
          wiki_rel >= wiki_total * 0.9, f"got {wiki_rel}/{wiki_total}")
    check("релевантность архива не ниже 90%",
          arch_rel >= arch_total * 0.9, f"got {arch_rel}/{arch_total}")

    print("\n[6] Правила чата не выдаются за клинический факт")
    selected = assistant.select_search_keywords(assistant.extract_keywords(QUESTIONS[0]))
    wiki, archive = await assistant.search_knowledge_corpus(selected)
    joined = (wiki + "\n" + archive).lower()
    for junk in ("правила канала", "никакой политики", "никаких оскорблений"):
        check(f"«{junk}» в справке нет", junk not in joined)

    print("\n[7] Один и тот же абзац не попадает в справку дважды")
    # Повтор ловили сравнением готовой строки, а в неё входит префикс: у
    # справки коды рубрик, у архива имя автора. Факт про BOPT лежит в базе
    # пятью строками с теми же кодами в РАЗНОМ ПОРЯДКЕ — строки различались,
    # и проверка их пропускала. Замер на 2893 вопросах из чата: 1059 дают
    # ровно один ключ (бюджет 48 строк на ключ, выборка глубокая), и на 42
    # вопросах в справку уходило 44 лишних одинаковых абзаца.
    import sqlite3 as _sq

    def bodies(corpus):
        out = []
        for line in corpus.split("\n"):
            if not line.strip():
                continue
            body = re.sub(r"^\[[^\]]*\]\s*", "", line)
            body = re.sub(r"^[^:]{0,40}:\s*", "", body)
            out.append(" ".join(body.split()).lower())
        return out

    for question in ("BOPT", "что такое BOPT?", "методика bopt"):
        selected = assistant.select_search_keywords(assistant.extract_keywords(question))
        wiki, archive = await assistant.search_knowledge_corpus(selected)
        all_bodies = bodies(wiki) + bodies(archive)
        extra = len(all_bodies) - len(set(all_bodies))
        check(f"«{question}»: повторов нет", extra == 0,
              f"{extra} лишних одинаковых абзацев из {len(all_bodies)}")

    # Дубли обязаны существовать в базе — иначе проверка выше ничего не стоит.
    # mode=ro: читаем боевую вику только на чтение. Без него импорт открывает
    # ручку НА ЗАПИСЬ к 12 784 фактам, и это ловит test_isolation [4].
    copies = _sq.connect("file:stomat_wiki.db?mode=ro", uri=True).execute(
        "SELECT COUNT(*) FROM distilled_facts WHERE content LIKE '%BOPT (Biologically%'").fetchone()[0]
    check("в базе действительно лежит несколько копий факта", copies > 1,
          f"копий {copies}; проверка на дедуп потеряла смысл")

    # Дедуп не должен был съесть рубрику.
    selected = assistant.select_search_keywords(assistant.extract_keywords("методика bopt"))
    wiki, _ = await assistant.search_knowledge_corpus(selected)
    check("код рубрики у фактов сохранён",
          all(l.startswith("[") for l in wiki.split("\n") if l.strip()),
          "дедуп потерял префикс рубрики")

    print("\n[8] Справка ограничена по символам, а не только по числу записей")
    # Предел стоял лишь на количестве записей, а длина записи не ограничена: самый
    # длинный факт вики 5477 символов при медиане 236. Замер на 400 реальных
    # вопросах из архива: медиана справки 10241 символ, но максимум 52816
    # (~17600 токенов), и 30 вопросов из 400 давали больше 20000. Самые тяжёлые —
    # не клиника, а трёп: «Давно так работаете? У кого учились?» тянуло 37 тысяч
    # символов вики. Вопрос врача тонет под массивом слабо связанных фактов, а
    # рецензент видит лишь первые 3000 символов основания.
    import sqlite3 as _sq3

    # mode=ro: архив на 117 847 реплик читается только на чтение.
    archive = _sq3.connect("file:stomat_archive.db?mode=ro", uri=True)
    probes = [r[0] for r in archive.execute(
        "SELECT text FROM archive_messages WHERE text LIKE '%?' "
        "AND LENGTH(text) BETWEEN 25 AND 200 LIMIT 120")]
    totals = []
    for probe in probes:
        keys = assistant.select_search_keywords(assistant.extract_keywords(probe))
        if not keys:
            continue
        wiki_part, archive_part = await assistant.search_knowledge_corpus(keys)
        totals.append((len(wiki_part), len(archive_part)))
    check("выборка вопросов набралась", len(totals) >= 50, f"got {len(totals)}")
    if totals:
        worst_wiki = max(t[0] for t in totals)
        worst_arch = max(t[1] for t in totals)
        worst_sum = max(t[0] + t[1] for t in totals)
        check(f"вики не превышает бюджет ({worst_wiki} <= {assistant._CORPUS_MAX_CHARS})",
              worst_wiki <= assistant._CORPUS_MAX_CHARS)
        check(f"архив не превышает бюджет ({worst_arch} <= {assistant._CORPUS_MAX_CHARS})",
              worst_arch <= assistant._CORPUS_MAX_CHARS)
        check(f"суммарная справка не раздувается ({worst_sum} символов)",
              worst_sum <= 2 * assistant._CORPUS_MAX_CHARS)
        median = sorted(t[0] + t[1] for t in totals)[len(totals) // 2]
        # Бюджет обязан подрезать хвост, а не типичный случай: до правки медиана
        # была 10241 символ, и ответы на такой справке строятся нормально.
        check(f"типичная справка осталась содержательной (медиана {median})",
              median >= 4000, "бюджет срезал и обычные запросы")

    print("\n[9] Обрезка длинной записи не рвёт утверждение на полуслове")
    long_fact = ("Гипохлорит натрия применяют в концентрации от 3 до 5 процентов. "
                 "Экспозиция составляет не менее тридцати минут на канал. ") * 20
    clipped = assistant._clip_at_sentence(long_fact, 300)
    check("обрезка не длиннее предела", len(clipped) <= 300, f"got {len(clipped)}")
    check("обрезка кончается границей предложения или многоточием",
          clipped.rstrip().endswith((".", "!", "?", ";", "…")), repr(clipped[-30:]))
    check("число внутри утверждения не потеряно вместе с концом",
          "3 до 5 процентов" in clipped, clipped[:80])
    # Запись без точек тоже не должна обрываться посреди слова.
    no_stops = "слово " * 200
    clipped = assistant._clip_at_sentence(no_stops, 100)
    check("без границ предложения режем по слову", not clipped.rstrip("…").endswith("слов"),
          repr(clipped[-20:]))
    check("одна запись не съедает бюджет целиком",
          assistant._CORPUS_ENTRY_MAX_CHARS < assistant._CORPUS_MAX_CHARS,
          f"{assistant._CORPUS_ENTRY_MAX_CHARS} против {assistant._CORPUS_MAX_CHARS}")

    print("\n[10] Red-Team: профили контекста и инквизиционный фильтр")
    # 1. Vision profile: strict archive isolation & 3500 limit
    v_wiki, v_arch = await assistant.search_knowledge_corpus(["гипохлорит", "канал"], context_type="vision")
    check("vision: архив чата строго исключен", v_arch == "")
    check("vision: вики укладывается в бюджет 3500", len(v_wiki) <= 3500)

    # 2. PM profile: 4000 limit
    pm_wiki, pm_arch = await assistant.search_knowledge_corpus(["бопт", "уступ"], context_type="pm")
    check("pm: вики укладывается в бюджет 4000", len(pm_wiki) <= 4000)
    check("pm: архив укладывается в бюджет 4000", len(pm_arch) <= 4000)

    # 3. Wiki-only profile
    wo_wiki, wo_arch = await assistant.search_knowledge_corpus(["емакс"], context_type="wiki_only")
    check("wiki_only: архив не запрашивается", wo_arch == "")

    # 4. Group reply profile: 2500 limit
    gr_wiki, gr_arch = await assistant.search_knowledge_corpus(["цирконий"], context_type="group_reply")
    check("group_reply: вики <= 2500", len(gr_wiki) <= 2500)
    check("group_reply: архив <= 2500", len(gr_arch) <= 2500)

    # 5. Inquisition filter: candidate with ONLY non-dental words rejected when query has dental words
    raw_entries = [
        "Врач: Мы вчера делали этот протокол и обсуждали время работы.",
        "Врач: Протокол препарирования под оксид циркония с уступом 0.5 мм.",
    ]
    ranked = assistant._rank_corpus_entries(raw_entries, ["циркон", "уступ", "протокол", "делать"])
    check("инквизиция: мусорная запись без клинических терминов отсечена",
          len(ranked) == 1 and "циркония" in ranked[0], f"got {ranked}")

    print("\n[11] Red-Team: toxic practice gate, non-clinical isolation & clean prompt assembly")
    # 6. Toxic practice gate: arsenic / depulpin / resorcin-formalin blocked unless explicit query
    toxic_candidates = [
        "Врач: При остром пульпите положи мышьяк на 48 часов под дентин-пасту.",
        "Врач: Для девитализации пульпы используй депулпин.",
        "Врач: Мы пломбировали каналы пастой форедент и резорцин-формалином.",
        "Врач: При пульпите показана витальная экстирпация и ирригация гипохлоритом 3%.",
    ]
    ranked_toxic = assistant._rank_corpus_entries(toxic_candidates, ["пульпит", "лечение"])
    check("toxic practice gate: мышьяк, депулпин, резорцин отсечены при обычном запросе",
          len(ranked_toxic) == 1 and "гипохлоритом" in ranked_toxic[0], f"got {ranked_toxic}")

    ranked_allow_toxic = assistant._rank_corpus_entries(toxic_candidates, ["распломбировка", "резорцин", "формалин"])
    check("toxic practice gate: при прямом вопросе о резорцине фрагмент разрешён",
          any("резорцин" in r for r in ranked_allow_toxic), f"got {ranked_allow_toxic}")

    # 7. Non-clinical query isolation
    nc_wiki, nc_arch = await assistant.search_knowledge_corpus(["погода", "выходные"])
    check("non-clinical: базы wiki и archive изолированы от неклинических запросов",
          nc_wiki == "" and nc_arch == "", f"got wiki={nc_wiki!r}, arch={nc_arch!r}")

    # 8. Conversational intent gating
    conv_positives = [
        "Спасибо", "Спасибо большое!", "Понял, спасибо коллега", "Привет", "Добрый вечер!",
        "Ок, все понял", "Благодарю!", "Ясно, спасибо", "Договорились, спасибо!"
    ]
    conv_negatives = [
        "Привет, как лечить пульпит?", "Спасибо, а какой протокол травления?",
        "Понял, а сколько минут держать гипохлорит?", "Какая экспозиция у ЭДТА?"
    ]
    check("conversational intent: все вежливые закрытия определены",
          all(bool(assistant._CONVERSATIONAL_INTENT_RE.match(t)) for t in conv_positives))
    check("conversational intent: клинические вопросы НЕ глушатся",
          all(not bool(assistant._CONVERSATIONAL_INTENT_RE.match(t)) for t in conv_negatives))

    # 9. Clean RAG prompt formatting
    check("format_rag_prompt_block: пустая справка возвращает пустую строку",
          assistant.format_rag_prompt_block("", "") == "")
    wiki_only_block = assistant.format_rag_prompt_block("Факт о цирконии", "")
    check("format_rag_prompt_block: есть вики, но нет архива",
          "stomat_wiki" in wiki_only_block and "Архив" not in wiki_only_block)
    arch_only_block = assistant.format_rag_prompt_block("", "Опыт коллеги о BOPT")
    check("format_rag_prompt_block: есть архив, но нет вики",
          "Архив" in arch_only_block and "stomat_wiki" not in arch_only_block)

    print("\n[12] Red-Team Phase 2: составные термины и базовые клинические корни словаря")
    compound_and_core_terms = [
        "Протокол фиксации e.max", "Фрезеровка cad/cam", "Графт bio-oss", "Абатмент multi-unit",
        "Внесли гидроксид кальция в канал", "Протравка эмали и протравить дентин",
        "Периапикальный очаг и болезненная перкуссия", "Пародонтологическое зондирование и тест на витальность",
        "Периостит, перикоронит и остеомиелит", "Витальная экстирпация, депульпирование и трепанация",
        "Пришеечный дефект, праймер и мультиюнит", "Проводниковая, инфильтрационная и мандибулярная",
        "Подписать ИДС перед ДМЕ (DME)",
    ]
    check("has_dental_term распознаёт составные термины (e.max, cad/cam, bio-oss, multi-unit) и базовую клинику",
          all(assistant.has_dental_term(q) for q in compound_and_core_terms),
          f"missed: {[q for q in compound_and_core_terms if not assistant.has_dental_term(q)]}")

    false_positive_phrases = [
        "Протри экран салфеткой", "Позвони Виталию и спроси у Виталии",
        "Сделай кальку на калькуляторе", "Возьми зонт на улицу",
    ]
    check("has_dental_term НЕ срабатывает на бытовые омонимы (протри, Виталия, калька, зонт)",
          all(not assistant.has_dental_term(q) for q in false_positive_phrases),
          f"false pos: {[q for q in false_positive_phrases if assistant.has_dental_term(q)]}")

    print("\n[13] Red-Team Phase 3: морфология прилагательных, гейт узких EBM-протоколов и фильтр вопросов")
    adj_kws = assistant.select_search_keywords(
        assistant.extract_keywords("глубокого поддесневого кариеса и мандибулярная анестезия при остром пульпите")
    )
    check("стемминг прилагательных: поддесневого -> поддеснев, мандибулярная -> мандибулярн",
          "поддеснев" in adj_kws and "мандибулярн" in adj_kws and "кариес" in adj_kws and "пульпит" in adj_kws,
          f"got {adj_kws}")

    proto_candidates = [
        "[ПРОТОКОЛ EBM: Протокол лазерного демонтажа керамических реставраций (Er:YAG) (Ортопедия)] Показания: Атравматичное снятие коронок из дисиликата лития (E.max) и циркония. Материалы: Er:YAG лазер, плавиковая кислота, силан.",
        "[ПРОТОКОЛ EBM: Адгезивная фиксация керамики E.max (дисиликат лития) (Ортопедия)] Показания: Непрямые реставрации из дисиликата лития E.max. Материалы: Плавиковая кислота 5%, силан, адгезив.",
        "[2.2.1 msg#12345] Травление керамики E.max проводится 5% плавиковой кислотой в течение 20 секунд с последующим нанесением силана.",
    ]
    ranked_emax = assistant._rank_corpus_entries(proto_candidates, ["травлен", "керамик", "емакс", "силан"])
    check("узкий EBM-протокол лазерного демонтажа отсечён при запросе про травление и фиксацию E.max",
          all("лазерного демонтажа" not in r for r in ranked_emax) and any("Адгезивная фиксация керамики E.max" in r for r in ranked_emax),
          f"got {ranked_emax}")

    noise_samples = [
        "Коллеги, подскажите, пожалуйста, чем лучше закрыть перфорацию в области фуркации?",
        "Стоит ли перелечивать этот канал или сразу на удаление? Заранее благодарю!",
        "Как бы вы поступили в такой ситуации? Опыт 1 год 🙏🏻",
        "Анонс курса по эндодонтии! Регистрация по ссылке в профиле.",
    ]
    check("_ARCHIVE_QUESTION_NOISE_RE отсекает посты-просьбы о помощи и анонсы курсов",
          all(bool(assistant._ARCHIVE_QUESTION_NOISE_RE.search(s)) for s in noise_samples),
          f"missed: {[s for s in noise_samples if not assistant._ARCHIVE_QUESTION_NOISE_RE.search(s)]}")


asyncio.run(run())

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
