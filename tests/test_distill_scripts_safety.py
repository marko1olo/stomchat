"""
Два скрипта лана дистилляции пишут в боевую вики, поэтому проверяется их безопасность.

reclass.py — единственная БЕЗВОЗВРАТНАЯ операция всего проекта: перезапись
category_code у 12 784 фактов. Что было сломано и чем это платил врач:

1. Прежний код категории не сохранялся НИКУДА. Ошибка классификатора означала,
   что факт навсегда уехал под чужую категорию: врач ищет «ВНЧС», факт лежит в
   «Экономике клиники», и вернуть прежнюю разметку нечем — ни строки, ни базы.
   Теперь прежний код уходит в category_code_prev тем же UPDATE-ом.
2. Копии базы перед прогоном не было вообще. Теперь VACUUM INTO снимается ДО
   первого UPDATE, и если копия не снялась, не читается или в ней не то число
   фактов — прогон обязан остановиться, НЕ ТРОНУВ ни одной строки. Бэкап,
   который «может не получиться и ладно», бэкапом не является, поэтому здесь
   проверяется именно отказ работать без копии.
3. Прогон «на всякий случай» оставлял копию на 9 158 656 байт даже когда
   переклассифицировать нечего (на боевом снимке is_reclassified = 0 ровно у
   НУЛЯ фактов). Каталог заполнялся пустыми копиями, и нужную среди них не найти.

videosi.py — импорт 53 видео-протоколов:

4. INSERT без защиты от повтора: второй прогон того же videos.txt удваивал все 53
   протокола, и врач видел один разбор дважды, после чего перестаёт верить
   счётчику находок. Защита сравнивает НАЧАЛО строки, а не LIKE '%MSG 148%':
   подстрока нашла бы «MSG 1489» внутри «MSG 148» и молча не импортировала бы
   часть протоколов.
5. Провенанс был ЛОЖНЫЙ. Номера в videos.txt — нумерация текстового файла, а не
   msg_id архива. Замер на боевых базах (повторён этим ланом независимо): 35 из
   53 номеров случайно совпали с существующими msg_id, и это чужие реплики —
   медиа есть лишь у 6, тип video ровно у 1. Врач шёл по «источнику» и читал
   разговор про контактные пункты вместо видео-разбора, то есть считал, что бот
   врёт. Теперь ссылка либо проверяема, либо пуста: пустая ничего не обещает.

Боевые stomat_wiki.db и stomat_archive.db открываются ТОЛЬКО через
file:...?mode=ro. Проверка [12] сверяет их md5 до и после прогона.

Запуск: python test_distill_scripts_safety.py
"""
import asyncio
import glob
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
from contextlib import redirect_stdout

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import reclass as R  # noqa: E402
import videosi as V  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  [SKIP] {name} -- {why}")


# Схема боевой вики, снятая PRAGMA с stomat_wiki.db (12 784 факта).
# category_code_prev в ней НЕТ — его добавляет миграция reclass.ensure_schema.
WIKI_SCHEMA = """CREATE TABLE distilled_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_code TEXT,
    content TEXT,
    source_ids TEXT,
    media_links TEXT,
    is_case BOOLEAN,
    confidence INTEGER,
    processed_at TIMESTAMP,
    is_reclassified BOOLEAN DEFAULT 0)"""

# Схема ДО появления is_reclassified: на такой базе миграция обязана добавить обе колонки.
WIKI_SCHEMA_LEGACY = """CREATE TABLE distilled_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_code TEXT,
    content TEXT,
    source_ids TEXT,
    media_links TEXT,
    is_case BOOLEAN,
    confidence INTEGER,
    processed_at TIMESTAMP)"""

ARCHIVE_SCHEMA = """CREATE TABLE archive_messages (
    msg_id INTEGER PRIMARY KEY, date TIMESTAMP, sender_id INTEGER, sender_name TEXT,
    sender_username TEXT, text TEXT, reply_to_msg_id INTEGER, has_media BOOLEAN,
    media_type TEXT, media_remote_url TEXT, vision_description TEXT,
    vision_processed BOOLEAN, category_l1 TEXT, category_l2 TEXT, category_l3 TEXT,
    is_processed_for_wiki BOOLEAN)"""

LIVE_WIKI = "stomat_wiki.db"
LIVE_ARCHIVE = "stomat_archive.db"


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


