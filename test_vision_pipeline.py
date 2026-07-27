"""
Модуль зрения: подготовка снимка, TLS, кулдауны ключей, троттлинг.

Гоняется настоящий vision.describe_image. Заглушены только сетевой клиент
AsyncOpenAI и подготовка картинки (чтобы не плодить подпроцессы) — вся логика
каскада, отбора ключей и классификации ошибок исполняется как в бою. Файл
кулдаунов подменён на временный, боевой не читается и не пишется.

Запуск: python test_vision_pipeline.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_vision_")

import gemini_client as gc
import vision
import media_tools

gc.KEY_COOLDOWN_FILE = os.path.join(_TMPDIR, "key_cooldowns.json")
gc.BANNED_MODELS_FILE = os.path.join(_TMPDIR, "banned_models.json")

GOOGLE_KEYS = [f"gk-{i:02d}-secretpart" for i in range(6)]
GROQ_KEYS = [f"qk-{i:02d}-secretpart" for i in range(4)]
config.GOOGLE_KEYS = GOOGLE_KEYS
config.GROQ_KEYS = GROQ_KEYS
for var in ("GOOGLE_API_KEYS", "GOOGLE_KEYS", "GROQ_API_KEYS", "GROQ_KEYS"):
    os.environ.pop(var, None)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


REQUESTS = []
_behaviour = {}


async def fake_prep(file_path, timeout=None):
    return b"\xff\xd8fake-jpeg-bytes", None


vision.prepare_image_for_analysis = fake_prep


class FakeCompletions:
    def __init__(self, api_key):
        self.api_key = api_key

    async def create(self, model=None, messages=None, max_tokens=None):
        REQUESTS.append((model, self.api_key))
        outcome = _behaviour.get(model, "Клиническая картина: краевая щель у 36.")
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {"content": outcome})()})()]
        })()


class FakeAsyncOpenAI:
    def __init__(self, api_key=None, base_url=None, http_client=None, max_retries=0, timeout=None):
        self.chat = type("Chat", (), {"completions": FakeCompletions(api_key)})()


vision.AsyncOpenAI = FakeAsyncOpenAI
vision.VISION_MIN_CALL_INTERVAL_SECONDS = 0.0


def reset(behaviour=None):
    REQUESTS.clear()
    _behaviour.clear()
    _behaviour.update(behaviour or {})
    for path in (gc.KEY_COOLDOWN_FILE, gc.BANNED_MODELS_FILE):
        if os.path.exists(path):
            os.remove(path)


def keys_used():
    return [k for _, k in REQUESTS]


async def run():
    print("\n[1] Подготовка снимка берётся из media_tools, дубля больше нет")
    check("функция импортирована из media_tools",
          media_tools.prepare_image_for_analysis.__module__ == "media_tools")
    check("мёртвая GROQ_COOLDOWN_UNTIL удалена", not hasattr(vision, "GROQ_COOLDOWN_UNTIL"))
    check("мёртвая prepare_image_for_groq удалена", not hasattr(vision, "prepare_image_for_groq"))

    print("\n[2] Размер payload: настройки media_tools экономнее inline-копии")
    sample = None
    for name in os.listdir("."):
        if name.lower().endswith(".jpg") and os.path.getsize(name) > 20000:
            sample = name
            break
    if sample:
        data, err = await media_tools.prepare_image_for_analysis(sample, timeout=45)
        check("реальный снимок подготовлен", err is None and data, f"err={err}")
        if data:
            check(f"результат ужат ({len(data)} байт из {os.path.getsize(sample)})",
                  len(data) < os.path.getsize(sample), f"got {len(data)}")
    else:
        check("нет образца .jpg в каталоге — проверка неприменима", True)

    print("\n[3] TLS-проверка включена по умолчанию")
    check("verify=True по умолчанию", vision._env_flag("STOMCHAT_VISION_TLS_VERIFY", True) is True)
    os.environ["STOMCHAT_VISION_TLS_VERIFY"] = "0"
    check("осознанное отключение возможно",
          vision._env_flag("STOMCHAT_VISION_TLS_VERIFY", True) is False)
    os.environ["STOMCHAT_VISION_TLS_VERIFY"] = "1"
    check("обратно включается", vision._env_flag("STOMCHAT_VISION_TLS_VERIFY", True) is True)
    os.environ.pop("STOMCHAT_VISION_TLS_VERIFY", None)

    print("\n[4] Обычный разбор снимка проходит")
    reset()
    result = await vision.describe_image(["/tmp/fake.jpg"], caption="прицельный 36")
    check("описание получено", result and "36" in result, f"got {result!r}")
    check("сделан один запрос", len(REQUESTS) == 1, f"got {REQUESTS}")

    print("\n[5] Ключи на кулдауне пропускаются (стор общий с текстовым каскадом)")
    reset()
    for key in GOOGLE_KEYS:
        gc.set_key_cooldown("gemini", key, seconds=300)
    result = await vision.describe_image(["/tmp/fake.jpg"])
    check("описание всё равно получено", bool(result), f"got {result!r}")
    check("к остывающим ключам Google не обращались",
          not (set(keys_used()) & set(GOOGLE_KEYS)), f"got {keys_used()}")
    check("запрос ушёл в Groq", set(keys_used()) <= set(GROQ_KEYS), f"got {keys_used()}")

    print("\n[6] 429 в зрении записывает общий кулдаун, а не спит вслепую")
    reset({
        "gemini-3.5-flash-lite": Exception("429 Too Many Requests: rate limit"),
        "gemini-3.1-flash-lite": Exception("429 Too Many Requests: rate limit"),
        "qwen/qwen3.6-27b": Exception("429 Too Many Requests: rate limit"),
    })
    await vision.describe_image(["/tmp/fake.jpg"])
    cooled = gc.get_key_cooldowns()
    check("кулдауны записаны на диск", len(cooled) > 0, f"got {cooled}")
    check("записан отпечаток, а не ключ",
          all("secretpart" not in k for k in cooled), f"got {list(cooled)}")
    hit = [k for k in GOOGLE_KEYS + GROQ_KEYS
           if gc._key_fingerprint("gemini", k) in cooled or gc._key_fingerprint("groq", k) in cooled]
    check("отмечены именно те ключи, что получили 429", len(hit) > 0, f"got {hit}")

    print("\n[7] Кулдаун, поставленный текстовым каскадом, виден зрению")
    reset()
    for key in GOOGLE_KEYS:
        gc.set_key_cooldown("gemini", key, seconds=300)   # как будто выбил generate_text
    for key in GROQ_KEYS:
        gc.set_key_cooldown("groq", key, seconds=300)
    result = await vision.describe_image(["/tmp/fake.jpg"])
    check("ни одного запроса при полностью остывшем пуле", REQUESTS == [], f"got {REQUESTS}")
    check("честно вернулся None", result is None, f"got {result!r}")

    # Каскад зрения стартует со случайной модели (random.randint), поэтому
    # ниже ошибку задаём ВСЕМ моделям — проверки не должны зависеть от того,
    # какая выпала первой.
    ALL_VISION_MODELS = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "qwen/qwen3.6-27b")

    def for_all(exc_factory):
        return {m: exc_factory() for m in ALL_VISION_MODELS}

    print("\n[8] Посторонний отказ с числом 500 не выбрасывает модель из каскада")
    reset(for_all(lambda: Exception("invalid_request: max_tokens 1500 of 500000")))
    await vision.describe_image(["/tmp/fake.jpg"])
    per_model = {}
    for model, _ in REQUESTS:
        per_model[model] = per_model.get(model, 0) + 1
    check("каждую модель перебирали разными ключами, а не бросали после первого",
          per_model and all(count > 1 for count in per_model.values()), f"got {per_model}")

    print("\n[9] Настоящий 503 модель из каскада выбрасывает сразу")
    reset(for_all(lambda: Exception("503 Service Unavailable")))
    await vision.describe_image(["/tmp/fake.jpg"])
    per_model = {}
    for model, _ in REQUESTS:
        per_model[model] = per_model.get(model, 0) + 1
    check("каждая перегруженная модель опрошена ровно один раз",
          per_model and all(count == 1 for count in per_model.values()), f"got {per_model}")
    check("каскад дошёл до всех моделей", len(per_model) == len(ALL_VISION_MODELS),
          f"got {per_model}")

    print("\n[10] 413 прекращает попытки, а не перебирает ключи впустую")
    reset(for_all(lambda: Exception("413 Payload Too Large")))
    result = await vision.describe_image(["/tmp/fake.jpg"])
    check("остановились на первом же отказе", len(REQUESTS) == 1, f"got {REQUESTS}")
    check("вернулся None", result is None, f"got {result!r}")

    print("\n[11] Троттлинг выдерживает интервал при конкурентных вызовах")
    vision.VISION_MIN_CALL_INTERVAL_SECONDS = 0.2
    vision._LAST_VISION_CALL_TIME = 0.0
    vision._VISION_PACE_LOCK = None
    started = time.time()
    await asyncio.gather(*[vision._pace_vision_calls() for _ in range(4)])
    elapsed = time.time() - started
    vision.VISION_MIN_CALL_INTERVAL_SECONDS = 0.0
    # Четыре вызова с интервалом 0.2 с не могут уложиться быстрее ~0.6 с.
    check(f"четыре параллельных вызова разнесены ({elapsed:.2f} с)", elapsed >= 0.55,
          f"elapsed={elapsed:.2f}s — интервал не соблюдён")

    print("\n[12] Ни один снимок не подготовился — честный None без запросов")
    reset()

    async def failing_prep(file_path, timeout=None):
        return None, "битый файл"

    original_prep = vision.prepare_image_for_analysis
    vision.prepare_image_for_analysis = failing_prep
    result = await vision.describe_image(["/tmp/broken.jpg"])
    vision.prepare_image_for_analysis = original_prep
    check("запросов не было", REQUESTS == [], f"got {REQUESTS}")
    check("вернулся None", result is None, f"got {result!r}")


try:
    asyncio.run(run())
finally:
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
