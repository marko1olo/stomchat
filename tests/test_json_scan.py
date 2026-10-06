# -*- coding: utf-8 -*-
"""
Разбор ответа модели в скриптах дистилляции: reclass.py и videosi.py.

Обе точки искали объект ЖАДНО — от первой открывающей скобки до ПОСЛЕДНЕЙ
закрывающей во всём ответе. Что это стоило врачу:

1. ЛЮБАЯ закрывающая скоба в болтовне модели после объекта ломала разбор целиком.
   Замер на 9 реалистичных ответах: 6 не разбирались. Для reclass это значит, что
   факт остаётся под ЧУЖОЙ категорией и врач его не найдёт (три попытки, потом
   [ПРОПУСК]); для videosi — что видео-протокол НЕ ЗАПИСЫВАЕТСЯ вовсе, и разбора
   нет в рубрикаторе, при том что модель ответила правильно.

2. Молчание вместо причины. videosi ловил всё одним `except Exception: continue`
   и перебирал ВСЕ 10 ключей: замер — 10 вызовов модели на один протокол и ни
   одной строки в журнале. Оператор видел ноль записанных протоколов без причины.

3. Обрыв ответа на середине объекта reclass принимал за ЛИМИТ КВОТЫ: сообщение
   json.loads — "Expecting ',' delimiter", а подстрока "limit" сидит внутри слова
   deLIMITer. Замер сквозного прогона: 13 вызовов модели, 26 с сна и обрыв ВСЕГО
   прогона на первом же таком факте с ложным «Все ключи в лимите», после чего
   печаталось BASE RECLASSIFIED SUCCESSFULLY. Оператор читал успех там, где не
   переклассифицирован ни один факт из 12 784, и повторного прогона не делал.

4. Пустой ответ модели (и объект без кодов) уезжал в базу кодом-заглушкой 10.1 с
   is_reclassified = 1. Этого кода нет ни в одной подтеме рубрикатора, а прежний
   код уже затёрт, и повторный прогон помеченный факт не берёт — факт становился
   недостижимым для врача НАВСЕГДА.

5. Несколько объектов в ответе склеивались в мусор вместо двух наборов кодов.

Сканер сбалансированных объектов — ОДИН на дерево: distiller._iter_json_objects.
Здесь проверяется, что обе точки ходят именно через него, а не через третью копию.

Сети в тесте нет: ответ модели подставляется заглушкой. Боевые базы не
открываются вообще, их md5 сверяется до и после ([7]).

Запуск: python test_json_scan.py
"""
import asyncio
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import types as pytypes
from contextlib import redirect_stdout

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import distiller as D  # noqa: E402
import reclass as R    # noqa: E402
import videosi as V    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


LIVE = [p for p in ("stomat_wiki.db", "stomat_archive.db") if os.path.exists(p)]


def md5(path):
    h = hashlib.md5()
    with io.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


LIVE_BEFORE = {p: md5(p) for p in LIVE}

WIKI_SCHEMA = """CREATE TABLE distilled_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, category_code TEXT, content TEXT,
    source_ids TEXT, media_links TEXT, is_case BOOLEAN, confidence INTEGER,
    processed_at TIMESTAMP, is_reclassified BOOLEAN DEFAULT 0)"""

ARCHIVE_SCHEMA = """CREATE TABLE archive_messages (
    msg_id INTEGER PRIMARY KEY, date TIMESTAMP, sender_id INTEGER, sender_name TEXT,
    sender_username TEXT, text TEXT, reply_to_msg_id INTEGER, has_media BOOLEAN,
    media_type TEXT, media_remote_url TEXT, vision_description TEXT,
    vision_processed BOOLEAN, category_l1 TEXT, category_l2 TEXT, category_l3 TEXT,
    is_processed_for_wiki BOOLEAN)"""

FACTS = [("1.1.1", "факт про ирригацию"), ("2.2.1", "факт про BOPT"),
         ("3.2.4", "факт про периимплантит")]