def make_wiki(path, facts, legacy=False):
    """facts: список (category_code, content, is_reclassified)."""
    con = sqlite3.connect(path)
    con.execute(WIKI_SCHEMA_LEGACY if legacy else WIKI_SCHEMA)
    con.execute("CREATE INDEX idx_cat ON distilled_facts(category_code)")
    for code, content, done in facts:
        if legacy:
            con.execute("INSERT INTO distilled_facts (category_code, content) VALUES (?,?)",
                        (code, content))
        else:
            con.execute("INSERT INTO distilled_facts (category_code, content, is_reclassified)"
                        " VALUES (?,?,?)", (code, content, done))
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


def rows_of(path, columns="id, category_code, category_code_prev, is_reclassified"):
    con = sqlite3.connect(path)
    try:
        return con.execute(f"SELECT {columns} FROM distilled_facts ORDER BY id").fetchall()
    finally:
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


FAKE_GENAI = types.SimpleNamespace(Client=lambda **kw: types.SimpleNamespace())
FAKE_CONFIG = types.SimpleNamespace(GOOGLE_KEYS=["ключ-1", "ключ-2"])


def reclass_patch(db_path, classifier):
    """Прогон reclass без сети и без сна: подменяются только точки входа наружу."""
    return patched(R, DB_PATH=db_path, classify_fact=classifier, genai=FAKE_GENAI,
                   config=FAKE_CONFIG, SLEEP_BETWEEN_FACTS=0, SLEEP_ON_ROTATE=0)


def run_reclass():
    """Вернуть (код выхода или None, напечатанное). Без wait_for: SystemExit важнее."""
    buf = io.StringIO()
    code = None
    try:
        with redirect_stdout(buf):
            asyncio.run(R.main())
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
    return code, buf.getvalue()


def run_bounded(coro, timeout=60):
    """Прогон с потолком: зависший цикл обязан валить тест, а не висеть вечно.

    SystemExit перехватывается намеренно. Без этого отказ внутри прогона убивал
    ВЕСЬ набор молча, с кодом 1 и без единой строки: ровно так первая версия
    этого файла «прошла» половину проверок и оборвалась на [6] без следа.
    """
    buf = io.StringIO()

    async def wrapper():
        return await asyncio.wait_for(coro, timeout=timeout)

    timed_out, exited = False, None
    try:
        with redirect_stdout(buf):
            asyncio.run(wrapper())
    except asyncio.TimeoutError:
        timed_out = True
    except SystemExit as exc:
        exited = exc.code if exc.code is not None else 0
    text = buf.getvalue()
    if exited is not None:
        text += f"\n[прогон оборвался SystemExit({exited})]"
    return timed_out, text


async def codes_by_index(client, content, f_id):
    """Классификатор-заглушка: код зависит от факта, чтобы подмену было видно."""
    return f"9.{f_id}"


async def always_none(client, content, f_id):
    """Классификатор всегда спотыкается — разметка обязана остаться прежней."""
    return None


async def always_retry(client, content, f_id):
    """Все ключи в лимите — прогон обязан остановиться, а не крутиться вечно."""
    return "RETRY"


async def video_code(body):
    return "2.1.1"


LIVE_MD5_BEFORE = {p: md5(p) for p in (LIVE_WIKI, LIVE_ARCHIVE) if os.path.exists(p)}

FACTS = [("1.1.1", "факт про ирригацию", 0),
         ("2.2.1", "факт про BOPT", 0),
         ("3.2.4", "факт про периимплантит", 0),
         ("7.1.1", "факт уже переклассифицирован", 1)]

print("=" * 78)
print("БЕЗОПАСНОСТЬ СКРИПТОВ ДИСТИЛЛЯЦИИ: reclass.py, videosi.py")
print("=" * 78)

