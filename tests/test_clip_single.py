# -*- coding: utf-8 -*-
"""
Обрезка по границе предложения: ОДНА реализация, и она не врёт врачу.

Запуск: python test_clip_single.py

Что здесь стережётся и чем это платит врач.

Д1. ТРИ КОПИИ. Функция жила в assistant.py (`_clip_at_sentence`),
    web_lookup.py и distiller.py. На 8 замеренных входах они расходились на 6,
    и расхождение было не косметическим: одна отдавала строку, другая кортеж,
    третья оставляла в конце висящий номер пункта. ПОСЛЕДСТВИЕ: правка,
    сделанная в одной копии (сирота-номер убрали в assistant и web_lookup),
    в третьей не появлялась — врач читал в статье пункт «5.», за которым нет
    текста, и не знал, потерян абзац или так и написано.

Д2. НЕТ ПРОВЕРКИ «ТЕКСТ КОРОЧЕ ПРЕДЕЛА». `assistant._clip_at_sentence`
    приклеивал многоточие к тексту, который никто не обрезал:
    'короткий' при пределе 50 -> 'короткий…'. ПОСЛЕДСТВИЕ: запись справки
    уходит в модель с меткой «факт оборван» там, где он полный, и модель
    дописывает за него; врач получает дописанное как знание коллег.

Д3. ПРИПИСКА-НЕПРАВДА. Ветка обрезки статьи включалась по длине С РАЗМЕТКОЙ, а
    резала ПЛОСКИЙ текст. Врач видел «[Показано 3072 символов из 4003; ещё 931
    не поместились в одно сообщение]» на статье, которая в предел Telegram
    помещается. ПОСЛЕДСТВИЕ: в отрезанном хвосте живут дозировки («Доза
    гипохлорита 3 мг на кг»), а приписка объясняет потерю пределом Telegram,
    которого статья не достигала.

Д4. СЛЕПОЙ ПОИСК ГРАНИЦЫ. Граница искалась как '. ' — точка ПЛЮС ПРОБЕЛ. Живые
    статьи разделены переводами строк, поэтому после точки стоит '\\n\\n', и
    разрез уезжал назад до 759 символов. Замер по 30 длинным статьям вики:
    выброшено 28 686 символов там, где не влезало 19 607. ПОСЛЕДСТВИЕ: врач
    терял целые разделы протокола («6. Финальные этапы», «Временное
    протезирование») — и это не «не поместилось», это выбросил алгоритм.

Д5. РЕЗУЛЬТАТ БОЛЬШЕ ПРЕДЕЛА. Обрезка «в бюджет 1200» возвращала 1201 символ
    (замер: 94 входа из 264 у копии в assistant, 93 у копии в web_lookup).
    ПОСЛЕДСТВИЕ: бюджет промпта считается по этим числам, и переполнение
    выталкивает из справки последнюю запись — самую слабую по релевантности,
    но всё же факт.

Д6. ЧИСЛО В ПРИПИСКЕ НЕ СХОДИТСЯ. Показано + не показано обязано равняться
    длине текста, иначе приписка — просто украшение. ПОСЛЕДСТВИЕ: врач не
    может понять, потерян абзац или треть статьи.
"""
import ast
import io
import os
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import assistant as A
import distiller as D
import html_safe as H
import web_lookup as W

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[OK  ] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name} {detail}")


def skip(name, detail=""):
    SKIP.append(name)
    print(f"[SKIP] {name} {detail}")


ELL = "…"
TAG_RE = re.compile(r"<[^>]+>")
CODE_RE = re.compile(r"\s*\[\d+(?:\.\d+)+\]")
HANGING_RE = re.compile(r"\d+\.\s*$")
# Заголовок страницы статьи: замер по WIKI_SUBTOPIC_NAMES — 61 символ на самом
# длинном из 50 имён подтем плюс «Статья 3734 из 3734».
MAX_HEADER = 61

PROBES = [
    "короткий",
    "Короткий текст.",
    "Пункт первый.\n5. Следующий пункт",
    "Первое предложение про адгезив. Второе про полимеризацию. Третье оборвётся",
    "слово " * 50,
    "а" * 100,
    "Доза не более 7 мг/кг. Далее продолжение фразы, которое обрывается",
    "Одно предложение без конца длиною больше предела",
    "Абзац первый кончается точкой.\n\nАбзац второй тоже кончается точкой.\n\nТретий",
    "",
    "   ",
    "раз два",
]
LIMITS = list(range(1, 80)) + [100, 300, 900, 1200, 3904]


