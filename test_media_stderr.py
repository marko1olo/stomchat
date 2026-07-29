"""
Зависший разбор снимка обязан оставить в журнале ПРИЧИНУ, а не только слово timeout.

Что здесь охраняется и чем это грозит врачу:

  * `communicate()` отдаёт собранное только по EOF. У зависшего ребёнка EOF не
    наступает, `wait_for` отменяет чтение, и всё вычитанное выбрасывается вместе
    с локальным списком кусков. Замерено на живом ребёнке: он записал и сбросил
    6 строк, в журнал ушло `stderr=''`. То есть ровно в том случае, который и
    надо диагностировать — снимок коллеги не разобран, и почему, не узнать, —
    объяснение терялось на 100 %. Поэтому stderr тянет ОТДЕЛЬНАЯ задача в
    кольцо, живущее СНАРУЖИ корутины: отмена уносит корутину, хвост остаётся.

  * `except Exception` в уборке после kill() НЕ ловит `CancelledError` — та не
    Exception. Внешняя отмена, пришедшая в эту секунду, уносила запись целиком:
    замерено 0 строк в журнале при шести написанных ребёнком. Отмена сюда
    долетает на каждой остановке бота и на альбоме, где внешний потолок разбора
    кончается раньше подготовки очередного снимка.

  * Кортеж ожиданий, собранный заранее, оставлял `proc.wait()` незапущенным,
    если первое ожидание унесла отмена: убитый ребёнок не пожат, а непожатые
    процессы на этой машине живут часами и едят ядра.

  * Кольцо ограничено: без предела мегабайт прогресса ffmpeg уезжал бы и в
    журнал, и в текст ошибки, который возвращается вызывающему.

Ребёнок здесь свой — маленький скрипт на sys.executable, который пишет в stderr
и засыпает. НЕ ffmpeg: ffmpeg в PATH на этой машине битый шим (108 КБ, exit 1,
пустой вывод), на нём ничего не докажешь. Подменяется ТОЛЬКО путь к дочернему
скрипту (`media_tools.__file__`); `create_subprocess_exec`, `wait_for`, `kill`,
трубы — настоящие.

Дочерние скрипты пишут в `sys.stderr.buffer` с явным utf-8: текстовый
`sys.stderr` на Windows кодирует в cp1251, и кириллица приезжает мусором мимо
любого якоря. Сетевых вызовов нет, боевые файлы не открываются.

Запуск: python test_media_stderr.py
"""
import asyncio
import gc
import logging
import os
import shutil
import sys
import tempfile
import warnings

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_stderr_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")

# Отмена оставляет транспорт убитого ребёнка неприбранным, и на Windows его
# чистит сборщик мусора: __del__ зовёт __repr__, тот дёргает fileno() закрытой
# трубы и сам поднимает ValueError. Это шум уборки ПОСЛЕ итога, а не результат
# проверки, — но выглядит он как падение, и глушить его надо здесь, а не в
# боевом коде. Всё остальное пропускаем дальше без изменений.
_real_unraisable = sys.unraisablehook


def _quiet_closed_pipe(item):
    if isinstance(item.exc_value, ValueError) and "closed pipe" in str(item.exc_value):
        return
    _real_unraisable(item)


sys.unraisablehook = _quiet_closed_pipe

import media_tools  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class Catcher(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level=logging.WARNING):
        return [r.getMessage() for r in self.records if r.levelno >= level]


catcher = Catcher()
logging.getLogger("media_tools").addHandler(catcher)
logging.getLogger("media_tools").setLevel(logging.INFO)


def write_child(name, source):
    """Дочерний скрипт во временном каталоге. Имя — переменная, не литерал."""
    path = os.path.join(_TMPDIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)
    return path


