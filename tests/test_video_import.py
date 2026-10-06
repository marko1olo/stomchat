"""
Один вход для импорта видео-протоколов. Что ловит каждый блок и чем это платил врач.

[1] ДВА СКРИПТА ИЗ ОДНОГО ВХОДА. videosi.py и import_videos.py читали один и тот
    же videos.txt и писали в одну боевую вику, но по-разному, и запускался тот,
    что попался под руку. import_videos.py вставлял строку БЕЗ колонки
    is_reclassified (в схеме DEFAULT 0) и с маркером, обёрнутым в <b>. Первое
    делало протокол невидимым для запроса выборочной проверки, второе — для
    защиты от повтора, поэтому следующий прогон дублировал протоколы. Врач видел
    один разбор дважды, а контроль качества не видел его ни разу.

[2] МОЛЧАЛИВЫЙ НОЛЬ. Без videos.txt скрипт печатал «[СТОП]» и заканчивался с
    кодом 0 (замер до правки: returncode 0). Для обёртки и cron это успех: импорт
    «прошёл», не вставив ни одного протокола, и никто не искал причину, пока врач
    ждал новых видео-разборов в рубрикаторе.

[3] УДВОЕНИЕ. UNIQUE-индексов в distilled_facts нет ни одного (замер: PRAGMA
    index_list -> только idx_cat, unique=0), поэтому INSERT OR IGNORE сам по себе
    не отсекает ничего — дубли держит только проверка на стороне скрипта.

[4] ЕДИНАЯ РАЗМЕТКА. Проверяется не «поле заполнено», а что вставленную строку
    достают ТЕ ЖЕ запросы, которыми бот достаёт остальные 12 784 факта:
    листалка энциклопедии (assistant.py:4519, category_code LIKE + непустой
    content + GROUP BY content), счётчик раздела (assistant.py:4454) и выборочная
    проверка (checker.py:71, WHERE is_reclassified = 1). Запрос, который находит
    всё, ничего не доказывает, поэтому рядом лежит строка, вставленная по-старому:
    её выборочная проверка обязана НЕ найти.

[5] ЛОЖНЫЙ ПРОВЕНАНС. Номер из текстового файла — не msg_id архива. Диапазон
    msg_id 5..139 701, номера в videos.txt четырёхзначные, поэтому совпадения
    неизбежны: 35 из 53 нашлись, но медиа есть у 6, а тип video ровно у 1. Врач
    открывал «источник» видео-разбора и читал чужую реплику про контактные
    пункты, после чего справедливо решал, что бот врёт. Пустая ссылка честнее.

[6] НЕВИДИМКА С confidence 100. Когда классификатор не дал код, факт уезжал с
    кодом-заглушкой 10.1. Ни одна подтема рубрикатора этот код не несёт (замер:
    53 кода в WIKI_TREE, 10.1 среди них нет и ни один не является его
    подстрокой), значит врач такой протокол не увидит ни в одном разделе. Хуже
    того, вылечить это было нельзя: защита от повтора опознаёт протокол по номеру
    и следующий прогон честно его пропускал — невидимка оставалась навсегда.

Схема временных баз берётся ИЗ БОЕВОЙ через sqlite_master (mode=ro), а не
переписывается руками: иначе тест проверяет разметку по схеме, которой в бою нет.
Боевые stomat_wiki.db / stomat_archive.db открываются только через
file:...?mode=ro, блок [7] сверяет их md5 до и после прогона.

Запуск: python test_video_import.py
"""
import asyncio
import hashlib
import importlib
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import videosi as V  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  [SKIP] {name} -- {why}")


LIVE_WIKI = os.path.join(ROOT, "stomat_wiki.db")
LIVE_ARCHIVE = os.path.join(ROOT, "stomat_archive.db")

# Потолки. Вложенность: подпроцесс поднимает интерпретатор и импортирует
# google.genai — измерено 2.4 с на этой машине, потолок 120 с даёт запас x50.
# Прогон в процессе идёт с SLEEP_BETWEEN_VIDEOS = 0, то есть миллисекунды при
# потолке 60 с; 60 < 120, поэтому внутренний потолок вложен во внешний.
SUBPROCESS_TIMEOUT = 120
RUN_TIMEOUT = 60


