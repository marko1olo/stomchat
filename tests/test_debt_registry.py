"""
Инвентарь технического долга: одна команда отвечает «сколько и где».

ЗАМЕР, из которого выросла проверка. `rg 'TODO|FIXME|HACK|XXX'` по дереву даёт
РОВНО НОЛЬ, и это читалось как «долга нет». Признания долга при этом есть — они
лежали прозой в докстрингах, и разведка волны 3 насчитала их восемь мест. Ни один
инвентарь их не находил, зато они находили ЛЮДЕЙ: половина той таблицы описывала
УЖЕ ЗАКРЫТЫЕ дефекты в прошедшем времени, и исполнители волны 4 ходили закрывать
закрытое (web_lookup «не вызывает его НИКТО» — /web подключена; три копии обрезки
— сведены в html_safe).

ПОСЛЕДСТВИЕ ДЛЯ ВРАЧА. Долг, который не виден инвентарю, не попадает и в очередь
работ. Два измеренных примера прямо из этого дерева: уборщик конвертатов снимал
`_converted.wav`, а перекодирование писало ещё и `_converted.ogg` — 2.0 МБ утечки
на случай, две волны никем не замеченные, а на полном диске бот перестаёт
скачивать медиа вообще (ни расшифровки голосового, ни разбора снимка). И отсев
рекламы в web_lookup выбрасывает clinicaltrials.gov как «рекламу клиники»: на
выдаче из регистра клинических исследований врач получает «нашлась только
реклама». Пока такие места не помечены машиночитаемо, они держатся на памяти
конкретного агента, а память кончается вместе с контекстом.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ ПОВЕДЕНЧЕСКИ:
  [1] парсер пометки — на подсаженных образцах, полных и неполных;
  [2] инвентарь дерева: все пометки найдены и напечатаны с приоритетом;
  [3] число пометок совпадает с зарегистрированным (и по файлам, и всего);
  [4] неполная пометка — та, у которой нет ПОСЛЕДСТВИЯ, — роняет набор;
  [5] долг, объявленный закрытым, закрыт ПОВЕДЕНЧЕСКИ, а не на словах;
  [6] «долгий», «долг медиа» и прочий оборот речи пометкой не считается;
  [7] вложенная копия репозитория в обход не попадает (замеры не удваиваются);
  [8] пометка — это КОММЕНТАРИЙ: текст в строковом литерале в инвентарь не идёт.

Формат пометки:

    # ДОЛГ P<1|2|3> <врач|внутр>: <что НЕ сделано> -> <ПОСЛЕДСТВИЕ>
    #   <продолжение, отступ ровно три пробела после решётки>

Запуск: python test_debt_registry.py
"""
import io
import os
import re
import sys
import tempfile
import tokenize

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# Реестр: сколько пометок в каком файле ЗАРЕГИСТРИРОВАНО отчётом волны.
#
# Незарегистрированная пометка роняет набор намеренно: «долг есть, но его нет в
# инвентаре» — ровно то состояние, из которого проект вытаскивают. Добавил
# пометку — добавь строку сюда и в свой _fix_*.md.
# ---------------------------------------------------------------------------
REGISTERED = {
    "web_lookup.py": 1,      # P1 врач: отсев рекламы бьёт по clinicaltrials.gov
    "main.py": 1,            # P2 врач: tg_safety в main.py не подключён нигде
    "blocking_tools.py": 1,  # P3 внутр: стем _converted литералом в двух модулях
}
DECLARED_TOTAL = 3

# ---------------------------------------------------------------------------
# Парсер. Пометка — это КОММЕНТАРИЙ, и строки комментариев берутся у tokenize, а
# не текстовым поиском. Это не украшение: описание формата в докстринге этого
# файла и подсаженные образцы ниже лежат в СТРОКОВЫХ ЛИТЕРАЛАХ, и текстовый
# сканер записал бы их в инвентарь — первая же версия так и сделала и объявила
# собственную документацию неполной пометкой. Ни один файл при этом не
# исключается из обхода: слепых зон у сканера нет, есть разделение
# «комментарий против литерала». Доказано в [8] на этом самом файле.
# ---------------------------------------------------------------------------
MARK = "ДОЛГ"
_HEAD_RE = re.compile(r"^\s*#\s*" + MARK + r"\s+P([123])\s+(врач|внутр)\s*:\s*(.*)$")
_CONT_RE = re.compile(r"^\s*#\s{3,}(\S.*)$")
# Строка, которая ХОТЕЛА быть пометкой: слово-маркер заглавными как отдельное
# слово в комментарии. Нужна, чтобы «пометка с опечаткой в приоритете» не
# исчезала из инвентаря молча — иначе неполноту можно спрятать, испортив шапку.
_LOOKS_RE = re.compile(r"^\s*#\s*" + MARK + r"\b")

