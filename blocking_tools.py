import asyncio
import json
import os
import sys
import time


def _json_exit(payload, code=0):
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    raise SystemExit(code)


def _read_stdin_json():
    raw = sys.stdin.buffer.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", errors="replace"))


def _create_telegraph_page_sync(title, html_content):
    import config
    from html_telegraph_poster import TelegraphPoster

    poster = TelegraphPoster(use_api=True, access_token=config.TELEGRAPH_TOKEN)
    if not config.TELEGRAPH_TOKEN:
        poster.create_api_token("StomatBot_Reporter")

    formatted_body = html_content.replace("\n", "<br>")
    page = poster.post(
        title=title,
        author="StomatBot AI",
        text=formatted_body,
    )
    return page["url"]


def _generate_gemini_text_sync(prompt, context):
    import gemini_client

    response = gemini_client.generate_text(prompt, context)
    if not response:
        return None
    return getattr(response, "text", None)


def _web_search_sync(query, max_results):
    import config

    results = []
    if config.SEARCH_PROVIDER == "tavily" and config.TAVILY_API_KEY:
        try:
            from tavily import TavilyClient

            response = TavilyClient(api_key=config.TAVILY_API_KEY).search(
                query=query,
                search_depth="basic",
                max_results=max_results,
            )
            for item in response.get("results", []):
                content = item.get("content")
                url = item.get("url")
                if content and url:
                    results.append(f"{content} ({url})")
        except Exception:
            results = []

    if not results:
        try:
            from ddgs import DDGS

            with DDGS() as ddgs:
                for item in ddgs.text(query, region="ru-ru", max_results=max_results, backend="api"):
                    body = item.get("body") if item else None
                    href = item.get("href") if item else None
                    if body and href:
                        results.append(f"{body}\n(Source: {href})")
        except Exception:
            results = []

    return results


async def _run_json_tool(action, payload, timeout=None):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-X",
        "utf8",
        os.path.abspath(__file__),
        action,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
    )
    request_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="replace")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(request_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return None, f"{action} timeout"
    except asyncio.CancelledError:
        proc.kill()
        await proc.communicate()
        raise

    stdout_text = stdout.decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
    json_line = None
    for line in reversed(stdout_text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            json_line = stripped
            break

    if not json_line:
        details = stderr_text or stdout_text.strip()
        return None, details or f"{action} failed with code {proc.returncode}"

    try:
        result = json.loads(json_line)
    except json.JSONDecodeError as exc:
        return None, f"{action} invalid output: {exc}"

    if not result.get("ok"):
        return None, result.get("error") or f"{action} failed"
    return result, None


async def create_telegraph_page_async(title, html_content, timeout):
    payload, error = await _run_json_tool(
        "telegraph-page",
        {"title": title, "html": html_content},
        timeout=timeout,
    )
    if error:
        return None, error
    return payload.get("url"), None


class TextResponse:
    def __init__(self, text):
        self.text = text


_LAST_GEMINI_CALL_TIME = 0.0

async def generate_gemini_text_async(prompt, context, timeout=None):
    global _LAST_GEMINI_CALL_TIME
    time_since_last_call = time.time() - _LAST_GEMINI_CALL_TIME
    if time_since_last_call < 3.0:
        await asyncio.sleep(3.0 - time_since_last_call)
    _LAST_GEMINI_CALL_TIME = time.time()
    
    try:
        payload, error = await _run_json_tool(
            "gemini-text",
            {"prompt": prompt, "context": context},
            timeout=timeout,
        )
        if error:
            return None, error

        text = payload.get("text")
        if not text:
            return None, None
        return TextResponse(text), None
    finally:
        try:
            import runtime_guard
            runtime_guard.write_summary_status({"active": False})
        except Exception:
            pass


async def web_search_async(query, max_results, timeout):
    payload, error = await _run_json_tool(
        "web-search",
        {"query": query, "max_results": max_results},
        timeout=timeout,
    )
    if error:
        return [], error
    return payload.get("results") or [], None


async def transcribe_audio_async(file_path, timeout):
    payload, error = await _run_json_tool(
        "whisper-transcribe",
        {"file_path": file_path},
        timeout=timeout,
    )
    if error:
        return None, error
    return payload.get("text"), None


async def correct_dental_transcription_async(raw_text, timeout=20):
    if not raw_text or len(raw_text) < 4:
        return raw_text
        
    prompt = f"""
Ты — специализированный стоматологический редактор. Твоя задача — исправить возможные ошибки распознавания речи (опечатки, ослышки) в стоматологических и медицинских терминах.
Исправь текст, сохранив исходный смысл. Заменяй только искаженные термины (например, 'верти преп' -> 'вертипреп', 'бэо пт' -> 'BOPT', 'гипохлорид' -> 'гипохлорит', 'кафердам' -> 'коффердам' и т.д.).

Исходный распознанный текст:
"{raw_text}"

Правило: выведи ИСКЛЮЧИТЕЛЬНО исправленный текст, без каких-либо комментариев, кавычек или пояснений. Если исправлений не требуется, выведи исходный текст без изменений.
"""
    response, error = await generate_gemini_text_async(prompt, {"kind": "transcription_corrector"}, timeout=timeout)
    if response and getattr(response, "text", None):
        corrected = response.text.strip().strip('"').strip("'")
        if corrected:
            return corrected
    return raw_text


def _transcribe_audio_sync(file_path):
    import gemini_client
    return gemini_client.transcribe_audio_bytes_or_file(file_path)


def _main():
    if len(sys.argv) != 2:
        _json_exit({"ok": False, "error": "usage: blocking_tools.py <action>"}, 2)

    action = sys.argv[1]
    try:
        payload = _read_stdin_json()
        if action == "telegraph-page":
            url = _create_telegraph_page_sync(payload.get("title") or "", payload.get("html") or "")
            _json_exit({"ok": bool(url), "url": url})

        if action == "gemini-text":
            text = _generate_gemini_text_sync(payload.get("prompt") or "", payload.get("context") or {})
            _json_exit({"ok": bool(text), "text": text})

        if action == "web-search":
            results = _web_search_sync(
                payload.get("query") or "",
                int(payload.get("max_results") or 2),
            )
            _json_exit({"ok": True, "results": results})

        if action == "whisper-transcribe":
            text = _transcribe_audio_sync(payload.get("file_path") or "")
            _json_exit({"ok": bool(text), "text": text})

        _json_exit({"ok": False, "error": f"unknown action: {action}"}, 2)
    except Exception as exc:
        _json_exit({"ok": False, "error": str(exc)}, 1)


if __name__ == "__main__":
    _main()