print("\n[1] Одна реализация под тремя именами")
# Проверка ПОВЕДЕНЧЕСКАЯ: не «в исходнике нет def», а «на всех входах три имени
# дают один и тот же разрез». Разошлись — значит копия вернулась.
mismatch = []
for probe in PROBES:
    for limit in LIMITS:
        a = A._clip_at_sentence(probe, limit)
        w = W.clip_at_sentence(probe, limit)
        d, dropped = D.clip_at_sentence(probe, limit)
        if not (a == w == d):
            mismatch.append((probe[:24], limit, a[-14:], w[-14:], d[-14:]))
check(f"три имени дают один разрез на {len(PROBES) * len(LIMITS)} входах",
      not mismatch, f"расхождений {len(mismatch)}: {mismatch[:3]}")
check("distiller отдаёт кортеж (текст, сколько не показано)",
      isinstance(D.clip_at_sentence("а" * 100, 10), tuple)
      and isinstance(D.clip_at_sentence("а" * 100, 10)[1], int))
check("web_lookup и assistant отдают текст",
      isinstance(W.clip_at_sentence("а" * 100, 10), str)
      and isinstance(A._clip_at_sentence("а" * 100, 10), str))
# Единственный дом — html_safe. Своя реализация в любом другом файле проекта
# означает, что копии снова три.
own_defs = []
for path in sorted(f for f in os.listdir(".") if f.endswith(".py")):
    try:
        # utf-8-sig, а не utf-8: benchmark.py начинается с BOM, и на нём разбор
        # падал — «файл не разобран» значит «копию в нём не проверили».
        tree = ast.parse(io.open(path, encoding="utf-8-sig").read())
    except SyntaxError:
        own_defs.append((path, "файл не разобран — проверка не выполнена"))
        continue
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name.lstrip("_").startswith("clip_at_sentence"):
            own_defs.append((path, node.name))
check("реализация только в html_safe",
      sorted({f for f, _ in own_defs}) == ["html_safe.py"]
      and sorted(n for _, n in own_defs) == ["clip_at_sentence", "clip_at_sentence_text"],
      f"найдено: {own_defs}")
check("clip_at_sentence_text — не вторая реализация, а [0] от первой",
      all(H.clip_at_sentence_text(p, lim) == H.clip_at_sentence(p, lim)[0]
          for p in PROBES for lim in LIMITS))


print("\n[2] Текст короче предела не трогается вообще")
for probe in ("короткий", "Короткий текст.", "Доза 3 мг на кг.", "", "раз два"):
    for limit in (len(probe), len(probe) + 1, 50, 3904):
        if limit < len(probe):
            continue
        clipped, dropped = H.clip_at_sentence(probe, limit)
        check(f"{probe[:16]!r} при пределе {limit} возвращён как есть",
              clipped == probe and dropped == 0, f"got {clipped!r}, dropped={dropped}")
check("многоточие не приклеивается к тексту, который влезает",
      not any(H.clip_at_sentence(p, len(p) + 5)[0].endswith(ELL) for p in PROBES if p),
      "модель прочтёт полный факт как оборванный и допишет за него")
check("assistant тоже не приклеивает (это его дефект и был)",
      A._clip_at_sentence("короткий", 50) == "короткий",
      f"got {A._clip_at_sentence('короткий', 50)!r}")
check("distiller: короткий текст даёт ноль отброшенных",
      D.clip_at_sentence("Короткий текст.", 100) == ("Короткий текст.", 0))


print("\n[3] Результат всегда влезает в предел, а число сходится")
over, sums = [], []
for probe in PROBES:
    for limit in LIMITS:
        clipped, dropped = H.clip_at_sentence(probe, limit)
        if len(clipped) > limit:
            over.append((probe[:20], limit, len(clipped)))
        if len(clipped) + dropped != len(probe):
            sums.append((probe[:20], limit, len(clipped), dropped, len(probe)))
check(f"len(результат) <= предел на {len(PROBES) * len(LIMITS)} входах", not over,
      f"превышений {len(over)}: {over[:3]}")
check("показано + не показано == длина входа", not sums,
      f"не сходится {len(sums)}: {sums[:3]}")
