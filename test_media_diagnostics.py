"""
Медиа-путь: отказ подготовки снимка или кадра не должен исчезать без следа.

media_tools.extract_first_frame_async возвращала None и ТЕРЯЛА причину.
Вызывающий (main.py, ветка подготовки медиа) при None молча пропускает видео,
поэтому отличить битый файл от таймаута, от отсутствующего cv2 было нельзя
вообще: в журнале не оставалось ни строки. Соседняя
prepare_image_for_analysis причину как раз возвращает — асимметрия на одном
и том же пути.

Подпись функции не менялась: она используется в чужом файле, а причина теперь
идёт в журнал модуля.

Настоящие подпроцессы здесь запускаются, но только на локальных файлах: ни
одного сетевого вызова, боевые файлы не открываются.

Запуск: python test_media_diagnostics.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_media_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")

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


def write_file(name, data=b"\x00\x01\x02broken"):
    path = os.path.join(_TMPDIR, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


async def run():
    print("\n[1] Битое видео: кадр не извлечён, причина в журнале")
    catcher.records.clear()
    broken = write_file("broken.mp4")
    frame = await media_tools.extract_first_frame_async(broken, timeout=60)
    check("кадр не получен", frame is None, f"got {frame!r}")
    logged = catcher.messages()
    check("причина отказа попала в журнал", any("extract-frame" in m for m in logged),
          f"в журнале: {logged}")
    check("в записи есть путь к файлу", any("broken.mp4" in m for m in logged),
          f"в журнале: {logged}")

    print("\n[2] Отсутствующий файл тоже оставляет след")
    catcher.records.clear()
    frame = await media_tools.extract_first_frame_async(
        os.path.join(_TMPDIR, "нет_такого.mp4"), timeout=60)
    check("кадр не получен", frame is None)
    check("причина записана", len(catcher.messages()) >= 1, "тишина в журнале")

    print("\n[3] Ребёнок отчитался успехом без пути — это тоже отказ со следом")
    catcher.records.clear()
    real_tool = media_tools._run_tool

    async def ok_without_path(action, file_path, timeout):
        return {"ok": True}, None

    media_tools._run_tool = ok_without_path
    try:
        frame = await media_tools.extract_first_frame_async("любой.mp4", timeout=5)
    finally:
        media_tools._run_tool = real_tool
    check("пустой путь трактуется как отказ", frame is None, f"got {frame!r}")
    check("отказ без пути записан", any("without path" in m for m in catcher.messages()),
          f"в журнале: {catcher.messages()}")

    print("\n[4] Успешный путь журнал предупреждениями не засоряет")
    catcher.records.clear()

    async def ok_with_path(action, file_path, timeout):
        return {"ok": True, "path": "кадр.jpg"}, None

    media_tools._run_tool = ok_with_path
    try:
        frame = await media_tools.extract_first_frame_async("любой.mp4", timeout=5)
    finally:
        media_tools._run_tool = real_tool
    check("путь возвращён как есть", frame == "кадр.jpg", f"got {frame!r}")
    check("предупреждений нет", not catcher.messages(), f"лишнее: {catcher.messages()}")

    print("\n[5] Подготовка снимка причину по-прежнему ВОЗВРАЩАЕТ")
    # Здесь контракт другой и менять его нельзя: вызывающий разбирает ошибку сам.
    data, error = await media_tools.prepare_image_for_analysis(
        write_file("broken.jpg"), timeout=60)
    check("данных нет", data is None)
    check("причина возвращена вызывающему", bool(error), f"got {error!r}")

    print("\n[6] Настоящий снимок проходит подготовку целиком")
    try:
        from PIL import Image
        source = os.path.join(_TMPDIR, "real.png")
        Image.new("RGB", (1400, 900), (200, 40, 40)).save(source)
        data, error = await media_tools.prepare_image_for_analysis(source, timeout=120)
        check("снимок подготовлен", data is not None and error is None, f"error={error!r}")
        if data:
            check("на выходе JPEG", data[:2] == b"\xff\xd8", f"первые байты {data[:4]!r}")
            check("длинная сторона уменьшена до предела",
                  len(data) < os.path.getsize(source),
                  f"{len(data)} против {os.path.getsize(source)} исходных")
    except ImportError:
        check("PIL недоступен — проверка пропущена", True)

    print("\n[7] Стикер не уходит в разбор как снимок")
    class Msg:
        def __init__(self, sticker=None, document=None):
            self.sticker = sticker
            self.document = document

    class Doc:
        def __init__(self, mime):
            self.mime_type = mime

    check("статический стикер отсечён",
          media_tools.image_document(Msg(sticker=object(), document=Doc("image/webp"))) is None)
    check("снимок документом распознан",
          media_tools.image_document(Msg(document=Doc("image/png"))) is not None)
    check("pdf снимком не считается",
          media_tools.image_document(Msg(document=Doc("application/pdf"))) is None)
    check("сообщение без документа не роняет", media_tools.image_document(Msg()) is None)


asyncio.run(run())
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
