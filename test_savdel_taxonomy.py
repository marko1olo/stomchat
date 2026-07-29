"""Проверки таксономии выгрузки savdel.py.

Каждый дефект здесь назван ПОСЛЕДСТВИЕМ для врача, а не описанием кода.

Д1. В CAT_MAP не было кодов разделов 8-10 (детская стоматология,
    материаловедение, прочее). Последствие: факт с таким кодом не попадает НИ В
    ОДИН файл ревью и в выводе экспорта о нём нет ни строки — врач не прочитает
    раздел и не узнает, что раздела нет. Замер по снимку до реклассификации
    backup_wiki_18_0140.db: 139 фактов, все коды которых лежат в 8/9/10.

Д2. Категория выбиралась через LIKE '%2.1.1%' — совпадение подстрокой без
    границы токена. Последствие: врач открывает файл "Профгигиена GBT" и читает
    в нём факты кода 1.3.10 как относящиеся к профгигиене. Это хуже потери:
    пропажу хотя бы видно, а подложенный факт читается как проверенный. Замер по
    тому же снимку: 42 лишних присвоения в 13 чужих файлов, 33 факта.

Д3. Путь базы подставлялся в текст SQL внутри ATTACH DATABASE. Последствие:
    апостроф в пути к каталогу роняет экспорт целиком — ни одного файла ревью, —
    а сообщение об ошибке указывает на середину пути, а не на путь.

Д4 (найдено волной 2). Опустевшая категория оставляла файл прошлой выгрузки на
    диске. Последствие: prompter.py:19 забирает из папки КАЖДЫЙ .txt и заказывает
    по нему платную монографию у Гемини, поэтому врач получает методичку по
    фактам, которых в вике больше нет, и не отличает её от актуальной.

Д5. Свалка для фактов вне CAT_MAP не должна иметь расширение .txt — иначе
    prompter.py заказывает монографию по мусорному коду, то есть платит за ничто.

Боевые базы этот тест открывает ТОЛЬКО в режиме ro и только для чтения схемы и
контрольного замера. Весь экспорт гоняется по временным базам в TEMP.
"""
import asyncio
import io
import contextlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import savdel

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"[OK  ] {name}")
    else:
        FAIL.append(name)
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


REPO = Path(__file__).resolve().parent
LIVE_WIKI = REPO / "stomat_wiki.db"
LIVE_ARCHIVE = REPO / "stomat_archive.db"