MIN_WHAT = 10
MIN_CONSEQUENCE = 25


def comment_lines(path):
    """
    Номера строк, которые РЕАЛЬНО являются комментарием. None — файл не разобран.

    Через tokenize, потому что описание формата в докстринге и образцы в
    строковых литералах комментариями не являются и в инвентарь попадать не
    должны.
    """
    found = set()
    try:
        with io.open(path, "rb") as handle:
            for tok in tokenize.tokenize(handle.readline):
                if tok.type == tokenize.COMMENT:
                    found.add(tok.start[0])
    except (OSError, ValueError, SyntaxError, UnicodeDecodeError,
            IndentationError, tokenize.TokenError):
        return None
    return found


def parse_marks(text, origin="<строка>", allowed=None):
    """
    Все пометки долга в тексте. Возвращает (полные, неполные).

    Полная: шапка разобрана, в теле есть `->`, обе половины не короче порога.
    Неполная попадает во второй список с причиной — молча она не исчезает.

    `allowed` — номера строк-комментариев; None значит «считать все строки»
    (так разбираются подсаженные образцы, у которых файла нет).
    """
    lines = text.split("\n")
    full, broken = [], []
    idx = 0
    while idx < len(lines):
        if allowed is not None and (idx + 1) not in allowed:
            idx += 1
            continue
        head = _HEAD_RE.match(lines[idx])
        if not head:
            if _LOOKS_RE.match(lines[idx]):
                broken.append({"file": origin, "line": idx + 1, "priority": None,
                               "audience": None, "body": lines[idx].strip(),
                               "why": "шапка не разобрана: нужно 'P1..P3' и 'врач|внутр'"})
            idx += 1
            continue
        start = idx + 1
        parts = [head.group(3).strip()]
        idx += 1
        while idx < len(lines):
            if allowed is not None and (idx + 1) not in allowed:
                break
            cont = _CONT_RE.match(lines[idx])
            if not cont or _HEAD_RE.match(lines[idx]):
                break
            parts.append(cont.group(1).strip())
            idx += 1
        body = " ".join(p for p in parts if p)
        item = {"file": origin, "line": start, "priority": int(head.group(1)),
                "audience": head.group(2), "body": body}
        if "->" not in body:
            item["why"] = "нет ПОСЛЕДСТВИЯ: в теле не найдена стрелка '->'"
            broken.append(item)
            continue
        what, consequence = body.split("->", 1)
        item["what"] = what.strip()
        item["consequence"] = consequence.strip()
        if len(item["what"]) < MIN_WHAT:
            item["why"] = f"не сказано, что не сделано (левая половина {len(item['what'])} симв)"
            broken.append(item)
        elif len(item["consequence"]) < MIN_CONSEQUENCE:
            item["why"] = (f"последствие пустое или отписка "
                           f"({len(item['consequence'])} симв, нужно {MIN_CONSEQUENCE})")
            broken.append(item)
        else:
            full.append(item)
    return full, broken


def scan_file(name):
    """Пометки одного файла. Третьим значением — False, если файл не разобран."""
    try:
        src = io.open(name, encoding="utf-8-sig").read()
    except (OSError, UnicodeDecodeError):
        return [], [], False
    allowed = comment_lines(name)
    if allowed is None:
        # Файл не разобрался (его пишут прямо сейчас или он битый). Считаем ВСЕ
        # строки: спрятать долг за неразбираемым файлом нельзя, ложное попадание
        # дешевле пропущенного долга.
        f, b = parse_marks(src, name)
        return f, b, False
    f, b = parse_marks(src, name, allowed)
    return f, b, True