def make_wiki(path, facts=FACTS):
    con = sqlite3.connect(path)
    con.execute(WIKI_SCHEMA)
    con.execute("CREATE INDEX idx_cat ON distilled_facts(category_code)")
    for code, content in facts:
        con.execute("INSERT INTO distilled_facts (category_code, content, is_reclassified)"
                    " VALUES (?,?,0)", (code, content))
    con.commit()
    con.close()


def rows_of(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT id, category_code, category_code_prev, is_reclassified"
                           " FROM distilled_facts ORDER BY id").fetchall()
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


class ReplyClient:
    """Клиент модели-заглушка: всегда отдаёт заданный текст, считает вызовы."""

    def __init__(self, reply, raises=None):
        self.reply, self.raises, self.calls = reply, raises, 0
        self.models = self

    def generate_content(self, **kw):
        self.calls += 1
        if self.raises is not None:
            raise Exception(self.raises)
        return pytypes.SimpleNamespace(text=self.reply)


def fake_genai(reply, raises=None, counter=None):
    """Фабрика genai-заглушки для videosi: считает вызовы по ВСЕМ ключам."""

    def factory(**kw):
        cli = ReplyClient(reply, raises)
        if counter is not None:
            cli.calls = 0

            def counted(**kwargs):
                counter["n"] += 1
                if raises is not None:
                    raise Exception(raises)
                return pytypes.SimpleNamespace(text=reply)

            cli.generate_content = counted
        return cli

    return pytypes.SimpleNamespace(Client=factory)


def classify(reply, f_id=101, raises=None):
    """(результат reclass.classify_fact, число вызовов модели, напечатанное)."""
    cli = ReplyClient(reply, raises)
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = asyncio.run(R.classify_fact(cli, "тело факта", f_id))
    return out, cli.calls, buf.getvalue()


def classify_video(reply, raises=None, keys=10):
    """(результат videosi.classify_video, число вызовов по всем ключам, вывод)."""
    counter = {"n": 0}
    buf = io.StringIO()
    with patched(V, genai=fake_genai(reply, raises, counter),
                 config=pytypes.SimpleNamespace(GOOGLE_KEYS=[f"k{i}" for i in range(keys)])):
        with redirect_stdout(buf):
            out = asyncio.run(V.classify_video("тело протокола"))
    return out, counter["n"], buf.getvalue()


def run_reclass(db, classifier=None, reply=None, keys=10):
    """Сквозной прогон reclass.main без сети и без сна. Возвращает (вывод, вызовы)."""
    counter = {"n": 0}

    def factory(**kw):
        cli = ReplyClient(reply)

        def counted(**kwargs):
            counter["n"] += 1
            return pytypes.SimpleNamespace(text=reply)

        cli.generate_content = counted
        return cli

    values = dict(DB_PATH=db, genai=pytypes.SimpleNamespace(Client=factory),
                  config=pytypes.SimpleNamespace(GOOGLE_KEYS=[f"k{i}" for i in range(keys)]),
                  SLEEP_BETWEEN_FACTS=0, SLEEP_ON_ROTATE=0)
    if classifier is not None:
        values["classify_fact"] = classifier
    buf = io.StringIO()
    with patched(R, **values):
        try:
            with redirect_stdout(buf):
                asyncio.run(asyncio.wait_for(R.main(), timeout=60))
        except SystemExit:
            pass
        except asyncio.TimeoutError:
            buf.write("\n[ПРОГОН ЗАВИС]")
    return buf.getvalue(), counter["n"]


CHATTER = 'Вот результат: {"codes": ["2.3.2"]} — надеюсь, помогло }'
TRUNCATED = '{"codes": ["1.1.1"'

print("=" * 78)
print("РАЗБОР ОТВЕТА МОДЕЛИ: reclass.py, videosi.py")
print("=" * 78)

