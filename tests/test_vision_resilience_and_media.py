# -*- coding: utf-8 -*-
"""
Регрессионные тесты для:
1. Устойчивости каскада vision.py:
   - Немедленный пропуск оставшихся ключей/моделей Google при project-wide 429 через gemini_client.available_keys(..., model_name=...)
   - Переход на следующую модель при timeout без сжигания второго ключа на зависшем эндпоинте
   - Резервный переход на meta-llama/llama-4-scout-17b-16e-instruct если основной Groq отверг image_url
   - Очистка NON_DENTAL метаданных от ложных триггеров сомнений
   - Компактность system_prompt (< 2500 симв.)
2. Нормализации изображений в media_tools.py:
   - EXIF Orientation transposition (не кладёт портретные фото на бок)
   - Прозрачный RGBA PNG композитится на белый фон, а не на чёрный
   - 16-битный grayscale рентген (I;16) не клиппируется в сплошной белый
3. Надёжности visiodent_enhancer.py:
   - Сохранение по относительному имени файла без директории ("collage.jpg")
   - Кириллические пути на Windows
   - Адаптивный размер HUD-бейджа в узких квадрантах
   - Fail-closed при неожиданной ошибке валидации
"""

import asyncio
import io
import os
import re
import sys

import numpy as np
from PIL import Image, ImageDraw

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import gemini_client
import media_tools
import visiodent_enhancer
import vision


def test_vision_prompt_rendered_under_2500_chars():
    src = open(os.path.join(_REPO_ROOT, "vision.py"), encoding="utf-8").read()
    vp = src.split("system_prompt = (", 1)[1].split("\n            )", 1)[0]
    rendered = re.sub(r'^\s*f?"|"\s*$', "", vp, flags=re.M)
    assert len(rendered) < 2500, f"system_prompt bloated: {len(rendered)} chars"


def test_non_dental_metadata_clears_false_dental_triggers():
    raw = (
        "[ПОДРОБНОЕ ОПИСАНИЕ]\n"
        "Мем про трещину на экране и очаг возгорания на кухне.\n\n"
        "[КРАТКАЯ ПОДПИСЬ]\nМем\n\n"
        "[МЕТАДАННЫЕ]\n"
        '{"modality": "NON_DENTAL", "has_clinical_doubt": true, "target_tooth": 47, "recommended_quadrant": 3}'
    )
    meta = vision.extract_vision_metadata(raw)
    assert meta["modality"] == "NON_DENTAL"
    assert meta["has_clinical_doubt"] is False
    assert meta["target_tooth"] is None
    assert meta["recommended_quadrant"] is None