def scan_tree():
    """Обход корня, НЕ рекурсивный: вложенная копия stomchat/ так не попадает."""
    full, broken, unparsed = [], [], []
    for name in sorted(f for f in os.listdir(".") if f.endswith(".py")):
        f, b, ok = scan_file(name)
        full.extend(f)
        broken.extend(b)
        if not ok:
            unparsed.append(name)
    return full, broken, unparsed


print("=" * 70)
print("[1] Парсер пометки: разбор на подсаженных образцах")
print("=" * 70)

GOOD = (
    "def f():\n"
    "    # ДОЛГ P2 врач: отправка не обёрнута бюджетом -> врач видит\n"
    "    #   зависший бот до 500 с и в журнале об этом ни строки\n"
    "    return 1\n"
)
_full, _broken = parse_marks(GOOD, "образец")
check("полная пометка найдена", len(_full) == 1 and not _broken,
      f"полных {len(_full)}, неполных {len(_broken)}")
if _full:
    item = _full[0]
    check("приоритет разобран", item["priority"] == 2, str(item["priority"]))
    check("видимость для врача разобрана", item["audience"] == "врач", str(item["audience"]))
    check("строка указана верно", item["line"] == 2, str(item["line"]))
    check("продолжение приклеено к последствию",
          "500" in item["consequence"] and "журнале" in item["consequence"],
          item["consequence"])
    check("левая половина — что не сделано",
          "обёрнута" in item["what"] and "500" not in item["what"], item["what"])

_ordinary = ("    # ДОЛГ P2 врач: что-то не сделано -> последствие для врача именно такое\n"
             "    # обычный комментарий проекта с одним пробелом\n")
_f2, _b2 = parse_marks(_ordinary, "образец")
check("обычный комментарий продолжением НЕ считается",
      len(_f2) == 1 and "обычный комментарий" not in _f2[0]["consequence"],
      _f2[0]["consequence"] if _f2 else "пометка не найдена")

_two = GOOD + GOOD.replace("P2 врач", "P3 внутр")
_f3, _b3 = parse_marks(_two, "образец")
check("две пометки подряд не склеиваются", len(_f3) == 2 and not _b3,
      f"полных {len(_f3)}, неполных {len(_b3)}")
check("приоритеты у обеих свои",
      [i["priority"] for i in _f3] == [2, 3], str([i["priority"] for i in _f3]))
check("видимость у обеих своя",
      [i["audience"] for i in _f3] == ["врач", "внутр"], str([i["audience"] for i in _f3]))

print()
print("=" * 70)
print("[2] Инвентарь дерева: сколько незакрытого долга и где")
print("=" * 70)

TREE, TREE_BROKEN, TREE_UNPARSED = scan_tree()
_by_priority = sorted(TREE, key=lambda i: (i["priority"], i["file"], i["line"]))
print(f"  файлов .py в обходе: {len([f for f in os.listdir('.') if f.endswith('.py')])}")
print(f"  ПОМЕТОК ДОЛГА: {len(TREE)}   неполных: {len(TREE_BROKEN)}")
if TREE_UNPARSED:
    print(f"  не разобрано tokenize (считаны текстом целиком): {TREE_UNPARSED}")
print()
for item in _by_priority:
    print(f"  P{item['priority']} [{item['audience']:5s}] {item['file']}:{item['line']}")
    print(f"        не сделано:  {item['what'][:100]}")
    print(f"        последствие: {item['consequence'][:100]}")
for item in TREE_BROKEN:
    print(f"  НЕПОЛНАЯ {item['file']}:{item['line']} — {item['why']}")
print()

check("инвентарь непустой", len(TREE) > 0,
      "нечего проверять: либо долга нет, либо сканер сломан")
check("у каждой пометки есть приоритет",
      all(i["priority"] in (1, 2, 3) for i in TREE), str([i["priority"] for i in TREE]))
check("у каждой пометки объявлена видимость для врача",
      all(i["audience"] in ("врач", "внутр") for i in TREE),
      str([i["audience"] for i in TREE]))
check("у каждой пометки есть ПОСЛЕДСТВИЕ",
      all(len(i.get("consequence") or "") >= MIN_CONSEQUENCE for i in TREE),
      str([(i["file"], len(i.get("consequence") or "")) for i in TREE]))