def ro(path):
    """Боевая база строго на чтение — единственный разрешённый способ её открыть."""
    uri = "file:" + os.path.abspath(path).replace(os.sep, "/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def md5(path):
    h = hashlib.md5()
    with io.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_script(path, cwd, io_encoding="utf-8"):
    """Запустить скрипт подпроцессом и прочитать его вывод ТОЙ ЖЕ кодировкой.

    PYTHONIOENCODING задаётся явно, потому что первая версия этого теста читала
    вывод как utf-8, а ребёнок писал в cp1251: кириллица приезжала мусором, и три
    проверки падали на своей же трубе, а не на коде. Отдельно нужен прогон с
    cp1251 — это кодировка консоли боевой машины, и именно на ней снятый
    import_videos.py падал UnicodeEncodeError на первом же print, то есть отказ,
    написанный неаккуратно, оператор увидит как «скрипт сломан».
    """
    env = dict(os.environ, PYTHONIOENCODING=io_encoding)
    proc = subprocess.run([sys.executable, path], cwd=cwd, capture_output=True, text=True,
                          encoding=io_encoding, errors="replace", env=env,
                          timeout=SUBPROCESS_TIMEOUT)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def live_wiki_ddl():
    """DDL таблицы фактов, снятый С БОЕВОЙ базы. Руками не переписан намеренно."""
    con = ro(LIVE_WIKI)
    try:
        return [r[0] for r in con.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = 'distilled_facts' "
            "AND sql IS NOT NULL ORDER BY type DESC")]
    finally:
        con.close()


ARCHIVE_SCHEMA = """CREATE TABLE archive_messages (
    msg_id INTEGER PRIMARY KEY, date TIMESTAMP, sender_id INTEGER, sender_name TEXT,
    sender_username TEXT, text TEXT, reply_to_msg_id INTEGER, has_media BOOLEAN,
    media_type TEXT, media_remote_url TEXT, vision_description TEXT,
    vision_processed BOOLEAN, category_l1 TEXT, category_l2 TEXT, category_l3 TEXT,
    is_processed_for_wiki BOOLEAN)"""


def make_wiki(path, ddl):
    con = sqlite3.connect(path)
    for statement in ddl:
        con.execute(statement)
    con.commit()
    con.close()


def make_archive(path, rows):
    """rows: список (msg_id, text, has_media, media_type)."""
    con = sqlite3.connect(path)
    con.execute(ARCHIVE_SCHEMA)
    con.executemany(
        "INSERT INTO archive_messages (msg_id, text, has_media, media_type) VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()


class patched:
    """Подменить атрибуты модуля на время сценария и вернуть как было."""

    def __init__(self, module, **values):
        self.module, self.values, self.saved = module, values, {}

    def __enter__(self):
        for name, value in self.values.items():
            self.saved[name] = getattr(self.module, name)
            setattr(self.module, name, value)
        return self.module

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(self.module, name, value)
        return False


def run_main(timeout=RUN_TIMEOUT):
    """Прогон videosi.main() с потолком. Возвращает (завис, вывод, код выхода).

    SystemExit перехватывается намеренно: отказ внутри прогона обязан валить
    ПРОВЕРКУ, а не весь набор молча с кодом 1 и без единой строки вывода.
    """
    buf = io.StringIO()

    async def wrapper():
        return await asyncio.wait_for(V.main(), timeout=timeout)

    timed_out, exited = False, None
    try:
        with redirect_stdout(buf):
            asyncio.run(wrapper())
    except asyncio.TimeoutError:
        timed_out = True
    except SystemExit as exc:
        exited = exc.code
    return timed_out, buf.getvalue(), exited


async def stub_codes(body):
    """Классификатор-заглушка: список кодов через запятую, как в боевой вике."""
    return "1.2.1, 2.1.1"


async def stub_dead(body):
    """Все ключи в лимите: классификатор отдаёт код-заглушку."""
    return V.FALLBACK_CODE


# --- запросы, которыми бот достаёт факты. Скопированы дословно ------------------
def encyclopedia_page(db, code):
    """assistant.query_wiki_fact_page: страница подтемы по коду."""
    con = sqlite3.connect(db)
    try:
        base = ("FROM distilled_facts WHERE (category_code LIKE ?) "
                "AND content IS NOT NULL AND TRIM(content) <> '' GROUP BY content")
        total = con.execute(f"SELECT COUNT(*) FROM (SELECT 1 {base})", (f"%{code}%",)).fetchone()[0]
        rows = con.execute(f"SELECT content, MIN(id) AS ord {base} ORDER BY ord",
                           (f"%{code}%",)).fetchall()
        return total, [r[0] for r in rows]
    finally:
        con.close()


def section_counter(db, code):
    """assistant._wiki_counts: число в кнопке раздела (COUNT(*) без GROUP BY)."""
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM distilled_facts WHERE (category_code LIKE ?) "
            "AND content IS NOT NULL AND TRIM(content) <> ''", (f"%{code}%",)).fetchone()[0]
    finally:
        con.close()