def test_vision_cascade_skips_cooled_pool_on_gcp_429_and_falls_back_to_groq(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_client, "KEY_COOLDOWN_FILE", str(tmp_path / "key_cooldowns.json"))
    monkeypatch.setattr(gemini_client, "MODEL_KEY_COOLDOWN_FILE", str(tmp_path / "model_key_cooldowns.json"))
    monkeypatch.setattr(gemini_client, "BANNED_MODELS_FILE", str(tmp_path / "banned_models.json"))
    monkeypatch.setattr(vision, "VISION_CACHE_FILE", str(tmp_path / "vision_cache.json"))
    monkeypatch.setattr(vision, "VISION_MIN_CALL_INTERVAL_SECONDS", 0.0)
    vision.clear_vision_cache()

    google_keys = [f"gk-fake-{i}" for i in range(4)]
    groq_keys = ["gq-fake-1", "gq-fake-2"]
    monkeypatch.delenv("GOOGLE_API_KEYS", raising=False)
    monkeypatch.delenv("GOOGLE_KEYS", raising=False)
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("GROQ_KEYS", raising=False)
    monkeypatch.setattr(vision.config, "GOOGLE_KEYS", google_keys, raising=False)
    monkeypatch.setattr(vision.config, "GROQ_KEYS", groq_keys, raising=False)

    async def fake_prep(fp, timeout=None):
        return b"\xff\xd8\xff\xe0fake-jpeg", None

    monkeypatch.setattr(vision, "prepare_image_for_analysis", fake_prep)

    calls = []

    class FakeCompletions:
        def __init__(self, api_key):
            self.api_key = api_key

        async def create(self, model=None, messages=None, max_tokens=None):
            calls.append((model, self.api_key))
            if model.startswith("gemini"):
                raise Exception(
                    "Error code: 429 - RESOURCE_EXHAUSTED: GenerateContentRequestsPerMinutePerProjectPerRegion quota exceeded"
                )
            return type(
                "R",
                (),
                {
                    "choices": [
                        type(
                            "C",
                            (),
                            {
                                "finish_reason": "stop",
                                "message": type(
                                    "M",
                                    (),
                                    {"content": "На прицельной рентгенограмме 36 определяется периапикальный очаг."},
                                )(),
                            },
                        )()
                    ]
                },
            )()

    class FakeAsyncOpenAI:
        def __init__(self, api_key=None, base_url=None, http_client=None, max_retries=0, timeout=None):
            self.chat = type("Chat", (), {"completions": FakeCompletions(api_key)})()

    monkeypatch.setattr(vision, "AsyncOpenAI", FakeAsyncOpenAI)

    res = asyncio.run(vision.describe_image(["/tmp/nonexistent.jpg"], is_passive=False))
    assert res is not None and "36" in res
    gemini_calls = [c for c in calls if c[0].startswith("gemini")]
    groq_calls = [c for c in calls if not c[0].startswith("gemini")]
    assert len(gemini_calls) == 1, f"Expected immediate skip of remaining Gemini keys/models, got {calls}"
    assert len(groq_calls) == 1


def test_vision_timeout_switches_model_without_burning_second_key(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_client, "KEY_COOLDOWN_FILE", str(tmp_path / "key_cooldowns.json"))
    monkeypatch.setattr(gemini_client, "MODEL_KEY_COOLDOWN_FILE", str(tmp_path / "model_key_cooldowns.json"))
    monkeypatch.setattr(gemini_client, "BANNED_MODELS_FILE", str(tmp_path / "banned_models.json"))
    monkeypatch.setattr(vision, "VISION_CACHE_FILE", str(tmp_path / "vision_cache.json"))
    monkeypatch.setattr(vision, "VISION_MIN_CALL_INTERVAL_SECONDS", 0.0)
    vision.clear_vision_cache()
    monkeypatch.delenv("GOOGLE_API_KEYS", raising=False)
    monkeypatch.delenv("GOOGLE_KEYS", raising=False)
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("GROQ_KEYS", raising=False)
    monkeypatch.setattr(vision.config, "GOOGLE_KEYS", ["gk-1", "gk-2", "gk-3"], raising=False)
    monkeypatch.setattr(vision.config, "GROQ_KEYS", ["gq-1"], raising=False)

    async def fake_prep(fp, timeout=None):
        return b"\xff\xd8\xff\xe0fake-jpeg", None

    monkeypatch.setattr(vision, "prepare_image_for_analysis", fake_prep)

    calls = []

    class FakeCompletions:
        def __init__(self, api_key):
            self.api_key = api_key

        async def create(self, model=None, messages=None, max_tokens=None):
            calls.append((model, self.api_key))
            if model == "gemini-3.5-flash-lite":
                raise Exception("Request timed out.")
            if model == "gemini-3.1-flash-lite":
                raise Exception("400 invalid_request: unsupported image")
            if model == "qwen/qwen3.8-27b":
                raise Exception("400 invalid_request: model does not support image input")
            return type(
                "R",
                (),
                {
                    "choices": [
                        type(
                            "C",
                            (),
                            {
                                "finish_reason": "stop",
                                "message": type(
                                    "M",
                                    (),
                                    {"content": "На снимке зуба 4.6 визуализируется кариозный дефект эмали."},
                                )(),
                            },
                        )()
                    ]
                },
            )()

    class FakeAsyncOpenAI:
        def __init__(self, api_key=None, base_url=None, http_client=None, max_retries=0, timeout=None):
            self.chat = type("Chat", (), {"completions": FakeCompletions(api_key)})()

    monkeypatch.setattr(vision, "AsyncOpenAI", FakeAsyncOpenAI)

    res = asyncio.run(vision.describe_image(["/tmp/nonexistent.jpg"], is_passive=False))
    assert res is not None and "4.6" in res
    m35_calls = [c for c in calls if c[0] == "gemini-3.5-flash-lite"]
    assert len(m35_calls) == 1, f"Timeout should break immediately to next model: {calls}"
    assert any(c[0] == "meta-llama/llama-4-scout-17b-16e-instruct" for c in calls)