check("реальная обрезка никогда не даёт ноль отброшенных",
      all(H.clip_at_sentence(p, lim)[1] > 0
          for p in PROBES for lim in LIMITS if len(p) > lim),
      "молчаливая обрезка: врач не узнает, что текста больше")


print("\n[4] Висящий номер следующего пункта не остаётся")
NUMBERED = ("Первый абзац с длинным текстом про препарирование зуба. " * 3
            + "\n\n5. Следующий пункт про адгезив")
hanging = [lim for lim in range(20, len(NUMBERED))
           if HANGING_RE.search(H.clip_at_sentence(NUMBERED, lim)[0])]
check(f"ни на одном из {len(NUMBERED) - 20} пределов нет сироты-номера", not hanging,
      f"осталось на пределах {hanging[:10]}")
check("distiller тоже (у него этой правки не было)",
      not [lim for lim in range(20, len(NUMBERED))
           if HANGING_RE.search(D.clip_at_sentence(NUMBERED, lim)[0])])
check("номер убран вместе с предшествующим переводом строки",
      not H.clip_at_sentence(NUMBERED, 175)[0].rstrip().endswith(("5.", "5")),
      repr(H.clip_at_sentence(NUMBERED, 175)[0][-24:]))


print("\n[5] Граница предложения видит точку с переводом строки")
# Ровно то, на чём терялись разделы статей: последняя '. ' далеко позади, а
# '.' + '\n\n' — рядом с бюджетом.
TAIL = "Третий раздел обрывается"
PARA = ("Первый раздел протокола описан подробно и без переводов строки внутри. "
        + "текст " * 30
        + "конец раздела.\n\nВторой раздел: экспозиция не менее тридцати минут.\n\n"
        + TAIL)
# Предел ровно такой, что в бюджет попадает граница «минут.» плюс перевод строки,
# а за бортом остаётся последний абзац.
limit = len(PARA) - len(TAIL)
kept, dropped = H.clip_at_sentence(PARA, limit)
check("разрез взял границу с переводом строки, а не уехал в начало",
      "экспозиция не менее тридцати минут" in kept,
      f"показано {len(kept)} из {len(PARA)}, хвост {kept[-40:]!r}")
old_style = max(PARA[:limit].rfind(". "), PARA[:limit].rfind("! "),
                PARA[:limit].rfind("? "), PARA[:limit].rfind("; "))
check("новая граница дальше старой (иначе правка ничего не дала)",
      len(kept) > old_style + 1,
      f"новая {len(kept)}, старая по '. ' {old_style + 1}")
check("переизбыток обрезки на этом входе меньше 60 символов",
      (len(PARA) - limit) + 60 > dropped, f"отброшено {dropped}, не влезало {len(PARA) - limit}")


print("\n[6] Оборванное слово помечено и не превращается в другую дозу")
DOSE = ("Доза не более 7 мг/кг. Далее следует продолжение фразы, "
        "которое обрывается по счётчику символов ровно посередине слова")
torn = []
for limit in range(12, len(DOSE) + 4):
    clipped = H.clip_at_sentence(DOSE, limit)[0]
    marked = clipped.rstrip().endswith((".", "!", "?", ";", ELL)) or len(DOSE) <= limit
    broken = (clipped.endswith(ELL) and len(clipped) > 1 and clipped[-2].isalnum()
              and len(clipped) - 1 < len(DOSE) and DOSE[len(clipped) - 1].isalnum())
    if not marked or broken:
        torn.append((limit, clipped[-24:]))
check("на всех пределах обрыв помечен и токен не разорван", not torn,
      f"плохих пределов {len(torn)}: {torn[:3]}")
unit_split = [lim for lim in range(24, 60)
              if "7 мг" in H.clip_at_sentence(DOSE, lim)[0]
              and "7 мг/кг" not in H.clip_at_sentence(DOSE, lim)[0]]
check("единица измерения не отрезана от числа", not unit_split, f"пределы {unit_split}")


print("\n[7] Статья, которую разметка вывела за предел, доходит целиком")
# Случай из задания: плоского текста меньше предела Telegram, с тегами больше.
# Длина выбрана В ОКНЕ ПОТЕРИ (больше бюджета показа, но меньше предела): на
# более коротком входе неверный порог не отнимает текста, и диверсия «резать по
# длине С РАЗМЕТКОЙ» прошла бы незамеченной — так и вышло на первой попытке.
body = ("Протокол ирригации канала описан ниже подробно и по шагам. " * 70)[:3920]
body += " Доза гипохлорита 3 мг на кг веса пациента."
tagged = body.replace("Протокол", "<b>Протокол</b>", 40)
check("вход подобран верно: в окне потери, с тегами за пределом",
      A._ARTICLE_SHOWN_MAX_CHARS < len(body) <= A._ARTICLE_PLAIN_MAX_CHARS
      and len(tagged) > 4096,
      f"плоских {len(body)}, с тегами {len(tagged)}, "
      f"окно {A._ARTICLE_SHOWN_MAX_CHARS}..{A._ARTICLE_PLAIN_MAX_CHARS}")