check("ни одной неполной пометки в дереве", not TREE_BROKEN,
      "; ".join(f"{i['file']}:{i['line']} {i['why']}" for i in TREE_BROKEN))
check("пометки с последствием для врача видны отдельной очередью",
      len([i for i in TREE if i["audience"] == "врач"])
      + len([i for i in TREE if i["audience"] == "внутр"]) == len(TREE))

print("=" * 70)
print("[3] Число пометок совпадает с зарегистрированным в отчёте")
print("=" * 70)

_counts = {}
for item in TREE:
    _counts[item["file"]] = _counts.get(item["file"], 0) + 1
for path, want in sorted(REGISTERED.items()):
    got = _counts.get(path, 0)
    check(f"{path}: пометок {want}", got == want,
          f"найдено {got} — поправь REGISTERED и свой _fix_*.md")
_unregistered = sorted(set(_counts) - set(REGISTERED))
check("незарегистрированных файлов с долгом нет", not _unregistered,
      f"долг помечен, но не в реестре: {_unregistered} — добавь их в REGISTERED")
check(f"всего пометок в дереве = {DECLARED_TOTAL}", len(TREE) == DECLARED_TOTAL,
      f"найдено {len(TREE)}: {[(i['file'], i['line']) for i in TREE]}")
check("сумма по реестру равна объявленному итогу",
      sum(REGISTERED.values()) == DECLARED_TOTAL,
      f"{sum(REGISTERED.values())} против {DECLARED_TOTAL}")

print()
print("=" * 70)
print("[4] Пометка без последствия для врача — НЕПОЛНАЯ и роняет набор")
print("=" * 70)
# Детектор проверяется на подсадке: ноль неполных одинаково выглядит у здорового
# дерева и у сломанного детектора. Именно эта диверсия в проекте уже проходила.
BAD_SAMPLES = (
    ("нет стрелки вообще",
     "    # ДОЛГ P1 врач: отправка не обёрнута бюджетом, надо переделать\n"),
    ("стрелка есть, последствия нет",
     "    # ДОЛГ P1 врач: отправка не обёрнута бюджетом ->\n"),
    ("последствие — отписка",
     "    # ДОЛГ P1 врач: отправка не обёрнута бюджетом -> плохо\n"),
    ("не сказано, что не сделано",
     "    # ДОЛГ P2 внутр: тут -> врач ждёт ответа, которого не будет никогда\n"),
    ("нет приоритета",
     "    # ДОЛГ врач: отправка не обёрнута -> врач видит зависший бот навсегда\n"),
    ("нет видимости для врача",
     "    # ДОЛГ P1: отправка не обёрнута -> врач видит зависший бот навсегда\n"),
    ("приоритет вне шкалы",
     "    # ДОЛГ P9 врач: отправка не обёрнута -> врач видит зависший бот\n"),
    ("продолжение оторвано обычным комментарием",
     "    # ДОЛГ P1 врач: отправка не обёрнута бюджетом\n"
     "    # врач видит зависший бот до 500 с и в журнале об этом ни строки\n"),
)
for why, sample in BAD_SAMPLES:
    f, b = parse_marks(sample, "образец")
    check(f"неполная поймана: {why}", not f and len(b) == 1,
          f"полных {len(f)}, неполных {len(b)}")
    if b:
        print(f"         причина: {b[0]['why']}")

# И обратная сторона: детектор не должен объявлять неполной ЖИВУЮ пометку.
for item in TREE:
    f, b, _ok = scan_file(item["file"])
    check(f"живая пометка в {item['file']} признана полной",
          any(i["line"] == item["line"] for i in f), f"неполных в файле: {len(b)}")

print()
print("=" * 70)
print("[5] Долг, объявленный ЗАКРЫТЫМ, закрыт поведенчески")
print("=" * 70)
# Инвентарь только из открытых пометок — половина инвентаря. Вторая половина:
# то, что перестали помечать долгом, обязано быть закрыто на деле, иначе
# «закрыто» превращается в способ убрать долг из счёта.

# (а) Конвертат голосового. Правило имени живёт в двух модулях, поэтому список
# суффиксов берём ИЗ ИСХОДНИКА gemini_client, а не литералом: появится третий
# формат — проверка увидит его сама, без правки теста.
import blocking_tools as B  # noqa: E402