def test_media_tools_exif_rgba_and_16bit_normalization(tmp_path):
    # 1. EXIF Orientation 6 (90° CW): stored as 120x60 -> displayed portrait 60x120
    img = Image.new("RGB", (120, 60), (200, 40, 40))
    exif = img.getexif()
    exif[274] = 6
    exif_path = str(tmp_path / "exif_rot.jpg")
    img.save(exif_path, format="JPEG", exif=exif)
    out_bytes, err = media_tools._prepare_image_sync(exif_path)
    assert err is None and out_bytes
    with Image.open(io.BytesIO(out_bytes)) as parsed:
        assert parsed.size == (60, 120), f"EXIF orientation not transposed: {parsed.size}"

    # 2. RGBA transparent background with black RGB (0,0,0,0) -> composited onto white, not black
    rgba = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(rgba)
    draw.rectangle([40, 40, 60, 60], fill=(10, 10, 10, 255))
    rgba_path = str(tmp_path / "transparent.png")
    rgba.save(rgba_path, format="PNG")
    out_rgba, err2 = media_tools._prepare_image_sync(rgba_path)
    assert err2 is None and out_rgba
    with Image.open(io.BytesIO(out_rgba)) as parsed_rgba:
        corner_px = parsed_rgba.getpixel((5, 5))
        assert corner_px[0] > 240 and corner_px[1] > 240 and corner_px[2] > 240, f"Transparent bg turned black: {corner_px}"

    # 3. 16-bit grayscale X-ray (I;16) -> normalized across 0..255 instead of pure white
    arr16 = np.zeros((100, 100), dtype=np.uint16)
    arr16[:, :50] = 1000
    arr16[:, 50:] = 40000
    img16 = Image.fromarray(arr16)
    p16 = str(tmp_path / "xray16.png")
    img16.save(p16)
    out16, err3 = media_tools._prepare_image_sync(p16)
    assert err3 is None and out16
    with Image.open(io.BytesIO(out16)) as parsed16:
        left_px = parsed16.getpixel((10, 50))[0]
        right_px = parsed16.getpixel((90, 50))[0]
        assert left_px < 30 and right_px > 225, f"16-bit normalization failed: left={left_px}, right={right_px}"


def test_visiodent_relative_path_cyrillic_path_and_narrow_badge(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    mono = np.full((180, 180, 3), 90, dtype=np.uint8)
    mono[30:150, 70:110] = 180
    cyr_dir = tmp_path / "снимки_врача"
    cyr_dir.mkdir()
    cyr_in = str(cyr_dir / "зуб_47.png")
    Image.fromarray(mono).save(cyr_in)

    ok, reason = visiodent_enhancer.validate_visiodent_candidate(cyr_in)
    assert ok and reason == "OK"

    out_rel, dims = visiodent_enhancer.generate_visiodent_collage(
        cyr_in, output_path="relative_collage.jpg", target_quadrant_width=220
    )
    assert out_rel == "relative_collage.jpg"
    assert os.path.exists(out_rel) and os.path.getsize(out_rel) > 1024
    assert dims is not None and dims[0] >= 440

    # Fail-closed validation when object causes unexpected decoding error
    ok_bad, msg_bad = visiodent_enhancer.validate_visiodent_candidate(object())
    assert ok_bad is False and msg_bad
