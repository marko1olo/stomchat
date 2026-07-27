"""
«Замолчи, бот»: требование замолчать против обычной речи стоматолога.

Прежний детектор искал подстроки, и половина списка совпадала с клинической
лексикой:

  «удали»   живёт в «пришлось зуб удалить»
  «отвали»  в «коронка отваливается»
  «завали»  в «завалил стенку»
  «достали» в «досталась по наследству»
  «хватит»  в «не хватит места под имплант»

А слово «бот» подстрокой живёт в «работа», «суббота», «заботиться».

Замер по живому архиву на 107 316 сообщений:
  глобальная тишина на 4 часа срабатывала 68 раз, из них 66 ложно (97%) —
    например на «Моя ортопедическая работа. Спустя 4 года обострился Pt»;
  отпиской от личных сообщений считались 623 сообщения.
После правки: 1 и 8 соответственно.

Цена ошибки разная в двух местах. В группе — бот молчит четыре часа. В личке
отписка выключает ему право писать врачу навсегда.

Запуск: python test_silence_policy.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import assistant

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def silences_in_group(text):
    """Условие check_and_apply_silence: обращение к боту И требование замолчать."""
    return bool(assistant._BOT_REFERENCE_RE.search(text.lower())) and assistant.is_negative_feedback(text)


print("\n[1] Настоящие требования замолчать распознаются")
for phrase in ["отвали", "уймись", "замолчи", "замолчите", "заткнись", "заткни",
               "закройся", "помолчи", "не зуди", "закрой рот",
               "не пиши мне", "не пишите мне больше", "хватит спамить",
               "ты надоел", "бот надоел", "назойливый бот", "бот бесишь",
               "удали бота из чата", "выключите бота", "забаньте бота"]:
    check(f"«{phrase}»", assistant.is_negative_feedback(phrase) is True)

print("\n[2] Обычная речь стоматолога требованием НЕ считается")
for phrase in ["надо удалить 36 зуб", "пришлось зуб удалить", "уже удалился сгусток",
               "коронка отваливается второй раз", "винир отвалился",
               "завалил стенку на фронтале", "не хватит места под имплант",
               "достали файл из канала", "досталась по наследству",
               "обработал канал гипохлоритом", "эта работа надоела",
               "работа задолбала", "в субботу приём", "заботиться о пациенте",
               "обработка уступа", "робот-ассистент в клинике не нужен"]:
    check(f"«{phrase}»", assistant.is_negative_feedback(phrase) is False,
          "считается требованием замолчать")

print("\n[3] Слово «бот» ищется как начало слова")
for phrase in ["бот", "боту", "боты", "робот", "@stomchat_bot", "координатор", "душный"]:
    check(f"«{phrase}» — обращение к боту",
          bool(assistant._BOT_REFERENCE_RE.search(phrase)))
for phrase in ["работа", "работу сдал", "суббота", "заботиться", "обработать", "оборот"]:
    check(f"«{phrase}» — НЕ обращение к боту",
          not assistant._BOT_REFERENCE_RE.search(phrase),
          "цепляется за подстроку «бот»")

print("\n[4] Глобальная тишина требует и обращения, и требования")
check("клинический пост про работу и удаление зуба не глушит",
      not silences_in_group("Моя ортопедическая работа. Спустя 4 года пришлось зуб удалить"))
check("«бот, замолчи» глушит", silences_in_group("бот, замолчи"))
check("«заткнись бот» глушит", silences_in_group("заткнись бот"))
check("требование без обращения к боту не глушит группу",
      not silences_in_group("хватит спамить"),
      "в группе это может быть адресовано человеку")
check("обращение без требования не глушит",
      not silences_in_group("бот, что скажешь про уступ?"))

print("\n[5] Замер на живом архиве")
archive = "stomat_archive.db"
if os.path.exists(archive):
    tmpdir = tempfile.mkdtemp(prefix="stomchat_silence_")
    copy = os.path.join(tmpdir, "archive.db")
    shutil.copy(archive, copy)
    conn = sqlite3.connect(copy)
    rows = [t for (t,) in conn.execute(
        "SELECT text FROM archive_messages WHERE text IS NOT NULL AND text <> ''")]
    conn.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    silenced = [t for t in rows if silences_in_group(t)]
    opted_out = [t for t in rows if assistant.is_negative_feedback(t)]
    print(f"      проверено сообщений: {len(rows)}")
    print(f"      глушат бота: {len(silenced)} (было 68)")
    print(f"      считаются отпиской: {len(opted_out)} (было 623)")
    check("глобальная тишина срабатывает единицы раз, а не десятки",
          len(silenced) <= 5, f"got {len(silenced)}")
    check("отписка перестала ловить клинику",
          len(opted_out) <= 40, f"got {len(opted_out)}")
    check("доля срабатываний на архиве меньше 0.05%",
          len(silenced) / max(1, len(rows)) < 0.0005,
          f"got {len(silenced) / max(1, len(rows)) * 100:.4f}%")
else:
    check("архив недоступен — замер пропущен", True)

print("\n[6] Подстрочных списков в детекторе больше нет")
import io
source = io.open("assistant.py", encoding="utf-8").read()
detector = source.split("def is_negative_feedback", 1)[1].split("\n\n\n", 1)[0]
check("список подстрок stop_words убран", "stop_words = [" not in detector)
check("используются регулярные выражения с границей слова",
      "_SILENCE_DEMAND_RE" in detector and "_SILENCE_EXACT_RE" in detector)
check("одиночного «удали» в наборах нет",
      '"удали"' not in source.split("_SILENCE_PHRASES", 1)[0].split("_SILENCE_DEMANDS", 1)[-1],
      "одиночное «удали» вернулось в список")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
