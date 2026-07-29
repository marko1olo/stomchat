# -*- coding: utf-8 -*-
"""
Полнота конвейера дистилляции: 117 847 реплик архива -> 12 784 факта вики.

ЗАМЕР КОНВЕЙЕРА END-TO-END (живые базы, 2026-07-29, только чтение mode=ro):

    реплик в архиве                        117 847   (msg_id 5..139701)
    помечено is_processed_for_wiki=1        117 403
    кандидатов к дистилляции сейчас               0
    вызовов модели по distiller.log            1 731  (200: 1636, 429: 72, 404: 19, 400: 4)
    провалов парсинга JSON по логу                 0
    фактов в вики                           12 784   (7.8 факта на успешный вызов)
    уникальных msg_id в source_ids           48 361, из них есть в архиве 47 832
    ПОКРЫТИЕ АРХИВА ФАКТАМИ                   40.59 %
    НИКОГДА не дистиллировано                 70 015 реплик (59.41 %)
      из них длиннее 150 символов              3 974
      из них с медиа                           9 794

СЦЕНАРИЙ ОТКАЗА ДЛЯ ВРАЧА, который эти проверки закрывают: врач спрашивает про
ВНЧС, в базе 88 фактов, а 59 % архива — включая 3 974 длинных содержательных
реплики — вообще никогда не превращались в факты, потому что пачку помечали
обработанной даже когда модель не ответила. И сито сегодня нельзя перезапустить:
первый же print() с эмодзи падает на cp1251-консоли.

Запуск: python test_distill_pipeline.py     (offline, ни одного вызова модели)
Живые БД читаются только на чтение; если их нет — секция печатает SKIP.
"""
import asyncio
import io
import os
import re
import sqlite3
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Лог сита уводим в temp ДО импорта: боевой distiller.log трогать нельзя.
_TMP = tempfile.mkdtemp(prefix="stomchat_distill_")
os.environ["STOMCHAT_DISTILLER_LOG"] = os.path.join(_TMP, "distiller_test.log")

import distiller as D  # noqa: E402
import dental_vocab as V  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  [SKIP] {name} -- {why}")


HERE = os.path.dirname(os.path.abspath(__file__))
WIKI = os.path.join(HERE, "stomat_wiki.db")
ARCH = os.path.join(HERE, "stomat_archive.db")