# ---------------------------------------------------------------- [1]
print("\n[1] Миграция схемы идемпотентна и не глотает ошибки")
tmp = tempfile.mkdtemp(prefix="stomchat_reclass_mig_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db, [("1.1.1", "старый факт", 0)], legacy=True)
    added_first = asyncio.run(R.ensure_schema(db))
    check("на старой схеме миграция добавляет обе колонки",
          set(added_first) == {"is_reclassified", "category_code_prev"}, f"got {added_first}")
    added_second = asyncio.run(R.ensure_schema(db))
    check("повторная миграция не добавляет ничего (идемпотентна)", added_second == [],
          f"got {added_second}")
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(distilled_facts)")]
    check("category_code_prev существует после миграции", "category_code_prev" in cols, f"{cols}")
    check("данные миграцию пережили",
          rows_of(db, "category_code, content") == [("1.1.1", "старый факт")])

    empty = os.path.join(tmp, "empty.db")
    sqlite3.connect(empty).execute("CREATE TABLE other (x INTEGER)")
    raised = None
    try:
        asyncio.run(R.ensure_schema(empty))
    except Exception as exc:
        raised = exc
    check("база без distilled_facts даёт громкую ошибку, а не молчаливый pass",
          isinstance(raised, RuntimeError), f"got {raised!r}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [2]
print("\n[2] Копия базы проверяется как копия, а не как факт вызова")
tmp = tempfile.mkdtemp(prefix="stomchat_reclass_bak_")
try:
    from datetime import datetime
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db, FACTS)
    # Момент задаётся явно: иначе имя копии зависело бы от секунды прогона и
    # могло совпасть с фиксированным моментом ниже.
    target = asyncio.run(R.backup_before_write(db, datetime(2026, 7, 29, 11, 0, 0)))
    check("копия создана", os.path.exists(target), target)
    check("копия не пустая", os.path.getsize(target) > 0)
    con = ro(target)
    check("integrity_check копии = ok", con.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
    check("в копии столько же фактов, сколько в источнике",
          con.execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0] == len(FACTS))
    check("копия содержит ИМЕННО прежние коды",
          [r[0] for r in con.execute("SELECT category_code FROM distilled_facts ORDER BY id")]
          == [f[0] for f in FACTS])
    con.close()
    check("имя копии начинается с wiki_backup_", os.path.basename(target).startswith("wiki_backup_"))

    # Один и тот же момент времени -> то же имя: второй раз обязан отказать, а не
    # перезаписать копию, ради которой всё и делается.
    moment = datetime(2026, 7, 29, 12, 0, 0)
    first = asyncio.run(R.backup_before_write(db, moment))
    raised = None
    try:
        asyncio.run(R.backup_before_write(db, moment))
    except Exception as exc:
        raised = exc
    check("копия поверх существующей не пишется", isinstance(raised, RuntimeError), f"{raised!r}")
    check("секунды в имени копии есть (два прогона за сутки не столкнутся)",
          os.path.basename(first).count("_") >= 2, os.path.basename(first))

    # Каждая ветка отказа — на подложенном файле. Годную копию от негодной
    # отличает только эта функция; если она пропускает брак, «бэкап» есть только
    # на бумаге, и узнается это в день отката.
    def refused(path, expected_facts=len(FACTS)):
        try:
            R.verify_copy(path, expected_facts)
            return None
        except Exception as exc:
            return exc

    planted = os.path.join(tmp, "planted.db")
    check("копии нет вовсе -> отказ",
          isinstance(refused(os.path.join(tmp, "нет-такого.db")), RuntimeError))

    io.open(planted, "wb").close()
    check("копия 0 байт -> отказ", isinstance(refused(planted), RuntimeError),
          f"got {refused(planted)!r}")

    io.open(planted, "wb").write(b"not a database at all" * 200)
    check("копия не открывается SQLite -> отказ", isinstance(refused(planted), RuntimeError),
          f"got {refused(planted)!r}")

    # Обрезанная копия ОТКРЫВАЕТСЯ и отдаёт верный COUNT(*): ловит только integrity_check.
    os.remove(planted)
    make_wiki(planted, FACTS + [("4.1.1", "ещё факт " + "текст " * 40, 0)] * 60)
    truncated_size = os.path.getsize(planted)
    with io.open(planted, "r+b") as fh:
        fh.truncate(truncated_size - 2048)
    err = refused(planted, len(FACTS) + 60)
    check("обрезанная копия (integrity_check != ok) -> отказ", isinstance(err, RuntimeError),
          f"got {err!r}")
    check("причина названа именно integrity_check", "integrity_check" in str(err), f"{err}")

    os.remove(planted)
    make_wiki(planted, FACTS[:2])
    check("в копии меньше фактов, чем в источнике -> отказ",
          isinstance(refused(planted), RuntimeError), f"got {refused(planted)!r}")
    check("годная копия принимается и отдаёт (размер, число фактов)",
          R.verify_copy(planted, 2) == (os.path.getsize(planted), 2),
          f"got {R.verify_copy(planted, 2)}")

    # Шов, появившийся из-за выноса verify_copy: сама функция теперь проверяется
    # напрямую, а её ВЫЗОВ из backup_before_write мог бы пропасть, и ни одна
    # проверка выше этого не заметит — копию снова никто не смотрит, а в журнале
    # стоит «Копия базы снята». Замер: подмена строки вызова на getsize() роняла
    # НОЛЬ проверок из 89.
    seen = []

    def spy_verify(target_path, expected_facts):
        seen.append((target_path, expected_facts))
        return os.path.getsize(target_path), expected_facts

    with patched(R, verify_copy=spy_verify):
        spied = asyncio.run(R.backup_before_write(db, datetime(2026, 7, 29, 13, 0, 0)))
    check("backup_before_write отдаёт копию на проверку verify_copy",
          len(seen) == 1 and seen[0][0] == spied, f"got {seen}")
    check("verify_copy получает число фактов из ИСТОЧНИКА, а не из копии",
          bool(seen) and seen[0][1] == len(FACTS), f"got {seen}")

    def refusing_verify(target_path, expected_facts):
        raise RuntimeError("копия забракована")

    with patched(R, verify_copy=refusing_verify):
        raised = None
        try:
            asyncio.run(R.backup_before_write(db, datetime(2026, 7, 29, 14, 0, 0)))
        except Exception as exc:
            raised = exc
    check("брак, найденный verify_copy, останавливает снятие копии",
          isinstance(raised, RuntimeError), f"got {raised!r}")

    # VACUUM не смог: каталога нет. Обязана быть громкая ошибка.
    with patched(R, backup_path_for=lambda p, now=None: os.path.join(tmp, "нет-каталога", "b.db")):
        raised = None
        try:
            asyncio.run(R.backup_before_write(db))
        except Exception as exc:
            raised = exc
    check("недоступный путь копии = RuntimeError, а не тихое продолжение",
          isinstance(raised, RuntimeError), f"got {raised!r}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [3]
print("\n[3] Без успешной копии — НИ ОДНОГО UPDATE")
tmp = tempfile.mkdtemp(prefix="stomchat_reclass_nobak_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db, FACTS)
    asyncio.run(R.ensure_schema(db))
    before = rows_of(db)

    async def broken_backup(db_path, now=None):
        raise RuntimeError("диск полон")

    with reclass_patch(db, codes_by_index):
        with patched(R, backup_before_write=broken_backup):
            code, out = run_reclass()

    check("прогон завершился отказом (SystemExit 1)", code == 1, f"got {code!r}")
    check("в журнале сказано, что копия не снята", "Резервная копия базы НЕ снята" in out,
          out[-200:])
    check("в журнале сказано, что разметка цела", "Прежняя разметка цела" in out, out[-200:])
    check("ни одна строка не изменилась", rows_of(db) == before, f"было {before}, стало {rows_of(db)}")
    check("ни один факт не помечен переклассифицированным",
          sum(1 for r in rows_of(db) if r[3]) == 1, "кроме заранее помеченного")
    check("файла копии не осталось", glob.glob(os.path.join(tmp, "wiki_backup_*.db")) == [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [4]
print("\n[4] После переклассификации прежний код доступен в category_code_prev")
tmp = tempfile.mkdtemp(prefix="stomchat_reclass_prev_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db, FACTS)
    with reclass_patch(db, codes_by_index):
        timed_out, out = run_bounded(R.main())
    check("прогон не завис", not timed_out)
    after = rows_of(db)
    pending_ids = [i for i, f in enumerate(FACTS, start=1) if f[2] == 0]

    check("все неразмеченные факты получили новый код",
          all(r[1] == f"9.{r[0]}" for r in after if r[0] in pending_ids),
          f"got {after}")
    check("прежний код сохранён в category_code_prev",
          [r[2] for r in after if r[0] in pending_ids] == [FACTS[i - 1][0] for i in pending_ids],
          f"got {[(r[0], r[2]) for r in after]}")
    check("прежний код НЕ равен новому (подмена действительно произошла)",
          all(r[1] != r[2] for r in after if r[0] in pending_ids))
    check("факты помечены is_reclassified = 1",
          all(r[3] == 1 for r in after if r[0] in pending_ids))
    check("уже размеченный факт не тронут",
          [r for r in after if r[0] == 4] == [(4, "7.1.1", None, 1)],
          f"got {[r for r in after if r[0] == 4]}")

    # Копия обязана быть снята ДО правок: доказываем содержимым копии, а не порядком строк.
    backups = glob.glob(os.path.join(tmp, "wiki_backup_*.db"))
    check("копия снята ровно одна", len(backups) == 1, f"got {backups}")
    if backups:
        con = ro(backups[0])
        old_in_copy = [r[0] for r in con.execute(
            "SELECT category_code FROM distilled_facts ORDER BY id")]
        con.close()
        check("в копии лежат ПРЕЖНИЕ коды, значит она снята до первого UPDATE",
              old_in_copy == [f[0] for f in FACTS], f"got {old_in_copy}")
        check("откат возможен: прежний код в копии совпал с category_code_prev",
              [old_in_copy[i - 1] for i in pending_ids]
              == [r[2] for r in after if r[0] in pending_ids])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [5]
print("\n[5] Нечего переклассифицировать — копия не снимается и база не трогается")
tmp = tempfile.mkdtemp(prefix="stomchat_reclass_noop_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db, [("1.1.1", "всё уже размечено", 1), ("2.2.2", "и это тоже", 1)])
    asyncio.run(R.ensure_schema(db))
    before = rows_of(db)
    with reclass_patch(db, codes_by_index):
        timed_out, out = run_bounded(R.main())
    check("прогон не завис", not timed_out)
    check("сказано, что переклассифицировать нечего", "Нечего переклассифицировать" in out,
          out[-200:])
    check("копия на 9 МБ не появилась", glob.glob(os.path.join(tmp, "wiki_backup_*.db")) == [],
          f"got {glob.glob(os.path.join(tmp, 'wiki_backup_*.db'))}")
    check("база не изменилась", rows_of(db) == before)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [6]
print("\n[6] Провал классификатора не хоронит разметку и не вешает прогон")
tmp = tempfile.mkdtemp(prefix="stomchat_reclass_fail_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db, FACTS)
    asyncio.run(R.ensure_schema(db))
    before = rows_of(db)
    with reclass_patch(db, always_none):
        timed_out, out = run_bounded(R.main(), timeout=60)
    check("прогон на неисправном классификаторе завершился (не вечный цикл)", not timed_out)
    check("в журнале есть [ПРОПУСК]", "[ПРОПУСК]" in out, out[-300:])
    after = rows_of(db)
    check("коды категорий не изменились", [r[1] for r in after] == [r[1] for r in before],
          f"было {[r[1] for r in before]}, стало {[r[1] for r in after]}")
    check("category_code_prev не забит пустотой", all(r[2] is None for r in after),
          f"got {[r[2] for r in after]}")
    check("непереклассифицированные остались is_reclassified = 0",
          [r[3] for r in after] == [r[3] for r in before])

    # Все ключи в лимите: прогон обязан остановиться и ничего не записать.
    # Отдельный каталог не для красоты: имя копии различается только секундами,
    # и два сценария в одном каталоге за одну секунду дают «файл копии уже
    # существует» — то есть тест падал бы на своей же фикстуре, а не на коде.
    tmp2 = tempfile.mkdtemp(prefix="stomchat_reclass_retry_")
    db2 = os.path.join(tmp2, "wiki2.db")
    make_wiki(db2, FACTS)
    asyncio.run(R.ensure_schema(db2))
    before2 = rows_of(db2)
    with reclass_patch(db2, always_retry):
        timed_out2, out2 = run_bounded(R.main(), timeout=60)
    check("вечный RETRY не крутится бесконечно", not timed_out2)
    check("в журнале сказано про лимит ключей", "Все ключи в лимите" in out2, out2[-300:])
    check("при лимите ключей ни один код не перезаписан", rows_of(db2) == before2)
    shutil.rmtree(tmp2, ignore_errors=True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [7]
print("\n[7] Повторный прогон videosi не плодит дубли")
tmp = tempfile.mkdtemp(prefix="stomchat_videosi_dup_")
try:
    db = os.path.join(tmp, "wiki.db")
    arch = os.path.join(tmp, "archive.db")
    inp = os.path.join(tmp, "videos.txt")
    make_wiki(db, [])
    make_archive(arch, [(1548, "чужая реплика про контакты", 0, None),
                        (1861, "тут действительно видео", 1, "video"),
                        (1999, "реплика с фото", 1, "photo")])
    io.open(inp, "w", encoding="utf-8").write(
        "1548\nразбор первый: адгезия\n1861\nразбор второй: препарирование\n"
        "1999\nразбор третий: окклюзия\n")

    with patched(V, DB_PATH=db, ARCHIVE_PATH=arch, INPUT_FILE=inp,
                 classify_video=video_code, SLEEP_BETWEEN_VIDEOS=0):
        timed_out, out_first = run_bounded(V.main(), timeout=60)
        check("первый прогон не завис", not timed_out)
        first_count = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
        check("первый прогон вставил все три протокола", first_count == 3, f"got {first_count}")
        check("в журнале сказано, сколько добавлено", "Добавлено 3" in out_first, out_first[-300:])

        timed_out, out_second = run_bounded(V.main(), timeout=60)
        check("второй прогон не завис", not timed_out)
        second_count = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
    check("ПОВТОРНЫЙ ПРОГОН НЕ УВЕЛИЧИЛ ЧИСЛО СТРОК", second_count == first_count,
          f"было {first_count}, стало {second_count}")
    check("второй прогон ничего не добавил", "Добавлено 0" in out_second, out_second[-300:])
    check("второй прогон отчитался о пропусках", "пропущено как повтор 3" in out_second,
          out_second[-300:])
    con = sqlite3.connect(db)
    dupes = con.execute("SELECT content, COUNT(*) FROM distilled_facts GROUP BY content "
                        "HAVING COUNT(*) > 1").fetchall()
    check("ни один протокол не лежит в базе дважды", dupes == [], f"got {[d[1] for d in dupes]}")
    con.close()

    # Отдельно — САМА save_to_db_safe. У main() есть свой предварительный
    # probe-запрос, и он маскирует внутреннюю защиту: с отключённой проверкой
    # внутри save_to_db_safe сквозной прогон всё равно проходил (диверсия S11
    # уронила НОЛЬ проверок). А внутренняя проверка — последняя линия: именно она
    # держит гонку двух одновременных прогонов, когда probe уже сказал «нет такого».
    db3 = os.path.join(tmp, "wiki3.db")
    make_wiki(db3, [])
    payload = ("2.1.1", f"{V.VIDEO_MARKER}4242]\n\nразбор про элайнеры", "", 1, 100,
               "2026-07-29 10:00:00")
    with patched(V, DB_PATH=db3):
        first_insert = asyncio.run(V.save_to_db_safe(payload, "4242"))
        second_insert = asyncio.run(V.save_to_db_safe(payload, "4242"))
    con = sqlite3.connect(db3)
    total = con.execute("SELECT COUNT(*) FROM distilled_facts").fetchone()[0]
    con.close()
    check("save_to_db_safe вставила протокол в первый раз", first_insert is True,
          f"got {first_insert!r}")
    check("save_to_db_safe ОТКАЗАЛА во второй раз (последняя линия защиты)",
          second_insert is False, f"got {second_insert!r}")
    check("после двух вызовов в базе одна строка, а не две", total == 1, f"got {total}")

    # Цикл ожидания обязан отличать «база занята» от «нет такой таблицы»: и то и
    # другое — OperationalError. Пока не отличал, импорт высиживал все попытки и
    # заканчивался ничем, а в журнале стояло только «База занята, жду» — врач не
    # получал ни одного протокола и не понимал, почему. Замер: со снятой
    # проверкой причины падало НОЛЬ проверок из 89, поэтому она здесь.
    db5 = os.path.join(tmp, "wiki5.db")
    sqlite3.connect(db5).execute("CREATE TABLE other (x INTEGER)")
    raised, elapsed = None, None
    with patched(V, DB_PATH=db5, DB_BUSY_ATTEMPTS=5):
        started = time.perf_counter()
        try:
            asyncio.run(V.save_to_db_safe(payload, "4242"))
        except Exception as exc:
            raised = exc
        elapsed = time.perf_counter() - started
    check("нет таблицы distilled_facts -> save_to_db_safe падает громко",
          isinstance(raised, sqlite3.OperationalError), f"got {raised!r}")
    check("причина названа, а не спрятана за «база занята»",
          "no such table" in str(raised).lower(), f"got {raised!r}")
    check("отказ приходит сразу, а не после высиживания всех попыток", elapsed < 3,
          f"{elapsed:.2f} c при 5 попытках по секунде")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [8]
print("\n[8] Граница номера протокола: MSG 148 не считается копией MSG 1489")
tmp = tempfile.mkdtemp(prefix="stomchat_videosi_bnd_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db, [])
    con = sqlite3.connect(db)
    con.execute("INSERT INTO distilled_facts (category_code, content) VALUES (?,?)",
                ("2.1.1", f"{V.VIDEO_MARKER}1489]\n\nразбор про винтовую фиксацию"))
    con.commit()
    con.close()

    async def probe():
        import aiosqlite
        async with aiosqlite.connect(db) as conn:
            return (await V.already_imported(conn, "1489"),
                    await V.already_imported(conn, "148"),
                    await V.already_imported(conn, "14890"))

    hit_1489, hit_148, hit_14890 = asyncio.run(probe())
    check("существующий протокол 1489 опознан как повтор", hit_1489 is not None)
    check("протокол 148 НЕ считается повтором 1489 (иначе он молча не импортируется)",
          hit_148 is None, f"got {hit_148}")
    check("протокол 14890 не считается повтором 1489", hit_14890 is None, f"got {hit_14890}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [9]
print("\n[9] Провенанс: пусто вместо ссылки на чужую реплику")
tmp = tempfile.mkdtemp(prefix="stomchat_videosi_prov_")
try:
    arch = os.path.join(tmp, "archive.db")
    make_archive(arch, [(1548, "чужая текстовая реплика", 0, None),
                        (1999, "реплика с фото", 1, "photo"),
                        (1861, "реплика с видео", 1, "video"),
                        (2000, "медиа есть, тип не указан", 1, None)])
    check("номер существует, но это текстовая реплика -> пусто",
          V.verify_provenance("1548", arch) == "", f"got {V.verify_provenance('1548', arch)!r}")
    check("номер существует, но медиа = фото -> пусто",
          V.verify_provenance("1999", arch) == "", f"got {V.verify_provenance('1999', arch)!r}")
    check("медиа есть, тип пустой -> пусто",
          V.verify_provenance("2000", arch) == "", f"got {V.verify_provenance('2000', arch)!r}")
    check("номера в архиве нет -> пусто", V.verify_provenance("777777", arch) == "")
    check("нечисловой номер -> пусто", V.verify_provenance("MSG_15", arch) == "")
    check("архива рядом нет -> пусто, а не падение",
          V.verify_provenance("1861", os.path.join(tmp, "нет.db")) == "")
    check("подтверждённое видео -> номер возвращается",
          V.verify_provenance("1861", arch) == "1861", f"got {V.verify_provenance('1861', arch)!r}")

    # Сквозная проверка: в базу уезжает пустой провенанс, а не номер из файла.
    db = os.path.join(tmp, "wiki.db")
    inp = os.path.join(tmp, "videos.txt")
    make_wiki(db, [])
    io.open(inp, "w", encoding="utf-8").write(
        "1548\nразбор с ложным номером\n1861\nразбор с настоящим видео\n")
    with patched(V, DB_PATH=db, ARCHIVE_PATH=arch, INPUT_FILE=inp,
                 classify_video=video_code, SLEEP_BETWEEN_VIDEOS=0):
        timed_out, out = run_bounded(V.main(), timeout=60)
    check("сквозной прогон не завис", not timed_out)
    con = sqlite3.connect(db)
    stored = dict(con.execute(
        "SELECT substr(content, instr(content, 'MSG ') + 4, 4), source_ids "
        "FROM distilled_facts").fetchall())
    con.close()
    check("у непроверяемого протокола source_ids ПУСТОЙ, а не номер из файла",
          stored.get("1548") == "", f"got {stored!r}")
    check("у проверяемого протокола source_ids сохранён", stored.get("1861") == "1861",
          f"got {stored!r}")
    check("в журнале посчитаны протоколы без подтверждённого источника",
          "Без подтверждённого источника: 1" in out, out[-400:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [10]
print("\n[10] В category_code уезжают только цифровые коды")
check("название словами отбрасывается", V.clean_codes(["ЭНДОДОНТИЯ"]) == "10.1",
      f"got {V.clean_codes(['ЭНДОДОНТИЯ'])!r}")
check("код из мусорной обёртки вырезается", V.clean_codes(["код 2.1.1 (виниры)"]) == "2.1.1",
      f"got {V.clean_codes(['код 2.1.1 (виниры)'])!r}")
check("порядок кодов сохраняется", V.clean_codes(["3.1", "1.1.1", "2.2"]) == "3.1, 1.1.1, 2.2",
      f"got {V.clean_codes(['3.1', '1.1.1', '2.2'])!r}")
check("дубликаты кодов сворачиваются", V.clean_codes(["1.1.1", "1.1.1"]) == "1.1.1")
check("пустой ответ модели -> 10.1", V.clean_codes([]) == "10.1")

# ---------------------------------------------------------------- [11]
print("\n[11] Живой снимок вики: маркер видит все 53 протокола")
if not os.path.exists(LIVE_WIKI):
    skip("боевая вики рядом", f"{LIVE_WIKI} не найден — регрессия маркера НЕ проверена")
else:
    con = ro(LIVE_WIKI)
    vids = con.execute("SELECT id, content, source_ids FROM distilled_facts "
                       "WHERE content LIKE '%ВИДЕО-ПРОТОКОЛ%' ORDER BY id").fetchall()
    check("видео-протоколов на снимке 53", len(vids) == 53, f"got {len(vids)}")
    starts = sum(1 for v in vids if v[1].startswith(V.VIDEO_MARKER))
    check("ВСЕ боевые протоколы начинаются ровно с VIDEO_MARKER (иначе повтор их удвоит)",
          starts == len(vids) and len(vids) > 0, f"{starts} из {len(vids)}")

    import re as _re
    recognised = 0
    for _id, content, _src in vids:
        m = _re.search(r"MSG (\d+)\]", content)
        if not m:
            continue
        marker = f"{V.VIDEO_MARKER}{m.group(1)}]"
        if con.execute("SELECT id FROM distilled_facts WHERE substr(content, 1, ?) = ?",
                       (len(marker), marker)).fetchone():
            recognised += 1
    check("повтор распознан для всех 53 (вставлено заново было бы 0)",
          recognised == len(vids) and len(vids) > 0, f"{recognised} из {len(vids)}")

    if not os.path.exists(LIVE_ARCHIVE):
        skip("боевой архив рядом", f"{LIVE_ARCHIVE} не найден — провенанс 35/53 НЕ пересчитан")
    else:
        arc = ro(LIVE_ARCHIVE)
        numeric = sum(1 for v in vids if (v[2] or "").strip().isdigit())
        found = with_media = as_video = 0
        for _id, _content, src in vids:
            s = (src or "").strip()
            if not s.isdigit():
                continue
            row = arc.execute("SELECT has_media, media_type FROM archive_messages WHERE msg_id = ?",
                              (int(s),)).fetchone()
            if not row:
                continue
            found += 1
            if row[0]:
                with_media += 1
            if (row[1] or "").strip().lower() == "video":
                as_video += 1
        arc.close()
        print(f"      замер: числовых {numeric}, найдено в архиве {found}, "
              f"из них с медиа {with_media}, тип video {as_video}")
        check("все 53 держат числовой source_ids", numeric == 53, f"got {numeric}")
        check("совпадений с msg_id архива ровно 35 (замер лана подтверждён)", found == 35,
              f"got {found}")
        check("из совпавших медиа лишь у 6 — то есть это чужие реплики", with_media == 6,
              f"got {with_media}")
        check("видео среди совпавших ровно одно", as_video == 1, f"got {as_video}")

        # Тот же вывод, но через саму функцию: 52 ссылки обязаны стать пустыми.
        empties = confirmed = 0
        for _id, _content, src in vids:
            if V.verify_provenance((src or "").strip(), LIVE_ARCHIVE):
                confirmed += 1
            else:
                empties += 1
        check("verify_provenance обнуляет 52 ложные ссылки из 53", empties == 52,
              f"got {empties}")
        check("подтверждает ровно одну", confirmed == 1, f"got {confirmed}")
    con.close()

# ---------------------------------------------------------------- [12]
print("\n[12] Боевые базы не изменились за прогон теста")
if not LIVE_MD5_BEFORE:
    skip("боевых баз рядом нет", "проверка неизменности неприменима")
for path, before_hash in LIVE_MD5_BEFORE.items():
    check(f"{path} побайтово тот же", md5(path) == before_hash, "ТЕСТ ПИСАЛ В БОЕВУЮ БАЗУ")

print()
print("=" * 78)
if SKIP:
    print(f"ПРОПУЩЕНО (не проверено!): {len(SKIP)} -> {SKIP}")
print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    for name in FAIL:
        print(f"  FAILED: {name}")
sys.exit(1 if FAIL else 0)
