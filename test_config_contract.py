"""
Шаблон конфигурации больше не расходится с тем, что код на самом деле читает.

config.py стоит в .gitignore и на боевую машину через git не уезжает. Значит
единственная форма конфига, которую видит человек, поднимающий бота на новой
машине, — это версионный config.example.py. До правки он объявлял 16 имён, а код
читает 16 других: в шаблоне не было API_ID, SOURCE_CHAT_ID и REPORT_TARGETS.
Цена каждого промаха измерена по дереву:

  * без API_ID падает импорт main.py — TelegramClient собирается на уровне
    модуля (main.py:888). Бот не поднимается, зато громко;
  * без SOURCE_CHAT_ID падает тот же импорт: WATCHED_CHATS считается на уровне
    модуля (main.py:1776). Тоже громко;
  * без REPORT_TARGETS бот поднимается и выглядит живым. Чтение стоит внутри
    resolve_report_targets() (main.py:591) под except Exception, поэтому
    отсутствие имени превращается в пустой список получателей: 749 врачей не
    получают ни дайджеста, ни недельной сводки, и об этом одна строка в журнале.

Проверки поведенческие. Шаблон здесь ИСПОЛНЯЕТСЯ как модуль в подставленном
окружении, и спрашивается настоящий объект модуля — hasattr, тип, значение. Это
ровно то, что делает `import config` у врача. Проверка «в исходнике есть такая
строка» не годится: в этом проекте она уже один раз пропустила снятый потолок.

Отдельно проверяется, что в шаблон не просочилось ни одного ЗНАЧЕНИЯ: он
загружается второй раз с вычищенным окружением, и всё, кроме относительного пути
базы, обязано оказаться пустым. Зашитый токен, id чата или имя сессии этот
прогон сделает красным.

И проверка проверки: в мета-блоке [8] делается копия шаблона с переименованным
обязательным именем, и контракт на ней ОБЯЗАН упасть. Без этого блока пункт [2]
мог бы не значить ничего.

Запуск: python test_config_contract.py
"""
import ast
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "config.example.py")
LIVE = os.path.join(HERE, "config.py")
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_cfgcontract_")
os.environ.setdefault("STOMCHAT_LOG_PATH", os.path.join(_TMPDIR, "t.log"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


# --------------------------------------------------------------------------
# Сбор контракта: какие имена код читает у config и какие из них обязательны.
#
# Правило обязательности:
#   config.ИМЯ в контексте чтения        -> обязательно (иначе AttributeError)
#   getattr(config, "ИМЯ")   в 2 аргум.  -> обязательно (то же AttributeError)
#   getattr(config, "ИМЯ", x) в 3 аргум. -> НЕ обязательно, у кода есть запас
#   hasattr(config, "ИМЯ")               -> НЕ обязательно, это проверка наличия
#   config.ИМЯ = ...  (контекст Store)   -> не чтение, так тесты подменяют конфиг
#
# Разбор через ast, а не через поиск подстроки: regex по `config\.ИМЯ` находит
# «config.py» в комментарии как имя `py`, «config.example.py» как `example`, а
# `config.json_loads` из docstring test_report_targets.py — как атрибут, которого
# нет. Ни одно из них не обращение к конфигу.
# --------------------------------------------------------------------------
def parse_file(path):
    return ast.parse(io.open(path, encoding="utf-8-sig").read())


def config_aliases(tree):
    """Под какими именами модуль держит config: import config / import config as X."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "config":
                    names.add(alias.asname or "config")
    return names


def loop_choices(tree):
    """for ИМЯ in ("A", "B"): ... -> {'ИМЯ': ['A', 'B']} для getattr по переменной."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name) \
                and isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
            literals = [e.value for e in node.iter.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if literals:
                out.setdefault(node.target.id, []).extend(literals)
    return out


def classify_tree(tree, fname, required, optional, dynamic):
    """Разложить обращения ОДНОГО разобранного файла по трём корзинам."""
    aliases = config_aliases(tree)
    if not aliases:
        return
    choices = loop_choices(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in aliases:
            if isinstance(node.ctx, ast.Load):
                required.setdefault(node.attr, []).append(f"{fname}:{node.lineno}")
            continue
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("getattr", "hasattr")):
            continue
        if len(node.args) < 2 or not (isinstance(node.args[0], ast.Name)
                                      and node.args[0].id in aliases):
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found = [arg.value]
        elif isinstance(arg, ast.Name) and arg.id in choices:
            # getattr(config, attr, None) в цикле по кортежу имён.
            found = choices[arg.id]
            dynamic.append(f"{fname}:{node.lineno} -> {found}")
        else:
            dynamic.append(f"{fname}:{node.lineno} -> имя не литерал")
            continue
        lenient = node.func.id == "hasattr" or len(node.args) >= 3
        for nm in found:
            target = optional if lenient else required
            target.setdefault(nm, []).append(f"{fname}:{node.lineno}")


def drop_shadowed(required, optional):
    """Имя, попавшее и туда и туда, обязательно: где-то код читает его без запаса."""
    for nm in list(optional):
        if nm in required:
            del optional[nm]


def classify_source(src, fname="<синтетика>"):
    """Тот же разбор, но по тексту. Нужен, чтобы проверить ПРАВИЛО, а не дерево."""
    required, optional, dynamic = {}, {}, []
    classify_tree(ast.parse(src), fname, required, optional, dynamic)
    drop_shadowed(required, optional)
    return required, optional, dynamic


def collect_contract():
    required, optional, dynamic = {}, {}, []
    sources = sorted(f for f in os.listdir(HERE) if f.endswith(".py"))
    for fname in sources:
        if fname in ("config.py", "config.example.py"):
            continue
        try:
            tree = parse_file(os.path.join(HERE, fname))
        except SyntaxError:
            continue
        classify_tree(tree, fname, required, optional, dynamic)
    drop_shadowed(required, optional)
    return required, optional, dynamic


REQUIRED, OPTIONAL, DYNAMIC = collect_contract()

print("[1] Контракт собран и не пуст")
check("обязательных имён собрано не меньше 12", len(REQUIRED) >= 12,
      f"собрано {len(REQUIRED)}: {sorted(REQUIRED)} — сборщик молча нашёл пусто, "
      f"и тогда все проверки ниже проходят вхолостую")
for _hot in ("SOURCE_CHAT_ID", "DB_PATH", "REPORT_TARGETS", "API_ID"):
    check(f"{_hot} распознан как обязательное", _hot in REQUIRED,
          "имя читается напрямую, сборщик его потерял")
check("необязательные имена отделены от обязательных",
      "DENTAL_KEYWORDS" in OPTIONAL and "DENTAL_KEYWORDS" not in REQUIRED,
      f"необязательные: {sorted(OPTIONAL)}")
print(f"       обязательных: {len(REQUIRED)}, необязательных: {len(OPTIONAL)} "
      f"({sorted(OPTIONAL)}), обращений по переменной: {len(DYNAMIC)}")

# --------------------------------------------------------------------------
# Правило обязательности проверяется на СИНТЕТИКЕ, а не на сегодняшнем дереве.
#
# Замер, из-за которого этот блок появился: в дереве 0 обращений вида
# getattr(config, "ИМЯ") в два аргумента, а все 58 присваиваний config.ИМЯ = ...
# трогают имена, которые и так читаются. Значит обе ветки правила на живом дереве
# не исполняются, и саботаж это подтвердил: подмена lenient на True и снятие
# проверки ast.Load не уронили НИ ОДНОЙ проверки из 80. Правило было написано, но
# не охранялось -- сломай его, и контракт молча начнёт требовать от шаблона лишние
# имена или прощать пропущенные.
# --------------------------------------------------------------------------
SYNTHETIC = '''
import config
import config as cfg

A = config.DIRECT_READ
B = getattr(config, "GETATTR_2")
C = getattr(config, "GETATTR_3", 7)
D = hasattr(config, "HASATTR_ONLY")
config.STORE_ONLY = 1
E = cfg.ALIAS_READ
F = getattr(config, "BOTH_WAYS", None)
G = config.BOTH_WAYS
'''
SREQ, SOPT, _SDYN = classify_source(SYNTHETIC)
# Ложные имена от затенения: если в файле есть ЛОКАЛЬНАЯ переменная config (в
# _probe_silent.py это GenerationConfig от Gemini), её атрибуты попадают в контракт
# как имена конфига. Сборщик ловит алиасы на уровне модуля и области видимости не
# считает. Сейчас все такие имена приходят из getattr с запасом, то есть в
# НЕобязательные, и шаблона не касаются. Печатаю их вслух, а не проверкой: проверка
# краснела бы от чужого черновика, а причина была бы не в шаблоне.
_phantom = sorted(n for n in list(REQUIRED) + list(OPTIONAL) if not n.isupper())
if _phantom:
    print(f"       имена не в верхнем регистре (вероятно затенение локальной "
          f"переменной config, шаблона не касаются): {_phantom}")
    _phantom_req = sorted(n for n in REQUIRED if not n.isupper())
    if _phantom_req:
        print(f"       ВНИМАНИЕ: из них ОБЯЗАТЕЛЬНЫМИ считаются {_phantom_req} — "
              f"шаблон потребуют дополнить именем, которого в конфиге нет")

print("[1a] Правило «обязательное или нет» проверено на синтетике")
check("config.ИМЯ на чтение -> обязательное", "DIRECT_READ" in SREQ,
      f"обязательные из синтетики: {sorted(SREQ)}")
check("getattr(config, \"ИМЯ\") в 2 аргумента -> обязательное", "GETATTR_2" in SREQ,
      "без третьего аргумента getattr бросает тот же AttributeError, что и точка; "
      "прощать его -- значит разрешить шаблону потерять имя")
check("getattr(config, \"ИМЯ\", запас) в 3 аргумента -> НЕ обязательное",
      "GETATTR_3" in SOPT and "GETATTR_3" not in SREQ,
      f"необязательные из синтетики: {sorted(SOPT)}")
check("hasattr(config, \"ИМЯ\") -> НЕ обязательное",
      "HASATTR_ONLY" in SOPT and "HASATTR_ONLY" not in SREQ)
check("config.ИМЯ = ... не считается чтением",
      "STORE_ONLY" not in SREQ and "STORE_ONLY" not in SOPT,
      "так тесты подменяют конфиг; считать это чтением -- требовать от шаблона "
      "имена, которых код никогда не читает")
check("import config as cfg тоже отслеживается", "ALIAS_READ" in SREQ,
      "иначе файл с алиасом молча выпадает из контракта целиком")
check("имя, читаемое и с запасом и без, остаётся обязательным",
      "BOTH_WAYS" in SREQ and "BOTH_WAYS" not in SOPT,
      "хватает одного места без запаса, чтобы пропуск имени стал AttributeError")


# --------------------------------------------------------------------------
# Исполнение шаблона. Копия уносится во временный каталог: load_dotenv() ищет
# .env вверх от файла, и из временного каталога боевой .env не подхватывается —
# значит проверяется шаблон, а не чужие значения из окружения этой машины.
# --------------------------------------------------------------------------
ENV_STUB = {
    "TG_BOT_TOKEN": "ENVSTUB_TG_BOT_TOKEN",
    "TG_API_ID": "424242",              # число: шаблон приводит его через int()
    "TG_API_HASH": "ENVSTUB_TG_API_HASH",
    "TG_SESSION_NAME": "ENVSTUB_TG_SESSION_NAME",
    "SOURCE_CHAT_ID": "-100123",        # число: тот же int()
    "REPORT_CHAT_ID": "ENVSTUB_REPORT_CHAT_ID",
    "REPORT_TARGETS": '[{"chat_id": -100123, "topic_id": null}]',  # разбирается json.loads
    "GROQ_VISION_MODEL": "ENVSTUB_GROQ_VISION_MODEL",
    "GOOGLE_API_KEYS": "kA, kB",        # список через запятую: проверяем parse_keys
    "GROQ_API_KEYS": "kC",
    "TELEGRAPH_TOKEN": "ENVSTUB_TELEGRAPH_TOKEN",
    "GEMINI_MODEL": "ENVSTUB_GEMINI_MODEL",
    "GROQ_MODEL": "ENVSTUB_GROQ_MODEL",
    "SEARCH_PROVIDER": "ENVSTUB_SEARCH_PROVIDER",
    "TAVILY_API_KEY": "ENVSTUB_TAVILY_API_KEY",
    "DB_PATH": "ENVSTUB_DB_PATH",
}
# Переменные, без которых шаблон обязан отказаться стартовать (required=True).
MUST_HAVE_ENV = ("TG_BOT_TOKEN", "TG_API_ID", "TG_API_HASH")


def env_keys_of(path):
    """Все ключи окружения, которые файл читает через get_env("КЛЮЧ", ...)."""
    keys = set()
    for node in ast.walk(parse_file(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "get_env" and node.args \
                and isinstance(node.args[0], ast.Constant):
            keys.add(node.args[0].value)
    return keys


def exec_template(path, env, drop=()):
    """
    Исполнить шаблон как модуль. Возвращает (модуль, напечатанное, исключение).

    Значения окружения подставляются ДО исполнения, поэтому load_dotenv() их не
    перекрывает (он не перезаписывает уже заданные ключи).
    """
    touched = set(ENV_STUB) | set(env) | set(drop)
    saved = {k: os.environ.get(k) for k in touched}
    for k in touched:
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    buf, old_out = io.StringIO(), sys.stdout
    sys.stdout = buf
    module, exc = None, None
    try:
        spec = importlib.util.spec_from_file_location("stomchat_cfg_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except SystemExit as e:
        exc = e
    except Exception as e:
        exc = e
    finally:
        sys.stdout = old_out
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return module, buf.getvalue(), exc


def template_copy(name, source=None, mutate=None):
    """Копия шаблона во временном каталоге; mutate(текст) -> текст для мета-блока."""
    src = source if source is not None else io.open(TEMPLATE, encoding="utf-8").read()
    if mutate is not None:
        src = mutate(src)
    dst = os.path.join(_TMPDIR, name)
    with io.open(dst, "w", encoding="utf-8") as fh:
        fh.write(src)
    return dst


TPL_SRC = io.open(TEMPLATE, encoding="utf-8").read()
PROBE = template_copy("probe_full.py")

print("\n[2] Шаблон исполняется и отдаёт КАЖДОЕ обязательное имя")
mod, printed, exc = exec_template(PROBE, dict(ENV_STUB))
check("шаблон исполнился без исключения", exc is None, f"{type(exc).__name__}: {exc}")
missing = sorted(n for n in REQUIRED if not hasattr(mod, n))
check("ни одного пропущенного обязательного имени", not missing,
      f"нет в шаблоне: {missing} — на чужой машине это AttributeError, "
      f"а для REPORT_TARGETS ещё и молчаливый ноль получателей")
def where_read(name):
    """
    Куда послать человека, который увидел FAIL.

    Не первое обращение по алфавиту: у API_ID первым оказывается deppd.py, у
    SOURCE_CHAT_ID -- _bak_assistant_tgdelivery.py, и подсказка отправляет чинить
    мусорный файл вместо main.py. Беру файл с НАИБОЛЬШИМ числом обращений, откинув
    черновики (_*) и тесты (test_*) -- по замеру это main.py для всех имён, кроме
    DB_PATH, где это database.py. Счёт самоподдерживающийся: он не завязан на
    список имён файлов, который однажды устареет.

    На вердикт не влияет, это только текст подсказки. Обязательность имени
    по-прежнему считается по ВСЕМ файлам: откинь тесты из самого контракта -- и
    боевой файл с неудачным именем однажды тихо выпадет из проверки.
    """
    hits = REQUIRED[name]
    per_file = {}
    for h in hits:
        per_file.setdefault(h.rsplit(":", 1)[0], []).append(h)
    live = {f: v for f, v in per_file.items()
            if not f.startswith("_") and not f.startswith("test_")}
    pool = live or per_file
    best = sorted(pool, key=lambda f: (-len(pool[f]), f))[0]
    return f"{pool[best][0]}, всего обращений {len(hits)}"


for name in sorted(REQUIRED):
    check(f"шаблон объявляет {name}", hasattr(mod, name),
          f"читается в {where_read(name)}")

print("\n[3] Значения приходят ИЗ ОКРУЖЕНИЯ, а типы приведены")
EXPECTED = {
    "BOT_TOKEN": "ENVSTUB_TG_BOT_TOKEN",
    "API_ID": 424242,
    "API_HASH": "ENVSTUB_TG_API_HASH",
    "SESSION_NAME": "ENVSTUB_TG_SESSION_NAME",
    "SOURCE_CHAT_ID": -100123,
    "REPORT_CHAT_ID": "ENVSTUB_REPORT_CHAT_ID",
    "REPORT_TARGETS": [{"chat_id": -100123, "topic_id": None}],
    "GOOGLE_KEYS": ["kA", "kB"],
    "GROQ_KEYS": ["kC"],
    "TELEGRAPH_TOKEN": "ENVSTUB_TELEGRAPH_TOKEN",
    "GEMINI_MODEL": "ENVSTUB_GEMINI_MODEL",
    "GROQ_MODEL": "ENVSTUB_GROQ_MODEL",
    "SEARCH_PROVIDER": "ENVSTUB_SEARCH_PROVIDER",
    "TAVILY_API_KEY": "ENVSTUB_TAVILY_API_KEY",
    "DB_PATH": "ENVSTUB_DB_PATH",
    "REPORT_HOUR": 0,
}
uncovered = sorted(n for n in REQUIRED if n not in EXPECTED)
check("проверкой значения покрыто каждое обязательное имя", not uncovered,
      f"не покрыты: {uncovered} — контракт вырос, тест не обновлён")
for name, want in EXPECTED.items():
    got = getattr(mod, name, "<нет>")
    check(f"{name} берётся из .env и приведён к типу", got == want,
          f"получено {got!r}, ожидалось {want!r}")
check("API_ID именно int, а не строка", isinstance(getattr(mod, "API_ID", None), int),
      "Telethon со строковым API_ID не авторизуется, и ошибка вылезет в сети")
check("SOURCE_CHAT_ID именно int", isinstance(getattr(mod, "SOURCE_CHAT_ID", None), int),
      "строка вместо id — EventBuilder.resolve рвёт регистрацию всех хендлеров")
check("REPORT_TARGETS именно list", isinstance(getattr(mod, "REPORT_TARGETS", None), list),
      "dict проходит json.loads, а рассылка при этом выключается целиком")
check("get_env и parse_keys есть в шаблоне",
      callable(getattr(mod, "get_env", None)) and callable(getattr(mod, "parse_keys", None)),
      "без них форма шаблона не совпадает с живым конфигом")
check("каждый ключ окружения шаблона подставлен этим тестом",
      not (env_keys_of(TEMPLATE) - set(ENV_STUB)),
      f"без заглушки остались {sorted(env_keys_of(TEMPLATE) - set(ENV_STUB))} — "
      f"их значения пришли бы из окружения машины, и проверка стала бы случайной")

print("\n[4] В шаблоне не зашито ни одного значения (пустое окружение)")
bare = {k: ENV_STUB[k] for k in MUST_HAVE_ENV}
mod2, printed2, exc2 = exec_template(PROBE, bare, drop=tuple(ENV_STUB))
check("шаблон исполняется, когда задано только обязательное", exc2 is None,
      f"{type(exc2).__name__}: {exc2}")
# Единственное разрешённое непустое значение: относительный путь базы. Он уже был
# в версионном шаблоне, в нём нет ни имени пользователя, ни секрета.
ALLOWED_DEFAULT = {"DB_PATH": "stomat_bot.db"}
leaked = []
for name in sorted(REQUIRED):
    value = getattr(mod2, name, None)
    if name in ALLOWED_DEFAULT:
        continue
    if name in ("BOT_TOKEN", "API_ID", "API_HASH"):
        continue  # заданы принудительно, иначе шаблон отказывается стартовать
    if value:
        leaked.append(name)
check("ни одно имя не имеет непустого значения по умолчанию", not leaked,
      f"значения зашиты в версионный файл: {leaked} — это утечка с рабочей машины")
check("DB_PATH остаётся относительным путём без имени пользователя",
      getattr(mod2, "DB_PATH", None) == ALLOWED_DEFAULT["DB_PATH"],
      f"получено {getattr(mod2, 'DB_PATH', None)!r}")
check("SOURCE_CHAT_ID без переменной окружения даёт None, а не 0 и не пустую строку",
      getattr(mod2, "SOURCE_CHAT_ID", "<нет>") is None,
      f"получено {getattr(mod2, 'SOURCE_CHAT_ID', '<нет>')!r}; 0 — валидный id для Telethon, "
      f"и молчаливая подмена увела бы бота слушать не тот чат")
check("REPORT_TARGETS без переменной окружения даёт пустой список",
      getattr(mod2, "REPORT_TARGETS", None) == [],
      f"получено {getattr(mod2, 'REPORT_TARGETS', None)!r}")

# Секрет мог попасть и не в аргумент get_env — например, в произвольную строку.
# Ищем в литералах шаблона длинные слитные строки: у токенов и ключей ровно такой
# вид, у русских комментариев — нет.
SECRETISH = re.compile(r"^[A-Za-z0-9_\-:/\.]{20,}$")
suspicious = []
for node in ast.walk(ast.parse(TPL_SRC)):
    if isinstance(node, ast.Constant) and isinstance(node.value, str) \
            and SECRETISH.match(node.value) and any(c.isdigit() for c in node.value):
        suspicious.append(node.value[:8] + "...")
check("в литералах шаблона нет строк, похожих на ключ или имя модели",
      not suspicious, f"подозрительные литералы: {suspicious}")
check("в шаблоне нет упоминания файла сессии", ".session" not in TPL_SRC.replace(
      "*.session", ""), "имя сессии с рабочей машины — утечка, .gitignore её не прикрывает")

print("\n[5] Валидации шаблона работают, а не просто описаны")
_, out_bad_id, exc_bad_id = exec_template(
    PROBE, dict(ENV_STUB, TG_API_ID="сорок два"))
check("нечисловой TG_API_ID останавливает старт", isinstance(exc_bad_id, SystemExit),
      f"получено {type(exc_bad_id).__name__}: {exc_bad_id} — Telethon упал бы позже и в сети")
check("причина отказа названа в выводе", "TG_API_ID" in out_bad_id, f"вывод: {out_bad_id!r}")

_, out_no_token, exc_no_token = exec_template(
    PROBE, {k: v for k, v in ENV_STUB.items() if k != "TG_BOT_TOKEN"},
    drop=("TG_BOT_TOKEN",))
check("отсутствие TG_BOT_TOKEN останавливает старт", isinstance(exc_no_token, SystemExit),
      f"получено {type(exc_no_token).__name__}")
check("в выводе названо ИМЯ пропавшей переменной", "TG_BOT_TOKEN" in out_no_token,
      f"вывод: {out_no_token!r}")

mod_bad_chat, out_bad_chat, exc_bad_chat = exec_template(
    PROBE, dict(ENV_STUB, SOURCE_CHAT_ID="12ab34"))
check("битый SOURCE_CHAT_ID не роняет конфиг", exc_bad_chat is None,
      f"{type(exc_bad_chat).__name__}: {exc_bad_chat}")
check("битый SOURCE_CHAT_ID даёт None", getattr(mod_bad_chat, "SOURCE_CHAT_ID", "<нет>") is None)
check("о битом SOURCE_CHAT_ID предупреждено", "ПРЕДУПРЕЖДЕНИЕ" in out_bad_chat,
      f"вывод: {out_bad_chat!r}")
check("само значение в вывод не подставляется", "12ab34" not in out_bad_chat,
      "id чата, напечатанный в stdout, подберёт журнал запуска")

mod_bad_t, out_bad_t, exc_bad_t = exec_template(
    PROBE, dict(ENV_STUB, REPORT_TARGETS="{это не json"))
check("битый REPORT_TARGETS не роняет конфиг", exc_bad_t is None,
      f"{type(exc_bad_t).__name__}: {exc_bad_t}")
check("битый REPORT_TARGETS даёт пустой список",
      getattr(mod_bad_t, "REPORT_TARGETS", None) == [])
check("в выводе названо последствие для рассылки",
      "REPORT_TARGETS" in out_bad_t and "никому" in out_bad_t.lower(),
      f"вывод: {out_bad_t!r}")

_, out_no_keys, _ = exec_template(
    PROBE, {k: v for k, v in ENV_STUB.items()
            if k not in ("GOOGLE_API_KEYS", "GROQ_API_KEYS")},
    drop=("GOOGLE_API_KEYS", "GROQ_API_KEYS"))
check("отсутствие ключей нейросетей объявлено вслух", "ВНИМАНИЕ" in out_no_keys,
      f"вывод: {out_no_keys!r} — иначе сводка молча не соберётся")

_, out_tavily, _ = exec_template(
    PROBE, {k: v for k, v in dict(ENV_STUB, SEARCH_PROVIDER="tavily").items()
            if k != "TAVILY_API_KEY"},
    drop=("TAVILY_API_KEY",))
check("Tavily без ключа объявлен вслух", "Tavily" in out_tavily or "TAVILY" in out_tavily,
      f"вывод: {out_tavily!r}")

print("\n[6] Шаблон печатается на cp1251-консоли без UnicodeEncodeError")
bad_chars = []
for ch in set(TPL_SRC):
    try:
        ch.encode("cp1251")
    except UnicodeEncodeError:
        bad_chars.append(ch)
check("ни одного символа вне cp1251", not bad_chars,
      f"cp1251 не берёт {sorted(bad_chars)!r} — print падает САМ, и в трейсбеке "
      f"оказывается строка печати вместо настоящей ошибки .env")

print("\n[7] Шаблон не отстаёт от живого config.py по набору имён")
if not os.path.exists(LIVE):
    check("config.py отсутствует (чистый клон) — сравнение неприменимо", True)
else:
    def upper_assignments(path):
        found = set()
        for node in ast.walk(parse_file(path)):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id.isupper():
                        found.add(t.id)
        return found

    live_names = upper_assignments(LIVE)
    tpl_names = upper_assignments(TEMPLATE)
    drift = sorted(live_names - tpl_names)
    # Только имена, без значений: значения живого конфига в вывод не попадают.
    check("каждое имя живого config.py есть в шаблоне", not drift,
          f"живой конфиг ушёл вперёд на {drift} — именно так шаблон и стух")
    print(f"       имён в живом конфиге: {len(live_names)}, в шаблоне: {len(tpl_names)}")

print("\n[8] Проверка [2] действительно падает на неполном шаблоне")


def rename_assignment(name):
    """Убрать имя из шаблона, не ломая синтаксис: переименовать цель присваивания."""
    def mutate(src):
        return re.sub(rf"^(\s*){name}(\s*=)", rf"\g<1>{name}_УБРАНО\g<2>", src,
                      flags=re.MULTILINE)
    return mutate


for victim in ("API_ID", "SOURCE_CHAT_ID", "REPORT_TARGETS", "DB_PATH"):
    broken = template_copy(f"probe_no_{victim.lower()}.py", mutate=rename_assignment(victim))
    bmod, _, bexc = exec_template(broken, dict(ENV_STUB))
    still_syntactic = bexc is None or isinstance(bexc, SystemExit)
    check(f"копия без {victim} собирается (проверяем контракт, а не синтаксис)",
          still_syntactic, f"{type(bexc).__name__}: {bexc}")
    check(f"контракт видит пропажу {victim}", not hasattr(bmod, victim),
          "имя осталось на месте — мутация не сработала, и мета-проверка пуста")

# То же, но целиком через ту же функцию сравнения, что и пункт [2].
def contract_gaps(module):
    return sorted(n for n in REQUIRED if not hasattr(module, n))


broken = template_copy("probe_no_three.py",
                       mutate=lambda s: rename_assignment("REPORT_TARGETS")(
                           rename_assignment("SOURCE_CHAT_ID")(
                               rename_assignment("API_ID")(s))))
bmod, _, _ = exec_template(broken, dict(ENV_STUB))
gaps = contract_gaps(bmod)
check("шаблон в состоянии «как было до правки» проверку НЕ проходит",
      set(gaps) >= {"API_ID", "SOURCE_CHAT_ID", "REPORT_TARGETS"},
      f"дыры найдены: {gaps} — если пусто, пункт [2] не значит ничего")
check("на целом шаблоне та же функция дыр не находит", not contract_gaps(mod),
      f"дыры: {contract_gaps(mod)}")

print("\n[9] Обращения по переменной перечислены, а не потеряны молча")
# getattr(config, attr, None) в gemini_client.py читает имена из кортежа. Такие
# имена тест разворачивает; всё, что развернуть нельзя, печатается здесь, чтобы
# пробел в проверке был видимым, а не притворялся полнотой.
for line in DYNAMIC:
    print(f"       динамическое обращение: {line}")
check("нераспознанных обращений по переменной нет",
      not [d for d in DYNAMIC if "не литерал" in d],
      f"эти имена контракт проверить не может: "
      f"{[d for d in DYNAMIC if 'не литерал' in d]}")
check("OPENROUTER_KEYS распознан как необязательный", "OPENROUTER_KEYS" in OPTIONAL,
      f"необязательные: {sorted(OPTIONAL)}")

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