def ro(path):
    """Соединение строго на чтение. Записать в боевую базу тест не может физически."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


CODE = io.open(os.path.join(HERE, "distiller.py"), encoding="utf-8").read()
CODE_NOCOMMENT = "\n".join(l for l in CODE.split("\n") if not l.lstrip().startswith("#"))
# Только исполняемый код: без комментариев и без тройных строк. Нужно потому,
# что документация distiller.py ЦИТИРУЕТ прежние дефектные строки (например
# запись source_ids без нормализации), и детектор «дефект вернулся» иначе
# срабатывает на собственном объяснении, а не на коде.
CODE_EXEC = re.sub(r'"""(?:.|\n)*?"""', '""', CODE_NOCOMMENT)


# ==============================================================================
# [1] Модуль импортируется без побочных эффектов
#
# ЗАМЕРЕНО: до правки `logging.basicConfig(handlers=[FileHandler("distiller.log")])`
# стоял на уровне модуля, а `import config` требовал .env и печатал баннер.
# Проверено запуском из пустого каталога: `import distiller` падал с
# UnicodeEncodeError внутри config.py, а до этого создавал distiller.log в CWD.
# СЦЕНАРИЙ ОТКАЗА: инструмент, который нельзя импортировать, нельзя и
# протестировать — именно поэтому 59 % непокрытого архива никто не заметил три
# месяца. А созданный в чужом каталоге distiller.log маскирует настоящий лог
# прогона, по которому только и можно понять, что пачка потерялась.
# ==============================================================================
print("\n[1] Модуль импортируется без побочных эффектов")
check("логирование НЕ поднимается при импорте",
      "logging.basicConfig" not in CODE_NOCOMMENT.split("def setup_logging")[0],
      "basicConfig на уровне модуля снова создаёт distiller.log при импорте")
check("config не тянется на уровне модуля",
      not re.search(r"(?m)^import config\b", CODE),
      "config читает .env и печатает баннер — модуль перестаёт импортироваться без .env")
check("gemini_knowledge импортируется лениво",
      not re.search(r"(?m)^import gemini_knowledge\b", CODE) and "import gemini_knowledge" in CODE,
      "клиент модели на уровне модуля делает импорт побочным")
check("пути к БД абсолютные (F11)",
      os.path.isabs(D.ARCHIVE_DB) and os.path.isabs(D.WIKI_DB),
      f"{D.ARCHIVE_DB} — запуск из другого каталога создаст пустую фальшивую вики")
check("путь лога переопределяется через окружение",
      D.LOG_PATH == os.environ["STOMCHAT_DISTILLER_LOG"],
      f"тест не смог увести лог: {D.LOG_PATH}")
check("мёртвый call_groq_llama удалён",
      "def call_groq_llama" not in CODE,
      "тянет config.GROQ_KEYS зря и путает читателя")


# ==============================================================================
# [2] print() не может уронить прогон (F1)
#
# ЗАМЕРЕНО НА ЭТОЙ МАШИНЕ: sys.stdout.encoding = cp1251, и
# `python -c "print('\U0001F4A1')"` падает с UnicodeEncodeError (проверено, exit=1,
# в том числе при перенаправлении в файл). Прежний distiller печатал эмодзи в
# семи местах, причём САМЫЙ ПЕРВЫЙ print стоял до цикла — сито умирало ДО первой
# пачки. Вывод текста факта стоял ВНУТРИ `async with wiki` ДО commit().
# 65 из 12 784 фактов содержат символы, непечатаемые в cp1251 (₽, ², é, ö);
# 10 074 из 107 316 реплик архива (9.4 %) — тоже.
# СЦЕНАРИЙ ОТКАЗА: человек запускает сито на ночь, чтобы вернуть 59 % архива.
# Оно падает на первой же строке вывода, и утром врач по-прежнему не находит
# ничего про ВНЧС, потому что за ночь не обработана ни одна реплика.
# ==============================================================================
print("\n[2] print() не может уронить прогон")


class _Cp1251Sink(io.TextIOBase):
    """Точная модель боевого stdout: кодировка cp1251, ошибки НЕ подавляются."""
    encoding = "cp1251"

    def __init__(self):
        self.data = []

    def write(self, s):
        s.encode("cp1251")  # как настоящий print: непечатаемое -> UnicodeEncodeError
        self.data.append(s)
        return len(s)


def _say_into_cp1251(text):
    sink = _Cp1251Sink()
    real = sys.stdout
    sys.stdout = sink
    try:
        D.say(text)
    finally:
        sys.stdout = real
    return "".join(sink.data)


_raised = False
try:
    _sink = _Cp1251Sink()
    _sink.write("\U0001F4A1")
except UnicodeEncodeError:
    _raised = True
check("модель боевого stdout действительно роняет эмодзи",
      _raised, "проверка потеряла смысл: cp1251 внезапно принимает эмодзи")

_ok = True
for _payload in ("\U0001F4A1 [1.1.2] факт", "цена 1500₽ за единицу", "площадь 2 мм²",
                 "Rubén Agustín-Panadero", "обычный текст"):
    try:
        _say_into_cp1251(_payload)
    except Exception as e:
        _ok = False
        check(f"say() не падает на {_payload[:22]!r}", False, f"{type(e).__name__}: {e}")
check("say() выдерживает все непечатаемые в cp1251 символы из живой базы", _ok)
check("say() всё же что-то печатает, а не глотает",
      "1.1.2" in _say_into_cp1251("\U0001F4A1 [1.1.2] факт"),
      "вывод пропал целиком — прогресс станет невидимым")
# Мало «не упасть»: аварийный ascii-фолбэк превратил бы весь русский текст в «?»,
# и человек не увидел бы, какой факт сохранён. Проверяем, что кириллица цела
# ИМЕННО в строке с непечатаемым символом (замер: таких фактов в базе 65).
check("say() сохраняет кириллицу в строке с непечатаемым символом",
      "ирригаци" in _say_into_cp1251("Цена набора для ирригации 1500₽"),
      "текст свалился в ascii-фолбэк: вывод превратился в «?»")
check("эмодзи из кода вычищены",
      not re.search(r"[\U0001F300-\U0001FAFF✅❌⏳⭐]", CODE),
      "остался литеральный эмодзи — на cp1251 это падение")
check("прямой print() из кода убран (весь вывод через say)",
      not re.search(r"(?m)^\s+print\(", CODE_NOCOMMENT),
      "остался print() — он и роняет прогон")
_body = CODE.split("if prepared:")[1].split("elif status ==")[0]
check("печать факта вынесена ИЗ транзакции вики",
      _body.index("say(") < _body.index("aiosqlite.connect(WIKI_DB"),
      "печать снова внутри `async with wiki` — падение теряет всю пачку без commit")


# ==============================================================================
# [3] Отказ модели — это НЕ «фактов нет» (F2)
#
# ЗАМЕРЕНО: 70 015 реплик (59.41 % архива) помечены обработанными, но ни один
# факт на них не ссылается; среди них 3 974 длиннее 150 символов и 92 непрерывные
# серии по >= 60 реплик (7 439 реплик) с 9-10 тыс. символов текста. Причина:
# `is_processed_for_wiki = 1` ставился ВНЕ ветки `if facts:`, а process_batch
# возвращал пустой список в трёх разных случаях — модель не ответила, JSON не
# разобрался, фактов честно нет.
# СЦЕНАРИЙ ОТКАЗА: врач спрашивает «какая экспозиция ЭДТА при финишной
# ирригации». Обсуждение из 153 реплик подряд (msg_id 52143..52302) лежит в
# архиве, но пачка попала на 429 по всем ключам, была помечена обработанной и
# больше никогда не будет прочитана. Справка уходит в промпт пустой.
# ==============================================================================
print("\n[3] Отказ модели — это НЕ «фактов нет»")
check("модель не ответила -> статус no_response",
      D.parse_facts(None) == ([], "no_response"),
      f"got {D.parse_facts(None)}")
check("пустая строка -> no_response", D.parse_facts("") == ([], "no_response"))
check("невалидный JSON -> parse_error",
      D.parse_facts("извините, не могу") == ([], "parse_error"),
      f"got {D.parse_facts('извините, не могу')}")
check("честно пустой ответ -> ok",
      D.parse_facts('{"facts": []}') == ([], "ok"),
      f"got {D.parse_facts('{\"facts\": []}')}")
_f, _s = D.parse_facts('```json\n{"facts": [{"c": "1.1.2", "f": "текст", "s": [1]}]}\n```')
check("маркдаун-обёртка снимается", _s == "ok" and len(_f) == 1, f"got {_s} {_f}")
check("три состояния РАЗЛИЧИМЫ, а не слиты в одно",
      len({D.parse_facts(None)[1], D.parse_facts("мусор")[1], D.parse_facts('{"facts":[]}')[1]}) == 3,
      "статусы совпали — значит отказ снова невозможно отличить от пустой пачки")

# Обрезанный по max_output_tokens=8192 ответ: первые объекты целые, внешние
# скобки не закрыты. Раньше терялась ВСЯ пачка; теперь целое спасается.
_trunc = '{"facts": [{"c":"1.1.3","f":"Ирригация 3% NaOCl с активацией.","s":[10,11]},' \
         '{"c":"1.1.4","f":"Обтурация биокерамикой.","s":[12]},{"c":"1.1.5","f":"Ретрит'
_f, _s = D.parse_facts(_trunc)
check("обрезанный ответ модели: целые факты спасены, а не потеряны все",
      _s == "salvaged" and len(_f) == 2,
      f"статус {_s}, фактов {len(_f)} — ожидалось salvaged/2")
_f, _s = D.parse_facts('[{"c":"1.1.2","f":"текст","s":[1]}]')
check("верхнеуровневый список тоже разбирается", _s == "ok" and len(_f) == 1, f"got {_s} {_f}")
_f, _s = D.parse_facts('{"articles": [{"c":"1.1.2","f":"текст","s":[1]}]}')
check("ключ назван иначе — факты всё равно найдены", len(_f) == 1, f"got {_f}")
_f, _s = D.parse_facts('{"facts": ["просто строка", {"c":"1.1.2","f":"текст","s":[1]}]}')
check("не-словари в facts отбрасываются, а не роняют прогон", _s == "ok" and len(_f) == 1, f"got {_f}")


# ==============================================================================
# [4] Пачка при отказе НЕ помечается обработанной — поведенческая проверка
#
# ЗАМЕРЕНО: is_processed_for_wiki=1 у 117 403 реплик, кандидатов сейчас 0.
# То есть повторный прогон в прежнем виде не обработал бы НИ ОДНОЙ реплики и
# напечатал «Архив полностью обработан». 444 оставшиеся — медиа без Vision.
# СЦЕНАРИЙ ОТКАЗА: у врача упал интернет на десять минут посреди ночного
# прогона. Прежний код за это время выжег бы ~40 пачек = ~2 900 реплик,
# и вернуть их можно только ручным UPDATE по журналу, которого не было.
#
# Фикстура — временные файлы SQLite, а не ':memory:': distiller открывает НОВОЕ
# соединение на каждый шаг, и ':memory:' давал бы каждый раз пустую базу.
# ==============================================================================
print("\n[4] Пачка при отказе НЕ помечается обработанной")

ARCH_SCHEMA = """
CREATE TABLE archive_messages (
    msg_id INTEGER PRIMARY KEY, date TIMESTAMP, sender_id INTEGER, sender_name TEXT,
    sender_username TEXT, text TEXT, reply_to_msg_id INTEGER, has_media BOOLEAN,
    media_type TEXT, media_remote_url TEXT, vision_description TEXT,
    vision_processed BOOLEAN DEFAULT 0, category_l1 TEXT, category_l2 TEXT,
    category_l3 TEXT, is_processed_for_wiki BOOLEAN DEFAULT 0)
