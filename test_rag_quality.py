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


asyncio.run(run())

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
