"""
Зрение: классификация «снимок слишком большой», порядок каскада, черновик модели.

Гоняется настоящий vision.describe_image. Заглушены только сетевой клиент
AsyncOpenAI и подготовка снимка (чтобы не плодить подпроцессы) — классификация
ошибок, отбор ключей и порядок моделей исполняются как в бою.

Главное утверждение: ветка «слишком большой снимок» — единственная в модуле,
которая делает return из describe_image, а не переходит к следующему ключу или
модели. Значит её ложное срабатывание стоит дороже всех остальных: рвётся весь
каскад из трёх моделей, и описание, уже полученное от предыдущей, выбрасывается.
Поэтому код сверяется по границе слова, а не подстрокой.

Журнал, файл кулдаунов и файл банов уведены во временный каталог: боевые
bot.log, key_cooldowns.json и banned_models.json не читаются и не пишутся.

Запуск: python test_fix_vision.py
"""
import asyncio
import inspect
import io
import logging
import os
import random
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_fixvision_")

# Путь журнала подменяем ДО импорта проектных модулей: runtime_guard вычисляет
# LOG_PATH на уровне модуля, и после импорта переменную окружения уже никто не
# перечитает — тестовая выдумка ушла бы в боевой bot.log.
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")

import config
import gemini_client as gc
import vision

gc.KEY_COOLDOWN_FILE = os.path.join(_TMPDIR, "key_cooldowns.json")
gc.BANNED_MODELS_FILE = os.path.join(_TMPDIR, "banned_models.json")

# Обработчик на корне гасит logging.lastResort: без него WARNING из vision
# уходит в stderr и мешается с выдачей проверок. Заодно гарантия, что журнал не
# пишется вообще никуда.
logging.getLogger().addHandler(logging.NullHandler())

# Ключи только поддельные. config.load_dotenv() затащил боевые в окружение, а
# vision читает сначала окружение и лишь потом config — без pop ниже тест
# подставлял бы в заголовки настоящие ключи.
GOOGLE_KEYS = [f"gk-{i:02d}-secretpart" for i in range(4)]
GROQ_KEYS = [f"qk-{i:02d}-secretpart" for i in range(3)]
config.GOOGLE_KEYS = GOOGLE_KEYS
config.GROQ_KEYS = GROQ_KEYS
for var in ("GOOGLE_API_KEYS", "GOOGLE_KEYS", "GROQ_API_KEYS", "GROQ_KEYS"):
    os.environ.pop(var, None)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


M35 = "gemini-3.5-flash-lite"
M31 = "gemini-3.1-flash-lite"
MQW = "qwen/qwen3.6-27b"
ALL_MODELS = (M35, M31, MQW)

RUSSIAN_OK = "Прицельный снимок 36 зуба, краевая щель под коронкой."
ENGLISH_ONLY = "The image shows a periapical radiograph of tooth 36."

REQUESTS = []          # (model, key) каждого фактически ушедшего запроса
_behaviour = {}        # model -> Exception | текст ответа


async def fake_prep(file_path, timeout=None):
    return b"\xff\xd8fake-jpeg-bytes", None


vision.prepare_image_for_analysis = fake_prep


class FakeCompletions:
    def __init__(self, api_key):
        self.api_key = api_key

    async def create(self, model=None, messages=None, max_tokens=None):
        REQUESTS.append((model, self.api_key))
        outcome = _behaviour.get(model, RUSSIAN_OK)
        if isinstance(outcome, Exception):
            raise outcome
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {"content": outcome})()})()]
        })()


class FakeAsyncOpenAI:
    def __init__(self, api_key=None, base_url=None, http_client=None, max_retries=0, timeout=None):
        self.chat = type("Chat", (), {"completions": FakeCompletions(api_key)})()