_GC = io.open("gemini_client.py", encoding="utf-8-sig").read()
_suffixes = sorted(set(re.findall(r'base \+ "(_converted\.[a-z0-9]+)"', _GC)))
check("суффиксы конвертата вычитаны из gemini_client", len(_suffixes) >= 2,
      f"найдено {_suffixes} — правило имени изменилось, проверку надо перечитать")
_tmp = tempfile.mkdtemp(prefix="debt_registry_")
_voice = os.path.join(_tmp, "голосовое_42.ogg")
io.open(_voice, "wb").write(b"\x00" * 64)
_made = {}
for _suf in _suffixes:
    _p = os.path.join(_tmp, "голосовое_42" + _suf)
    io.open(_p, "wb").write(b"\x00" * 4096)
    _made[_suf] = _p
B._remove_converted_wav(_voice)
_left = sorted(suf for suf, p in _made.items() if os.path.exists(p))
check("уборщик снимает ВСЕ виды конвертата, а не только .wav", not _left,
      f"осталось {_left} — утечка вернулась, на полном диске бот не скачает медиа")
check("исходное голосовое уборщик не трогает", os.path.exists(_voice),
      "уборщик снёс сам файл врача")
# Посторонний файл рядом сноситься не должен: стем чужой.
_alien = os.path.join(_tmp, "другое_converted.wav")
io.open(_alien, "wb").write(b"\x00")
B._remove_converted_wav(_voice)
check("конвертат ДРУГОГО голосового не задет", os.path.exists(_alien),
      "уборщик снёс чужой конвертат — параллельная расшифровка потеряет вход")
for _p in list(_made.values()) + [_voice, _alien]:
    try:
        os.remove(_p)
    except OSError:
        pass
try:
    os.rmdir(_tmp)
except OSError:
    pass

# (б) Обрезка по границе предложения: одна реализация на бот.
import html_safe  # noqa: E402
import web_lookup as W  # noqa: E402

check("web_lookup.clip_at_sentence — это функция из html_safe",
      W.clip_at_sentence is html_safe.clip_at_sentence_text,
      f"{getattr(W.clip_at_sentence, '__module__', '?')} — заведена своя копия")
_short = "короткий"
check("текст короче лимита не получает многоточия",
      W.clip_at_sentence(_short, 50) == _short, repr(W.clip_at_sentence(_short, 50)))
_WL = io.open("web_lookup.py", encoding="utf-8-sig").read()
check("в web_lookup нет своего def clip_at_sentence",
      "def clip_at_sentence" not in _WL, "третья копия обрезки вернулась")

# (в) /web действительно подключена: докстринг web_lookup больше не имеет права
# утверждать обратное, и путь обязан быть в /help и в меню Telegram.
_A = io.open("assistant.py", encoding="utf-8-sig").read()
check("run_lookup вызывается из assistant", "web_lookup.run_lookup(" in _A,
      "слой качества снова никем не вызван")
check("/web есть в меню Telegram", "command='web'" in _A or 'command="web"' in _A,
      "команда обещана, а в меню её нет")
check("/web есть в тексте /help", "/web" in _A, "в /help о команде не сказано")
check("докстринг web_lookup не утверждает, что его никто не вызывает",
      "Не вызывает его НИКТО" not in _WL,
      "докстринг снова врёт: следующий исполнитель пойдёт подключать подключённое")

print()
print("=" * 70)
print("[6] Оборот речи пометкой не считается")
print("=" * 70)
# Слово «долг» в этом дереве встречается в обороте: «долгий», «долгая
# синхронизация», «долг медиа». Инвентарь, который их считает, врёт в другую
# сторону — и следующая волна получит список из пустых мест.
NOISE = (
    "    # долгая синхронизация архива идёт до 40 минут\n",
    "    # долг медиа: 2.0 МБ на случай\n",
    "    # долг P1 врач: строчными это не пометка -> и не должно быть\n",
    '    text = "ДОЛГИЙ"  # не комментарий-пометка\n',
    "    # ЛАЙФХАК: обрезка по предложению\n",
    "    # ДОЛГОЖДАННАЯ правка: обрезка сведена в одно место\n",
)
for sample in NOISE:
    f, b = parse_marks(sample, "образец")
    check(f"не пометка: {sample.strip()[:52]}", not f and not b,
          f"полных {len(f)}, неполных {len(b)}")
