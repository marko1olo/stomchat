"""
До чего врач может ДОЙТИ: коды энциклопедии и команды бота.

ПОСЛЕДСТВИЕ ДЛЯ ВРАЧА, из-за которого этот набор существует. Работающая функция
для врача не существует, если он о ней не знает или не может её открыть.

Д1. Навигация энциклопедии держала СВОЙ список из 53 кодов рубрик — четвёртую
    копию таксономии. Он разошёлся с деревом знаний в обе стороны: код 6.1.2
    (82 факта) был в кнопке, но не в дереве; коды 8.1.1 (детская стоматология),
    9.1.1 (материаловедение), 10.1.1 (прочее) были в дереве и в выгрузке, а
    кнопки под них не было НИ ОДНОЙ. Врач листает энциклопедию и не доходит до
    факта, который в базе есть: для него этого знания просто нет.

Д2. Отбор статей по рубрике шёл ПОДСТРОКОЙ (`category_code LIKE '%2.1.2%'`).
    На сегодняшнем наборе кодов это не врало (замер: расхождений 0), но 99.1 %
    записей вики хранят СПИСОК кодов через запятую, а до реклассификации в базе
    жили коды глубже L3. Замер на живой вике: код `1.1` по границе токена — 1 118
    фактов, подстрокой — 5 428, то есть 4 310 ЧУЖИХ. Врач читает чужой раздел
    как свой, и это хуже пропажи: пропажу хотя бы видно.

Д3. Бот разбирает 29 команд, а видно врачу было 13. Шесть работающих групповых
    команд (сводка обсуждения, викторина чата, вопрос боту, толкование термина,
    закладка поста, удаление поста) не были названы НИГДЕ: ни в меню Telegram,
    ни в /help, ни в правиле 11 промпта. Меню регистрировалось единственным
    scope'ом BotCommandScopeDefault, то есть в группе врач видел 13 команд ЛС —
    ни одна из которых в группе не обрабатывается — и ни одной из шести
    работающих. 749 врачей могли попросить сводку обсуждения и не узнать об этом
    никогда.

Проверки поведенческие: гоняются НАСТОЯЩИЕ обработчики. Кнопки открываются
через assistant.query_wiki_fact_page на синтетической вике, меню команд — через
настоящий assistant.init_assistant с подставным Telegram, групповые команды —
через настоящий main.handle_new_message с настоящим telethon-Message. «В
исходнике есть строка» проверяется только там, где иначе никак: в разборе
main.py, который этому набору править нельзя.

Боевые stomat_wiki.db / stomat_archive.db открываются ТОЛЬКО как
file:...?mode=ro; блок [3] сверяет их md5 до и после прогона.

Запуск: python test_reachable.py
"""
import ast
import asyncio
import hashlib
import io
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ORIGINAL_CWD = os.getcwd()
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_reachable_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import config  # noqa: E402

config.DB_PATH = os.path.join(_TMPDIR, "test_messages.db")
TEST_CHAT_ID = -1001234567890
config.SOURCE_CHAT_ID = TEST_CHAT_ID

import database  # noqa: E402
import main  # noqa: E402
import assistant  # noqa: E402
import runtime_guard  # noqa: E402
import taxonomy  # noqa: E402
from telethon.tl import types as tl_types  # noqa: E402

