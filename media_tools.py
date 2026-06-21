import asyncio
import base64
import io
import json
import os
import sys


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
    payload, error = await _run_tool("extract-frame", video_path, timeout)
    if error:
        return None
    return payload.get("path")


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