def quality_audit(db):
    """checker.py:71 — выборочная проверка классификации. RANDOM убран для повторяемости."""
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT id, content, category_code FROM distilled_facts "
            "WHERE is_reclassified = 1").fetchall()
    finally:
        con.close()


LIVE_MD5_BEFORE = {p: md5(p) for p in (LIVE_WIKI, LIVE_ARCHIVE) if os.path.exists(p)}

print("=" * 78)
print("ОДИН ВХОД ДЛЯ ИМПОРТА ВИДЕО-ПРОТОКОЛОВ: videosi.py / import_videos.py")
print("=" * 78)

# ---------------------------------------------------------------- [1]
print("\n[1] Второй вход закрыт: import_videos.py не запустить и не импортировать")
RETIRED = os.path.join(ROOT, "import_videos.py")
if not os.path.exists(RETIRED):
    check("import_videos.py удалён — второго входа нет", True)
else:
    tmp = tempfile.mkdtemp(prefix="stomchat_retired_")
    try:
        # cwd временный: боевой базы там нет, дотянуться до неё скрипт не может
        # даже если отказ не сработает.
        code, out = run_script(RETIRED, tmp)
        check("прямой запуск import_videos.py заканчивается отказом, а не импортом",
              code != 0, f"returncode {code}")
        check("отказ называет единственный правильный вход", "videosi.py" in out, out[-300:])

        # То же самое на кодировке боевой консоли: отказ обязан читаться, а не
        # падать. Именно так снятый скрипт и умирал — UnicodeEncodeError на эмодзи.
        code_cp, out_cp = run_script(RETIRED, tmp, io_encoding="cp1251")
        check("на консоли cp1251 отказ по-прежнему отказ", code_cp != 0, f"returncode {code_cp}")
        check("на cp1251 отказ не падает UnicodeEncodeError (в нём нет эмодзи)",
              "UnicodeEncodeError" not in out_cp, out_cp[-300:])
        check("на cp1251 текст отказа читается", "videosi.py" in out_cp, out_cp[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Импорт — второй способ «случайно запустить»: на уровне модуля этот файл
    # раньше исполнял всю работу целиком.
    sys.modules.pop("import_videos", None)
    imported, refused = None, None
    try:
        imported = importlib.import_module("import_videos")
    except SystemExit as exc:
        refused = exc
    except BaseException as exc:  # noqa: BLE001 — важен сам факт отказа
        refused = exc
    sys.modules.pop("import_videos", None)
    check("import import_videos отказывает, а не выполняет импорт видео",
          refused is not None and imported is None, f"модуль импортировался: {imported!r}")

    source = io.open(RETIRED, encoding="utf-8").read()
    # Не «в исходнике есть строка», а поведенческое следствие: писать в
    # distilled_facts этому файлу больше нечем.
    check("в снятом с вооружения файле не осталось INSERT в distilled_facts",
          "INSERT" not in source.upper() or "distilled_facts" not in source,
          "код вставки остался: его можно выполнить построчно")

# ---------------------------------------------------------------- [2]
print("\n[2] Нет входного файла или базы -> ГРОМКИЙ отказ, а не молчаливый ноль")
tmp = tempfile.mkdtemp(prefix="stomchat_videos_loud_")
try:
    script = os.path.join(ROOT, "videosi.py")
    code, out = run_script(script, tmp)
    check("НЕТ videos.txt -> процесс заканчивается НЕ нулём (для cron это отказ)",
          code != 0, f"returncode {code} — обёртка считает это успехом")
    check("в выводе названо, какого файла не хватает", "videos.txt" in out, out[-300:])
    check("отказ помечен как СТОП", "[СТОП]" in out, out[-300:])
    # Тот же отказ на кодировке боевой консоли: он не должен превращаться в
    # UnicodeEncodeError, иначе оператор прочитает «скрипт сломан» вместо причины.
    code_cp, out_cp = run_script(script, tmp, io_encoding="cp1251")
    check("на консоли cp1251 отказ читается и остаётся отказом",
          code_cp != 0 and "videos.txt" in out_cp and "UnicodeEncodeError" not in out_cp,
          f"returncode {code_cp}: {out_cp[-300:]}")

    # Входной файл есть, базы нет: отказ обязан прийти ДО обращения к модели,
    # иначе прогон платит за классификацию и всё равно ничего не запишет.
    io.open(os.path.join(tmp, "videos.txt"), "w", encoding="utf-8").write(
        "1548\nразбор про адгезию\n")
    code2, out2 = run_script(script, tmp)
    check("НЕТ базы -> процесс заканчивается НЕ нулём", code2 != 0, f"returncode {code2}")
    check("сказано, что не найдена именно база", "База не найдена" in out2, out2[-300:])
    check("до классификации дело не дошло (ни одного протокола не обработано)",
          "Обработка Видео" not in out2, out2[-300:])
    check("база рядом со скриптом не создана впустую",
          not os.path.exists(os.path.join(tmp, "stomat_wiki.db")),
          "пустая база в рабочем каталоге сойдёт за боевую")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [3]
print("\n[3] Повторный прогон не удваивает протоколы (схема снята с боевой базы)")
DDL = live_wiki_ddl()
check("DDL боевой таблицы фактов прочитан", any("distilled_facts" in d for d in DDL), f"{DDL}")
check("в боевой схеме есть колонка is_reclassified — разметка вообще применима",
      any("is_reclassified" in d for d in DDL), f"{DDL}")

tmp = tempfile.mkdtemp(prefix="stomchat_videos_dup_")
try:
    db = os.path.join(tmp, "wiki.db")
    arch = os.path.join(tmp, "archive.db")
    inp = os.path.join(tmp, "videos.txt")
    make_wiki(db, DDL)
    make_archive(arch, [(1548, "чужая реплика про контактные пункты", 0, None),
                        (1861, "тут действительно видео", 1, "video"),
                        (1999, "реплика с фото", 1, "photo")])
    io.open(inp, "w", encoding="utf-8").write(
        "1548\nразбор первый: адгезия\n1861\nразбор второй: препарирование\n"
        "1999\nразбор третий: окклюзия\n")

    with patched(V, DB_PATH=db, ARCHIVE_PATH=arch, INPUT_FILE=inp,
                 classify_video=stub_codes, SLEEP_BETWEEN_VIDEOS=0):
        timed_out, first_out, exited = run_main()
        check("первый прогон не завис", not timed_out)
        check("первый прогон не отказал", exited is None, f"SystemExit {exited}")
        con = sqlite3.connect(db)
        first_count = con.execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
        con.close()
        check("первый прогон вставил все три протокола", first_count == 3, f"got {first_count}")

        timed_out2, second_out, exited2 = run_main()
        check("второй прогон не завис", not timed_out2)
        con = sqlite3.connect(db)
        second_count = con.execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
        dupes = con.execute("SELECT content, COUNT(*) FROM distilled_facts GROUP BY content "
                            "HAVING COUNT(*) > 1").fetchall()
        con.close()
    check("ПОВТОРНЫЙ ПРОГОН НЕ УВЕЛИЧИЛ ЧИСЛО СТРОК", second_count == first_count,
          f"было {first_count}, стало {second_count}")
    check("ни один протокол не лежит в базе дважды", dupes == [], f"got {[d[1] for d in dupes]}")
    check("второй прогон отчитался, что добавил ноль", "Добавлено 0" in second_out,
          second_out[-300:])

    # Прямой вызов save_to_db_safe: у main() есть свой предварительный probe, и он
    # маскирует внутреннюю защиту. Внутренняя — последняя линия при двух
    # одновременных прогонах, когда probe уже сказал «такого нет».
    db2 = os.path.join(tmp, "wiki2.db")
    make_wiki(db2, DDL)
    payload = ("2.1.1", f"{V.VIDEO_MARKER}4242]\n\nразбор про элайнеры", "", 1, 100,
               "2026-07-29 12:00:00")
    with patched(V, DB_PATH=db2):
        first_insert = asyncio.run(V.save_to_db_safe(payload, "4242"))
        second_insert = asyncio.run(V.save_to_db_safe(payload, "4242"))
    con = sqlite3.connect(db2)
    total2 = con.execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
    con.close()
    check("save_to_db_safe вставила протокол в первый раз", first_insert is True,
          f"got {first_insert!r}")
    check("save_to_db_safe ОТКАЗАЛА во второй раз", second_insert is False,
          f"got {second_insert!r}")
    check("после двух вызовов в базе одна строка", total2 == 1, f"got {total2}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [4]
print("\n[4] Единая разметка: строку достают ТЕ ЖЕ запросы, что и остальные факты")
tmp = tempfile.mkdtemp(prefix="stomchat_videos_mark_")
try:
    db = os.path.join(tmp, "wiki.db")
    arch = os.path.join(tmp, "archive.db")
    inp = os.path.join(tmp, "videos.txt")
    make_wiki(db, DDL)
    make_archive(arch, [(1861, "тут действительно видео", 1, "video")])
    io.open(inp, "w", encoding="utf-8").write("1861\nразбор про адгезивный протокол\n")

    # Обычный факт вики: с ним новый протокол обязан оказаться в одной выдаче.
    con = sqlite3.connect(db)
    con.execute("INSERT INTO distilled_facts (category_code, content, source_ids, is_case,"
                " confidence, processed_at, is_reclassified) VALUES"
                " ('1.2.1', 'обычный факт про адгезию', '', 0, 10, '2026-07-01 00:00:00', 1)")
    # Строка, вставленная СНЯТЫМ скриптом: без is_reclassified и с <b> в маркере.
    # Она здесь как контроль: запрос, который находит всё, ничего не доказывает.
    con.execute("INSERT INTO distilled_facts (category_code, content, source_ids, is_case,"
                " confidence, processed_at) VALUES ('1.2.1', ?, '4244', 1, 100,"
                " '2026-07-01 00:00:00')",
                ("\U0001f3a5 <b>[ВИДЕО-ПРОТОКОЛ | MSG 4244]</b>\n\nразбор по-старому",))
    con.commit()
    con.close()

    with patched(V, DB_PATH=db, ARCHIVE_PATH=arch, INPUT_FILE=inp,
                 classify_video=stub_codes, SLEEP_BETWEEN_VIDEOS=0):
        timed_out, out, exited = run_main()
    check("прогон не завис", not timed_out)

    total, pages = encyclopedia_page(db, "1.2.1")
    fresh = [p for p in pages if "MSG 1861" in p]
    check("новый протокол достаётся ЛИСТАЛКОЙ энциклопедии по своему коду",
          len(fresh) == 1, f"страниц {total}, из них с MSG 1861: {len(fresh)}")
    check("он лежит в одной выдаче с обычным фактом того же кода",
          any("обычный факт про адгезию" in p for p in pages), f"страницы: {len(pages)}")
    check("СЧЁТЧИК раздела его тоже считает", section_counter(db, "1.2.1") >= 2,
          f"счётчик {section_counter(db, '1.2.1')}")
    check("код-список через запятую не мешает: LIKE находит по любому из кодов",
          encyclopedia_page(db, "2.1.1")[0] >= 1, "точное сравнение со списком не совпало бы никогда")

    audit = quality_audit(db)
    audit_text = "\n".join(str(r[1]) for r in audit)
    check("ВЫБОРОЧНАЯ ПРОВЕРКА (is_reclassified = 1) видит новый протокол",
          "MSG 1861" in audit_text, f"строк в выборке {len(audit)}")
    check("а строку, вставленную снятым скриптом, НЕ видит — запрос различающий",
          "MSG 4244" not in audit_text,
          "запрос находит всё подряд, значит блок [4] ничего не доказывает")

    con = sqlite3.connect(db)
    flag = con.execute("SELECT is_reclassified FROM distilled_facts "
                       "WHERE content LIKE '%MSG 1861%'").fetchone()[0]
    marker_ok = con.execute("SELECT COUNT(*) FROM distilled_facts WHERE substr(content, 1, ?) = ?",
                            (len(V.VIDEO_MARKER), V.VIDEO_MARKER)).fetchone()[0]
    con.close()
    check("разметка ровно одна: is_reclassified = 1 у вставленного протокола", flag == 1,
          f"got {flag!r}")
    check("маркер один и тот же, без <b>", marker_ok == 1, f"строк с маркером videosi: {marker_ok}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [5]
print("\n[5] Провенанс: пусто вместо выдуманной ссылки")
tmp = tempfile.mkdtemp(prefix="stomchat_videos_prov_")
try:
    arch = os.path.join(tmp, "archive.db")
    make_archive(arch, [(1548, "чужая текстовая реплика", 0, None),
                        (1999, "реплика с фото", 1, "photo"),
                        (1861, "реплика с видео", 1, "video"),
                        (2000, "медиа есть, тип не указан", 1, None),
                        (2001, "медиа есть, тип file", 1, "file")])
    check("номер есть, но это текстовая реплика -> пусто", V.verify_provenance("1548", arch) == "",
          f"got {V.verify_provenance('1548', arch)!r}")
    check("номер есть, но медиа = фото -> пусто", V.verify_provenance("1999", arch) == "",
          f"got {V.verify_provenance('1999', arch)!r}")
    check("медиа есть, тип не указан -> пусто", V.verify_provenance("2000", arch) == "")
    check("тип file (документ) видео не считается -> пусто",
          V.verify_provenance("2001", arch) == "")
    check("номера в архиве нет -> пусто", V.verify_provenance("777777", arch) == "")
    check("нечисловой номер -> пусто", V.verify_provenance("MSG_15", arch) == "")
    check("архива рядом нет -> пусто, а не падение",
          V.verify_provenance("1861", os.path.join(tmp, "нет.db")) == "")
    check("подтверждённое видео -> номер возвращается", V.verify_provenance("1861", arch) == "1861",
          f"got {V.verify_provenance('1861', arch)!r}")

    # Сквозной прогон: в базу уезжает пустой провенанс, а не номер из файла.
    db = os.path.join(tmp, "wiki.db")
    inp = os.path.join(tmp, "videos.txt")
    make_wiki(db, DDL)
    io.open(inp, "w", encoding="utf-8").write(
        "1548\nразбор с ложным номером\n1861\nразбор с настоящим видео\n")
    with patched(V, DB_PATH=db, ARCHIVE_PATH=arch, INPUT_FILE=inp,
                 classify_video=stub_codes, SLEEP_BETWEEN_VIDEOS=0):
        timed_out, out, exited = run_main()
    check("сквозной прогон не завис", not timed_out)
    con = sqlite3.connect(db)
    stored = dict(con.execute(
        "SELECT substr(content, instr(content, 'MSG ') + 4, 4), source_ids "
        "FROM distilled_facts").fetchall())
    con.close()
    check("у НЕПРОВЕРЯЕМОГО протокола source_ids ПУСТОЙ, а не номер из файла",
          stored.get("1548") == "", f"got {stored!r}")
    check("у проверяемого протокола source_ids сохранён", stored.get("1861") == "1861",
          f"got {stored!r}")
    check("в выводе посчитаны протоколы без подтверждённого источника",
          "Без подтверждённого источника: 1" in out, out[-400:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# Живые данные: сколько из 53 боевых видео-фактов прошли бы строгую проверку.
if os.path.exists(LIVE_ARCHIVE) and os.path.exists(LIVE_WIKI):
    con = ro(LIVE_WIKI)
    live_vids = con.execute("SELECT id, source_ids FROM distilled_facts "
                            "WHERE content LIKE '%ВИДЕО-ПРОТОКОЛ%' ORDER BY id").fetchall()
    con.close()
    verified = [(fid, s) for fid, s in live_vids if V.verify_provenance((s or "").strip(), LIVE_ARCHIVE)]
    check("боевых видео-фактов ровно 53", len(live_vids) == 53, f"got {len(live_vids)}")
    check("строгую проверку проходит РОВНО 1 из 53 (остальные 52 — коллизия нумерации)",
          len(verified) == 1, f"прошли: {verified}")
    check("единственный проверенный — msg_id 1861",
          [s for _, s in verified] == ["1861"], f"got {verified}")
else:
    skip("строгая проверка на боевых 53 фактах", "боевых баз нет рядом")

# ---------------------------------------------------------------- [6]
print("\n[6] Классификатор не дал код -> протокол НЕ записан, а не спрятан невидимкой")
tmp = tempfile.mkdtemp(prefix="stomchat_videos_fallback_")
try:
    db = os.path.join(tmp, "wiki.db")
    arch = os.path.join(tmp, "archive.db")
    inp = os.path.join(tmp, "videos.txt")
    make_wiki(db, DDL)
    make_archive(arch, [(1861, "тут действительно видео", 1, "video")])
    io.open(inp, "w", encoding="utf-8").write("1861\nразбор, который не удалось разметить\n")

    with patched(V, DB_PATH=db, ARCHIVE_PATH=arch, INPUT_FILE=inp,
                 classify_video=stub_dead, SLEEP_BETWEEN_VIDEOS=0):
        timed_out, out, exited = run_main()
    check("прогон не завис", not timed_out)
    con = sqlite3.connect(db)
    rows = con.execute("SELECT category_code, content FROM distilled_facts").fetchall()
    con.close()
    check("НЕРАЗМЕЧЕННЫЙ протокол в базу не попал", rows == [], f"got {rows}")
    check("в выводе сказано, что он НЕ записан", "НЕ ЗАПИСАН" in out, out[-400:])
    check("итог прогона громко называет число неразмеченных", "[ВНИМАНИЕ]" in out
          and "НЕ записано: 1" in out, out[-400:])
    check("итог не соврал про добавленные", "Добавлено 0" in out, out[-400:])

    # И главное: он не заблокировал сам себя. Повторный прогон с живым
    # классификатором обязан взять этот протокол и разметить нормально.
    with patched(V, DB_PATH=db, ARCHIVE_PATH=arch, INPUT_FILE=inp,
                 classify_video=stub_codes, SLEEP_BETWEEN_VIDEOS=0):
        timed_out2, out2, exited2 = run_main()
    check("повторный прогон не завис", not timed_out2)
    total, pages = encyclopedia_page(db, "1.2.1")
    check("после повторного прогона протокол в базе и достаётся запросом энциклопедии",
          total == 1 and any("MSG 1861" in p for p in pages), f"страниц {total}")
    check("код-заглушка в базу так и не попала",
          section_counter(db, V.FALLBACK_CODE) == 0,
          f"фактов с кодом {V.FALLBACK_CODE}: {section_counter(db, V.FALLBACK_CODE)}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [7]
print("\n[7] Боевые базы не изменились")
for path, before in LIVE_MD5_BEFORE.items():
    check(f"{os.path.basename(path)} побайтово тот же", md5(path) == before,
          f"было {before}, стало {md5(path)}")
if not LIVE_MD5_BEFORE:
    skip("сверка md5 боевых баз", "боевых баз нет рядом")

print(f"\n{'=' * 62}")
if SKIP:
    print("ПРОПУЩЕНО: " + "; ".join(SKIP))
print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