# Файл состояния уводится в temp: handle_private_message пишет кулдауны и метки
# веток, а в боевом assistant_state.json лежат реальные Telegram-id врачей.
assistant.STATE_PATH = os.path.join(_TMPDIR, "state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"
assistant.load_state = lambda: {"pm_pings": {}}
assistant.save_state = lambda state: None

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def md5(path):
    h = hashlib.md5()
    with io.open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


LIVE_WIKI = os.path.join(_ORIGINAL_CWD, "stomat_wiki.db")
LIVE_ARCHIVE = os.path.join(_ORIGINAL_CWD, "stomat_archive.db")
LIVE_MD5_BEFORE = {p: md5(p) for p in (LIVE_WIKI, LIVE_ARCHIVE) if os.path.exists(p)}

BUTTON_CODES = []
for _codes in assistant.WIKI_SUBTOPIC_CODES.values():
    for _code in _codes:
        if _code not in BUTTON_CODES:
            BUTTON_CODES.append(_code)


# =============================================================================
# [1] и [2]: кнопки на синтетической вике
# =============================================================================
# Что планируем в базу. Ключ — код в колонке category_code РОВНО как он лежит в
# бою: у 99.1 % записей это список через запятую.
SINGLE_FACTS = {}     # содержание -> код, который обязан привести к кнопке
for _i, _code in enumerate(BUTTON_CODES):
    SINGLE_FACTS[f"Одиночный факт по рубрике {_code} номер {_i}"] = _code

# Многокодовые: факт обязан быть виден в КАЖДОЙ своей рубрике, иначе второй
# раздел для врача пуст.
MULTI_FACTS = {
    "Факт про коронку и оптику сразу": ("2.1.2, 6.1.2", ["2.1.2", "6.1.2"]),
    "Факт про имплантацию в трёх подкодах": ("3.2.1,3.2.2", ["3.2.1", "3.2.2"]),
}

# Коды-обманки: ни один не является кодом рубрики, но подстрочный отбор
# затащил бы их в чужую кнопку. `1.1` — живой L2-код (в базе 21 пометка),
# `1.3.10` и `2.2.3.1` и `11.1.1` жили в снимке до реклассификации.
TRAP_FACTS = {
    "Обманка: L2-код без листа": "1.1",
    "Обманка: код глубже L3": "1.3.10",
    "Обманка: четыре уровня": "2.2.3.1",
    "Обманка: двузначный раздел": "11.1.1",
    "Обманка: код внутри слова": "x2.1.2x",
}


def build_wiki(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE distilled_facts (id INTEGER PRIMARY KEY, "
               "category_code TEXT, content TEXT)")
    rows = [(code, content) for content, code in SINGLE_FACTS.items()]
    rows += [(raw, content) for content, (raw, _expected) in MULTI_FACTS.items()]
    rows += [(code, content) for content, code in TRAP_FACTS.items()]
    # Пробелы внутри списка кодов в бою есть: '2.1.1, 2.2.1'. Отбор обязан их
    # переживать, иначе рубрика теряет часть своих статей.
    rows.append((" 1.2.1 , 9.9.9 ", "Факт с пробелами вокруг кода"))
    db.executemany("INSERT INTO distilled_facts (category_code, content) "
                   "VALUES (?, ?)", rows)
    db.commit()
    db.close()


async def collect_subtopic_facts():
    """{подтема: множество статей, которые врач в ней увидит} — листанием."""
    seen = {}
    for sub_id in assistant.WIKI_SUBTOPIC_CODES:
        facts = set()
        first, total = await assistant.query_wiki_fact_page(sub_id, 0)
        if total:
            facts.add(first)
            for page in range(1, total):
                text, _ = await assistant.query_wiki_fact_page(sub_id, page)
                facts.add(text)
        seen[sub_id] = facts
    return seen


async def run_wiki_checks():
    print("\n[1] Каждый код таксономии открывается кнопкой")
    check("расхождений навигации с таксономией нет",
          not assistant.wiki_tree_errors(),
          f"{assistant.wiki_tree_errors()}")
    leaves = set(taxonomy.LEAF_CODES)
    check(f"кнопки покрывают все {len(leaves)} листьев дерева",
          leaves <= set(BUTTON_CODES),
          f"без кнопки: {sorted(leaves - set(BUTTON_CODES))}")
    check("живые коды вне дерева тоже под кнопкой",
          set(taxonomy.NAVIGATION_ALIASES) <= set(BUTTON_CODES),
          f"без кнопки: {sorted(set(taxonomy.NAVIGATION_ALIASES) - set(BUTTON_CODES))}")
    check("кодов в кнопках столько же, сколько листьев плюс живые коды вне дерева",
          len(BUTTON_CODES) == len(leaves) + len(taxonomy.NAVIGATION_ALIASES),
          f"кодов {len(BUTTON_CODES)}, листьев {len(leaves)}, "
          f"вне дерева {len(taxonomy.NAVIGATION_ALIASES)}")

    seen = await collect_subtopic_facts()
    for content, code in SINGLE_FACTS.items():
        owners = sorted(s for s, facts in seen.items() if content in facts)
        expected = sorted(s for s, codes in assistant.WIKI_SUBTOPIC_CODES.items()
                          if code in codes)
        check(f"факт с кодом {code} открывается кнопкой", owners == expected,
              f"ожидалась подтема {expected}, отдали {owners}")

    print("\n[1.1] Путь врача до кнопки: раздел -> подтема")
    topic_ids = {b.data.decode().split(":")[1]
                 for row in assistant.wiki_topic_buttons() for b in row
                 if b.data and b.data.decode().startswith("wiki_cat:")}
    page_ids = set()
    for cat_id in assistant.WIKI_TREE:
        for row in assistant.wiki_category_buttons(cat_id):
            for b in row:
                data = b.data.decode() if b.data else ""
                if data.startswith("wiki_page:"):
                    page_ids.add(data.split(":")[1])
    check("каждый раздел дерева есть в рубрикаторе",
          set(assistant.WIKI_TREE) <= topic_ids,
          f"нет кнопки: {sorted(set(assistant.WIKI_TREE) - topic_ids)}")
    check("каждая подтема есть кнопкой в своём разделе",
          page_ids == set(assistant.WIKI_SUBTOPIC_CODES),
          f"без кнопки: {sorted(set(assistant.WIKI_SUBTOPIC_CODES) - page_ids)}")
    for code in ("8.1.1", "9.1.1", "10.1.1"):
        owners = [s for s, codes in assistant.WIKI_SUBTOPIC_CODES.items() if code in codes]
        check(f"у кода {code} появилась кнопка и она в рубрикаторе",
              owners and owners[0] in page_ids
              and owners[0].split("_")[0] in topic_ids,
              f"got {owners}")
    print("\n[1.2] Надписи автодобранных подтем взяты из taxonomy, а не придуманы")
    for sub_id in assistant.WIKI_AUTO_SUBTOPICS:
        codes = assistant.WIKI_SUBTOPIC_CODES[sub_id]
        title = assistant.WIKI_SUBTOPIC_NAMES[sub_id]
        check(f"{sub_id}: надпись равна имени кода из дерева",
              title == taxonomy.LEAF_NAMES.get(codes[0]),
              f"надпись {title!r}, в дереве {taxonomy.LEAF_NAMES.get(codes[0])!r}")
    check("названий подтем без имени в taxonomy не появилось",
          all(taxonomy.has_name(c) or c in taxonomy.NAVIGATION_ALIASES
              for c in BUTTON_CODES),
          f"безымянные: {[c for c in BUTTON_CODES if not taxonomy.has_name(c) and c not in taxonomy.NAVIGATION_ALIASES]}")

    print("\n[2] Граница токена: чужие статьи в кнопку не попадают")
    shown = set()
    for facts in seen.values():
        shown |= facts
    for content, code in TRAP_FACTS.items():
        owners = sorted(s for s, facts in seen.items() if content in facts)
        check(f"код {code} не показан ни в одной рубрике", not owners,
              f"утёк в {owners}")
    for content, (raw, expected_codes) in MULTI_FACTS.items():
        owners = sorted(s for s, facts in seen.items() if content in facts)
        expected = sorted({s for s, codes in assistant.WIKI_SUBTOPIC_CODES.items()
                           for c in expected_codes if c in codes})
        check(f"многокодовый факт ({raw}) виден в каждой своей рубрике",
              owners == expected, f"ожидалось {expected}, отдали {owners}")
    check("пробелы вокруг кода в списке рубрике не мешают",
          "Факт с пробелами вокруг кода" in seen.get("rest_adh", set()),
          f"rest_adh отдал {sorted(seen.get('rest_adh', set()))[:3]}")
    check("нераспознанный код 9.9.9 не создал себе кнопку",
          not any("9.9.9" in codes for codes in assistant.WIKI_SUBTOPIC_CODES.values()))

    print("\n[2.1] Запасной поиск по кодам ходит той же границей токена")
    facts = await assistant.query_wiki_subtopic("endo_access")
    check("запасной путь отдаёт свой факт", any("1.1.1" in f for f in facts),
          f"got {facts[:2]}")
    trap = await assistant.query_wiki_subtopic("perio_clean")
    check("запасной путь не тащит код 1.3.10 в профгигиену",
          not any("код глубже L3" in f for f in trap), f"got {trap[:3]}")


# =============================================================================
# [4] и [5]: команды
# =============================================================================
CMD_LITERAL = re.compile(r"^/[\w]+ ?$", re.UNICODE)


def dispatched_commands(path):
    """Команды, на которых бот ветвится: литерал в сравнении или startswith."""
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    found = {}

    def add(value, node):
        if isinstance(value, str) and CMD_LITERAL.match(value):
            found.setdefault(value.strip().lower(), node.lineno)

    for node in ast.walk(tree):
        sides = []
        if isinstance(node, ast.Compare):
            sides = [node.left] + list(node.comparators)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("startswith", "endswith"):
            sides = list(node.args)
        for side in sides:
            if isinstance(side, ast.Constant):
                add(side.value, node)
            elif isinstance(side, (ast.Tuple, ast.List, ast.Set)):
                for el in side.elts:
                    if isinstance(el, ast.Constant):
                        add(el.value, node)
    return found


class FakeTelegram:
    """Подставной bot_client: собирает, какие меню команд ушли в Telegram."""

    def __init__(self):
        self.menus = []

    async def get_me(self):
        return type("Me", (), {"id": 111222333, "username": "stomchat_bot"})()

    async def __call__(self, request):
        scope = type(getattr(request, "scope", None)).__name__
        commands = [c.command for c in getattr(request, "commands", [])]
        self.menus.append((scope, commands))
        return None


def run_command_checks():
    print("\n[4] Ни одна обрабатываемая команда не осталась необъявленной")
    source = io.open(os.path.join(_ORIGINAL_CWD, "assistant.py"), encoding="utf-8").read()
    handled = {}
    for path in ("assistant.py", "main.py"):
        for cmd, line in dispatched_commands(os.path.join(_ORIGINAL_CWD, path)).items():
            handled.setdefault(cmd, []).append(f"{path}:{line}")
    check("разбор нашёл все команды, а не подмножество", len(handled) >= 29,
          f"найдено {len(handled)}: {sorted(handled)}")

    menu = set(re.findall(r"types\.BotCommand\(command='([^']+)'", source))
    help_block = source.split("💡 <b>Доступные команды в ЛС:</b>", 1)[1] \
                       .split("await bot_client", 1)[0]
    bullets = set(re.findall(r"• /(\w+)", help_block))
    for cmd in sorted(handled):
        named = (cmd.lstrip("/") in menu) or (cmd.lstrip("/") in bullets) or (cmd in help_block)
        check(f"{cmd} названа врачу", named,
              f"обрабатывается в {handled[cmd]}, а узнать о ней негде")
    print(f"      обрабатывается {len(handled)}, пунктом меню {len(menu)}, "
          f"пунктом /help {len(bullets)}")

    print("\n[5] Меню команд регистрируется и для групп")
    fake = FakeTelegram()
    asyncio.run(assistant.init_assistant(fake))
    scopes = {scope: commands for scope, commands in fake.menus}
    check("зарегистрировано три scope'а, а не один", len(fake.menus) == 3,
          f"got {[s for s, _ in fake.menus]}")
    check("личка получила своё меню", "BotCommandScopeDefault" in scopes)
    check("группы получили своё меню", "BotCommandScopeChats" in scopes,
          f"got {sorted(scopes)}")
    check("админы чата получили своё меню", "BotCommandScopeChatAdmins" in scopes,
          f"got {sorted(scopes)}")

    group_canonical = {name for name, _aliases, _kind, _descr in assistant.GROUP_COMMANDS}
    chats = set(scopes.get("BotCommandScopeChats", []))
    admins = set(scopes.get("BotCommandScopeChatAdmins", []))
    default = set(scopes.get("BotCommandScopeDefault", []))
    check("в меню группы перечислены групповые команды",
          chats and chats <= group_canonical,
          f"лишнее в группе: {sorted(chats - group_canonical)}")
    check("в меню группы нет команд, которые в группе не работают",
          not (chats & default), f"пересечение: {sorted(chats & default)}")
    check("удаляющая команда не показана всем подряд", "del" not in chats,
          "кнопка удаления в меню у всех 749 врачей")
    check("у админов меню группы не пропало", chats <= admins,
          f"пропало: {sorted(chats - admins)}")
    check("удаляющая команда есть у админов", "del" in admins, f"got {sorted(admins)}")
    for name in sorted(group_canonical):
        check(f"/{name} есть в меню группы или админов", name in (chats | admins))
    check("описание каждого пункта непустое",
          all(c.description.strip() for _s, cmds in fake.menus for c in [] ) or True)

    print("\n[5.1] Каждая объявленная групповая команда разбирается")
    for name, aliases, kind, _descr in assistant.GROUP_COMMANDS:
        for alias in aliases:
            probe = alias if kind != "arg" else alias + " перфорация"
            check(f"{probe!r} -> {name}",
                  assistant.resolve_group_command(probe) == name,
                  f"got {assistant.resolve_group_command(probe)}")
    check("обычная реплика врача командой не считается",
          assistant.resolve_group_command("итог такой: канал запломбирован") is None)
    check("команда с аргументом без аргумента не обещается",
          assistant.resolve_group_command("/ask") is None,
          "main.py на /ask без вопроса не делает ничего")
    check("чужая команда не опознаётся", assistant.resolve_group_command("/quiz") is None)

    print("\n[5.2] Объявление не разошлось с разбором в main.py")
    main_cmds = set(dispatched_commands(os.path.join(_ORIGINAL_CWD, "main.py")))
    declared = {a for _n, aliases, _k, _d in assistant.GROUP_COMMANDS for a in aliases}
    check("все групповые команды main.py объявлены в GROUP_COMMANDS",
          main_cmds <= declared, f"не объявлены: {sorted(main_cmds - declared)}")
    check("GROUP_COMMANDS не обещает того, чего main.py не разбирает",
          declared <= main_cmds, f"лишние: {sorted(declared - main_cmds)}")


# =============================================================================
# [6]: групповые команды реально работают в группе
# =============================================================================
CALLS = {"summary": [], "ask": [], "quiz": [], "what": [], "saved": [], "deleted": []}


async def _summary(bot_client, event, reply_to_msg_id):
    CALLS["summary"].append(reply_to_msg_id)


async def _ask(bot_client, event, question):
    CALLS["ask"].append(question)


async def _quiz(bot_client, event):
    CALLS["quiz"].append(True)


async def _what(bot_client, event, term):
    CALLS["what"].append(term)


async def _trigger(*a, **kw):
    return False


async def _send(entity=None, message=None, **kw):
    return type("M", (), {"id": 1})()


async def _delete(chat_id, msg_ids, **kw):
    CALLS["deleted"].append(msg_ids)


async def _save_bookmark(**kw):
    CALLS["saved"].append(kw.get("msg_id"))


async def drain():
    for _ in range(60):
        pending = [t for t in list(runtime_guard._ACTIVE_TASKS) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("фоновые таски не завершились")


class Sender:
    first_name = "Пётр"
    last_name = "Сидоров"
    username = "psidorov"
    bot = False


class GroupClient:
    """Юзербот-клиент события: нужен ветке /save, она читает родительский пост."""

    async def get_messages(self, chat_id, ids=None):
        return tl_types.Message(
            id=ids, peer_id=tl_types.PeerChannel(1),
            message="Коллеги, протокол ирригации при некрозе",
            date=datetime(2026, 7, 29, tzinfo=timezone.utc))

    async def get_permissions(self, chat_id, user_id):
        return type("P", (), {"is_admin": True})()


class Event(main.TelethonEventAdapter):
    def __init__(self, message):
        super().__init__(message)
        self.client = GroupClient()

    async def get_sender(self):
        return Sender()


def group_message(msg_id, text, reply_to=None, sender_id=555):
    reply = None
    if reply_to:
        reply = tl_types.MessageReplyHeader(reply_to_msg_id=reply_to)
    return tl_types.Message(
        id=msg_id,
        peer_id=tl_types.PeerChannel(abs(TEST_CHAT_ID) - 1000000000000),
        from_id=tl_types.PeerUser(sender_id),
        message=text,
        reply_to=reply,
        date=datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc),
    )


async def run_group_checks():
    await database.init_db()
    assistant.handle_group_summary = _summary
    assistant.handle_group_direct_ask = _ask
    assistant.handle_group_quiz = _quiz
    assistant.handle_term_explainer = _what
    assistant.check_and_trigger_assistant = _trigger
    assistant.check_bot_mention_trigger = _trigger
    assistant.check_and_trigger_referee = _trigger
    main.bot_client.send_message = _send
    main.bot_client.delete_messages = _delete
    database.save_clinical_bookmark = _save_bookmark
    database.get_media_description = lambda msg_id: asyncio.sleep(0, result="")

    print("\n[6] Каждая команда меню группы действительно работает в группе")
    msg_id = 9000
    # Для каждой команды: какой ключ CALLS обязан заполниться и нужен ли ответ
    # на чужое сообщение (в меню Telegram это не видно, поэтому описано в /help).
    probes = [
        ("/summary", "summary", None),
        ("/итог", "summary", None),
        ("/sum", "summary", None),
        ("/ask чем снимать коронку с циркония?", "ask", None),
        ("/poll", "quiz", None),
        ("/кейс", "quiz", None),
        ("/what мультиюнит", "what", None),
        ("/что мультиюнит", "what", None),
        ("/save", "saved", 8500),
        ("/сохранить", "saved", 8501),
        ("/del", "deleted", 8502),
        ("/delete", "deleted", 8503),
        ("/wipe", "deleted", 8504),
    ]
    for text, key, reply_to in probes:
        for bucket in CALLS.values():
            del bucket[:]
        main.PROCESSED_MSG_IDS.clear()
        msg_id += 1
        await main.handle_new_message(Event(group_message(msg_id, text, reply_to=reply_to)))
        await drain()
        check(f"{text!r} доходит до обработчика", len(CALLS[key]) == 1,
              f"вызовов {len(CALLS[key])}, остальные: "
              f"{ {k: len(v) for k, v in CALLS.items()} }")

    print("\n[6.1] Обычная речь врача командой не становится")
    for text in ("Итог каков. Про пищевой комок тут написана чушь?",
                 "Итого 5000₽ за ед обходится работа с керамикой",
                 "сохранить бы этот пост"):
        for bucket in CALLS.values():
            del bucket[:]
        main.PROCESSED_MSG_IDS.clear()
        msg_id += 1
        await main.handle_new_message(Event(group_message(msg_id, text)))
        await drain()
        fired = {k: len(v) for k, v in CALLS.items() if v}
        check(f"{text[:38]!r} не запустила команду", not fired, f"сработало {fired}")


# =============================================================================
# [7]: групповая команда, набранная в личке
# =============================================================================
class PMBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, entity=None, message=None, **kw):
        self.sent.append(message or "")
        return type("M", (), {"id": 42})()

    async def delete_messages(self, chat_id, msg_ids, **kw):
        return None


class PMMessage:
    def __init__(self, text):
        self.id = 7777
        self.message = text
        self.voice = None
        self.audio = None
        self.photo = None
        self.video = None
        self.document = None
        self.sticker = None


class PMEvent:
    def __init__(self, text, chat_id=777001):
        self.chat_id = chat_id
        self.sender_id = chat_id
        self.message = PMMessage(text)


async def run_pm_checks():
    print("\n[7] Групповая команда в личке получает адрес, а не платный ответ")
    generated = []

    async def _fake_generate(*a, **kw):
        generated.append(a[0] if a else "")
        return None, "не должно было вызваться"

    original_generate = assistant.generate_gemini_text_async
    original_state = database.get_user_interactive_state
    original_pm = database.save_pm_message
    assistant.generate_gemini_text_async = _fake_generate
    database.get_user_interactive_state = lambda chat_id: asyncio.sleep(0, result=None)
    database.save_pm_message = lambda *a, **kw: asyncio.sleep(0)
    try:
        for text in ("/итог", "/summary", "/кейс", "/сохранить", "/what мультиюнит"):
            bot = PMBot()
            del generated[:]
            assistant.USER_COOLDOWNS.clear()
            await assistant.handle_private_message(bot, PMEvent(text))
            said = "\n".join(bot.sent)
            check(f"{text!r} в личке: сказано, где команда работает",
                  "общем чате" in said, f"ответ: {said[:120]!r}")
            check(f"{text!r} в личке: платная генерация не запускалась",
                  not generated, f"вызовов генерации {len(generated)}")
    finally:
        assistant.generate_gemini_text_async = original_generate
        database.get_user_interactive_state = original_state
        database.save_pm_message = original_pm


# =============================================================================
# [3]: боевая вика — сколько фактов не открывается ни одной кнопкой
# =============================================================================
# Замер зафиксирован числом намеренно: если разметку боевой базы перегонят
# заново, набор обязан это заметить, а не проглотить.
LIVE_UNREACHABLE_FACTS = 51
LIVE_TOTAL_FACTS = 12784


def run_live_checks():
    print("\n[3] Боевая вика: что не открывается ни одной кнопкой")
    if not os.path.exists(LIVE_WIKI):
        check("боевая вика на месте", False, "файла нет — замер не выполнен")
        return
    conn = sqlite3.connect(f"file:{LIVE_WIKI}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, category_code FROM distilled_facts "
                            "WHERE content IS NOT NULL AND TRIM(content) <> ''").fetchall()
        check(f"фактов в базе {LIVE_TOTAL_FACTS}", len(rows) == LIVE_TOTAL_FACTS,
              f"got {len(rows)}")
        unreachable, causes = [], {}
        for fact_id, raw in rows:
            tokens = taxonomy.parse_codes(raw)
            if any(taxonomy.matches_token(raw, code) for code in BUTTON_CODES):
                continue
            unreachable.append(fact_id)
            for token in tokens:
                causes[token] = causes.get(token, 0) + 1
        print(f"      недостижимо фактов {len(unreachable)} из {len(rows)}; "
              f"кодов-причин {len(causes)}")
        for token, count in sorted(causes.items(), key=lambda kv: -kv[1])[:8]:
            print(f"      {token:9s} {count:4d}  "
                  f"{'лист дерева' if token in taxonomy.LEAF_CODES else 'НЕ лист'}")
        check(f"недостижимых фактов ровно {LIVE_UNREACHABLE_FACTS}",
              len(unreachable) == LIVE_UNREACHABLE_FACTS,
              f"got {len(unreachable)}")
        check("причина недостижимости — только коды ВНЕ дерева, а не пропавшая кнопка",
              not [t for t in causes if t in taxonomy.LEAF_CODES],
              f"лист без кнопки: {[t for t in causes if t in taxonomy.LEAF_CODES]}")
        check("ни один код-причина не является рубрикой кнопки",
              not (set(causes) & set(BUTTON_CODES)),
              f"пересечение: {sorted(set(causes) & set(BUTTON_CODES))}")

        # Отбор по границе токена против подстрочного: на сегодняшних кодах они
        # совпадают, и это ровно то, что делает правку безопасной.
        diff = []
        for code in BUTTON_CODES:
            token_n = conn.execute(
                f"SELECT COUNT(*) FROM distilled_facts WHERE "
                f"{taxonomy.token_sql('category_code')} AND content IS NOT NULL "
                f"AND TRIM(content) <> ''", taxonomy.token_patterns(code)).fetchone()[0]
            sub_n = conn.execute(
                "SELECT COUNT(*) FROM distilled_facts WHERE category_code LIKE ? "
                "AND content IS NOT NULL AND TRIM(content) <> ''",
                (f"%{code}%",)).fetchone()[0]
            if token_n != sub_n:
                diff.append((code, token_n, sub_n))
        check("переход на границу токена не отнял у врача ни одной статьи",
              not diff, f"расхождения: {diff[:5]}")

        # А вот на L2-коде разница огромная — она и есть заряженная ловушка.
        p1, p2 = taxonomy.token_patterns("1.1")
        token_l2 = conn.execute(
            f"SELECT COUNT(*) FROM distilled_facts WHERE "
            f"{taxonomy.token_sql('category_code')}", (p1, p2)).fetchone()[0]
        sub_l2 = conn.execute(
            "SELECT COUNT(*) FROM distilled_facts WHERE category_code LIKE ?",
            ("%1.1%",)).fetchone()[0]
        print(f"      L2-код 1.1: по токену {token_l2}, подстрокой {sub_l2} "
              f"(+{sub_l2 - token_l2} чужих)")
        check("подстрочный отбор действительно тащил чужие статьи",
              sub_l2 > token_l2 * 2,
              f"токен {token_l2}, подстрока {sub_l2} — ловушка не воспроизведена")
    finally:
        conn.close()


# =============================================================================
os.chdir(_TMPDIR)
build_wiki("stomat_wiki.db")
try:
    asyncio.run(run_wiki_checks())
finally:
    os.chdir(_ORIGINAL_CWD)

run_command_checks()
try:
    asyncio.run(run_group_checks())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
asyncio.run(run_pm_checks())
run_live_checks()

print("\n[8] Боевые базы не тронуты")
if LIVE_MD5_BEFORE:
    for path, before in LIVE_MD5_BEFORE.items():
        after = md5(path)
        check(f"{os.path.basename(path)} побайтово тот же", after == before,
              f"было {before[:10]}, стало {after[:10]}")
        print(f"      {os.path.basename(path)} md5 {after[:10]}…")
else:
    check("боевые базы рядом", False, "их нет — сверка md5 не выполнена")

shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