# ---------------------------------------------------------------- [1]
print("\n[1] Болтовня ПОСЛЕ объекта разбирается, коды не теряются")
for name, reply, want in [
    ("закрывающая скоба в хвосте", CHATTER, "2.3.2"),
    ("объект без болтовни (контроль)", '{"codes": ["2.3.2"]}', "2.3.2"),
    ("болтовня до и после", 'Итог. {"codes": ["1.1.3"]} Готово }', "1.1.3"),
    ("тройные кавычки с json", '```json\n{"codes": ["2.2.1"]}\n```', "2.2.1"),
    ("вложенный объект и скоба в хвосте",
     'Итог {"codes": ["6.2.1"], "meta": {"sure": true}} (правило 3 })', "6.2.1"),
    ("двойная закрывающая в хвосте", '{"codes": ["4.1.2"]} }}', "4.1.2"),
]:
    out, calls, log = classify(reply, f_id=11)
    check(f"reclass: {name} -> {want}", out == want, f"got {out!r}, лог: {log.strip()[:120]}")
    check(f"reclass: {name} стоит один вызов модели", calls == 1, f"got {calls}")

for name, reply, want in [
    ("закрывающая скоба в хвосте", CHATTER, "2.3.2"),
    ("вложенный объект и скоба в хвосте",
     'Итог {"codes": ["6.2.1"], "meta": {"sure": true}} (правило 3 })', "6.2.1"),
]:
    out, calls, log = classify_video(reply)
    check(f"videosi: {name} -> {want}", out == want, f"got {out!r}")
    check(f"videosi: {name} НЕ жжёт все 10 ключей", calls == 1, f"вызовов {calls}")

