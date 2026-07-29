"""
Голосовое в архиве перестало быть безликим «file».

deppd.py — единственный путь, которым 117 847 реплик врачей попали в
stomat_archive.db, и до этой проверки у него не было НИ ОДНОГО теста. Разбор типа
медиа стоял тремя ветками внутри цикла по Telegram:

    if message.photo:      m_type = 'photo'
    elif message.video:    m_type = 'video'
    elif message.document: m_type = 'file'

В Telethon ГОЛОСОВОЕ — это document с атрибутом audio(voice=True), и аудиофайл
тоже document. Значит и диктовка врача, и присланный рентген-документ, и mp3
записывались одним словом 'file', а mime не сохранялся вовсе.

Замер по живой базе, из-за которого это и нашлось: media_type принимает РОВНО три
значения — photo 14 754, video 804, file 444 — а типов voice и audio нет ВООБЩЕ.
Значит:
  * восстановить пост-фактум, сколько диктовок в архиве, НЕЛЬЗЯ. Любая оценка
    потерянных голосовых ограничена сверху числом 444, и точнее уже не будет;
  * те же 444 строки заперты условием has_media=1 AND vision_processed=0 — сито
    их не увидит никогда, а зрение к голосовому неприменимо в принципе.

Последствие для врача: он продиктовал клинический вопрос голосом, реплика попала в
архив как «файл», в вики не превратилась, и в дайджесте её нет. Ни он, ни коллеги
не узнают, что мысль потерялась.

Правка не восстанавливает прошлое — прошлое невосстановимо. Она делает так, что
СЛЕДУЮЩИЙ дамп различает voice и audio, и 444 перестанут быть слепым пятном.

Проверки поведенческие: гоняется настоящая media_kind на поддельных сообщениях.
Ни одного обращения к Telegram и ни одной записи в базу.

Запуск: python test_archive_dump.py
"""
import io
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import deppd  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class Msg:
    """Сообщение Telethon в объёме, который читают media_kind и is_garbage.

    Все виды заданы явно и по умолчанию пусты: в настоящем Message они тоже
    существуют как свойства и возвращают None, поэтому getattr-проверки в
    media_kind обязаны работать на этой подделке так же, как на живом объекте.
    """

    def __init__(self, **kw):
        self.sender_id = kw.pop("sender_id", 777)
        self.message = kw.pop("message", "")
        for kind in ("photo", "video", "voice", "audio", "document",
                     "sticker", "gif", "video_note", "web_preview"):
            setattr(self, kind, kw.pop(kind, None))
        assert not kw, f"неизвестные поля подделки: {sorted(kw)}"


D = object()  # непрозрачный «объект документа»: media_kind смотрит только на наличие

print("[1] Голосовое и аудио больше не сваливаются в 'file'")
# Голосовое в Telethon — это document ПЛЮС voice. Подделка повторяет это, иначе
# проверка проходила бы на входе, которого в жизни не бывает.
check("голосовое опознано как voice",
      deppd.media_kind(Msg(voice=D, document=D)) == 'voice',
      f"got {deppd.media_kind(Msg(voice=D, document=D))!r} — диктовка врача снова безлика")
check("аудиофайл опознан как audio",
      deppd.media_kind(Msg(audio=D, document=D)) == 'audio',
      f"got {deppd.media_kind(Msg(audio=D, document=D))!r}")
check("голосовое проверяется РАНЬШЕ аудио",
      deppd.media_kind(Msg(voice=D, audio=D, document=D)) == 'voice',
      "иначе диктовка станет 'audio' — тот же класс ошибки, только словом мягче")

print("\n[2] Прежние три типа не изменились: старые строки архива остаются читаемыми")
check("фото по-прежнему photo", deppd.media_kind(Msg(photo=D)) == 'photo')
check("видео по-прежнему video", deppd.media_kind(Msg(video=D)) == 'video')
check("документ по-прежнему file", deppd.media_kind(Msg(document=D)) == 'file',
      "рентген присылают документом — это норма клинического чата, ветка обязана жить")
check("текст без вложений — не медиа", deppd.media_kind(Msg(message="просто текст")) is None)
check("None не роняет разбор", deppd.media_kind(None) is None)

print("\n[3] Порядок ветвей: частное раньше общего")
# Фото Telegram может приехать и как photo, и как document (несжатое). Обе ветки
# должны давать 'photo', а не 'file': иначе снимок врача попадёт в слепые 444.
check("несжатое фото документом всё равно не 'voice' и не 'audio'",
      deppd.media_kind(Msg(photo=D, document=D)) == 'photo',
      "снимок ушёл бы в 'file' и застрял в запертых 444")
check("видео документом даёт video, а не file",
      deppd.media_kind(Msg(video=D, document=D)) == 'video')

print("\n[4] Мусор не доходит до разбора: его отсекает is_garbage выше")
check("стикер — мусор", deppd.is_garbage(Msg(sticker=D, message="")) is True)
check("гифка — мусор", deppd.is_garbage(Msg(gif=D, message="")) is True)
check("кружок — мусор", deppd.is_garbage(Msg(video_note=D, message="")) is True)
check("служебное без автора — мусор", deppd.is_garbage(Msg(sender_id=None)) is True)
check("голосовое БЕЗ текста мусором НЕ считается",
      deppd.is_garbage(Msg(voice=D, document=D, message="")) is False,
      "иначе диктовка врача не попадёт в архив вообще — это хуже безликого 'file'")
check("реплика с текстом не мусор",
      deppd.is_garbage(Msg(message="препарирование под винир")) is False)
check("короткое 'ок' — мусор", deppd.is_garbage(Msg(message="ок")) is True)

print("\n[5] has_media выводится из типа, а не задаётся отдельно")
# В deppd в базу уходит bool(m_type). Проверяем именно это соответствие: рассинхрон
# между has_media и media_type дал бы строку «медиа есть, типа нет», которую сито
# не разберёт, а зрение не возьмёт.
for kw, expect in ((dict(voice=D, document=D), True), (dict(photo=D), True),
                   (dict(document=D), True), (dict(message="текст"), False)):
    kind = deppd.media_kind(Msg(**kw))
    check(f"has_media={expect} при типе {kind!r}", bool(kind) is expect,
          f"got {bool(kind)}")

print("\n[6] Разбор вынесен из цикла по Telegram и виден тесту")
SRC = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "deppd.py"),
              encoding="utf-8-sig").read()
check("media_kind — функция модуля, а не три ветки внутри main",
      "def media_kind(" in SRC)
check("main зовёт media_kind, а не разбирает сам",
      "m_type = media_kind(message)" in SRC,
      "прежний разбор вернулся в цикл, где его нечем проверить")
check("прежних трёх ветвей в main не осталось",
      "if message.photo: m_type = 'photo'" not in SRC)
check("голосовое и аудио вообще упомянуты в разборе",
      "'voice'" in SRC and "'audio'" in SRC)

print("\n[7] Проверки выше ловят поломку")
check("разбор действительно смотрит на вид, а не возвращает одно слово",
      len({deppd.media_kind(Msg(voice=D, document=D)),
           deppd.media_kind(Msg(photo=D)),
           deppd.media_kind(Msg(document=D))}) == 3,
      "если бы всё сводилось к одному значению, проверки [1] и [2] ничего не значили")
check("подделка сообщения правда пуста по умолчанию",
      all(getattr(Msg(), k, None) is None for k in
          ("photo", "video", "voice", "audio", "document", "sticker", "gif", "video_note")),
      "иначе проверки шли бы на входе, где выставлено всё сразу")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