# А вот это ОБЯЗАНО попасть в неполные, а не пропасть: шапка испорчена.
_typo = "    # ДОЛГ P2: приоритет есть, видимости нет -> последствие описано подробно\n"
_f, _b = parse_marks(_typo, "образец")
check("пометка с испорченной шапкой не пропадает молча", not _f and len(_b) == 1,
      f"полных {len(_f)}, неполных {len(_b)}")

print()
print("=" * 70)
print("[7] Вложенная копия репозитория в обход не попадает")
print("=" * 70)
# Внутри репозитория лежит его ПОЛНАЯ КОПИЯ в подкаталоге stomchat/ (её оставил
# сторонний органайзер, она в .gitignore). Любой рекурсивный обход удваивает
# числа: на этом уже попались двое, включая лида. Обход здесь не рекурсивный.
_nested = os.path.isdir("stomchat")
print(f"  вложенная копия на диске: {'ДА' if _nested else 'нет'}")
check("обход не рекурсивный — подкаталоги не читаются",
      all(os.sep not in i["file"] and "/" not in i["file"] for i in TREE),
      str([i["file"] for i in TREE]))
if _nested:
    _dupes = [i for i in TREE if i["file"].startswith("stomchat")]
    check("из вложенной копии не взято ни одной пометки", not _dupes, str(_dupes))
else:
    check("вложенной копии нет — удваивать нечему", True)
check("каждая пометка встречается в инвентаре один раз",
      len({(i["file"], i["line"]) for i in TREE}) == len(TREE),
      f"уникальных {len({(i['file'], i['line']) for i in TREE})} из {len(TREE)}")

print()
print("=" * 70)
print("[8] Пометка — это КОММЕНТАРИЙ, а не текст в литерале")
print("=" * 70)
# Первая версия сканера искала текстом и записала в инвентарь описание формата из
# докстринга ЭТОГО файла, объявив собственную документацию неполной пометкой.
# Проверяется на этом же файле: в нём десятки строк, похожих на пометку, и все
# они лежат в строковых литералах.
_self = "test_debt_registry.py"
_sf, _sb, _sok = scan_file(_self)
_raw = io.open(_self, encoding="utf-8-sig").read()
_textual_full, _textual_broken = parse_marks(_raw, _self)
check("файл реестра разобран tokenize", _sok, "разбор упал — проверка ниже вакуумна")
check("в файле реестра сканер не нашёл ни одной пометки",
      not _sf and not _sb, f"полных {len(_sf)}, неполных {len(_sb)}")
check("текстовый сканер на этом файле НАХОДИТ ложную пометку — значит разделение"
      " не вакуумно",
      len(_textual_full) + len(_textual_broken) >= 1,
      f"текстом найдено {len(_textual_full) + len(_textual_broken)}: описание формата из"
      " докстринга исчезло, доказывать разделение стало нечем")
check("описание формата из докстринга текстовый поиск ловит, а tokenize нет",
      any(i["line"] < 45 for i in _textual_full + _textual_broken)
      and not any(i["line"] < 45 for i in _sf + _sb),
      f"текстом {[i['line'] for i in _textual_full + _textual_broken][:6]}, "
      f"tokenize {[i['line'] for i in _sf + _sb]}")
# И наоборот: настоящий комментарий в настоящем файле сканер видеть обязан.
_bt_full, _bt_broken, _ = scan_file("blocking_tools.py")
check("настоящий комментарий-пометка в blocking_tools найден", len(_bt_full) == 1,
      f"найдено {len(_bt_full)} — tokenize отфильтровал лишнее")

print()
print("=" * 70)
print(f"ИНВЕНТАРЬ: {len(TREE)} пометок долга, "
      f"P1={len([i for i in TREE if i['priority'] == 1])} "
      f"P2={len([i for i in TREE if i['priority'] == 2])} "
      f"P3={len([i for i in TREE if i['priority'] == 3])}, "
      f"с последствием для врача {len([i for i in TREE if i['audience'] == 'врач'])}")
print("=" * 70)
print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