class as_child:
    """Подменяет ТОЛЬКО путь к дочернему скрипту, который запускает _run_tool."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.real = media_tools.__file__
        media_tools.__file__ = self.path
        return self

    def __exit__(self, *exc):
        media_tools.__file__ = self.real
        return False


# Пишет шесть строк, сбрасывает буфер и висит десять минут. Классический случай:
# ребёнок УСПЕЛ объяснить, что не так со снимком, и только потом встал.
LOUD_HANGER = write_child("loud_hanger.py", r'''
import sys, time
for i in range(6):
    sys.stderr.buffer.write(("CV2-OPEN-FAILED ЖАЛОБА строка %d\n" % i).encode("utf-8"))
sys.stderr.buffer.flush()
time.sleep(600)
''')

# Шестьдесят пронумерованных строк: проверяем, что в журнал уходит ХВОСТ.
CHATTY_HANGER = write_child("chatty_hanger.py", r'''
import sys, time
for i in range(60):
    sys.stderr.buffer.write(("СТРОКА-%02d\n" % i).encode("utf-8"))
sys.stderr.buffer.flush()
time.sleep(600)
''')

# Прогресс через \r без перевода строки — так сыплет ffmpeg. readline() на такой
# строке упёрся бы в лимит и поднял ValueError, а причина стоит ПОСЛЕ прогресса.
PROGRESS_HANGER = write_child("progress_hanger.py", r'''
import sys, time
for i in range(400):
    sys.stderr.buffer.write(("frame=%5d fps=25 q=28.0 size=%7dkB\r" % (i, i * 3)).encode("utf-8"))
sys.stderr.buffer.write("ОШИБКА-РАЗБОРА moov atom not found\n".encode("utf-8"))
sys.stderr.buffer.flush()
time.sleep(600)
''')

# Мегабайт болтовни и выход с ошибкой: кольцо обязано обрезать.
FLOOD = write_child("flood.py", r'''
import sys
line = ("ШУМ" * 20 + "\n").encode("utf-8")
for _ in range(20000):
    sys.stderr.buffer.write(line)
sys.stderr.buffer.write("ПОСЛЕДНЯЯ-ПРИЧИНА нет места на диске\n".encode("utf-8"))
sys.stderr.buffer.flush()
raise SystemExit(1)
''')

# Успех с жалобами: и валидный JSON в stdout, и ворчание в stderr.
NOISY_OK = write_child("noisy_ok.py", r'''
import sys
sys.stderr.buffer.write("PIL-WARNING ЖАЛОБА профиль icc битый\n".encode("utf-8"))
sys.stderr.buffer.flush()
sys.stdout.buffer.write(b'{"ok": true, "path": "\xd0\xba\xd0\xb0\xd0\xb4\xd1\x80.jpg"}')
sys.stdout.buffer.flush()
''')

# Ненулевой код, ничего в stdout: причина обязана ВЕРНУТЬСЯ вызывающему.
LOUD_FAIL = write_child("loud_fail.py", r'''
import sys
sys.stderr.buffer.write("ПРИЧИНА-ОТКАЗА cv2 отсутствует\n".encode("utf-8"))
sys.stderr.buffer.flush()
raise SystemExit(3)
''')

# Внук с той же трубой: kill() бьёт только ребёнка, EOF не придёт никогда.
GRANDCHILD = write_child("grandchild.py", r'''
import time
time.sleep(120)
''')
FORKER = write_child("forker.py", r'''
import subprocess, sys, time
sys.stderr.buffer.write("ЖАЛОБА-ВНУКА cv2 повис на файле\n".encode("utf-8"))
sys.stderr.buffer.flush()
subprocess.Popen([sys.executable, %r], stderr=sys.stderr, stdout=sys.stdout)
time.sleep(120)
''' % (GRANDCHILD,))


async def timed(coro):
    loop = asyncio.get_event_loop()
    started = loop.time()
    result = await coro
    return result, loop.time() - started


async def run():
    print("\n[1] Ребёнок написал в stderr и завис: убит, и его хвост в журнале")
    catcher.records.clear()
    with as_child(LOUD_HANGER):
        (payload, error), spent = await timed(
            media_tools._run_tool("prepare-image", "снимок.jpg", 1.0))
    logged = catcher.messages()
    check("payload не получен", payload is None, f"got {payload!r}")
    check("причина отказа — таймаут", error == "prepare-image timeout", f"got {error!r}")
    check("вернулся, а не висел десять минут вместе с ребёнком", spent < 20,
          f"прошло {spent:.2f} с")
    check("в журнале ровно одна запись об отказе", len(logged) == 1, f"в журнале: {logged}")
    check("запись называет действие и таймаут",
          any("prepare-image timeout" in m for m in logged), f"в журнале: {logged}")
    check("ХВОСТ STDERR РЕБЁНКА в журнале",
          any("CV2-OPEN-FAILED" in m for m in logged), f"в журнале: {logged}")
    check("в записи есть путь к снимку", any("снимок.jpg" in m for m in logged),
          f"в журнале: {logged}")
    check("stderr в записи не пустой",
          not any("stderr=''" in m for m in logged), f"в журнале: {logged}")
    check("ребёнок пожат за отсрочку, а не отпущен утёкшим",
          not any("пережил kill()" in m for m in logged), f"в журнале: {logged}")

    print("\n[2] В журнал уходит ХВОСТ stderr, и он ограничен по длине")
    catcher.records.clear()
    with as_child(CHATTY_HANGER):
        payload, error = await media_tools._run_tool("prepare-image", "болтун.jpg", 1.0)
    logged = catcher.messages()
    joined = " ".join(logged)
    check("последняя строка ребёнка попала в журнал", "СТРОКА-59" in joined,
          f"в журнале: {logged}")
    check("первая строка вытеснена хвостом", "СТРОКА-00" not in joined,
          "в журнал уехали все 60 строк, прогресс ffmpeg вытеснит причину")
    kept = [seg for seg in joined.split(" | ") if "СТРОКА-" in seg]
    check("строк в записи не больше предела", 0 < len(kept) <= 12,
          f"оставлено {len(kept)} строк")
    check("запись одна, а не шестьдесят", len(logged) == 1, f"записей {len(logged)}")

    print("\n[3] Прогресс через \\r не ломает чтение и не съедает причину")
    catcher.records.clear()
    with as_child(PROGRESS_HANGER):
        payload, error = await media_tools._run_tool("extract-frame", "видео.mp4", 1.0)
    logged = catcher.messages()
    check("отказ по таймауту", error == "extract-frame timeout", f"got {error!r}")
    check("причина ПОСЛЕ прогресса в журнале",
          any("ОШИБКА-РАЗБОРА" in m for m in logged), f"в журнале: {logged}")
    check("прогресс не поднял исключение на чтении",
          any("extract-frame timeout" in m for m in logged), f"в журнале: {logged}")

    print("\n[4] Внешняя отмена во время уборки после kill() не уносит запись")
    catcher.records.clear()
    with as_child(FORKER):
        task = asyncio.ensure_future(
            media_tools._run_tool("prepare-image", "проба.jpg", 1.0))
        # Таймаут инструмента 1 с, отсрочка после kill() = min(2, 1) = 1 с:
        # с 1.0 по 2.0 с вызов сидит именно в уборке.
        await asyncio.sleep(1.5)
        still_running = not task.done()
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
    logged = catcher.messages()
    check("отмена застала уборку после kill()", still_running,
          "вызов успел вернуться сам — проверка ничего не проверяет")
    check("отмена проброшена вызывающему", cancelled, "отмену проглотили")
    check("запись об отказе всё равно есть", len(logged) >= 1,
          "снимок врача исчез без единой строки в журнале")
    check("и в ней хвост stderr ребёнка",
          any("ЖАЛОБА-ВНУКА" in m for m in logged), f"в журнале: {logged}")

    print("\n[5] Уборка после kill() не оставляет незапущенных корутин")
    catcher.records.clear()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with as_child(FORKER):
            task = asyncio.ensure_future(
                media_tools._run_tool("prepare-image", "проба.jpg", 1.0))
            await asyncio.sleep(1.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        del task
        gc.collect()
        leaked = [str(w.message) for w in caught if "never awaited" in str(w.message)]
    check("ни одной незапущенной корутины", not leaked,
          f"утекло: {leaked} — убитый ребёнок остался непожатым")

    print("\n[6] Успешный путь дренажом не сломан")
    catcher.records.clear()
    with as_child(NOISY_OK):
        payload, error = await media_tools._run_tool("extract-frame", "хороший.mp4", 20)
    check("payload разобран", payload == {"ok": True, "path": "кадр.jpg"}, f"got {payload!r}")
    check("ошибки нет", error is None, f"got {error!r}")
    info = [r.getMessage() for r in catcher.records if r.levelno == logging.INFO]
    check("жалоба ребёнка при успехе записана как INFO",
          any("PIL-WARNING" in m for m in info), f"INFO: {info}")
    check("успех не выдаётся за отказ", not catcher.messages(logging.WARNING),
          f"предупреждения: {catcher.messages(logging.WARNING)}")

    print("\n[7] Ненулевой код без stdout: причина ВОЗВРАЩАЕТСЯ вызывающему")
    catcher.records.clear()
    with as_child(LOUD_FAIL):
        payload, error = await media_tools._run_tool("prepare-image", "нет_cv2.jpg", 20)
    check("payload не получен", payload is None, f"got {payload!r}")
    check("текст ребёнка вернулся вызывающему", "ПРИЧИНА-ОТКАЗА" in (error or ""),
          f"got {error!r}")
    check("отказ записан в журнал", any("код=3" in m for m in catcher.messages()),
          f"в журнале: {catcher.messages()}")

    print("\n[8] Мегабайт stderr обрезан и в журнале, и в тексте ошибки")
    catcher.records.clear()
    with as_child(FLOOD):
        payload, error = await media_tools._run_tool("prepare-image", "поток.jpg", 30)
    logged = catcher.messages()
    check("ошибка вернулась", payload is None and bool(error), f"got {error!r}")
    check("в ошибку не уехал весь мегабайт", len(error) <= 70000,
          f"в тексте ошибки {len(error)} символов")
    check("последняя жалоба ребёнка сохранилась", "ПОСЛЕДНЯЯ-ПРИЧИНА" in error,
          "обрезали не с той стороны: пропала самая свежая причина")
    check("запись в журнале не разрослась",
          logged and max(len(m) for m in logged) <= 2000,
          f"самая длинная запись {max((len(m) for m in logged), default=0)} символов")

    print("\n[9] Публичная точка входа отдаёт ошибку, а не исключение")
    catcher.records.clear()
    with as_child(LOUD_HANGER):
        data, error = await media_tools.prepare_image_for_analysis("живой.jpg", 1.0)
    check("данных нет", data is None, f"got {type(data)}")
    check("причина возвращена, вызывающий её разбирает сам",
          error == "prepare-image timeout", f"got {error!r}")
    check("хвост stderr всё равно в журнале",
          any("CV2-OPEN-FAILED" in m for m in catcher.messages()),
          f"в журнале: {catcher.messages()}")
    catcher.records.clear()
    with as_child(LOUD_HANGER):
        frame = await media_tools.extract_first_frame_async("живое.mp4", 1.0)
    check("кадр не получен", frame is None, f"got {frame!r}")
    check("и здесь причина в журнале",
          any("CV2-OPEN-FAILED" in m for m in catcher.messages()),
          f"в журнале: {catcher.messages()}")

    print("\n[10] Перерасход после таймаута вложен в отсрочку уборки")
    # Тайминги флакуют под нагрузкой, поэтому берём МИНИМУМ из трёх прогонов.
    spans = []
    for _ in range(3):
        catcher.records.clear()
        with as_child(LOUD_HANGER):
            _, spent = await timed(media_tools._run_tool("prepare-image", "бюджет.jpg", 1.0))
        spans.append(spent)
    best = min(spans)
    ceiling = 1.0 + 2 * media_tools._KILL_REAP_GRACE_SECONDS
    check("вызов уложился в свой таймаут плюс отсрочку", best <= ceiling + 0.5,
          f"минимум из трёх {best:.2f} с при потолке {ceiling:.2f} с")
    check("отсрочка вложена в бюджет вызова",
          media_tools._KILL_REAP_GRACE_SECONDS <= 45,
          "отсрочка больше подготовки снимка (45 с) — вылезет за потолок разбора")


asyncio.run(run())
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
