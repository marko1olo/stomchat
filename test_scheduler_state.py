"""
Состояние планировщика: единственное, что помнит, ушёл ли сегодняшний дайджест.

Пустой или повреждённый файл читается как «ничего не отправляли», и отчёт
уходит в общий чат ВТОРОЙ раз — то есть цена отказа здесь измеряется спамом
всему сообществу врачей.

Что было не так:
  * запись шла без fsync. os.replace атомарна, но содержимое временного файла
    может не дойти до диска раньше переименования: после сбоя питания на месте
    состояния оказывается пустой файл. Та же связка, что уже стояла в
    assistant.save_state, здесь отсутствовала;
  * резервной копии не было вовсе;
  * перехват при чтении не включал UnicodeDecodeError, и файл с невалидными
    байтами ронял цикл планировщика вместо отката на копию;
  * отметки о доставке копились с первого запуска — на момент правки 22 корзины
    с 22 мая, чистки не было.

Работает на копии в временном каталоге; боевой bot_state.json не открывается.

Запуск: python test_scheduler_state.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date, timedelta

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ORIGINAL_CWD = os.getcwd()
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_sched_")

sys.path.insert(0, _ORIGINAL_CWD)
os.chdir(_TMPDIR)

import main  # noqa: E402  импорт после chdir: пути состояния относительные

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def read_state():
    return json.load(io.open(main.SCHEDULER_STATE_PATH, encoding="utf-8"))


def reset(deliveries=None):
    for path in (main.SCHEDULER_STATE_PATH, main.SCHEDULER_STATE_BAK_PATH):
        if os.path.exists(path):
            os.remove(path)
    main.save_scheduler_state(date.today(), date.today(), deliveries or {})


print("\n[1] Запись доходит до диска и оставляет резервную копию")
reset()
check("файл создан", os.path.exists(main.SCHEDULER_STATE_PATH))
main.save_scheduler_state(date.today(), date.today(), {})
check("резервная копия появилась после второй записи",
      os.path.exists(main.SCHEDULER_STATE_BAK_PATH))
check("даты сохранены",
      read_state()["last_daily_date"] == date.today().isoformat())
source = io.open(os.path.join(_ORIGINAL_CWD, "main.py"), encoding="utf-8").read()
saver = source.split("def save_scheduler_state", 1)[1].split("\ndef ", 1)[0]
check("fsync вызывается перед заменой файла", "os.fsync" in saver, "запись без fsync")
check("временный файл убирается при отказе", "os.remove(temp_path)" in saver)
check("отказ записи логируется как опасный",
      "SCHEDULER STATE NOT SAVED" in saver, "молчаливый провал записи")

print("\n[2] Повреждённое состояние восстанавливается из копии")
reset({f"daily:{date.today().isoformat()}": {"-100:main": {"message_id": 7}}})
main.save_scheduler_state(date.today(), date.today(),
                          {f"daily:{date.today().isoformat()}": {"-100:main": {"message_id": 7}}})
for label, payload in (("обрезанный JSON", "{неполный"),
                       ("пустой файл", ""),
                       ("текст вместо JSON", "не json вообще")):
    io.open(main.SCHEDULER_STATE_PATH, "w", encoding="utf-8").write(payload)
    recovered = main.load_scheduler_state_raw()
    check(f"{label} -> восстановлено из копии", bool(recovered.get("deliveries")),
          f"got {recovered}")

open(main.SCHEDULER_STATE_PATH, "wb").write(b"\xee\xff\x00\x80binary")
recovered = main.load_scheduler_state_raw()
check("невалидные байты -> восстановлено, а не падение",
      bool(recovered.get("deliveries")), f"got {recovered}")

print("\n[3] Отсутствие обоих файлов не роняет планировщик")
for path in (main.SCHEDULER_STATE_PATH, main.SCHEDULER_STATE_BAK_PATH):
    if os.path.exists(path):
        os.remove(path)
check("сырое состояние пустое", main.load_scheduler_state_raw() == {})
check("даты возвращаются как None", main.load_scheduler_state() == (None, None))

print("\n[4] Отметки доставки чистятся по сроку хранения")
today = date.today()
main.save_scheduler_state(today, today, {
    f"daily:{today.isoformat()}": {"a": 1},
    f"daily:{(today - timedelta(days=5)).isoformat()}": {"b": 1},
    f"weekly:{(today - timedelta(days=29)).isoformat()}": {"c": 1},
    f"daily:{(today - timedelta(days=90)).isoformat()}": {"d": 1},
    f"daily:{(today - timedelta(days=400)).isoformat()}": {"e": 1},
})
kept = read_state()["deliveries"]
check("сегодняшняя отметка сохранена", f"daily:{today.isoformat()}" in kept)
check("пятидневной давности сохранена",
      f"daily:{(today - timedelta(days=5)).isoformat()}" in kept)
check("на границе срока сохранена",
      f"weekly:{(today - timedelta(days=29)).isoformat()}" in kept)
check("девяностодневная выброшена",
      f"daily:{(today - timedelta(days=90)).isoformat()}" not in kept)
check("годовалая выброшена",
      f"daily:{(today - timedelta(days=400)).isoformat()}" not in kept)

print("\n[5] Корзина с неразбираемым именем не теряется молча")
main.save_scheduler_state(today, today, {"битое:имя": {"x": 1},
                                         f"daily:{today.isoformat()}": {"y": 1}})
kept = read_state()["deliveries"]
check("неизвестный формат имени сохранён, а не удалён", "битое:имя" in kept,
      "чистка выбросила то, чего не поняла")

print("\n[6] Защита от повторной отправки работает по назначению")
reset()
main.mark_target_delivered("daily", today, "-1001820467444:main", today, today, 555)
sent = main.load_sent_targets("daily", today)
check("цель отмечена доставленной", "-1001820467444:main" in sent, f"got {sent}")
check("другая цель не считается доставленной",
      "-1003735006121:26" not in sent)
check("вчерашняя корзина не влияет на сегодня",
      main.load_sent_targets("daily", today - timedelta(days=1)) == set())
check("недельный отчёт отделён от дневного",
      main.load_sent_targets("weekly", today) == set())

main.mark_target_delivered("daily", today, "-1003735006121:26", today, today, 556)
sent = main.load_sent_targets("daily", today)
check("вторая цель добавилась, первая цела", sent == {"-1001820467444:main", "-1003735006121:26"},
      f"got {sent}")

os.chdir(_ORIGINAL_CWD)
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