"""


def make_fixture(n=10, texts=None, reply_map=None):
    """Мини-архив и пустая вика во временном каталоге. Боевые БД не участвуют."""
    d = tempfile.mkdtemp(prefix="stomchat_fx_")
    a, w = os.path.join(d, "a.db"), os.path.join(d, "w.db")
    db = sqlite3.connect(a)
    db.execute(ARCH_SCHEMA)
    for i in range(1, n + 1):
        txt = (texts or {}).get(i, f"Реплика про ирригацию каналов номер {i}")
        db.execute(
            "INSERT INTO archive_messages (msg_id, date, sender_name, text, reply_to_msg_id,"
            " has_media, vision_processed, is_processed_for_wiki) VALUES (?,?,?,?,?,0,0,0)",
            (i, "2026-01-01", f"Врач{i}", txt, (reply_map or {}).get(i)))
    db.commit()
    db.close()
    wdb = sqlite3.connect(w)
    wdb.execute("""CREATE TABLE distilled_facts (id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_code TEXT, content TEXT, source_ids TEXT, media_links TEXT, is_case BOOLEAN,
        confidence INTEGER, processed_at TIMESTAMP, is_reclassified BOOLEAN DEFAULT 0)""")
    wdb.commit()
    wdb.close()
    return a, w


def run_main(archive, wiki, batch_responses, batch_size=4, overlap=1, max_attempts=2):
    """
    Крутит настоящий distiller.main() на фикстуре с подставленным process_batch.
    Ни одного вызова модели. Возвращает (помечено, фактов, сколько раз позвали).
    """
    saved = (D.ARCHIVE_DB, D.WIKI_DB, D.BATCH_SIZE, D.OVERLAP,
             D.MAX_BATCH_ATTEMPTS, D.process_batch, asyncio.sleep)
    calls = []

    async def fake_batch(rows):
        i = min(len(calls), len(batch_responses) - 1)
        calls.append([r[0] for r in rows])
        facts, status = batch_responses[i]
        return facts, status, {"used": len(rows), "empty": 0, "clipped": 0,
                               "clipped_chars": 0, "deferred": []}

    async def no_sleep(_s):
        return None

    D.ARCHIVE_DB, D.WIKI_DB = archive, wiki
    D.BATCH_SIZE, D.OVERLAP, D.MAX_BATCH_ATTEMPTS = batch_size, overlap, max_attempts
    D.process_batch = fake_batch
    asyncio.sleep = no_sleep
    try:
        asyncio.run(D.main())
    finally:
        (D.ARCHIVE_DB, D.WIKI_DB, D.BATCH_SIZE, D.OVERLAP,
         D.MAX_BATCH_ATTEMPTS, D.process_batch, asyncio.sleep) = saved
    marked = sqlite3.connect(archive).execute(
        "SELECT COUNT(*) FROM archive_messages WHERE is_processed_for_wiki=1").fetchone()[0]
    nfacts = sqlite3.connect(wiki).execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
    return marked, nfacts, calls


_a, _w = make_fixture(4)
_marked, _n, _calls = run_main(_a, _w, [([], "no_response")], batch_size=4, overlap=1, max_attempts=2)
# Разница между «починено» и «нет» видна в СОСТАВЕ повторной пачки: если реплики
# не помечены, второй вызов получает РОВНО ТЕ ЖЕ msg_id. Если пачку выжгли после
# первого отказа — второй вызов увидит уже другой хвост.
check("отказ транспорта: пачка повторяется тем же составом, а не выжигается",
      len(_calls) >= 2 and _calls[1] == _calls[0],
      f"вызовов {len(_calls)}; первая {_calls[0] if _calls else None}, "
      f"вторая {_calls[1] if len(_calls) > 1 else None} — реплики уже помечены")
check("отказ транспорта: после MAX_BATCH_ATTEMPTS потеря ЗАЛОГИРОВАНА, не молчит",
      "ПОТЕРЯ" in io.open(D.LOG_PATH, encoding="utf-8").read(),
      "в логе нет строки ПОТЕРЯ — реплики исчезли бы бесследно")

_a, _w = make_fixture(4)
_ok_fact = [{"c": "1.1.3", "f": "Ирригация 3% гипохлоритом с ультразвуковой активацией.", "s": [1, 2]}]
_marked, _n, _calls = run_main(_a, _w, [(_ok_fact, "ok")], batch_size=4, overlap=1)
check("успешная пачка: реплики помечены и факт сохранён",
      _marked == 4 and _n == 1, f"помечено {_marked}, фактов {_n}")

_a, _w = make_fixture(4)
_marked, _n, _calls = run_main(_a, _w, [([], "ok")], batch_size=4, overlap=1)
_log = io.open(D.LOG_PATH, encoding="utf-8").read()
check("честно пустая пачка: помечена (цикл не встаёт) И записана в лог",
      _marked == 4 and "ПУСТАЯ ПАЧКА" in _log,
      f"помечено {_marked}; 'ПУСТАЯ ПАЧКА' в логе: {'ПУСТАЯ ПАЧКА' in _log}")
check("в логе пустой пачки есть диапазон msg_id — потерю можно перепрогнать",
      re.search(r"ПУСТАЯ ПАЧКА msg_id \d+\.\.\d+", _log) is not None,
      "нет диапазона: непонятно, что именно перепрогонять")
check("в логе пустой пачки есть клиническая плотность",
      "клинических по dental_vocab" in _log,
      "без неё «флуд» и «модель промолчала на клинике» неразличимы")

_a, _w = make_fixture(6)
_marked, _n, _calls = run_main(_a, _w, [(_ok_fact, "ok")] * 4, batch_size=4, overlap=1)
check("нахлёст соблюдён: полная пачка помечает BATCH_SIZE-OVERLAP",
      len(_calls[0]) == 4 and _calls[1][0] == 4,
      f"первая пачка {_calls[0]}, вторая начинается с {_calls[1][0] if len(_calls) > 1 else None}")
check("цикл доходит до конца очереди", _marked == 6, f"помечено {_marked} из 6")
check("прогресс всегда > 0 — цикл не может встать",
      all(len(c) > 0 for c in _calls) and len(_calls) < 20, f"вызовов {len(_calls)}")


# ==============================================================================
# [5] Провенанс: source_ids нормализуются и сверяются с пачкой (F3)
#
# ЗАМЕРЕНО НА ЖИВОЙ ВИКЕ: 353 факта (2.76 %) держат ВСЕ source_ids в виде
# `MSG_12345` — 2 073 битых токена; регуляркой восстанавливается 1 985 id,
# 1 972 из них есть в архиве, покрытие архива 40.59 % -> 42.09 %. Ещё 759 фактов
# (5.94 %) ссылаются на 2 049 msg_id, которых в архиве нет; 8 токенов
# семизначные (1080108, 1144522) при максимуме архива 139 701.
# СЦЕНАРИЙ ОТКАЗА: `savdel.py:113` фильтрует id через `isdigit()`, у этих 353
# фактов список пустеет, блок «подтягиваем фото» не срабатывает, и врач получает
# статью про препарирование БЕЗ снимка, хотя у исходных реплик есть
# vision_description. Плюс 2 049 ложных ссылок ведут на чужие реплики.
# ==============================================================================
print("\n[5] Провенанс: source_ids нормализуются и сверяются с пачкой")
_allowed = {100, 101, 102}
check("MSG_-ярлык превращается в число",
      D.normalize_source_ids(["MSG_100", "MSG_101"], _allowed)[0] == [100, 101],
      f"got {D.normalize_source_ids(['MSG_100', 'MSG_101'], _allowed)}")
check("чужой id отбрасывается И попадает в список отброшенных",
      D.normalize_source_ids([100, 999], _allowed) == ([100], ["999"]),
      f"got {D.normalize_source_ids([100, 999], _allowed)}")
check("семизначный id (замер: 8 таких в базе) не проходит",
      D.normalize_source_ids([1080108], _allowed) == ([], ["1080108"]),
      f"got {D.normalize_source_ids([1080108], _allowed)}")
check("строка вместо списка тоже разбирается",
      D.normalize_source_ids("MSG_102", _allowed)[0] == [102])
check("дубли внутри одного факта схлопываются",
      D.normalize_source_ids([100, 100, "MSG_100"], _allowed)[0] == [100],
      f"got {D.normalize_source_ids([100, 100, 'MSG_100'], _allowed)}")
check("мусор не превращается в id, а уходит в отброшенные",
      D.normalize_source_ids([None, "abc", True], _allowed) == ([], ["None", "abc", "True"]),
      f"got {D.normalize_source_ids([None, 'abc', True], _allowed)}")
_row, _notes = D.prepare_fact({"c": "1.1.3", "f": "Текст статьи про ирригацию.", "s": ["MSG_100"]},
                              _allowed, set())
check("prepare_fact пишет провенанс числами", _row[2] == "100", f"got {_row[2]!r}")
_row, _notes = D.prepare_fact({"c": "1.1.3", "f": "Другая статья.", "s": [777]}, _allowed, set())
check("полностью ложный провенанс отмечается в заметках",
      any("провенанс пуст" in n for n in _notes), f"got {_notes}")
check("ни один отброшенный id не теряется молча",
      any("отброшено ссылок провенанса" in n for n in _notes), f"got {_notes}")


# ==============================================================================
# [6] Коды категорий сверяются с деревом и с картой экспорта (F4)
#
# ЗАМЕРЕНО: в базе 110 различных кодов при 52 легальных; 393 присвоения
# приходятся на 58 самопальных кодов (6.1.2 — 82, 1.1 — 21, 2.0.0 — 19,
# 7.2.2 — 19); 51 факт не имеет НИ ОДНОГО кода из savdel.py CAT_MAP.
# Отдельно замерено: из 34 кодов, общих для прежнего дерева 4.0 и CAT_MAP,
# 14 означают ДРУГУЮ клиническую тему — 3.3.1 у distiller «Синус-лифтинг», а
# savdel экспортирует этот код в файл «Пластика_Десны»; 6.1.1 «Анализ КЛКТ» ->
# «Оборудование_Оптика»; 5.3.1 «Микроскопы» -> «Цифра_3D_Печать».
# СЦЕНАРИЙ ОТКАЗА: врач открывает рубрику «Костная пластика», чтобы прочесть
# про синус-лифтинг, и находит там статьи про пластику десны — а 51 факт не
# лежит ни в одной рубрике вообще.
# ==============================================================================
print("\n[6] Коды категорий сверяются с деревом и с картой экспорта")
_savdel = os.path.join(HERE, "savdel.py")
if os.path.exists(_savdel):
    # Карту берём ОБЪЕКТОМ, а не регуляркой по исходнику savdel.py. Регулярка
    # искала литералы "1.2.3": и после сведения таксономии в taxonomy.py находила
    # ноль кодов — проверка при этом оставалась «зелёной по построению» на пустом
    # множестве. Так уже пропускали снятый потолок: проверка исходника доказывает
    # написанное, а не работающее.
    import savdel as _S  # noqa: E402
    _cat = set(_S.CAT_MAP)
    check(f"все {len(_cat)} кодов savdel.CAT_MAP достижимы для сита",
          _cat and not (_cat - D.LEAF_CODES),
          f"недостижимы: {sorted(_cat - D.LEAF_CODES)} — эти рубрики пусты по построению")
    # Инвариант стал строже: раньше сверяли разницу с РУЧНЫМ списком
    # NON_EXPORTABLE_LEAVES, теперь список вычисляется, поэтому проверяем то, что
    # важно врачу — у каждого листа дерева есть файл выгрузки. Лист без рубрики
    # означает, что факт сохранится, но ни в один файл ревью не попадёт.
    check("каждый лист сита имеет рубрику выгрузки",
          not (D.LEAF_CODES - _cat) and not D.NON_EXPORTABLE_LEAVES,
          f"листья без рубрики: {sorted(D.LEAF_CODES - _cat)}")
    check("сито и выгрузка берут коды из ОДНОГО объекта",
          _S.CAT_MAP is _S.taxonomy.EXPORT_SLUGS and D.LEAF_CODES is _S.taxonomy.LEAF_CODES,
          "снова две карты — они разойдутся так же, как разошлись пять прежних")
else:
    skip("сверка с savdel.CAT_MAP", "savdel.py отсутствует")
check("легальный лист проходит без подмены", D.normalize_category("1.1.2") == ("1.1.2", None))
check("2.2.6 (Фиксация) достижим — раньше его в дереве сита не было",
      D.normalize_category("2.2.6") == ("2.2.6", None), f"got {D.normalize_category('2.2.6')}")
check("7.1.1 (Экономика клиники) достижим",
      D.normalize_category("7.1.1") == ("7.1.1", None), f"got {D.normalize_category('7.1.1')}")
_c, _why = D.normalize_category("6.1.2")
check("самый частый самопальный код 6.1.2 (82 присвоения) поднят до 6.1.1",
      _c == "6.1.1" and _why, f"got {_c!r}, {_why!r}")
_c, _why = D.normalize_category("2.0.0")
check("2.0.0 (19 присвоений) не проходит как есть", _c != "2.0.0" and _why, f"got {_c!r}")
_c, _why = D.normalize_category("мусор")
check("нечитаемый код -> fallback, и это залогировано", _c == D.FALLBACK_CODE and _why, f"got {_c!r}")
check("подмена кода НИКОГДА не молчит",
      all(D.normalize_category(x)[1] for x in ["6.1.2", "1.1", "2.0.0", "1.1.0", "9.9.9", ""]),
      "какая-то подмена прошла без записи в лог")
# Проверка «выведено из дерева» тоже стала поведенческой: коды разбираются из того
# самого текста, который уходит в промпт модели, и сверяются с тем, что сито считает
# легальным. Ручной список кодов разойдётся с промптом — модель будет присваивать
# коды, которых сито не знает, и факт уедет в «прочее» вместо своей рубрики.
_from_tree = set(re.findall(r"\b(\d{1,2}(?:\.\d+){1,2})\b", D.KNOWLEDGE_TREE))
check("таксономия выведена из дерева, а не переписана пятым списком",
      _from_tree == set(D.LEGAL_CODES) and len(_from_tree) == 65,
      f"дерево даёт {len(_from_tree)} кодов, сито знает {len(D.LEGAL_CODES)}; "
      f"расхождение: {sorted(_from_tree ^ set(D.LEGAL_CODES))}")
check("дерево сита совпадает по смыслу с деревом reclass 5.0",
      "2.2.6 Адгезивная и цементная фиксация" in D.KNOWLEDGE_TREE
      and "3.3.1 Мукогингивальная пластика" in D.KNOWLEDGE_TREE,
      "дерево снова 4.0 — коды означают другое, чем в экспорте")


# ==============================================================================
# [7] Структура ветки обсуждения подаётся модели (F5)
#
# ЗАМЕРЕНО: reply_to_msg_id заполнен у 68 725 из 117 847 реплик (58.3 %) и не
# использовался в distiller вообще — SELECT его даже не запрашивал. Из 63 680
# пар «ответ-родитель» внутри обработанного набора 9 301 (14.6 %) попали в
# РАЗНЫЕ пачки, 3 712 (5.8 %) разнесены дальше, чем спасает OVERLAP = 8.
# СЦЕНАРИЙ ОТКАЗА: в чате параллельно идут три обсуждения. Модель получает
# плоское окно из 80 строк и склеивает их в одну «статью» — так в базе появился
# факт про синус-лифтинг с меткой «брекеты». Врач читает статью, в которой
# смешаны два разных клинических протокола, и не может понять, что к чему.
# ==============================================================================
print("\n[7] Структура ветки обсуждения подаётся модели")
check("reply_to_msg_id есть в списке колонок SELECT",
      re.search(r"SELECT\s+msg_id[^']*?reply_to_msg_id[^']*?FROM archive_messages", CODE) is not None,
      "поле снова не запрашивается — связи ветки модели неоткуда взять")
check("SELECT возвращает 7 колонок, как их читает build_prompt_log",
      len(D.build_prompt_log([(1, "d", "Врач", "текст", None, None, 0)])[0]) > 0 and
      re.search(r"SELECT\s+((?:\w+,\s*){6}\w+)\s+FROM archive_messages", CODE) is not None,
      "число колонок разошлось с распаковкой m[6] — reply_to молча станет None")
_msgs = [
    (100, "2026-01-01", "Иван", "Какой торк при установке импланта?", None, None, None),
    (101, "2026-01-01", "Пётр", "35 Нküчем", None, None, 100),
    (102, "2026-01-01", "Ольга", "А у нас другая тема — ирригация", None, None, None),
]
_log_txt, _stats = D.build_prompt_log(_msgs)
check("ответ помечен стрелкой на родителя", "MSG_101 -> MSG_100" in _log_txt, _log_txt)
check("корневая реплика без стрелки", "MSG_100 |" in _log_txt, _log_txt)
check("все три реплики попали в промпт", _stats["used"] == 3, f"got {_stats}")
check("промпт объясняет модели смысл стрелки",
      "является ОТВЕТОМ" in CODE, "модель увидит '->' и не поймёт, что это")
check("промпт прямо запрещает префикс MSG_ в поле s",
      "БЕЗ префикса MSG_" in CODE, "353 факта в базе появились именно из-за этого")


# ==============================================================================
# [8] Любая граница логирует то, что отбросила; обрезка по границе предложения
#
# ЗАМЕРЕНО: самая длинная реплика архива 4 072 символа, максимум
# vision_description 1 276; на пачку из 80 реплик приходится p50 = 7 854,
# p99 = 15 194, максимум 20 961 символ — то есть MSG_CHAR_CAP = 4200 и
# PROMPT_CHAR_BUDGET = 48000 в норме не срабатывают вовсе. Максимальная длина
# факта в базе 5 477 при CONTENT_CHAR_CAP = 6000. Обрезанных на полуслове
# фактов в базе 1 из 12 784, и восстановить его уже нечем.
# СЦЕНАРИЙ ОТКАЗА: врач читает статью про адгезивный протокол, а она кончается
# на «наносить бонд в течение 20 сек, затем разду» — и он не знает, «раздувать»
# или «раздельно», то есть протокол непригоден к применению.
# ==============================================================================
print("\n[8] Любая граница логирует то, что отбросила")
_t = "Первое предложение про адгезив. Второе предложение про полимеризацию. Третье оборвётся"
_clipped, _dropped = D.clip_at_sentence(_t, 40)
check("обрезка идёт по границе предложения",
      _clipped.endswith(".") and _dropped > 0, f"got {_clipped!r}, отброшено {_dropped}")
check("обрезка не режет слово пополам",
      not re.search(r"[а-яА-Я]$", D.clip_at_sentence(_t, 40)[0]), D.clip_at_sentence(_t, 40)[0])
check("короткий текст не трогается", D.clip_at_sentence("Короткий текст.", 100) == ("Короткий текст.", 0))
_long = "слово " * 50
_c2, _d2 = D.clip_at_sentence(_long, 60)
check("текст без точек режется по границе слова",
      not _c2.endswith("сло") and _d2 > 0, f"got {_c2!r}")
check("clip_at_sentence возвращает СКОЛЬКО отброшено (иначе обрезка молчаливая)",
      isinstance(D.clip_at_sentence(_t, 40)[1], int) and D.clip_at_sentence(_t, 40)[1] > 0)
check("границы объявлены константами с замером в комментарии",
      all(k in CODE for k in ("MSG_CHAR_CAP", "PROMPT_CHAR_BUDGET", "CONTENT_CHAR_CAP")))
check("границы взяты с запасом над замером архива",
      D.MSG_CHAR_CAP > 4072 and D.PROMPT_CHAR_BUDGET > 20961 and D.CONTENT_CHAR_CAP > 5477,
      f"{D.MSG_CHAR_CAP}/{D.PROMPT_CHAR_BUDGET}/{D.CONTENT_CHAR_CAP} — граница режет живые данные")
_saved_budget = D.PROMPT_CHAR_BUDGET
D.PROMPT_CHAR_BUDGET = 90
_log_txt, _stats = D.build_prompt_log([
    (i, "2026-01-01", "Врач", "Достаточно длинный текст про ирригацию каналов", None, None, None)
    for i in range(1, 6)])
D.PROMPT_CHAR_BUDGET = _saved_budget
check("исчерпанный бюджет промпта ОТКЛАДЫВАЕТ реплики, а не теряет",
      _stats["deferred"] and _stats["used"] >= 1, f"got {_stats}")
check("отложенные реплики перечислены по msg_id",
      all(isinstance(x, int) for x in _stats["deferred"]), f"got {_stats['deferred']}")
_log_now = io.open(D.LOG_PATH, encoding="utf-8").read()
check("исчерпание бюджета попало в лог", "Бюджет промпта" in _log_now,
      "граница сработала молча — запрещено")
_a, _w = make_fixture(4)
_saved_budget = D.PROMPT_CHAR_BUDGET
D.PROMPT_CHAR_BUDGET = 90
try:
    _marked, _n, _calls = run_main(_a, _w, [(_ok_fact, "ok")] * 8, batch_size=4, overlap=1)
finally:
    D.PROMPT_CHAR_BUDGET = _saved_budget
check("отложенные бюджетом реплики всё равно доходят до обработки",
      _marked == 4, f"помечено {_marked} из 4 — часть реплик потерялась")

# Пустые реплики: замер — 10 531 с пустым text, из них 10 136 имеют
# vision_description (полезны) и 395 пусты целиком.
_log_txt, _stats = D.build_prompt_log([
    (1, "2026-01-01", "Врач", "", None, None, None),
    (2, "2026-01-01", "Врач", None, "Снимок КЛКТ с признаками периимплантита", None, None),
    (3, "2026-01-01", "Врач", "Текст про ирригацию", None, None, None),
])
check("совсем пустая реплика не занимает слот пачки", _stats["empty"] == 1, f"got {_stats}")
check("реплика с vision_description слот занимает (это знание, а не пустота)",
      "MSG_2" in _log_txt and "периимплантита" in _log_txt, _log_txt)


# ==============================================================================
# [9] Повторный прогон не удваивает базу (F10)
#
# ЗАМЕРЕНО: у distilled_facts НЕТ ни одного UNIQUE-индекса (единственный
# индекс — idx_cat по category_code). Точных дублей content сейчас 3 группы /
# 9 строк, по первым 120 нормализованным символам — 10 групп / 24 строки, то
# есть база пока чистая. Но чтобы вернуть 59 % архива, надо сбросить
# is_processed_for_wiki, и прежний код вставил бы вторые копии всех 12 784
# фактов.
# СЦЕНАРИЙ ОТКАЗА: врач жмёт /search коронка и получает один и тот же абзац
# трижды подряд, а лимит строк справки при этом съеден копиями — реального
# второго и третьего факта по теме он не увидит.
# ==============================================================================
print("\n[9] Повторный прогон не удваивает базу")
_seen = set()
_r1, _n1 = D.prepare_fact({"c": "1.1.3", "f": "Ирригация 3% NaOCl.", "s": [100]}, {100}, _seen)
_r2, _n2 = D.prepare_fact({"c": "1.1.3", "f": "Ирригация 3% NaOCl.", "s": [100]}, {100}, _seen)
check("точный дубль не вставляется", _r1 is not None and _r2 is None, f"{_r1 is None} {_r2 is None}")
check("отказ от дубля залогирован", any("дубль" in n for n in _n2), f"got {_n2}")
_r3, _ = D.prepare_fact({"c": "1.1.3", "f": "  ирригация   3%  NaOCl!  ", "s": [100]}, {100}, _seen)
check("почти-дубль (регистр, пробелы, пунктуация) тоже не вставляется", _r3 is None)
_r4, _ = D.prepare_fact({"c": "1.1.4", "f": "Обтурация биокерамикой.", "s": [100]}, {100}, _seen)
check("другой факт вставляется", _r4 is not None)
_r5, _n5 = D.prepare_fact({"c": "1.1.3", "f": "   ", "s": [100]}, {100}, set())
check("факт без текста не вставляется", _r5 is None and _n5, f"got {_r5}, {_n5}")
check("схема боевой базы НЕ мигрируется из рабочего инструмента",
      "ALTER TABLE" not in CODE_EXEC and "CREATE UNIQUE" not in CODE_EXEC,
      "миграция боевой БД должна идти патчем, а не ночным прогоном")
# Протечка примера промпта: замер — 23 факта начинаются «Методика BOPT», 16
# содержат дословную фразу примера, 40 — «коррекции зенитов».
_r6, _n6 = D.prepare_fact(
    {"c": "2.2.1", "f": "Методика BOPT позволяет добиться прироста мягких тканей и коррекции зенитов.",
     "s": [100]}, {100}, set())
check("протечка примера промпта отмечается (замер: 23 факта в базе)",
      any("ПРОТЕЧКА ПРОМПТА" in n for n in _n6), f"got {_n6}")
check("протечка не удаляет факт молча — решает человек", _r6 is not None)


# ==============================================================================
# [10] dental_vocab: бытовое слово с клиническим корнем — не клиника (F9)
#
# ЗАМЕРЕНО НА ЖИВОМ АРХИВЕ (107 316 реплик с текстом): в словаре есть
# четырёхбуквенные корни «преп» и «скан», и проверка по началу слова принимала
# за клинику «преподаватель», «препятствие», «скандал». 28 реплик признавались
# клиническими ТОЛЬКО из-за такого слова («Без скандалов, интриг, расследований»,
# «Мне по нраву то как он преподносит инфу»). После правки клинических 42 191
# вместо 42 219 — ровно −28, ни одна клиническая реплика не потеряна.
# СЦЕНАРИЙ ОТКАЗА: сито логирует пустую пачку с пометкой «клинических реплик:
# 12», человек идёт перепрогонять её ради знания, а там дюжина реплик про
# скандал в администрации и преподавателя на курсах — потраченный вызов модели
# и ложный след в журнале потерь.
# ==============================================================================
print("\n[10] dental_vocab: бытовое слово с клиническим корнем — не клиника")
for _w in ["преподаватель", "преподавателя", "преподносит", "препятствие",
           "препирательства", "скандал", "скандальный", "скандинавский", "беспрепятственно"]:
    check(f"«{_w}» — не клиника", not V.has_dental_term(_w), "гейт считает это клиникой")
for _w in ["препарирование", "отпрепарировал", "вертипреп", "недопреп", "препарат",
           "сканер", "сканмаркер", "отсканить", "праймскан", "периимплантит",
           "коронка", "внчс", "эдта", "зуб", "расцементировка", "дебондинг"]:
    check(f"«{_w}» — клиника (полнота не потеряна)", V.has_dental_term(_w),
          "правка отрезала клинический термин")
check("«Без скандалов, интриг, расследований» не клиника",
      not V.has_dental_term("Без скандалов, интриг, расследований"))
# is_dental_keyword проверяется ОТДЕЛЬНО: триаж ассистента и фильтр дайджеста
# вызывают именно его, минуя has_dental_term. Guard обязан стоять в обоих.
for _w in ["преподаватель", "преподавателя", "скандал", "препятствие"]:
    check(f"is_dental_keyword сам отсекает «{_w}»", not V.is_dental_keyword(_w),
          "guard стоит только в has_dental_term — триаж ассистента снова ловит бытовое слово")
for _w in ["преп", "препар", "скан", "коронк", "внчс"]:
    check(f"is_dental_keyword по-прежнему знает «{_w}»", V.is_dental_keyword(_w),
          "guard отрезал клинический корень")
check("реплика с бытовым И клиническим словом остаётся клинической",
      V.has_dental_term("преподаватель показал препарирование под коронку"),
      "отсекли всю реплику из-за одного бытового слова")
check("список бытовых префиксов закрытый и объявлен",
      isinstance(V.NON_CLINICAL_PREFIXES, frozenset) and len(V.NON_CLINICAL_PREFIXES) >= 6)
check("dental_vocab импортируется без .env (мягкий import config)",
      "except Exception" in io.open(os.path.join(HERE, "dental_vocab.py"),
                                    encoding="utf-8").read().split("import config")[1][:200],
      "жёсткий import config делает словарь неимпортируемым из сита")
check("clinical_density опирается на dental_vocab, а не на свой список",
      "import dental_vocab" in CODE and "has_dental_term" in CODE,
      "второй список терминов разойдётся с первым — это уже случалось с summarizer")
_dens = D.clinical_density([
    (1, "d", "Врач", "Ирригация каналов гипохлоритом", None, None, None),
    (2, "d", "Врач", "Без скандалов, интриг, расследований", None, None, None),
])
check("clinical_density считает клинические реплики, а не все", _dens == 1, f"got {_dens}")


# ==============================================================================
# [11] Живая база: замеры, на которых стоят все выводы выше (только чтение)
#
# ЗАМЕРЕНО: архив 117 847 реплик, вика 12 784 факта, покрытие 40.59 %,
# кандидатов сейчас 0, 353 факта с MSG_-провенансом, 759 фактов со ссылками на
# несуществующие msg_id, 51 факт невидим для экспорта savdel.py.
# СЦЕНАРИЙ ОТКАЗА: если эти числа поедут (например, покрытие «вырастет» из-за
# того, что кто-то залил дубли), выводы всех секций выше станут неверными, а
# врач продолжит получать пустую справку по ВНЧС.
# ==============================================================================
print("\n[11] Живая база: замеры, на которых стоят все выводы выше")
if not (os.path.exists(WIKI) and os.path.exists(ARCH)):
    skip("замеры по живым базам", f"нет {WIKI if not os.path.exists(WIKI) else ARCH} — "
                                  f"проверки НЕ выполнены, это не успех")
else:
    _A, _W = ro(ARCH), ro(WIKI)
    _n_arch = _A.execute("SELECT COUNT(*) FROM archive_messages").fetchone()[0]
    _n_fact = _W.execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
    check(f"архив на месте: {_n_arch} реплик (замер 117847)", _n_arch > 100000, f"got {_n_arch}")
    check(f"вика на месте: {_n_fact} фактов (замер 12784)", _n_fact > 12000, f"got {_n_fact}")

    _cand = _A.execute("SELECT COUNT(*) FROM archive_messages WHERE is_processed_for_wiki=0 "
                       "AND (has_media=0 OR vision_processed=1)").fetchone()[0]
    _locked = _A.execute("SELECT COUNT(*) FROM archive_messages WHERE is_processed_for_wiki=0 "
                         "AND has_media=1 AND vision_processed=0").fetchone()[0]
    print(f"      кандидатов к дистилляции: {_cand}; заперто медиа без Vision: {_locked}")
    check("очередь пуста, но это НЕ значит «архив дистиллирован» — сито это теперь говорит",
          "НЕ значит, что архив дистиллирован" in CODE,
          "вернулось прежнее «Архив полностью обработан!» при 59 % непокрытых")
    check("запертые до Vision реплики больше не молчат",
          "Заперто до Vision" in CODE, f"{_locked} реплик снова невидимы для человека")

    _arch_ids = set(r[0] for r in _A.execute("SELECT msg_id FROM archive_messages"))
    _cited, _bad_tok, _msg_facts, _ghost_facts = set(), 0, 0, 0
    for _s, in _W.execute("SELECT source_ids FROM distilled_facts"):
        _toks = [t.strip() for t in (_s or "").split(",") if t.strip()]
        _num = [t for t in _toks if t.isdigit()]
        _bad_tok += len(_toks) - len(_num)
        if _toks and not _num:
            _msg_facts += 1
        _ints = [int(t) for t in re.findall(r"\d+", _s or "")]
        if any(i not in _arch_ids for i in _ints):
            _ghost_facts += 1
        _cited.update(int(t) for t in _num)
    _cov = len(_cited & _arch_ids) / _n_arch * 100
    print(f"      покрытие архива фактами: {_cov:.2f}% (замер 40.59%)")
    print(f"      фактов с MSG_-провенансом: {_msg_facts} (замер 353), битых токенов {_bad_tok} (замер 2073)")
    print(f"      фактов со ссылкой на несуществующий msg_id: {_ghost_facts} (замер 759)")
    check("покрытие архива по-прежнему катастрофическое — это НЕ починено кодом",
          _cov < 60, f"{_cov:.2f}% — если выросло, замеры устарели, перепроверить выводы")
    check("порча провенанса в базе воспроизводится (замер 353 факта)",
          _msg_facts > 300, f"got {_msg_facts}")
    check("сито больше не может записать MSG_ в source_ids",
          '",".join(map(str, f.get(\'s\', [])))' not in CODE_EXEC,
          "вернулась запись провенанса без нормализации")

    _codes = {}
    for _cc, in _W.execute("SELECT category_code FROM distilled_facts"):
        for _c in re.split(r"[,\s]+", (_cc or "").strip()):
            if _c:
                _codes[_c] = _codes.get(_c, 0) + 1
    print(f"      различных кодов в базе: {len(_codes)} (замер 110 при 52 легальных)")
    check("самопальные коды в базе воспроизводятся (замер 110 кодов)", len(_codes) > 60,
          f"got {len(_codes)}")
    check("сито больше не пишет код модели без сверки",
          "normalize_category" in CODE and "f.get('c', '10.1')" not in CODE,
          "код снова берётся как есть")

    _dupg = _W.execute("SELECT COUNT(*) FROM (SELECT content FROM distilled_facts "
                       "GROUP BY content HAVING COUNT(*)>1)").fetchone()[0]
    _trunc = sum(1 for _c, in _W.execute("SELECT content FROM distilled_facts")
                 if (_c or "").rstrip() and not (_c or "").rstrip().endswith(tuple(".!?)»\"'…:;")))
    print(f"      групп точных дублей: {_dupg} (замер 3); фактов с обрывом фразы: {_trunc} (замер 1)")
    check("база пока не засорена дублями — и повторный прогон это сохранит",
          _dupg < 50, f"{_dupg} групп дублей — похоже, сито уже прогнали дважды")
    check("обрыв фразы в базе единичный", _trunc < 50, f"got {_trunc}")
    check("отсутствие UNIQUE-индекса подтверждено (нужна миграция от ведущего)",
          not [r for r in _W.execute("SELECT name,sql FROM sqlite_master WHERE type='index'")
               if "UNIQUE" in (r[1] or "").upper()],
          "UNIQUE появился — пункт патча можно снять")

    _reply = _A.execute("SELECT COUNT(*) FROM archive_messages "
                        "WHERE reply_to_msg_id IS NOT NULL").fetchone()[0]
    print(f"      reply_to_msg_id заполнен у {_reply} реплик (замер 68725)")
    check("структура ветки в архиве есть — и теперь используется", _reply > 60000, f"got {_reply}")

    _nonprint = 0
    for _c, in _W.execute("SELECT content FROM distilled_facts"):
        try:
            (_c or "").encode("cp1251")
        except UnicodeEncodeError:
            _nonprint += 1
    print(f"      фактов с непечатаемыми в cp1251 символами: {_nonprint} (замер 65)")
    check("риск падения на печати факта реален и измерен", _nonprint > 0,
          "если 0 — проверка секции [2] потеряла обоснование, перепроверить")


# ==============================================================================
# [12] Проверки выше ЛОВЯТ поломку, а не украшают отчёт
#
# Каждая проверка обязана падать на заведомо сломанном коде. Здесь показано,
# что детекторы срабатывают на подставленных поломках, а не проходят всегда.
# СЦЕНАРИЙ ОТКАЗА без этой секции: набор из 90 зелёных галочек, который не
# заметил бы возврата ни одного из десяти дефектов — ровно то состояние, в
# котором конвейер прожил три месяца с 59 % непокрытого архива.
# ==============================================================================
print("\n[12] Проверки выше ЛОВЯТ поломку")
check("детектор эмодзи поймал бы возврат литерала",
      re.search(r"[\U0001F300-\U0001FAFF]", "print('\U0001F4A1 факт')") is not None)
check("детектор эмодзи не срабатывает на чистом тексте",
      re.search(r"[\U0001F300-\U0001FAFF]", "say('[СИТО] пачка')") is None)
check("детектор прямого print поймал бы возврат",
      re.search(r"(?m)^\s+print\(", "    print('x')") is not None)
check("детектор старой записи провенанса поймал бы возврат",
      '",".join(map(str, f.get(\'s\', [])))' in
      'x = ",".join(map(str, f.get(\'s\', [])))')
check("детектор кода-без-сверки поймал бы возврат",
      "f.get('c', '10.1')" in "cat = f.get('c', '10.1')")
check("статусный контракт parse_facts не вырожден: 'ok' не возвращается на None",
      D.parse_facts(None)[1] != "ok",
      "если no_response переименуют в ok, секция [3] станет пустой")
_broken_seen = set()
D.prepare_fact({"c": "1.1.3", "f": "дубль-проба", "s": [1]}, {1}, _broken_seen)
check("детектор дублей опирается на РЕАЛЬНОЕ состояние, а не на счётчик",
      len(_broken_seen) == 1 and D.prepare_fact(
          {"c": "1.1.3", "f": "дубль-проба", "s": [1]}, {1}, _broken_seen)[0] is None)
check("normalize_source_ids без allowed_ids не превращается в заглушку",
      D.normalize_source_ids(["MSG_5"], None)[0] == [5],
      "сверка со пачкой обязана быть отключаемой ТОЛЬКО осознанно")
check("clip_at_sentence не может вернуть 0 отброшенных при реальной обрезке",
      D.clip_at_sentence("а" * 100, 10)[1] > 0)
check("фикстура действительно изолирована от боевых БД",
      D.ARCHIVE_DB == os.path.join(HERE, "stomat_archive.db") and
      D.WIKI_DB == os.path.join(HERE, "stomat_wiki.db"),
      f"пути не восстановлены после теста: {D.ARCHIVE_DB} / {D.WIKI_DB}")

print(f"\n{'=' * 62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}   SKIPPED: {len(SKIP)}")
if SKIP:
    print("ПРОПУЩЕНО (это НЕ успех): " + ", ".join(SKIP))
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