vision.AsyncOpenAI = FakeAsyncOpenAI
# Троттлинг проверяется в test_vision_pipeline; здесь он только удлинял бы прогон
# на 3 с за попытку (VISION_MIN_CALL_INTERVAL_SECONDS), а их тут десятки.
vision.VISION_MIN_CALL_INTERVAL_SECONDS = 0.0


def reset(behaviour=None):
    REQUESTS.clear()
    _behaviour.clear()
    _behaviour.update(behaviour or {})
    for path in (gc.KEY_COOLDOWN_FILE, gc.BANNED_MODELS_FILE):
        if os.path.exists(path):
            os.remove(path)


def for_all(message):
    return {m: Exception(message) for m in ALL_MODELS}


def models_used():
    return [m for m, _ in REQUESTS]


def models_in_order():
    """Модели в порядке первого обращения (ключей у каждой несколько)."""
    order = []
    for model in models_used():
        if model not in order:
            order.append(model)
    return order


async def run():
    print("\n[1] Код 413 сверяется по границе слова, а не подстрокой")
    # Числа из посторонних сообщений об ошибке. Подстрочный поиск "413" ловил
    # каждое из них: лимиты, счётчики токенов, идентификатор запроса.
    for text in (
        "400 invalid_request: max_tokens 4130 exceeds the limit",
        "context length of 413000 tokens exceeded",
        "server error, request id req_8413ac2",
        "model produced 1413 tokens of 2048",
    ):
        check(f"«{text[:44]}» не считается отказом по размеру",
              vision._PAYLOAD_TOO_LARGE_RE.search(text) is None,
              "посторонний отказ трактуется как превышение размера снимка")
    for text in (
        "413 payload too large",
        "openai.BadRequestError: error code 413",
        "request entity too large",
    ):
        check(f"«{text[:44]}» считается отказом по размеру",
              vision._PAYLOAD_TOO_LARGE_RE.search(text) is not None,
              "настоящее превышение размера не распознано")

    print("\n[2] Посторонний отказ с цифрами 413 внутри не рвёт каскад")
    reset(for_all("400 invalid_request: max_tokens 4130 exceeds the 413000 token limit (req_8413ac2)"))
    result = await vision.describe_image(["/tmp/fake.jpg"])
    check("каскад дошёл до всех трёх моделей", len(set(models_used())) == len(ALL_MODELS),
          f"got {models_used()}")
    check("модели перебирались разными ключами, а не бросались после первого",
          len(REQUESTS) > len(ALL_MODELS), f"got {REQUESTS}")
    check("описания нет — но это результат обхода каскада, а не отказа на входе",
          result is None, f"got {result!r}")

    print("\n[3] Настоящее превышение размера прекращает попытки сразу")
    reset(for_all("413 Payload Too Large"))
    result = await vision.describe_image(["/tmp/fake.jpg"])
    check("остановились на первом же отказе", len(REQUESTS) == 1, f"got {REQUESTS}")
    check("вернулся None", result is None, f"got {result!r}")

    reset(for_all("Request Entity Too Large"))
    result = await vision.describe_image(["/tmp/fake.jpg"])
    # Groq и перехватывающие прокси отдают эту формулировку без кода вовсе —
    # подстрочный поиск "413" её не видел и впустую перебирал все ключи пула.
    check("формулировка без кода тоже прекращает попытки", len(REQUESTS) == 1,
          f"got {REQUESTS}")

    print("\n[4] Отказ по размеру не выбрасывает уже полученное описание")
    reset({M35: ENGLISH_ONLY, M31: Exception("413 Payload Too Large")})
    result = await vision.describe_image(["/tmp/fake.jpg"], is_passive=False)
    check("английское описание от первой модели сохранено", result == ENGLISH_ONLY,
          f"got {result!r} — описание выброшено вместе с отказом по размеру")
    check("после отказа по размеру третья модель не опрашивалась",
          models_in_order() == [M35, M31], f"got {models_used()}")

    print("\n[5] is_passive применён: адресный вызов начинается со старшей модели")
    firsts = set()
    for _ in range(12):
        reset(for_all("503 Service Unavailable"))
        await vision.describe_image(["/tmp/fake.jpg"], is_passive=False)
        firsts.add(models_used()[0])
    check("адресный вызов всегда стартует с gemini-3.5-flash-lite", firsts == {M35},
          f"got {firsts}")

    random.seed(20260728)   # чтобы «случайность» пула была воспроизводимой
    firsts = set()
    for _ in range(30):
        reset(for_all("503 Service Unavailable"))
        await vision.describe_image(["/tmp/fake.jpg"], is_passive=True)
        firsts.add(models_used()[0])
    check("фоновая загрузка по-прежнему размазывает квоту по пулу",
          firsts == set(ALL_MODELS), f"got {firsts}")

    print("\n[6] Приоритет не сузил каскад: резервные модели остались доступны")
    reset({M35: Exception("400 invalid_request: unsupported image"),
           M31: Exception("400 invalid_request: unsupported image"),
           MQW: RUSSIAN_OK})
    result = await vision.describe_image(["/tmp/fake.jpg"], is_passive=False)
    check("ответ получен от последней модели пула", result == RUSSIAN_OK, f"got {result!r}")
    check("порядок обхода — от старшей к младшей", models_in_order() == list(ALL_MODELS),
          f"got {models_in_order()}")

    print("\n[7] Параметр is_passive нельзя просто убрать: его передают двое")
    params = inspect.signature(vision.describe_image).parameters
    check("describe_image принимает is_passive", "is_passive" in params)
    for name in ("main.py", "assistant.py"):
        source = io.open(os.path.join(_HERE, name), encoding="utf-8").read()
        calls = [chunk for chunk in source.split("describe_image(")[1:]
                 if "is_passive" in chunk.split(")", 1)[0]]
        check(f"{name} передаёт is_passive именованным аргументом", bool(calls),
              "вызов изменился — проверку и параметр надо пересмотреть вместе")

    print("\n[8] Черновик модели не уходит как описание снимка")
    # Гипотеза «обрыв внутри <think> отдаёт размышления за описание» относилась к
    # местной срезке, которой в модуле больше нет: vision зовёт общий
    # gemini_client.strip_reasoning. Проверка сторожит именно это — не текст
    # помощника, а поведение каскада на оборванном ответе.
    reset({m: "<think>прикидываю: похоже на 36, но кадр обрезан и" for m in ALL_MODELS})
    result = await vision.describe_image(["/tmp/fake.jpg"])
    check("оборванный внутри <think> ответ описанием не становится", result is None,
          f"got {result!r}")

    reset({m: "<think>сначала прикидываю</think>" + RUSSIAN_OK for m in ALL_MODELS})
    result = await vision.describe_image(["/tmp/fake.jpg"])
    check("из ответа с размышлениями остаётся только описание", result == RUSSIAN_OK,
          f"got {result!r}")

    reset({m: RUSSIAN_OK + "<think>а теперь прикидываю дальше" for m in ALL_MODELS})
    result = await vision.describe_image(["/tmp/fake.jpg"])
    check("хвост незакрытого <think> после описания срезан", result == RUSSIAN_OK,
          f"got {result!r}")

    leaked = []
    for draft in ("<think>прикидываю: похоже на 36, но кадр обрезан и",
                  "<think>сначала прикидываю</think>" + RUSSIAN_OK,
                  RUSSIAN_OK + "<think>а теперь прикидываю дальше"):
        reset({m: draft for m in ALL_MODELS})
        out = await vision.describe_image(["/tmp/fake.jpg"]) or ""
        if "think" in out or "прикидыв" in out:
            leaked.append(draft[:40])
    check("ни один черновик не просочился в описание", not leaked, f"утечки: {leaked}")


try:
    asyncio.run(run())
finally:
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
