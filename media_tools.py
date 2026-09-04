import asyncio
import base64
import io
import json
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Каталог для временных файлов медиа — ОДИН на весь проект.
#
# Он жил в трёх местах разными способами: main.py читал переменную окружения,
# уборщик временных файлов и голосовой путь ходили по литералу "temp_media", и
# путь медиа в личных сообщениях (assistant.py) тоже по литералу. При заданной
# STOMCHAT_MEDIA_TEMP_DIR уборщик подметал пустой каталог, а файлы копились в
# настроенном — вечно, вместе с обрывками скачиваний.
#
# Место выбрано здесь, а не в main.py: media_tools импортируют и main, и
# assistant, а обратный импорт был бы круговым.
MEDIA_TEMP_DIR = os.getenv("STOMCHAT_MEDIA_TEMP_DIR", "temp_media")


def _json_exit(payload, code=0):
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace"))
    sys.stdout.buffer.flush()
    raise SystemExit(code)


def _prepare_image_sync(file_path):
    from PIL import Image

    img = None
    try:
        Image.MAX_IMAGE_PIXELS = 49_000_000
        if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) <= 0:
            return None, "Пустой файл"

        try:
            with Image.open(file_path) as source:
                source.load()
                if source.mode != "RGB":
                    img = source.convert("RGB")
                else:
                    img = source.copy()
        except Exception as exc:
            return None, f"Невалидный файл изображения: {exc}"

        max_size = 1000
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        with io.BytesIO() as buffer:
            img.save(buffer, format="JPEG", quality=70, optimize=True)
            return buffer.getvalue(), None
    except Exception as exc:
        return None, f"Ошибка CPU обработки: {exc}"
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


def _extract_frame_sync(video_path):
    import cv2

    vid_cap = None
    try:
        vid_cap = cv2.VideoCapture(video_path)
        success, image = vid_cap.read()
        if not success:
            return None, "Не удалось прочитать первый кадр"

        frame_path = video_path + ".jpg"
        if not cv2.imwrite(frame_path, image):
            return None, "Не удалось сохранить первый кадр"
        return frame_path, None
    except Exception as exc:
        return None, f"Ошибка извлечения кадра: {exc}"
    finally:
        if vid_cap is not None:
            vid_cap.release()


# Сколько stderr ребёнка держать под рукой. Столько же, сколько буферизует сам
# StreamReader по умолчанию: меньше — и мы потеряли бы то, что здоровый ребёнок
# успел сказать до backpressure, больше — незачем. Главное, что теперь это
# ПОТОЛОК: раньше communicate() копил весь stderr без границы.
_STDERR_KEEP_BYTES = 65536
# Сколько последних строк stderr уходит в журнал. Трассировка Python на три кадра
# — восемь строк (заголовок, 3x2, строка исключения); двенадцать берут её целиком
# плюс предупреждение cv2/PIL над ней. Весь stderr в журнал не влезет: ffmpeg
# сыплет прогрессом, и он вытеснит из кольца сам себя, а не причину отказа.
_STDERR_TAIL_LINES = 12
# Отсрочка на «дожать хвост и похоронить ребёнка» ПОСЛЕ kill(). Ограничена
# бюджетом самого вызова (см. _settle_after_kill), чтобы не вылезти за дедлайн
# вызывающего: у извлечения кадра внешнего потолка нет вообще.
_KILL_REAP_GRACE_SECONDS = 2.0


async def _drain_stderr(stream, sink):
    """Тянет stderr ребёнка ПО ХОДУ дела, оставляя в sink последние байты.

    Смысл в том, что sink живёт СНАРУЖИ этой корутины: отмена по таймауту
    уносит корутину, а собранный хвост остаётся. Иначе объяснение зависшего
    разбора снимка теряется целиком — замерено, было 0 байт из 6 строк.

    Читаем кусками, а не readline(): ffmpeg отбивает прогресс через \\r без
    перевода строки, и readline() на такой строке падает ValueError по лимиту.
    """
    while True:
        try:
            chunk = await stream.read(4096)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Труба порвалась. То, что уже в sink, ценнее исключения.
            return
        if not chunk:
            return
        sink += chunk
        if len(sink) > _STDERR_KEEP_BYTES:
            del sink[:-_STDERR_KEEP_BYTES]