out = A.clean_html_formatting(tagged)
check("приписки про непоместившийся текст нет", "не поместились" not in out,
      f"хвост: {out[-90:]!r}")
check("дозировка последнего предложения на месте", "3 мг на кг" in out,
      f"хвост: {out[-70:]!r}")
check("последнее предложение целиком, а не до последней точки",
      out.rstrip().endswith("веса пациента."), repr(out[-40:]))
check("врачу отдан весь текст, ни одного слова не выброшено",
      len(TAG_RE.sub("", out).replace("&amp;", "&")) >= len(body),
      f"плоских на входе {len(body)}, на выходе {len(TAG_RE.sub('', out))}")
# Проверки выше не должны быть зелёными сами по себе. Старый порядок включал
# ветку потерь по длине С РАЗМЕТКОЙ и резал плоский текст по последней «. »
# без проверки «влезает» — вот ровно то, что врач терял на этом входе.
old_shown = body[:body[:3900].rfind(". ") + 1]
check("наивный разрез на этом входе действительно теряет дозировку",
      "3 мг на кг" not in old_shown and len(old_shown) < len(body),
      f"вход выродился: показано {len(old_shown)} из {len(body)}")


print("\n[8] Статья, которая действительно не влезает: честное число")
LONG = ("Гипохлорит натрия применяют в концентрации от 3 до 5 процентов. "
        "Экспозиция составляет не менее тридцати минут на канал.\n\n") * 40
out = A.clean_html_formatting(LONG)
body_only = out.split("\n\n[Показано")[0]
check("приписка есть", "не поместились" in out, out[-90:])
m = re.search(r"Показано (\d+) символов из (\d+); ещё (\d+) не поместились", out)
check("приписка называет три числа", m is not None, out[-90:])
if m:
    shown_n, total_n, hidden_n = (int(x) for x in m.groups())
    check("показано + не показано == длина статьи", shown_n + hidden_n == total_n,
          f"{shown_n} + {hidden_n} != {total_n}")
    check("число показанного совпадает с тем, что реально в сообщении",
          shown_n == len(body_only), f"сказано {shown_n}, в сообщении {len(body_only)}")
    check("длина статьи в приписке — длина плоского текста",
          total_n == len(TAG_RE.sub("", LONG)), f"сказано {total_n}")
check("сообщение вместе с заголовком страницы влезает в 4096",
      len(out) + MAX_HEADER <= 4096, f"got {len(out)} + {MAX_HEADER}")
check("обрыв на границе предложения", body_only.rstrip().endswith((".", "!", "?", ";", ELL)),
      repr(body_only[-40:]))
check("выброшено не больше, чем не влезало, плюс одно предложение",
      len(TAG_RE.sub("", LONG)) - len(body_only) <= (len(LONG) - A._ARTICLE_SHOWN_MAX_CHARS) + 200,
      f"не влезало {len(LONG) - A._ARTICLE_SHOWN_MAX_CHARS}, "
      f"выброшено {len(TAG_RE.sub('', LONG)) - len(body_only)}")
short = "Короткая статья про ирригацию канала."
check("короткая статья не получает приписки", "Показано" not in A.clean_html_formatting(short))


print("\n[9] Живой корпус: 12 784 статьи проходят путь показа")
if not os.path.exists("stomat_wiki.db"):
    skip("вики рядом нет — проверка на живом корпусе не выполнена")
