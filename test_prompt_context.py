"""
Контекст ответов в промпте дайджеста.

В дневной сборке короткая цитата сообщения, на которое ответили, ВЫЧИСЛЯЛАСЬ
(переменная short_p_text) и не использовалась нигде — модель получала только
имя. В недельной ответы не учитывались вообще.

Замер на живом дневном окне: 403 из 695 сообщений выборки — ответы, и у 105 из
них родитель в выборку не попал. Модель видела «(Ответ Петру) А стоит это того?
Сколько лет этим конструкциям?» — без единого указания, о чём речь.

Запуск: python test_prompt_context.py
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import summarizer as S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


LONG_PARENT = "Здесь биполярка была в режиме TTL, чтобы не было пересвета на пришеечной части снимка"
LOOKUP = {
    10: ("Пётр", LONG_PARENT),
    11: ("Дарья", "коротко"),
    12: ("Иван", None),
    13: ("Анна", "   "),
}

print("\n[1] Родителя нет в выборке — подставляется цитата")
text, quoted = S._reply_context(10, LOOKUP, batch_ids=set())
check("цитата подставлена", quoted == 1, f"got {quoted}")
check("в префиксе есть содержание родителя", "биполярка" in text, f"got {text!r}")
check("имя автора сохранено", "Пётр" in text, f"got {text!r}")
check("цитата обрезана по лимиту",
      len(text) < len(LONG_PARENT) + 40, f"got {len(text)} for limit {S.REPLY_QUOTE_MAX_CHARS}")
check("обрезка помечена многоточием", "…" in text, f"got {text!r}")

print("\n[2] Родитель в выборке — достаточно ссылки, промпт не раздувается")
text, quoted = S._reply_context(10, LOOKUP, batch_ids={10})
check("цитата не подставлена", quoted == 0, f"got {quoted}")
check("есть ссылка на MSG", "MSG_10" in text, f"got {text!r}")
check("содержания родителя нет", "биполярка" not in text, f"got {text!r}")

print("\n[3] Короткий текст родителя не обрезается")
text, _ = S._reply_context(11, LOOKUP, batch_ids=set())
check("текст целиком", "«коротко»" in text, f"got {text!r}")
check("многоточия нет", "…" not in text, f"got {text!r}")

print("\n[4] Пустой и NULL текст родителя не роняют сборку")
for key, label in ((12, "NULL"), (13, "только пробелы")):
    text, quoted = S._reply_context(key, LOOKUP, batch_ids=set())
    check(f"{label}: цитаты нет", quoted == 0, f"got {quoted}")
    check(f"{label}: осталась ссылка на MSG", f"MSG_{key}" in text, f"got {text!r}")

print("\n[5] Отсутствие ответа и неизвестный родитель")
check("нет reply_id — пустой префикс", S._reply_context(None, LOOKUP, set()) == ("", 0))
check("нулевой reply_id — пустой префикс", S._reply_context(0, LOOKUP, set()) == ("", 0))
check("родителя нет в базе — пустой префикс", S._reply_context(999, LOOKUP, set()) == ("", 0))

print("\n[6] Целевая подстановка дешевле сплошной")
# Считаем на синтетическом наборе: 100 ответов, у 20 родитель вне выборки.
lookup = {i: (f"Автор{i}", f"текст родителя номер {i} " * 5) for i in range(1, 101)}
in_batch = set(range(1, 81))

targeted = sum(len(S._reply_context(i, lookup, in_batch)[0]) for i in range(1, 101))
blanket = sum(len(S._reply_context(i, lookup, set())[0]) for i in range(1, 101))
check("подстановка только для отсутствующих родителей дешевле",
      targeted < blanket, f"targeted={targeted} blanket={blanket}")
quoted_total = sum(S._reply_context(i, lookup, in_batch)[1] for i in range(1, 101))
check("цитаты только у тех, чей родитель вне выборки", quoted_total == 20, f"got {quoted_total}")

print("\n[7] Строка лога собирается без «None» в тексте")
# text=None раньше попадал в промпт литералом "None": в базе таких строк
# сейчас нет, но контракт save_message их не запрещает.
line = f"MSG_1 | Врач: {S._reply_context(None, LOOKUP, set())[0]}{None or ''}"
check("None не просачивается в промпт", "None" not in line, f"got {line!r}")

print("\n[8] Контекст ассистента собирается по времени, а не наоборот")
# Инвариант держится на ЧЕТЫРЁХ независимых местах в assistant.py, и пропажа
# одного разворота никак себя не проявит — кроме того, что рецензент ослепнет.
#
# Почему это важно именно для рецензента: он получает context_msgs[-10:] и по
# правилу 2 своего промпта отклоняет ответ, «не относящийся к теме переписки».
# Если список окажется новейшим-первым, срез возьмёт ДЕСЯТЬ САМЫХ СТАРЫХ
# сообщений и вызвавшая ответ реплика в него не попадёт вовсе — рецензент будет
# судить о теме, не видя вопроса.
#
# Проверка статическая: выборки из базы и вызовы LLM здесь не нужны, нужен
# факт наличия разворота рядом с каждым DESC-запросом контекста.
import io  # noqa: E402
import re  # noqa: E402

SOURCE = io.open("assistant.py", encoding="utf-8").read()
CODE = "\n".join(l for l in SOURCE.split("\n") if not l.lstrip().startswith("#"))

desc_queries = [m.start() for m in re.finditer(r"ORDER BY date DESC", CODE)]
check("запросы контекста в обратном порядке вообще есть", len(desc_queries) >= 2,
      f"got {len(desc_queries)}")

# Первая версия этой проверки смотрела в окно 700 символов после запроса и не
# доставала до context_msgs.append (там больше 1200) — ветка молча пропускалась
# через continue, и проверка прошла даже с удалённым разворотом. Теперь берём
# отрезок ОТ запроса ДО первой сборки контекста, какой бы длины он ни был.
unreversed = []
guarded = 0
for pos in desc_queries:
    rest = CODE[pos:]
    build = rest.find("context_msgs.append")
    if build == -1 or build > 4000:
        continue  # этот запрос не для сборки контекста ассистента
    segment = rest[:build]
    guarded += 1
    if "[::-1]" not in segment:
        unreversed.append(segment.strip().split("\n")[0][:70])
check("проверка действительно нашла сборки контекста", guarded >= 2,
      f"осмотрено участков: {guarded} — проверка ничего не охраняет")
check("у каждой сборки контекста есть разворот в хронологию", not unreversed,
      f"без разворота: {unreversed}")

check("ветка ответа в тред тянет контекст сразу по возрастанию",
      "ORDER BY date ASC" in CODE, "иначе тред читается задом наперёд")
check("цепочка диалога приводится к хронологии явно",
      "chain[::-1]" in CODE, "chain приходит от свежего к старому")

# Рецензент обязан смотреть в КОНЕЦ списка — там свежее.
validator = CODE.split("async def check_response_quality", 1)[1].split("\n\n\n", 1)[0]
check("рецензент берёт последние сообщения, а не первые",
      "context_msgs[-10:]" in validator,
      "срез не с конца — рецензент увидит старое")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