def _kill_quietly(proc):
    """Убить ребёнка, не дав самому kill() отобрать у нас запись в журнале.

    kill() уже мёртвого ребёнка поднимает ProcessLookupError. Гонка узкая (мы
    попали в таймаут ровно в момент выхода), но исключение отсюда улетело бы
    вызывающему вместо строки «timeout ... stderr=...» — то есть съело бы ровно
    ту диагностику, ради которой всё это и делается.
    """
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception as exc:
        logger.warning("не удалось убить ребёнка pid=%s: %s", proc.pid, exc)


async def _settle_after_kill(proc, drain, timeout):
    """Дожать хвост stderr и дождаться убитого ребёнка — но не бесконечно.

    Прежде здесь стоял второй `await proc.communicate()` без потолка. Он висел
    НАВСЕГДА, если убитый ребёнок оставил внука с той же трубой: EOF приходит,
    когда трубу отпустит последний держатель, а kill() бьёт только ребёнка.
    Замерено: вызов не вернулся за 12 с при таймауте инструмента 1 с, и в
    журнале не было ни строки. Воркер медиа один — вставал весь разбор снимков
    до перезапуска бота, молча.

    Отсрочка вложена в бюджет вызова: min(2 с, timeout). Ждать больше нечего —
    хвост stderr уже собран дренажом, здесь мы лишь добираем последний вздох.
    """
    grace = min(_KILL_REAP_GRACE_SECONDS, max(0.1, timeout))
    # Ожидания создаём ПООЧЕРЁДНО, а не готовым кортежем. Кортеж вычисляется
    # целиком заранее, и внешняя отмена на первом ожидании уносила нас из цикла
    # с так и не запущенным proc.wait(): интерпретатор ругался «coroutine
    # Process.wait was never awaited», а убитый ребёнок оставался непожатым — на
    # этой машине непожатые процессы измеримо живут часами и едят ядра.
    for make_waitable in (lambda: asyncio.shield(drain), proc.wait):
        try:
            await asyncio.wait_for(make_waitable(), timeout=grace)
        except Exception:
            # Ни таймаут добора, ни порванная труба не должны мешать записи в
            # журнал: она идёт следующей строкой и она здесь главная.
            pass
    if proc.returncode is None:
        # Ребёнок пережил kill() — это утёкший процесс, а на этой машине
        # утёкшие процессы измеримо съедают ядра часами. Пусть будет в журнале.
        logger.warning("ребёнок пережил kill() pid=%s, отпущен как утёкший", proc.pid)