else:
    conn = sqlite3.connect("file:stomat_wiki.db?mode=ro", uri=True)
    facts = [r[0] for r in conn.execute(
        "SELECT content FROM distilled_facts WHERE content IS NOT NULL")]
    conn.close()
    check("корпус прочитан", len(facts) > 10000, f"got {len(facts)}")

    notice_on_fitting = []
    oversize = []
    bad_sum = []
    waste = 0
    clipped_count = 0
    for fact in facts:
        text = CODE_RE.sub("", fact or "")
        plain = TAG_RE.sub("", text)
        out = A.clean_html_formatting(fact)
        if len(out) + MAX_HEADER > 4096:
            oversize.append(len(out))
        if "не поместились" not in out:
            continue
        clipped_count += 1
        shown = out.split("\n\n[Показано")[0]
        shown_plain = shown.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        m = re.search(r"Показано (\d+) символов из (\d+); ещё (\d+) ", out)
        if not m or int(m.group(1)) + int(m.group(3)) != int(m.group(2)):
            bad_sum.append(out[-80:])
        if len(plain) <= A._ARTICLE_PLAIN_MAX_CHARS:
            notice_on_fitting.append(len(plain))
        waste += (len(plain) - len(shown_plain)) - max(
            0, len(plain) - A._ARTICLE_SHOWN_MAX_CHARS)
    check("ни одна помещающаяся статья не получила приписку", not notice_on_fitting,
          f"получили {len(notice_on_fitting)}: {notice_on_fitting[:5]}")
    check("каждое сообщение с заголовком влезает в 4096", not oversize,
          f"превышений {len(oversize)}: {oversize[:3]}")
    check("арифметика приписки сходится на всём корпусе", not bad_sum,
          f"битых {len(bad_sum)}: {bad_sum[:2]}")
    check(f"статей с обрезкой {clipped_count} — как замерено", clipped_count == 30,
          f"got {clipped_count}: изменился порог или корпус")
    # Замер: слепой поиск границы выбрасывал 9 079 символов сверх нужного,
    # новый — 2 909. Порог 3 500 ловит возврат старого поведения.
    check(f"перерасход обрезки по корпусу {waste} символов (было 9 079)",
          waste <= 3500, "вернулся слепой поиск границы: врач снова теряет разделы")


print("\n[10] Вызывающие получили то, что ждут")
footer = W.format_sources_footer([{"host": "pubmed.ncbi.nlm.nih.gov",
                                   "url": "https://pubmed.ncbi.nlm.nih.gov/1/"}])
composed = W.compose_answer("Короткий ответ [1].", footer)
check("короткий ответ не обрезан и без многоточия",
      "Короткий ответ [1]." in composed and ELL not in composed.split("\n\n")[0],
      repr(composed[:60]))
long_answer = ("Очень подробный разбор клинического случая. " * 200).strip()
composed = W.compose_answer(long_answer, footer)
check("длинный ответ влез в предел сообщения", len(composed) <= W.MESSAGE_MAX_CHARS,
      f"got {len(composed)}")
check("ссылка выжила обрезку", "pubmed.ncbi.nlm.nih.gov/1/" in composed)
fitted, dropped = W.fit_budget([{
    "host": "h.example", "url": "https://h.example/", "tier": 1,
    "text": ("Первое предложение обзора. " * 40) + "Обрывок без точки"}])
check("выдержка обрезана в бюджет", len(fitted[0]["text"]) <= W.WEB_ENTRY_MAX_CHARS,
      f"got {len(fitted[0]['text'])}")
check("короткая выдержка не помечается обрывом",
      W.fit_budget([{"host": "h.example", "url": "https://h.example/", "tier": 1,
                     "text": "Короткая выдержка."}])[0][0]["text"] == "Короткая выдержка.")
row, notes = D.prepare_fact({"c": "1.1.3", "f": "Факт про адгезив.", "s": [1]}, {1}, set())
check("distiller не помечает обрезкой короткий факт",
      row is not None and not any("обрезан" in n for n in notes), f"notes={notes}")
row, notes = D.prepare_fact(
    {"c": "1.1.3", "f": "Ф. " * 3000, "s": [1]}, {1}, set())
check("distiller считает и логирует обрезку длинного факта",
      any("обрезан" in n for n in notes), f"notes={notes}")
check("запись факта влезает в CONTENT_CHAR_CAP",
      row is not None and len(row[1]) <= D.CONTENT_CHAR_CAP,
      f"got {len(row[1]) if row else None}")

entries = ["Короткая запись справки без точки", "Ещё одна запись про адгезив"]
check("короткая запись справки уходит в модель без метки обрыва",
      A._fit_corpus_budget(entries) == entries,
      f"got {A._fit_corpus_budget(entries)}")
check("короткая реплика истории ЛС тоже",
      A._fit_pm_history(entries) == entries, f"got {A._fit_pm_history(entries)}")
