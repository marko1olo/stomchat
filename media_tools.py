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


async def _run_tool(action, file_path, timeout):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        os.path.abspath(__file__),
        action,
        os.fspath(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return None, f"{action} timeout"
    except asyncio.CancelledError:
        proc.kill()
        await proc.communicate()
        raise

    if proc.returncode != 0 and not stdout:
        err_text = stderr.decode("utf-8", errors="replace").strip()
        return None, err_text or f"{action} failed with code {proc.returncode}"

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"{action} invalid output: {exc}"

    if not payload.get("ok"):
        return None, payload.get("error") or f"{action} failed"
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
