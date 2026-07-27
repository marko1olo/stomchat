"""
Общий словарь триажа и фильтр сообщений для дайджеста.

До этого у summarizer был свой список на 80 корней, а у ассистента — свой на
227. Клинические реплики, чьих терминов не было в коротком списке, выпадали из
дайджеста молча. Замер на 4000 живых сообщений: 166 таких сообщений.

Здесь проверяется единый словарь: что клиника распознаётся, что бытовая речь
НЕ распознаётся (это правило приоритетнее — ложный триггер медицинского
ассистента дороже пропуска), и что фильтр дайджеста ведёт себя как заявлено.

Запуск: python test_dental_vocab.py
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import dental_vocab as dv
import summarizer
import assistant

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


print("\n[1] Словарь один на всех потребителей")
check("assistant берёт словарь из dental_vocab",
      set(assistant.DENTAL_KEYWORDS) == set(dv.DENTAL_KEYWORDS))
check("копии словаря в summarizer больше нет",
      "dental_keywords = {" not in open("summarizer.py", encoding="utf-8").read())
check("распознаватель один и тот же", assistant.is_dental_keyword is dv.is_dental_keyword)
check("словарь непустой и полный", len(dv.DENTAL_KEYWORDS) >= 227, f"got {len(dv.DENTAL_KEYWORDS)}")

print("\n[2] Бытовая речь клиникой НЕ считается")
# Приоритетнее всего остального: ложный триггер медицинского ассистента в
# профессиональном чате дороже пропущенной реплики.
for phrase in ["привет всем", "спасибо, помогло", "ага", "ну ок)", "доброе утро коллеги",
               "кто идет на конференцию", "удалось договориться с поставщиком",
               "удалить сообщение", "плоскость стола", "это смешно", "я в отпуске",
               "с днем рождения", "Кто-то придёт позже", "Вы самый эффективный доктор"]:
    check(f"«{phrase}» не клиника", dv.has_dental_term(phrase.lower()) is False)

print("\n[3] Клиническая речь распознаётся, включая склонения")
for phrase in ["болит зуб после лечения канала", "какой уступ под цирконий",
               "чем снимать коронку с циркония", "А раз нет - коронки на 6 и 7",
               "Логичнее было 2 коронки встречных", "периодонтит не всегда заканчивается заживлением",
               "Бонд хим отверждения", "Ретрит файлы и протейперы", "Ретракции не было перед фиксацией",
               "корневой канал запломбирован", "кость атрофирована в области 36",
               "под седацией удалял восьмёрки", "интактный зуб", "Вошли сканмаркеры без усилий",
               "Уз, бор", "нужно КТ", "кт-снимок приложил"]:
    check(f"«{phrase[:38]}» клиника", dv.has_dental_term(phrase.lower()) is True)

print("\n[4] Двухбуквенные аббревиатуры сверяются целиком")
# "кт" как префикс цеплялся к «кто», и любая реплика с этим словом проходила.
check("«кт» распознаётся", dv.is_dental_keyword("кт") is True)
check("«кто» не распознаётся", dv.is_dental_keyword("кто") is False)
check("«кто-то» не распознаётся", dv.has_dental_term("кто-то") is False)

print("\n[5] Стемминг: словарная форма и форма из чата сходятся")
check("«коронки» -> «коронк»", dv.stem_word("коронки") == "коронк")
check("«коронку» -> «коронк»", dv.stem_word("коронку") == "коронк")
check("короткое слово не режется", dv.stem_word("зуб") == "зуб")
check("остаток короче четырёх не режется", dv.stem_word("дуга") == "дуга")
for form in ("коронка", "коронки", "коронку", "коронок", "коронками"):
    check(f"форма «{form}» распознана", dv.is_dental_keyword(dv.stem_word(form)) is True)

print("\n[6] Дефисные термины ловятся и целиком, и по частям")
check("«air-flow» распознан", dv.has_dental_term("делали air-flow") is True)
check("«airflow» распознан", dv.has_dental_term("делали airflow") is True)
check("«e-max» распознан", dv.has_dental_term("коронка e-max") is True)

print("\n[7] Добавленные корни не тянут бытовую речь")
# "удал" из старого списка summarizer сознательно НЕ добавлен: он матчит
# "удалось" и "удалить сообщение".
check("«удал» в словарь не попал", "удал" not in dv.DENTAL_KEYWORDS)
for phrase in ["удалось", "удалить", "удаленная работа"]:
    check(f"«{phrase}» не клиника", dv.has_dental_term(phrase) is False)
check("«кость» добавлена", "кость" in dv.DENTAL_KEYWORDS)
check("но «плоскость» ей не матчится", dv.has_dental_term("плоскость") is False)

print("\n[8] Фильтр дайджеста: что оставляет и что выбрасывает")
def row(text, media_desc=None, url=None):
    return (1, "Врач", None, text, media_desc, "2026-07-27 12:00:00", None, url)

kept = summarizer.filter_useful_messages([row("Периодонтит тоже не всегда заканчивается заживлением")])
check("клиническое утверждение без вопроса остаётся", len(kept) == 1, f"got {kept}")

kept = summarizer.filter_useful_messages([row("ага")])
check("короткий флуд выбрасывается", kept == [], f"got {kept}")

kept = summarizer.filter_useful_messages([row("а что вы думаете про это?")])
check("вопрос остаётся даже без терминов", len(kept) == 1, f"got {kept}")

kept = summarizer.filter_useful_messages([row("что?")])
check("слишком короткий вопрос выбрасывается", kept == [], f"got {kept}")

kept = summarizer.filter_useful_messages([row("", media_desc="снимок 36 зуба")])
check("сообщение с описанием медиа остаётся", len(kept) == 1, f"got {kept}")

kept = summarizer.filter_useful_messages([row("сколько берете за коронку в рублях")])
check("деловая лексика остаётся", len(kept) == 1, f"got {kept}")

kept = summarizer.filter_useful_messages([row("аренда кабинета сколько выходит")])
check("аренда/цены остаются без клинических терминов", len(kept) == 1, f"got {kept}")

check("пустой вход не падает", summarizer.filter_useful_messages([]) == [])
check("None-текст не падает", summarizer.filter_useful_messages([row(None)]) == [])

print("\n[9] Ограничение по устройству: совпадение только по началу слова")
# Честно фиксируем предел: сопоставление префиксное, поэтому термин внутри
# слова не находится. Тест существует, чтобы это не выглядело сюрпризом.
check("«шлифовка» распознана (термин в начале)", dv.has_dental_term("шлифовка") is True)
check("«пришлифовка» НЕ распознана (термин в середине)",
      dv.has_dental_term("пришлифовка") is False)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