def _tail_lines(raw, lines=_STDERR_TAIL_LINES):
    """Последние `lines` непустых строк stderr, склеенных в ОДНУ запись журнала.

    Многострочная запись в журнале не ищется потом ни одним rg по времени, а
    искать её будут именно так — «что бот сказал про этот снимок».
    """
    text = bytes(raw or b"").decode("utf-8", errors="replace")
    parts = [p.strip() for p in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return " | ".join([p for p in parts if p][-lines:])[-1200:]


async def _run_tool(action, file_path, timeout):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        os.path.abspath(__file__),
        action,
        os.fspath(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # stderr тянем ОТДЕЛЬНОЙ задачей в кольцо снаружи, а не через communicate().
    # communicate() отдаёт собранное только по EOF: у зависшего ребёнка EOF не
    # наступает, wait_for отменяет чтение, и всё вычитанное выбрасывается
    # (StreamReader.read(-1) копит куски в локальном списке). Замерено: ребёнок
    # написал и сбросил 6 строк, в журнал попало stderr=''. То есть ровно в том
    # случае, который и надо диагностировать, причина отказа терялась целиком.
    err_tail = bytearray()
    drain = asyncio.ensure_future(_drain_stderr(proc.stderr, err_tail))
    # Всё, что ребёнок сказал в stderr, до этой правки выбрасывалось на трёх из
    # четырёх путей отказа, и ни один путь не писал в журнал сам — строка отказа
    # только возвращалась вызывающему, а тот её местами теряет. Итог: на весь
    # bot.log ни одной записи про prepare-image или extract-frame, то есть провал
    # разбора снимка врача был полностью невидим. Логируем ЗДЕСЬ, чтобы потеря
    # не зависела от дисциплины вызывающего. Поток управления при этом не
    # меняется: наблюдаемость не имеет права менять поведение.
    def _tail(raw):
        return (raw or b"").decode("utf-8", errors="replace").strip()[-400:]

    async def _read_child():
        # То же, что делал communicate(): stdout и stderr читаются
        # ОДНОВРЕМЕННО, иначе ребёнок упёрся бы в полную трубу и встал.
        out = await proc.stdout.read()
        await asyncio.shield(drain)
        await proc.wait()
        return out

    try:
        try:
            stdout = await asyncio.wait_for(_read_child(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_quietly(proc)
            try:
                await _settle_after_kill(proc, drain, timeout)
            finally:
                # Запись уходит ДАЖЕ если отсрочку унесла внешняя отмена: её
                # `except Exception` внутри не ловит, CancelledError — не
                # Exception. Без этого finally замерено 0 строк в журнале, то
                # есть снимок врача пропадал совсем без следа — а отмена сюда
                # долетает на каждой остановке бота и на альбоме, где внешний
                # потолок разбора кончается раньше подготовки пятого снимка.
                logger.warning("%s timeout после %s с pid=%s path=%s stderr=%r",
                               action, timeout, proc.pid, file_path, _tail_lines(err_tail))
            return None, f"{action} timeout"
        except asyncio.CancelledError:
            _kill_quietly(proc)
            # Пишем ДО уборки: хвост stderr уже собран дренажом, а любое ожидание
            # на пути отмены может не вернуться — тогда отменённый разбор снимка
            # снова стал бы неотличим от несуществующего.
            logger.warning("%s cancelled pid=%s path=%s stderr=%r",
                           action, proc.pid, file_path, _tail_lines(err_tail))
            raise
        # Дальше по коду stderr читается как байты — отдаём ему хвост из кольца.
        stderr = bytes(err_tail)
        return _parse_child_output(action, file_path, proc, stdout, stderr, _tail)
    finally:
        # Дренаж — задача, а не корутина: без отмены он остался бы висеть на
        # мёртвой трубе и всплыл бы «Task was destroyed but it is pending».
        if not drain.done():
            drain.cancel()


def _parse_child_output(action, file_path, proc, stdout, stderr, _tail):
    if proc.returncode != 0 and not stdout:
        err_text = stderr.decode("utf-8", errors="replace").strip()
        logger.warning("%s код=%s path=%s stderr=%r",
                       action, proc.returncode, file_path, _tail(stderr))
        return None, err_text or f"{action} failed with code {proc.returncode}"

    if proc.returncode != 0:
        # Вышел с ошибкой, но что-то написал в stdout: разбираем дальше, как
        # раньше, однако жалобу ребёнка больше не теряем.
        logger.warning("%s код=%s, но вывод есть path=%s stderr=%r",
                       action, proc.returncode, file_path, _tail(stderr))

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("%s нечитаемый вывод path=%s: %s stdout=%r stderr=%r",
                       action, file_path, exc, _tail(stdout), _tail(stderr))
        return None, f"{action} invalid output: {exc}"

    if not payload.get("ok"):
        logger.warning("%s отказался path=%s reason=%s stderr=%r", action, file_path,
                       payload.get("error") or payload.get("reason"), _tail(stderr))
        return None, payload.get("error") or f"{action} failed"
    if stderr:
        # Успех с жалобами: предупреждения cv2/PIL про битый файл видеть полезно.
        logger.info("%s ok, но ребёнок жаловался path=%s stderr=%r",
                    action, file_path, _tail(stderr))
    return payload, None


async def prepare_image_for_analysis(file_path, timeout):
    payload, error = await _run_tool("prepare-image", file_path, timeout)
    if error:
        return None, error
    try:
        return base64.b64decode(payload["data_b64"]), None
    except Exception as exc:
        return None, f"prepare-image decode failed: {exc}"


async def extract_first_frame_async(video_path, timeout):
    """
    Первый кадр видео или None.

    Причина отказа ПИШЕТСЯ В ЖУРНАЛ, а не возвращается: вызывающий
    (main.py, ветка подготовки медиа) ожидает один путь и молча пропускает
    видео, если пришло None. До этого не логировалось ничего, и отличить
    битый файл от таймаута, от отсутствующего cv2 было нельзя вообще — при
    этом соседняя prepare_image_for_analysis причину как раз возвращает.
    Контракт не меняем: подпись используется в чужом файле.
    """
    payload, error = await _run_tool("extract-frame", video_path, timeout)
    if error:
        logger.warning("extract-frame failed path=%s: %s", video_path, error)
        return None
    frame_path = payload.get("path")
    if not frame_path:
        # Ребёнок отчитался успехом, но пути не дал: молча вернуть None здесь
        # означало бы потерять видео без единого следа.
        logger.warning("extract-frame returned ok without path path=%s payload=%s",
                       video_path, payload)
        return None
    return frame_path


def image_document(message):
    """
    Снимок, присланный ДОКУМЕНТОМ, а не фотографией. Возвращает документ или None.

    Рентген, КТ и внутриротовые снимки шлют именно так — чтобы Telegram не
    пережал изображение. Для клинического чата это не редкий случай, а норма.
    Учитывались только photo/video, поэтому такой снимок не доходил ни до
    анализа, ни до базы: has_media=False, описания нет, и от разбора случая в
    дайджесте оставалась пустая строка.

    Стикеры отсекаются отдельно: статический стикер Telegram — это документ с
    mime image/webp, и без этой проверки каждый стикер уезжал бы в vision.
    """
    if getattr(message, "sticker", None) is not None:
        return None
    document = getattr(message, "document", None)
    if document is None:
        return None
    mime = (getattr(document, "mime_type", "") or "").lower()
    return document if mime.startswith("image/") else None


def clinical_media_kind(message):
    """
    Что из сообщения имеет смысл вести в разбор: "photo", "video" или None.

    Правило одно на все три места пути (живой обработчик, догоняющая
    синхронизация, постановка в очередь): раньше каждое решало само через
    `message.photo or message.video`, и это пускало в ПЛАТНЫЙ Vision то, что
    клиническим снимком не является.

    Почему так выходило — свойства telethon шире, чем кажутся:
      * Message.photo при отсутствии MessageMediaPhoto лезет в
        web_preview.photo, то есть КАРТИНКА ПРЕВЬЮ любой ссылки становилась
        «снимком коллеги»;
      * Message.video — это «документ с DocumentAttributeVideo», под которое
        подходят и гифка (DocumentAttributeAnimated), и кружок-видеозаметка
        (round_message), и видеостикер (DocumentAttributeSticker).

    Замер разведки на подставных сообщениях: превью ссылки, гифка,
    видеозаметка и видеостикер — все четыре принимались как медиа для разбора,
    скачивались, для видео извлекался кадр, и всё уезжало в Vision.

    Рентген и КТ присылают ДОКУМЕНТОМ с mime image/*, и этот путь обязан
    остаться рабочим: он и есть норма для клинического чата (см.
    image_document). Поэтому отсев построен на типе медиа и атрибутах
    документа, а не на mime.
    """
    if message is None:
        return None
    # Стикеры, кружки и гифки — не клинический материал ни в каком случае.
    # Видеостикер попадает и под sticker, и под video, поэтому проверяем раньше.
    for junk in ("sticker", "video_note", "gif"):
        if getattr(message, junk, None) is not None:
            return None

    # Превью ссылки: photo есть, но принадлежит веб-странице, а не сообщению.
    if getattr(message, "web_preview", None) is not None:
        return None

    if getattr(message, "photo", None) is not None:
        return "photo"
    if image_document(message) is not None:
        return "photo"
    if getattr(message, "video", None) is not None:
        return "video"
    return None


def upload_clinical_image_sync(file_path):
    """
    Загружает клиническое изображение на постоянный надежный CDN (Freeimage / iili.io),
    возвращающий прямой URL image/jpeg без блокировки хотлинка и без срока сгорания.
    """
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "rb") as f:
            img_bytes = f.read()
        if not img_bytes:
            return None

        api_key = os.getenv("FREEIMAGE_API_KEY", "6d207e02198a847aa98d0a2a901485a5")
        import requests
        r = requests.post(
            "https://freeimage.host/api/1/upload",
            data={
                "key": api_key,
                "action": "upload",
                "format": "json"
            },
            files={"source": ("clinical_case.jpg", img_bytes, "image/jpeg")},
            timeout=20
        )

        if r.status_code == 200:
            data = r.json()
            direct_url = data.get("image", {}).get("url")
            return direct_url
    except Exception as e:
        logger.warning(f"Failed to upload clinical image {file_path}: {e}")
    return None


async def upload_clinical_image_async(file_path):
    return await asyncio.to_thread(upload_clinical_image_sync, file_path)


def _main():
    if len(sys.argv) != 3:
        _json_exit({"ok": False, "error": "usage: media_tools.py <action> <path>"}, 2)

    action, file_path = sys.argv[1], sys.argv[2]
    if action == "prepare-image":
        data, error = _prepare_image_sync(file_path)
        if error:
            _json_exit({"ok": False, "error": error}, 1)
        _json_exit({"ok": True, "data_b64": base64.b64encode(data).decode("ascii")})

    if action == "extract-frame":
        path, error = _extract_frame_sync(file_path)
        if error:
            _json_exit({"ok": False, "error": error}, 1)
        _json_exit({"ok": True, "path": path})

    _json_exit({"ok": False, "error": f"unknown action: {action}"}, 2)


if __name__ == "__main__":
    _main()
