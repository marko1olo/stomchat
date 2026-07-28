"""
Отбор медиа для разбора и общий каталог временных файлов.

Разведка агента (успела завершиться до его гибели на лимите кредитов) замерила
на подставных сообщениях: превью ссылки, гифка, видеозаметка-кружок и
видеостикер — все четыре принимались как медиа для разбора, скачивались, для
видео извлекался кадр, и всё уезжало в ПЛАТНЫЙ Vision как клинический снимок.

Причина в самих свойствах telethon, а не в невнимательности:
  * Message.photo при отсутствии MessageMediaPhoto лезет в web_preview.photo,
    то есть картинка превью любой ссылки становилась «снимком коллеги»;
  * Message.video — это «документ с DocumentAttributeVideo», под которое
    подходят и гифка, и кружок, и видеостикер.

Правило отбора было в ТРЁХ местах (живой обработчик, догоняющая синхронизация,
догон медиа), каждое решало само. Теперь одно: media_tools.clinical_media_kind.

Каталог временных файлов жил в ЧЕТЫРЁХ местах: main.py читал переменную
окружения, уборщик и голосовой путь ходили по литералу "temp_media", медиа в
личных сообщениях — тоже по литералу. При заданной STOMCHAT_MEDIA_TEMP_DIR
уборщик подметал пустой каталог, а файлы копились в настроенном вечно.

Ничего не скачивается и не разбирается: проверяются классификатор на подставных
сообщениях и согласованность констант.

Запуск: python test_media_selection.py
"""
import io
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ["STOMCHAT_LOG_PATH"] = os.path.join(tempfile.mkdtemp(prefix="stomchat_ms_"), "t.log")

import assistant as A  # noqa: E402
import main as M  # noqa: E402
import media_tools as MT  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class Msg:
    """Подставное сообщение: только те свойства, по которым идёт отбор."""

    FIELDS = ("photo", "video", "gif", "video_note", "sticker", "web_preview", "document")

    def __init__(self, **kw):
        for field in self.FIELDS:
            setattr(self, field, kw.get(field))


class Doc:
    def __init__(self, mime):
        self.mime_type = mime


print("\n[1] В разбор идёт только клинический материал")
CASES = [
    ("настоящее фото", Msg(photo=object()), "photo"),
    ("рентген документом png", Msg(document=Doc("image/png")), "photo"),
    ("КТ документом jpeg", Msg(document=Doc("image/jpeg")), "photo"),
    ("снимок документом tiff", Msg(document=Doc("image/tiff")), "photo"),
    ("настоящее видео", Msg(video=object()), "video"),
    ("превью ссылки", Msg(photo=object(), web_preview=object()), None),
    ("гифка", Msg(video=object(), gif=object()), None),
    ("видеозаметка-кружок", Msg(video=object(), video_note=object()), None),
    ("видеостикер", Msg(video=object(), sticker=object()), None),
    ("статический стикер", Msg(document=Doc("image/webp"), sticker=object()), None),
    ("pdf документом", Msg(document=Doc("application/pdf")), None),
    ("голосовое без медиа", Msg(), None),
]
for name, message, expect in CASES:
    got = MT.clinical_media_kind(message)
    check(f"{name} -> {expect!r}", got == expect, f"got {got!r}")
check("None вместо сообщения не роняет", MT.clinical_media_kind(None) is None)

print("\n[2] Рентген документом не сломан отсевом")
# Это норма клинического чата: снимок присылают документом, чтобы Telegram его
# не пережал. Отсев построен на атрибутах, а не на mime, поэтому путь цел.
for mime in ("image/png", "image/jpeg", "image/tiff", "image/bmp"):
    check(f"{mime} остаётся снимком", MT.clinical_media_kind(Msg(document=Doc(mime))) == "photo")
check("но webp со стикером — нет",
      MT.clinical_media_kind(Msg(document=Doc("image/webp"), sticker=object())) is None)

print("\n[3] Правило отбора одно на все места пути")
MAIN_CODE = "\n".join(l for l in io.open("main.py", encoding="utf-8").read().split("\n")
                      if not l.lstrip().startswith("#"))
check("живой обработчик и догон синхронизации зовут общее правило",
      MAIN_CODE.count("clinical_media_kind(") >= 3,
      f"мест: {MAIN_CODE.count('clinical_media_kind(')}, а решений о медиа три")
check("прежнего решения «photo or video» в отборе не осталось",
      "message.photo is not None\n                or message.video is not None" not in MAIN_CODE,
      "какое-то место снова решает само и пустит превью ссылки в Vision")

print("\n[4] Каталог временных файлов один на проект")
check("каталог объявлен в media_tools", isinstance(MT.MEDIA_TEMP_DIR, str) and MT.MEDIA_TEMP_DIR)
check("main смотрит в тот же каталог", M.MEDIA_TEMP_DIR == MT.MEDIA_TEMP_DIR,
      f"{M.MEDIA_TEMP_DIR!r} против {MT.MEDIA_TEMP_DIR!r}")
check("уборщик не ходит по литералу",
      'os.path.isdir("temp_media")' not in MAIN_CODE,
      "уборщик подметает не тот каталог, файлы копятся вечно")
check("голосовой путь не ходит по литералу",
      'os.makedirs("temp_media"' not in MAIN_CODE)
ASSIST_CODE = "\n".join(l for l in io.open("assistant.py", encoding="utf-8").read().split("\n")
                        if not l.lstrip().startswith("#"))
check("медиа в личных сообщениях не ходит по литералу",
      'os.makedirs("temp_media"' not in ASSIST_CODE and '"temp_media/"' not in ASSIST_CODE,
      "снимки из ЛС ложатся туда, куда уборщик не заходит")
check("переменная окружения перекрывает каталог",
      'os.getenv("STOMCHAT_MEDIA_TEMP_DIR"' in
      io.open("media_tools.py", encoding="utf-8").read())

print("\n[5] Догон медиа не врёт о поставленном")
recovery = MAIN_CODE.split("async def recover_pending_media_analysis", 1)[1].split("\nasync def ", 1)[0]
check("догон ставит пачкой (переполнение штатно)", "bulk=True" in recovery,
      "штатное переполнение снова будет писать ERROR")
check("счётчик растёт только при успешной постановке",
      "if await enqueue_media_analysis(" in recovery,
      "сводная строка врёт при полной очереди")

print("\n[6] Проверки выше ловят поломку")
check("классификатор различает фото и видео",
      MT.clinical_media_kind(Msg(photo=object())) != MT.clinical_media_kind(Msg(video=object())))
check("классификатор не пропускает всё подряд",
      MT.clinical_media_kind(Msg(gif=object(), video=object())) is None,
      "если бы пропускал, вся секция [1] ничего не значила")
check("детектор литерала поймал бы возврат",
      'os.path.isdir("temp_media")' in 'if not os.path.isdir("temp_media"):')

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