long_entry = ("Гипохлорит применяют в концентрации от 3 до 5 процентов. " * 40)
kept = A._fit_corpus_budget([long_entry])[0]
check("длинная запись справки обрезана в бюджет записи",
      len(kept) <= A._CORPUS_ENTRY_MAX_CHARS, f"got {len(kept)}")
check("обрезанная запись справки кончается границей предложения",
      kept.rstrip().endswith((".", "!", "?", ";", ELL)), repr(kept[-30:]))


print("\n[11] Дописано скептиком: три диверсии проходили мимо набора")
# С1. Ранняя граница не имеет права съесть бюджет. `if best >= limit // 2`
# заменяется на `if best >= 0` без единого падения: на живой вике при пределе
# 3904 граница всегда во второй половине, и корпусная проверка [9] слепа.
# ПОСЛЕДСТВИЕ: у факта, где единственная точка стоит в начале («Да.» или
# «Рис. 1»), врач и модель получают три символа вместо тысячи двухсот.
EARLY = "Да. " + "слово " * 80
starved = []
for limit in (60, 100, 240, 400, 480):
    shown, dropped = H.clip_at_sentence(EARLY, limit)
    if len(shown) < limit - 8:
        starved.append((limit, len(shown), repr(shown[:20])))
check("ранняя точка не съедает бюджет разреза", not starved,
      f"голодных пределов {len(starved)}: {starved[:3]}")
check("на таком входе отброшено не больше непоместившегося плюс слово",
      H.clip_at_sentence(EARLY, 240)[1] <= len(EARLY) - 240 + 8,
      f"отброшено {H.clip_at_sentence(EARLY, 240)[1]} из {len(EARLY)} при пределе 240")

# С2. Экранирование тела в ветке приписки. `body = shown` без replace проходит
# весь набор: у 12 784 фактов вики «<» встречается в двух, и оба короткие.
# ПОСЛЕДСТВИЕ: Telegram отклоняет сообщение с неразбираемой разметкой ЦЕЛИКОМ —
# врач видит не урезанную статью, а пустоту и мёртвые кнопки листания.
DIRTY = "Условие pH < 7 и связка Ca & P в остатке раствора. " * 90
dirty_out = A.clean_html_formatting(DIRTY)
dirty_body = dirty_out.split("\n\n[Показано")[0]
check("вход подобран: ветка приписки, «<» и «&» в теле",
      "не поместились" in dirty_out and len(TAG_RE.sub("", DIRTY)) > A._ARTICLE_PLAIN_MAX_CHARS,
      f"плоских {len(DIRTY)}")
check("«<» в теле статьи экранирован", "&lt;" in dirty_body and "<" not in dirty_body,
      f"хвост: {dirty_body[-60:]!r}")
check("«&» в теле статьи экранирован",
      "&amp;" in dirty_body and "&" not in re.sub(r"&(amp|lt|gt);", "", dirty_body),
      f"хвост: {dirty_body[-60:]!r}")
check("после экранирования незакрытых тегов не осталось",
      not H.unclosed_tags(dirty_out) and H.balance_html(dirty_out)[0] == dirty_out,
      f"незакрытые: {H.unclosed_tags(dirty_out)}")

# С3. Двоеточие — не конец предложения. Добавление ':' в набор знаков не ронял
# ни одной проверки, хотя разрез после «Протокол включает:» — тот же дефект,
# что и висящий номер пункта. ПОСЛЕДСТВИЕ: врач читает заголовок перечня, за
# которым в сообщении ничего нет, и не знает, что список потерян.
LEAD = ("Первый раздел протокола закончен точкой. " + "текст " * 24
        + "Протокол включает: ")
COLON = LEAD + "первый пункт, второй пункт и третий пункт перечня"
dangling = [lim for lim in range(len(LEAD) - 30, len(LEAD) + 2)
            if H.clip_at_sentence(COLON, lim)[0].rstrip().endswith(":")]
check("разрез не кончается двоеточием — перечня за ним нет", not dangling,
      f"пределы с висящим двоеточием: {dangling[:6]}")
check("двоеточие не считается границей ни на одном пределе",
      not [lim for lim in LIMITS
           if H.clip_at_sentence(COLON, lim)[0].rstrip().endswith(":")])


print(f"\n{'=' * 62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}   SKIPPED: {len(SKIP)}")
if SKIP:
    print("Пропущено: " + ", ".join(SKIP))
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