def live_schema(db_path, table):
    """Схему берём из боевой базы, чтобы временная не расходилась с реальной."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


# ---------------------------------------------------------------- стенд
FACTS = [
    # (id, category_code, content)
    (1, "2.1.1", "ФАКТ-РОВНО-2.1.1"),
    (2, "2.1.10", "ФАКТ-ХВОСТ-2.1.10"),
    (3, "2.1.1.5", "ФАКТ-ПОДКОД-2.1.1.5"),
    (4, "12.1.1", "ФАКТ-ГОЛОВА-12.1.1"),
    (5, "2.2.3.1", "ФАКТ-СЕРЕДИНА-2.2.3.1"),
    (6, "1.1.1, 2.1.1", "ФАКТ-МУЛЬТИТЕГ"),
    (7, " 2.1.1 ", "ФАКТ-С-ПРОБЕЛАМИ"),
    (8, "8.1.1", "ФАКТ-ДЕТСКАЯ-8.1.1"),
    (9, "9.1.1", "ФАКТ-МАТЕРИАЛЫ-9.1.1"),
    (10, "10.1.1", "ФАКТ-ПРОЧЕЕ-10.1.1"),
    (11, "2.1", "ФАКТ-БЕЗ-ИМЕНИ-L2"),
    (12, "3.0.0", "ФАКТ-БЕЗ-ИМЕНИ-ЗАГЛУШКА"),
]


def build_stand(root):
    """Временная вика + временный архив той же схемы, что боевые."""
    wiki = root / "tmp_wiki.db"
    arch = root / "tmp_archive.db"

    sql_facts = live_schema(LIVE_WIKI, "distilled_facts")
    sql_arch = live_schema(LIVE_ARCHIVE, "archive_messages")
    if not sql_facts or not sql_arch:
        return None, None

    con = sqlite3.connect(wiki)
    con.execute(sql_facts)
    con.executemany(
        "INSERT INTO distilled_facts (id, category_code, content, source_ids, "
        "is_case, confidence) VALUES (?,?,?,?,?,?)",
        [(i, c, txt, "777", 0, 90) for i, c, txt in FACTS])
    con.commit()
    con.close()

    con = sqlite3.connect(arch)
    con.execute(sql_arch)
    con.execute("INSERT INTO archive_messages (msg_id, vision_description, "
                "vision_processed) VALUES (777, 'ОПИСАНИЕ-ФОТО-777', 1)")
    con.commit()
    con.close()
    return wiki, arch


def run_export(wiki, arch, outdir):
    """Гоняем настоящий export_v7 по временным базам. Возвращаем вывод."""
    saved = (savdel.WIKI_DB, savdel.ARCHIVE_DB, savdel.OUTPUT_DIR)
    savdel.WIKI_DB = str(wiki)
    savdel.ARCHIVE_DB = str(arch)
    savdel.OUTPUT_DIR = str(outdir)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            asyncio.run(savdel.export_v7())
    except Exception as e:
        # Экспорт умер на полпути — это и есть дефект, но набор проверок ронять
        # нельзя: иначе одна поломка спрячет все остальные за трейсбеком, и
        # непонятно, что ещё сломано.
        buf.write(f"\nЭКСПОРТ УПАЛ: {type(e).__name__}: {e}\n")
    finally:
        savdel.WIKI_DB, savdel.ARCHIVE_DB, savdel.OUTPUT_DIR = saved
    return buf.getvalue()


def file_for(outdir, code):
    """None, если кода нет в CAT_MAP: это дефект Д1, а не сбой самого теста."""
    if code not in savdel.CAT_MAP:
        return None
    name = f"{code}_{savdel.CAT_MAP[code]}".replace('/', '_').replace(' ', '_')
    return Path(outdir) / f"{name}.txt"


def exists(path):
    return bool(path) and path.exists()


def body(path):
    return path.read_text(encoding="utf-8") if exists(path) else ""


# Каталог с апострофом и процентом в имени — Д3. Апостроф раньше рвал текст SQL,
# процент — подстановочный знак LIKE и спецсимвол URI.
TMP = Path(tempfile.mkdtemp(prefix="savdel_q'uote_100%_"))
try:
    WIKI, ARCH = build_stand(TMP)
    check("стенд собран по схеме боевых баз",
          WIKI is not None, "не нашёл CREATE TABLE в боевой базе")
    if WIKI is None:
        print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
        sys.exit(1)

    OUT = TMP / "out"
    log = run_export(WIKI, ARCH, OUT)

    # ---- Д3: путь с апострофом и процентом не роняет экспорт
    check("Д3: экспорт из каталога с апострофом и % в пути дошёл до конца",
          "Готово" in log, f"вывод: {log[-200:]!r}")
    check("Д3: ATTACH параметром отдал фото из архива",
          "ОПИСАНИЕ-ФОТО-777" in body(file_for(OUT, "2.1.1")),
          "фото не подтянулось — ATTACH/архив не сработал")

    # ---- Д2: граница токена
    f211 = body(file_for(OUT, "2.1.1"))
    check("Д2: код 2.1.1 попал в свой файл",
          "ФАКТ-РОВНО-2.1.1" in f211)
    check("Д2: код 2.1.10 НЕ попал в файл 2.1.1 (хвост)",
          "ФАКТ-ХВОСТ-2.1.10" not in f211,
          "врач читает чужой раздел как свой")
    check("Д2: код 12.1.1 НЕ попал в файл 2.1.1 (голова)",
          "ФАКТ-ГОЛОВА-12.1.1" not in f211,
          "затянут код чужого раздела верхнего уровня")
    check("Д2: подкод 2.1.1.5 попал в файл родителя 2.1.1",
          "ФАКТ-ПОДКОД-2.1.1.5" in f211,
          "подкоды обязаны читаться вместе с родителем")
    check("Д2: код 2.2.3.1 НЕ попал в файл 2.3.1 (середина)",
          "ФАКТ-СЕРЕДИНА-2.2.3.1" not in body(file_for(OUT, "2.3.1")))
    check("Д2: код 2.2.3.1 попал в файл своего родителя 2.2.3",
          "ФАКТ-СЕРЕДИНА-2.2.3.1" in body(file_for(OUT, "2.2.3")))
    check("Д2: мультитег виден в ОБОИХ своих файлах",
          "ФАКТ-МУЛЬТИТЕГ" in f211 and "ФАКТ-МУЛЬТИТЕГ" in body(file_for(OUT, "1.1.1")),
          "второй тег фактически терялся")
    check("Д2: код с пробелами по краям нашёлся",
          "ФАКТ-С-ПРОБЕЛАМИ" in f211)
    check("Д2: обратное направление — файл 2.1.1 не забрал факт у 2.1.10",
          "ФАКТ-РОВНО-2.1.1" not in body(file_for(OUT, "2.1.2")))

    # ---- Д1: коды разделов 8-10 доходят до файла
    for code, marker in (("8.1.1", "ФАКТ-ДЕТСКАЯ-8.1.1"),
                         ("9.1.1", "ФАКТ-МАТЕРИАЛЫ-9.1.1"),
                         ("10.1.1", "ФАКТ-ПРОЧЕЕ-10.1.1")):
        p = file_for(OUT, code)
        check(f"Д1: категория {code} вернула непустой файл с фактом",
              exists(p) and marker in body(p),
              "кода нет в CAT_MAP" if p is None else
              "факт исчез как класс: ни в одном файле ревью")

    # ---- Д1: ничего не пропадает молча
    leftover = OUT / savdel.LEFTOVER_FILE
    lb = body(leftover)
    check("Д1: факт с кодом без имени ушёл в свалку, а не в никуда",
          "ФАКТ-БЕЗ-ИМЕНИ-L2" in lb and "ФАКТ-БЕЗ-ИМЕНИ-ЗАГЛУШКА" in lb,
          "невидимый факт нигде не назван")
    # Шапку разбираем на ТОКЕНЫ, а не ищем подстрокой: '2.1' лежит внутри
    # '2.1.10', и проверка подстрокой оставалась зелёной, даже когда L2-код в
    # шапку не попадал вовсе. Врач тогда не узнает, что потерян целый раздел, а
    # не один подкод (проверено диверсией: шапка без L2-кодов проходила).
    head_codes = set()
    for chunk in (lb.split("=" * 60)[0]
                  .split("Коды, которых нет в карте:")[-1].split(",")):
        token = chunk.split("(")[0].strip()
        if token:
            head_codes.add(token)
    check("Д1: код без имени назван в шапке свалки ОТДЕЛЬНЫМ токеном",
          {"2.1", "3.0.0"} <= head_codes,
          f"в шапке: {sorted(head_codes)} — врач не поймёт, какой раздел потерян")
    # 4 факта: 2.1(L2), 3.0.0(заглушка) и — важно — 2.1.10 с 12.1.1, которые до
    # правки Д2 молча оседали в чужих файлах вместо попадания в свалку.
    check("Д1: про свалку есть громкая строка в выводе с числом фактов",
          "Вне карты: 4 фактов" in log, f"вывод: {log!r}"[:300])
    check("Д2+Д1: отвергнутые по границе токена коды видны в свалке, а не потеряны",
          "ФАКТ-ХВОСТ-2.1.10" in lb and "ФАКТ-ГОЛОВА-12.1.1" in lb,
          "факт исчез: из чужого файла его убрали, в свалку не положили")
    check("Д1: факт, попавший в категорию, в свалку НЕ дублируется",
          "ФАКТ-РОВНО-2.1.1" not in lb)

    # ---- Д5: расширение свалки не .txt
    txts = sorted(p.name for p in OUT.iterdir() if p.suffix == ".txt")
    check("Д5: свалка не имеет расширения .txt (иначе платная монография по мусору)",
          not savdel.LEFTOVER_FILE.endswith(".txt")
          and savdel.LEFTOVER_FILE not in txts,
          f".txt в папке: {txts}")

    # ---- Д4: опустевшая категория снимает свой файл
    stale = file_for(OUT, "8.1.1")
    check("Д4: файл категории 8.1.1 существует до опустошения", exists(stale))
    con = sqlite3.connect(WIKI)          # своя временная база, не боевая
    con.execute("DELETE FROM distilled_facts WHERE id=8")
    con.commit()
    con.close()
    log2 = run_export(WIKI, ARCH, OUT)
    check("Д4: файл опустевшей категории снят с диска",
          not exists(stale),
          "prompter.py закажет платную монографию по фактам, которых уже нет")
    check("Д4: снятие устаревшего файла названо в выводе",
          "снят устаревший файл" in log2, f"вывод: {log2[:400]!r}")
    check("Д4: соседние категории от этого не пострадали",
          "ФАКТ-РОВНО-2.1.1" in body(file_for(OUT, "2.1.1")))
    # Повторный экспорт обязан ПЕРЕЗАПИСАТЬ файл категории, а не дописать в него:
    # иначе каждый прогон удваивает текст, врач читает один и тот же факт дважды
    # и не знает, какая из копий свежая, а prompter.py платит Гемини за раздутый
    # файл. Проверяется на втором прогоне выше (open('a') вместо 'w' раньше
    # проходил незамеченным).
    f211_again = body(file_for(OUT, "2.1.1"))
    check("Д4: повторный экспорт перезаписывает файл, а не удваивает факты",
          f211_again.count("ФАКТ-РОВНО-2.1.1") == 1
          and f211_again.count("Найдено записей:") == 1,
          f"вхождений факта {f211_again.count('ФАКТ-РОВНО-2.1.1')}, "
          f"шапок {f211_again.count('Найдено записей:')}")

    # ---- Д3: ro действительно держит запись
    ro = sqlite3.connect(savdel.read_only_uri(str(WIKI)), uri=True)
    try:
        ro.execute("UPDATE distilled_facts SET confidence=1")
        wrote = True
        err = ""
    except sqlite3.OperationalError as e:
        wrote, err = False, str(e)
    finally:
        ro.close()
    check("Д3: read_only_uri физически запрещает запись",
          not wrote and "readonly" in err,
          f"запись прошла: вика открыта на запись без нужды ({err})")

    # ---- Д3: путь ЭКРАНИРУЕТСЯ, а не склеивается в строку 'file:'+путь. На '#'
    # в имени каталога сырая склейка отрезает всё после решётки и sqlite молча
    # открывает ДРУГУЮ базу: экспорт видит 0 фактов в каждой категории и снимает
    # файлы прошлой выгрузки как устаревшие. Замер на копии боевой раскладки: 52
    # файла ревью удалены, в выводе при этом "Готово" и ни слова об ошибке.
    hash_dir = Path(tempfile.mkdtemp(prefix="savdel_hash#tag_"))
    try:
        probe_db = hash_dir / "p.db"
        con = sqlite3.connect(probe_db)
        con.execute("CREATE TABLE t(x)")
        con.execute("INSERT INTO t VALUES (42)")
        con.commit()
        con.close()
        try:
            c = sqlite3.connect(savdel.read_only_uri(str(probe_db)), uri=True)
            got = c.execute("SELECT x FROM t").fetchone()[0]
            c.close()
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        check("Д3: путь с '#' в каталоге открывает ТУ ЖЕ базу, а не чужую пустую",
              got == 42, f"прочитано {got!r} вместо 42")
    finally:
        shutil.rmtree(hash_dir, ignore_errors=True)

    # ---- Д3: код с кавычкой/подстановочным знаком отвергается, а не идёт в SQL
    bad_ok = []
    for bad in ("2.1.1'; DROP TABLE distilled_facts--", "2.1._", "2.1.%", "", "abc"):
        try:
            savdel.category_patterns(bad)
            bad_ok.append(bad)
        except ValueError:
            pass
    check("Д3: недопустимый код категории отвергается до похода в SQL",
          not bad_ok, f"пропущены: {bad_ok}")

    # ---- Д4/кодировка: import savdel выживает при cp1251 на stdout
    env = dict(os.environ, PYTHONIOENCODING="cp1251")
    proc = subprocess.run([sys.executable, "-c",
                           "import savdel; print('\\U0001F680 ok')"],
                          cwd=str(REPO), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    check("кодировка: эмодзи в выводе не роняет экспорт при cp1251 на stdout",
          proc.returncode == 0,
          f"rc={proc.returncode} err={proc.stderr.decode('utf-8', 'replace')[-200:]!r}")

    # ---- контрольный замер по БОЕВОЙ вике (только чтение)
    if LIVE_WIKI.exists():
        con = sqlite3.connect(Path(LIVE_WIKI).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            n211 = con.execute(
                "SELECT COUNT(*) FROM distilled_facts "
                "WHERE (','||REPLACE(category_code,' ','')||',') LIKE ? "
                "   OR (','||REPLACE(category_code,' ','')||',') LIKE ?",
                savdel.category_patterns("2.1.1")).fetchone()[0]
            check("боевая вика: токенный запрос работает на реальных данных",
                  n211 > 0, f"2.1.1 вернул {n211} — запрос сломан на живой схеме")
            named = sum(1 for code in savdel.CAT_MAP if con.execute(
                "SELECT 1 FROM distilled_facts "
                "WHERE (','||REPLACE(category_code,' ','')||',') LIKE ? "
                "   OR (','||REPLACE(category_code,' ','')||',') LIKE ? LIMIT 1",
                savdel.category_patterns(code)).fetchone())
            print(f"       (замер: 2.1.1 -> {n211} фактов; непустых категорий "
                  f"{named} из {len(savdel.CAT_MAP)})")
        finally:
            con.close()
    else:
        print("[SKIP] боевой stomat_wiki.db не найден — контрольный замер пропущен")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
sys.exit(1 if FAIL else 0)