# Сквозной прогон reclass: болтовня больше не оставляет факт под чужим кодом.
tmp = tempfile.mkdtemp(prefix="stomchat_jsonscan_chat_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db)
    log, calls = run_reclass(db, reply=CHATTER)
    after = rows_of(db)
    check("сквозной прогон: все факты переклассифицированы, а не пропущены",
          [r[1] for r in after] == ["2.3.2"] * len(FACTS), f"got {[r[1] for r in after]}")
    check("сквозной прогон: прежние коды сохранены в category_code_prev",
          [r[2] for r in after] == [f[0] for f in FACTS], f"got {[r[2] for r in after]}")
    check("сквозной прогон: все помечены is_reclassified = 1",
          all(r[3] == 1 for r in after), f"got {[r[3] for r in after]}")
    check("сквозной прогон: один вызов модели на факт",
          calls == len(FACTS), f"вызовов {calls} при {len(FACTS)} фактах")
    check("сквозной прогон: в журнале нет [ПРОПУСК]", "[ПРОПУСК]" not in log, log[-200:])
    check("сквозной прогон: отчёт об успехе честный",
          "BASE RECLASSIFIED SUCCESSFULLY" in log, log[-200:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# Сквозной прогон videosi: протокол реально доезжает до базы.
tmp = tempfile.mkdtemp(prefix="stomchat_jsonscan_vid_")
try:
    db = os.path.join(tmp, "wiki.db")
    arch = os.path.join(tmp, "archive.db")
    inp = os.path.join(tmp, "videos.txt")
    make_wiki(db, [])
    con = sqlite3.connect(arch)
    con.execute(ARCHIVE_SCHEMA)
    con.execute("INSERT INTO archive_messages (msg_id, text, has_media, media_type)"
                " VALUES (1548, 'разбор ВНЧС', 1, 'video')")
    con.commit()
    con.close()
    with io.open(inp, "w", encoding="utf-8") as fh:
        fh.write("\n1548\nПротокол лечения ВНЧС: сплинт, контроль через месяц.\n")
    counter = {"n": 0}
    buf = io.StringIO()
    with patched(V, DB_PATH=db, ARCHIVE_PATH=arch, INPUT_FILE=inp, SLEEP_BETWEEN_VIDEOS=0,
                 genai=fake_genai(CHATTER, None, counter),
                 config=pytypes.SimpleNamespace(GOOGLE_KEYS=[f"k{i}" for i in range(10)])):
        try:
            with redirect_stdout(buf):
                asyncio.run(asyncio.wait_for(V.main(), timeout=60))
        except SystemExit:
            pass
    log = buf.getvalue()
    con = sqlite3.connect(db)
    saved = con.execute("SELECT category_code, content, source_ids FROM distilled_facts").fetchall()
    con.close()
    check("videosi сквозной: протокол ЗАПИСАН, а не отброшен", len(saved) == 1,
          f"строк {len(saved)}, лог: {log.strip()[-200:]}")
    if saved:
        check("videosi сквозной: код категории из ответа модели", saved[0][0] == "2.3.2",
              f"got {saved[0][0]!r}")
        check("videosi сквозной: тело протокола на месте", "сплинт" in saved[0][1],
              f"got {saved[0][1][:80]!r}")
        check("videosi сквозной: провенанс подтверждён архивом", saved[0][2] == "1548",
              f"got {saved[0][2]!r}")
    check("videosi сквозной: один вызов модели на протокол", counter["n"] == 1,
          f"вызовов {counter['n']}")
    check("videosi сквозной: нет строки НЕ ЗАПИСАН", "НЕ ЗАПИСАН" not in log, log[-200:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [2]
print("\n[2] Обрыв ответа = ГРОМКИЙ отказ с причиной, а не молчание и не ложный лимит")
out, calls, log = classify(TRUNCATED, f_id=42)
check("reclass: обрыв не даёт код в базу", out is None, f"got {out!r}")
check("reclass: обрыв НЕ принимается за лимит квоты", out != "RETRY", f"got {out!r}")
check("reclass: отказ громкий", "[ОТКАЗ РАЗБОРА]" in log, f"лог: {log.strip()[:160]!r}")
check("reclass: в отказе назван номер факта", "42" in log, log.strip()[:160])
check("reclass: в отказе названа причина (обрыв)", "обрезан" in log, log.strip()[:160])
check("reclass: в отказе показан конец ответа", '"1.1.1"' in log, log.strip()[:160])
check("reclass: отказ не молчаливая пустота", log.strip() != "", "лог пуст")

out, calls, log = classify('Не могу определить категорию.', f_id=43)
check("reclass: ответ без объекта -> отказ, а не заглушка", out is None, f"got {out!r}")
check("reclass: ответ без объекта громкий", "[ОТКАЗ РАЗБОРА]" in log, log.strip()[:160])

out, calls, log = classify_video(TRUNCATED)
check("videosi: обрыв не пишет протокол", out == V.FALLBACK_CODE, f"got {out!r}")
check("videosi: обрыв стоит ОДИН вызов, а не 10", calls == 1, f"вызовов {calls}")
check("videosi: обрыв назван в журнале", "[ОТКАЗ РАЗБОРА]" in log, f"лог: {log.strip()[:160]!r}")
check("videosi: в журнале названа причина", "обрезан" in log, log.strip()[:160])

# Ловушка «limit внутри delimiter»: транспортная ветка обязана отличать
# сообщение json.loads от настоящего лимита квоты.
for msg, want, why in [
    ("Expecting ',' delimiter: line 1 column 19", None, "delimiter не лимит"),
    ("Expecting ':' delimiter: line 1 column 9", None, "delimiter не лимит"),
    ("429 RESOURCE_EXHAUSTED", "RETRY", "настоящий 429"),
    ("Quota exceeded for quota metric", "RETRY", "настоящая квота"),
    ("TPM limit reached for this key", "RETRY", "настоящий лимит"),
    ("You exceeded your current quota", "RETRY", "настоящая квота"),
]:
    out, calls, log = classify("не важно", f_id=44, raises=msg)
    check(f"транспорт: {why} -> {want}", out == want, f"got {out!r} на {msg!r}")

# Сквозной прогон: обрыв не обрывает ВЕСЬ прогон и не врёт про лимит ключей.
tmp = tempfile.mkdtemp(prefix="stomchat_jsonscan_trunc_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db)
    log, calls = run_reclass(db, reply=TRUNCATED)
    after = rows_of(db)
    check("обрыв: прогон не завис", "[ПРОГОН ЗАВИС]" not in log, log[-200:])
    check("обрыв: ни один код не перезаписан",
          [r[1] for r in after] == [f[0] for f in FACTS], f"got {[r[1] for r in after]}")
    check("обрыв: category_code_prev не забит пустотой", all(r[2] is None for r in after),
          f"got {[r[2] for r in after]}")
    check("обрыв: факты остались is_reclassified = 0", all(r[3] == 0 for r in after),
          f"got {[r[3] for r in after]}")
    # Считаем именно строки про факт («[ПРОПУСК] ID»): слово [ПРОПУСК] есть ещё
    # и в итоговой строке прогона, и счёт по нему давал на единицу больше.
    check("обрыв: пройдены ВСЕ факты, а не только первый",
          log.count("[ПРОПУСК] ID") == len(FACTS),
          f"строк [ПРОПУСК] ID: {log.count('[ПРОПУСК] ID')} при {len(FACTS)} фактах")
    check("обрыв: ротации ключей не было", "Rotating" not in log, log[-300:])
    check("обрыв: ложного «Все ключи в лимите» нет", "Все ключи в лимите" not in log, log[-300:])
    check("обрыв: цена ограничена MAX_FAILS_PER_FACT на факт",
          calls == len(FACTS) * R.MAX_FAILS_PER_FACT,
          f"вызовов {calls}, ожидалось {len(FACTS) * R.MAX_FAILS_PER_FACT}")
    check("обрыв: успехом прогон НЕ отчитался",
          "BASE RECLASSIFIED SUCCESSFULLY" not in log, log[-200:])
    check("обрыв: в итоге сказано, сколько пропущено",
          f"{len(FACTS)} фактов пропущено" in log, log[-200:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [3]
print("\n[3] Несколько объектов в одном ответе разбираются ВСЕ")
for name, reply, want in [
    ("два объекта подряд",
     'префикс {"codes": ["1.2.1"]} суффикс {"codes": ["1.2.6"]}', "1.2.1, 1.2.6"),
    ("три объекта",
     '{"codes": ["1.1.1"]} и {"codes": ["2.2.2"]} и {"codes": ["3.3.1"]}',
     "1.1.1, 2.2.2, 3.3.1"),
    ("повтор кода в двух объектах сворачивается",
     '{"codes": ["1.1.1"]} {"codes": ["1.1.1"]}', "1.1.1"),
    ("образец формата из промпта отбрасывается цифровым фильтром",
     'Формат: {"codes": ["X.X.X", "Y.Y.Y"]}\nОтвет: {"codes": ["4.1.2"]}', "4.1.2"),
    ("объект в объекте не теряет внешние коды",
     '{"result": {"codes": ["5.1.1"]}, "note": "готово"}', "5.1.1"),
]:
    out, calls, log = classify(reply, f_id=12)
    check(f"reclass: {name}", out == want, f"got {out!r}, лог: {log.strip()[:120]}")

out, calls, log = classify_video('префикс {"codes": ["1.2.1"]} суффикс {"codes": ["1.2.6"]}')
check("videosi: два объекта дают два кода", out == "1.2.1, 1.2.6", f"got {out!r}")

# Порядок кодов обязан быть стабильным: раньше reclass отдавал list(set(...)),
# и один и тот же ответ модели давал разную строку в базе от прогона к прогону.
ORDERS = [["3.1", "1.1.1", "2.2", "4.1.2"], ["7.3.1", "6.2.1", "5.1.1", "1.1.4"],
          ["2.4.3", "3.3.2", "1.3.1", "6.1.1"], ["4.1.1", "2.1.4", "5.3.1", "7.2.1"],
          ["1.2.5", "3.1.2", "2.3.3", "6.3.1"]]
stable = 0
for codes in ORDERS:
    reply = '{"codes": ' + str(codes).replace("'", '"') + '}'
    out, _, _ = classify(reply, f_id=13)
    if out == ", ".join(codes):
        stable += 1
check("порядок кодов не переставляется (5 наборов из 4 кодов)", stable == len(ORDERS),
      f"сохранён порядок в {stable} из {len(ORDERS)} наборов")

# ---------------------------------------------------------------- [4]
print("\n[4] Сканер ОДИН на дерево: обе точки идут через distiller._iter_json_objects")
check("reclass использует функцию distiller, а не свою копию",
      R._iter_json_objects is D._iter_json_objects,
      f"got {getattr(R, '_iter_json_objects', None)!r}")
check("videosi использует разбор reclass, а не свою копию",
      V.extract_codes is R.extract_codes, f"got {V.extract_codes!r}")
check("жадной clean_json_raw в reclass больше нет", not hasattr(R, "clean_json_raw"))
check("жадной clean_json_raw в videosi больше нет", not hasattr(V, "clean_json_raw"))

seen = []


def spy(text):
    seen.append(text)
    return D._iter_json_objects(text)


with patched(R, _iter_json_objects=spy):
    out_r, _, _ = classify('{"codes": ["1.1.1"]}', f_id=14)
    calls_after_reclass = len(seen)
    out_v, _, _ = classify_video('{"codes": ["2.2.2"]}')
check("подмена сканера видна в reclass (разбор идёт через него)", calls_after_reclass >= 1,
      f"вызовов сканера {calls_after_reclass}")
check("подмена сканера видна и в videosi (тот же источник)", len(seen) > calls_after_reclass,
      f"вызовов сканера всего {len(seen)}")
check("после подмены разбор всё равно верный", (out_r, out_v) == ("1.1.1", "2.2.2"),
      f"got {(out_r, out_v)}")

# ---------------------------------------------------------------- [5]
print("\n[5] Пустой ответ и объект без кодов не хоронят факт кодом-заглушкой")
for name, reply in [("пустая строка", ""), ("None вместо текста", None),
                    ("объект без ключа codes", '{"category": "Эндодонтия"}'),
                    ("пустой массив codes", '{"codes": []}'),
                    ("codes словами", '{"codes": ["ЭНДОДОНТИЯ"]}')]:
    out, calls, log = classify(reply, f_id=15)
    check(f"reclass: {name} -> отказ, а не 10.1", out is None, f"got {out!r}")
    check(f"reclass: {name} назван в журнале", "[ОТКАЗ" in log, f"лог: {log.strip()[:140]!r}")

tmp = tempfile.mkdtemp(prefix="stomchat_jsonscan_empty_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db)
    log, calls = run_reclass(db, reply="")
    after = rows_of(db)
    check("пустые ответы: коды категорий целы",
          [r[1] for r in after] == [f[0] for f in FACTS], f"got {[r[1] for r in after]}")
    check("пустые ответы: заглушки 10.1 в базе нет",
          all("10.1" not in (r[1] or "") for r in after), f"got {[r[1] for r in after]}")
    check("пустые ответы: факты ждут следующего прогона (is_reclassified = 0)",
          all(r[3] == 0 for r in after), f"got {[r[3] for r in after]}")
    check("пустые ответы: успехом прогон не отчитался",
          "BASE RECLASSIFIED SUCCESSFULLY" not in log, log[-200:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [6]
print("\n[6] Настоящий лимит ключей: прогон останавливается и НЕ врёт об успехе")
tmp = tempfile.mkdtemp(prefix="stomchat_jsonscan_retry_")
try:
    db = os.path.join(tmp, "wiki.db")
    make_wiki(db)

    async def always_retry(client, content, f_id):
        return "RETRY"

    log, calls = run_reclass(db, classifier=always_retry, reply="не важно")
    after = rows_of(db)
    check("лимит ключей: прогон не завис", "[ПРОГОН ЗАВИС]" not in log, log[-200:])
    check("лимит ключей: сказано про лимит", "Все ключи в лимите" in log, log[-300:])
    check("лимит ключей: ни один код не перезаписан",
          [r[1] for r in after] == [f[0] for f in FACTS], f"got {[r[1] for r in after]}")
    check("лимит ключей: сказано, что прогон ОБОРВАН", "ПРОГОН ОБОРВАН" in log, log[-300:])
    check("лимит ключей: ложного BASE RECLASSIFIED SUCCESSFULLY нет",
          "BASE RECLASSIFIED SUCCESSFULLY" not in log, log[-300:])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------- [7]
print("\n[7] Боевые базы не тронуты")
if not LIVE:
    check("боевые базы рядом (иначе сверять нечего)", False, "ни одной базы не найдено")
for path, was in LIVE_BEFORE.items():
    check(f"{path} не изменился", md5(path) == was, "md5 разошёлся")

print(f"\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено:")
    for name in FAIL:
        print(f"  - {name}")
sys.exit(1 if FAIL else 0)
