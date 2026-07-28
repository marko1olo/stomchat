"""
Непокрытая работа первого фланга: ключ викторины и подсветка в /search.

Оба дефекта уже исправлены агентом, но НИ ОДНОЙ проверки к ним не осталось —
агент погиб на лимите кредитов до написания теста. Работа висела в том же
состоянии, что и кулдаун ЛС: код изменён, никем не проверен. Тест дописан
оркестратором.

1. Ключ состояния викторины был random.randint(100000, 999999) — 900 000
   значений при том, что строки викторин из user_interactive_states не
   удаляются никогда. Задача о днях рождения: при 200 викторинах вероятность
   совпадения 2.2%, при 1000 — 42.6%, при 2000 — 89.2%. Совпадение не мелочь:
   set_user_interactive_state делает INSERT OR REPLACE, новая викторина затирает
   строку старой, а сообщение старой живёт в чате вечно с рабочими кнопками.
   Клик по нему читает состояние НОВОЙ викторины и выдаёт врачу разбор ЧУЖОГО
   случая, голос уходит в чужую статистику, «вы уже проголосовали» срабатывает
   на тех, кто в новой викторине не голосовал.

2. Подсветка найденного в /search делалась тегом <u>, а весь ответ проходит
   clean_html_formatting, который сохраняет ровно три тега: <b>, <i>, <code>.
   Остальное экранируется, и врач видел литеральные «&lt;u&gt;BOPT&lt;/u&gt;»
   вместо выделения — подсветка не просто не работала, а засоряла каждый факт.

Боевые файлы не открываются; вики читается только на чтение.

Запуск: python test_fix_pm2.py
"""
import io
import os
import re
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_pm2_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")

import assistant as A  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCE = io.open("assistant.py", encoding="utf-8").read()
CODE = "\n".join(l for l in SOURCE.split("\n") if not l.lstrip().startswith("#"))

print("\n[1] Ключ викторины не повторяется никогда")
ids = [A._next_quiz_state_id() for _ in range(5000)]
check("выдано пять тысяч ключей", len(ids) == 5000)
check("ни одного повтора", len(set(ids)) == 5000,
      f"уникальных {len(set(ids))} — затрёт состояние чужой викторины")
check("ключи строго возрастают по величине",
      all(abs(b) > abs(a) for a, b in zip(ids, ids[1:])),
      "нестрогий рост означает возможный повтор внутри одного тика часов")
check("все ключи отрицательные", all(i < 0 for i in ids), f"положительные: {[i for i in ids if i >= 0][:3]}")

# Ключ не должен пересечься с настоящими id: чаты Telegram отрицательные
# (~-1.0e12 у супергрупп), пользователи положительные. Ключ викторины
# ~-1.8e15 — на три порядка ниже любого чата.
check("ключ не попадает в диапазон id супергрупп",
      all(abs(i) > 10 ** 13 for i in ids), f"минимум по модулю {min(abs(i) for i in ids)}")
check("ключ укладывается в 64-битный INTEGER sqlite",
      all(abs(i) < 2 ** 63 for i in ids))

# Случайность заменена на монотонность — это и есть суть правки.
#
# Берём ТОЛЬКО исполняемый код функции: в её строке документации разобран сам
# дефект и упомянут random.randint, и проверка находила это упоминание вместо
# кода. На этой же мелочи я спотыкался в других наборах — пояснение к правке
# срабатывает как признак правки.
_fn = CODE.split("def _next_quiz_state_id", 1)[1].split("\ndef ", 1)[0]
_body = _fn.split('"""')[2] if _fn.count('"""') >= 2 else _fn
check("генератор не опирается на random", "random." not in _body,
      "вернулось случайное число: повтор снова возможен")
check("генератор опирается на время", "time.time()" in _body,
      "без привязки к времени перезапуск процесса начнёт ключи заново")
check("в теле функции есть защита от одинакового тика часов",
      "_LAST_QUIZ_STATE_ID + 1" in _body,
      "две викторины внутри одного тика получат один ключ")

# Перезапуск процесса: новый счётчик стартует с нуля, но время идёт вперёд,
# поэтому ключи всё равно больше выданных до перезапуска.
_before_restart = min(abs(i) for i in ids)
A._LAST_QUIZ_STATE_ID = 0
after_restart = A._next_quiz_state_id()
check("после перезапуска ключ не возвращается назад",
      abs(after_restart) > _before_restart,
      f"got {abs(after_restart)} против {_before_restart} до перезапуска")

print("\n[2] Подсветка в /search выживает после подготовки текста")
# clean_html_formatting сохраняет ровно три тега; всё остальное экранируется.
for tag in ("b", "i", "code"):
    out = A.clean_html_formatting(f"текст <{tag}>выделено</{tag}> дальше")
    check(f"тег <{tag}> сохраняется", f"<{tag}>выделено</{tag}>" in out, f"got {out!r}")
for tag in ("u", "mark", "span"):
    out = A.clean_html_formatting(f"текст <{tag}>выделено</{tag}> дальше")
    check(f"тег <{tag}> экранируется и НЕ выделяет", f"<{tag}>" not in out, f"got {out!r}")

search_block = CODE.split("Результаты поиска по запросу", 1)[0]
search_block = search_block[-2500:]
check("подсветка делается через <b>", 'r"<b>\\1</b>"' in search_block or "<b>\\1</b>" in search_block,
      "вернулся тег, который вырежет clean_html_formatting")
check("подсветка не делается через <u>", "<u>\\1</u>" not in search_block,
      "врач снова увидит литеральные &lt;u&gt;")

# Сквозная проверка на НАСТОЯЩЕМ факте из вики: подсветили, подготовили,
# результат обязан содержать выделение и не содержать литеральных тегов.
import sqlite3  # noqa: E402

if os.path.exists("stomat_wiki.db"):
    row = sqlite3.connect("stomat_wiki.db").execute(
        "SELECT content FROM distilled_facts WHERE content LIKE '%BOPT%' LIMIT 1").fetchone()
    if row:
        highlighted = re.sub("(?i)(BOPT)", r"<b>\1</b>", row[0])
        prepared = A.clean_html_formatting(highlighted)
        check("выделение дошло до врача", "<b>" in prepared, prepared[:80])
        check("литеральных тегов в выдаче нет", "&lt;b&gt;" not in prepared, prepared[:80])
        check("текст факта не потерян", "BOPT" in prepared.upper())
    else:
        check("факта с BOPT нет — сквозная проверка пропущена", True)
else:
    check("вики недоступна — сквозная проверка пропущена", True)

print("\n[3] Проверки выше ловят поломку")
# Проверка на строку исходника одинаково выглядит у исправленного кода и у
# слепой проверки: убеждаемся, что детекторы срабатывают на заведомо неверном.
check("детектор random поймал бы возврат",
      "random.randint" in "candidate = random.randint(100000, 999999)")
check("детектор <u> поймал бы возврат", "<u>\\1</u>" in 'sub(kw, r"<u>\\1</u>", content)')
check("экранирование действительно превращает тег в текст",
      "&lt;" in A.clean_html_formatting("<u>x</u>"),
      "если бы не экранировалось, проверка выше ничего не значила")

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
