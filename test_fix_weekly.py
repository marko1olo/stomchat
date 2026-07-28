"""
Недельный выпуск: расхождения с дневной веткой.

Правки годами вносили в одну ветку из двух, поэтому process_weekly_batch отстал
от process_summary_batch. Здесь прогоняется НАСТОЯЩИЙ недельный конвейер с
подставным Telegram-клиентом, подставной генерацией и подставным Telegraph, и
проверяется именно то, что расходилось:

  * кэш выпуска. Планировщик обходит цели по очереди; без кэша каждая цель
    получала свою генерацию и свою страницу Telegraph — два разных «единственных
    выпуска» про одну неделю и двойная плата за самую дорогую генерацию проекта;
  * бюджет длины. Промпт просил 8000–10000 символов при обрезке на 9500;
  * тег <br>. Telethon вырезает его молча, здесь это замерено на живой
    библиотеке, а не заявлено;
  * закреп без проверки sent_msg: отчёт доставлен и одновременно объявлен
    неудачей, а значит будет опубликован повторно;
  * стадия для сторожа в аварийной ветке;
  * подвал со счётчиком, который дописывался до обрезки и уезжал в отрез;
  * приписывание анонимных авторов реальному врачу по имени.

Боевые файлы не трогаются: журнал и сторожевые отметки уведены во временный
каталог, база не открывается вовсе (у выборки нет ответов, и get_texts_by_ids
возвращает пустой словарь, не дойдя до соединения).

Запуск: python test_fix_weekly.py
"""
import asyncio
import inspect
import logging
import os
import re
import sys
import tempfile
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
TMP_DIR = tempfile.mkdtemp(prefix="stomchat_weekly_")

# Путь журнала runtime_guard считает НА ИМПОРТЕ, поэтому переменная ставится до
# импорта проекта: иначе прогон дописывал бы боевой bot.log, который читают,
# когда разбираются в поведении бота.
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(TMP_DIR, "bot_test.log")

LIVE_FILES = (
    "stomat_bot.db", "assistant_state.json", "bot_state.json",
    "bot_summary_status.json", "bot_heartbeat.json", "bot.log",
)


def snapshot():
    state = {}
    for name in LIVE_FILES:
        try:
            stat = os.stat(os.path.join(REPO_DIR, name))
            state[name] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            state[name] = None
    return state


LIVE_BEFORE = snapshot()

import runtime_guard  # noqa: E402
import summarizer as S  # noqa: E402

# Пути сторожевых файлов в runtime_guard относительные, поэтому подменяем сами
# записи: боевой bot_summary_status.json — это то, по чему сторож решает, не
# зависла ли сводка, и тестовая отметка в нём означает ложную тревогу.
STAGES = []
runtime_guard.write_summary_status = lambda status: STAGES.append(dict(status))
runtime_guard.HEARTBEAT_PATH = os.path.join(TMP_DIR, "bot_heartbeat.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(TMP_DIR, "bot_watchdog_dump.txt")

# Без обработчика logging печатает ERROR через lastResort прямо в вывод теста.
logging.getLogger().addHandler(logging.NullHandler())

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def current_status():
    return dict(STAGES[-1]) if STAGES else {}


class FakeClient:
    """Считает отправки и закрепы и запоминает, что видел сторож в момент отправки."""

    def __init__(self, send_result="ok"):
        self.send_result = send_result
        self.sends = []
        self.pins = []
        self.status_at_send = []

    async def get_messages(self, chat_id, limit=None):
        return []

    async def send_message(self, chat_id, text, **params):
        self.status_at_send.append(current_status())
        self.sends.append({"chat_id": chat_id, "text": text, "params": params})
        if self.send_result is None:
            return None
        return type("Sent", (), {"id": 900 + len(self.sends)})()

    async def pin_message(self, chat_id, message_id, notify=False):
        self.pins.append((chat_id, message_id))


class FakeResponse:
    def __init__(self, text):
        self.text = text


GEN_CALLS = []
TELEGRAPH_CALLS = []


def install_stubs(generated_text, telegraph_ok=True):
    """Подменяет генерацию и публикацию; всё остальное в конвейере — настоящее."""
    del GEN_CALLS[:]
    del TELEGRAPH_CALLS[:]
    del STAGES[:]

    async def fake_generate(prompt, kind, chat_id, topic_id, message_count, prompt_chars):
        GEN_CALLS.append({
            "prompt": prompt, "kind": kind, "chat_id": chat_id,
            "message_count": message_count,
        })
        return FakeResponse(generated_text)

    async def fake_telegraph(title, html_content, timeout=None):
        TELEGRAPH_CALLS.append({"title": title, "html": html_content})
        if not telegraph_ok:
            return None, "telegraph-page timeout after 60s"
        return f"https://telegra.ph/weekly-{len(TELEGRAPH_CALLS)}", None

    S._generate_text_singleflight = fake_generate
    S.create_telegraph_page_async = fake_telegraph


def make_messages(count=6):
    """Выборка недели: реплики, которые проходят filter_useful_messages."""
    return [
        (
            1000 + i,
            f"Врач {i}",
            f"doc{i}",
            "Ретрит 36 зуба, протейперы, отлом файла в апикальной трети — что делать?",
            None,
            datetime(2026, 7, 20 + (i % 5), 10, i),
            None,
            None,
        )
        for i in range(count)
    ]


SHORT_ARTICLE = (
    "**ЭНДОДОНТИЧЕСКИЙ МАРАФОН**\n"
    "Разбирали ретрит 36 зуба: протейперы, отлом файла в апикальной трети.\n\n"
    "**МАТЕРИАЛОВЕДЕНИЕ**\nСравнивали два бонда, обсуждали MDP-мономер."
)

LONG_ARTICLE = "\n\n".join(
    f"**РАЗДЕЛ {i}: ПРОТОКОЛ АДГЕЗИИ И ГИБРИДНЫЙ СЛОЙ**\n"
    + "Обсуждали травление 15 секунд, промывку, подсушивание и бонд в два слоя. " * 8
    for i in range(1, 21)
)


async def scenario_cache_fast_path():
    print("\n[1] Кэш выпуска: вторая цель не платит за генерацию заново")
    install_stubs(SHORT_ARTICLE)
    client = FakeClient()
    delivered = []
    cached = (
        "🗞 <b>ВЫШЕЛ НОВЫЙ НОМЕР WEEKLY (27 июля 2026)</b>\n\n"
        "👉 <b><a href='https://telegra.ph/weekly-1'>ЧИТАТЬ ПОЛНЫЙ ВЫПУСК</a></b>"
    )
    result = await S.process_weekly_batch(
        make_messages(), client, -100777,
        delivery_hook=lambda message: delivered.append(message),
        cached_message=cached,
    )
    check("генерации не было", GEN_CALLS == [], f"got {len(GEN_CALLS)}")
    check("вторая страница Telegraph не создавалась", TELEGRAPH_CALLS == [], f"got {len(TELEGRAPH_CALLS)}")
    check("кэш отправлен как есть", [s["text"] for s in client.sends] == [cached],
          f"got {client.sends}")
    check("выпуск закреплён", len(client.pins) == 1, f"got {client.pins}")
    check("доставка отмечена для планировщика", len(delivered) == 1, f"got {delivered}")
    check("возвращён тот же текст", result == cached)
    check("сторож не остался с зажжённым флагом",
          current_status().get("active") is False, f"got {current_status()}")

    print("\n[2] Кэш не роняет ветку, если клиент вернул None вместо сообщения")
    install_stubs(SHORT_ARTICLE)
    client = FakeClient(send_result=None)
    result = await S.process_weekly_batch(
        make_messages(), client, -100777, cached_message="<b>тизер</b>",
    )
    check("возврата None (то есть «неудачи») нет", result == "<b>тизер</b>", f"got {result!r}")
    check("закреп не пытались делать у None", client.pins == [], f"got {client.pins}")


async def scenario_two_targets():
    print("\n[3] Две цели подряд, как их обходит планировщик")
    install_stubs(SHORT_ARTICLE)
    client = FakeClient()
    cache = None
    results = []
    for chat_id in (-100111, -100222):
        result = await S.process_weekly_batch(
            make_messages(), client, chat_id, cached_message=cache,
        )
        results.append(result)
        if result and not cache:
            cache = result

    check("генерация ровно одна на обе цели", len(GEN_CALLS) == 1, f"got {len(GEN_CALLS)}")
    check("страница Telegraph ровно одна на неделю", len(TELEGRAPH_CALLS) == 1,
          f"got {len(TELEGRAPH_CALLS)}")
    check("оба чата получили один и тот же выпуск", results[0] == results[1],
          f"{(results[0] or '')[:40]!r} vs {(results[1] or '')[:40]!r}")
    check("отправлено в оба чата", [s["chat_id"] for s in client.sends] == [-100111, -100222],
          f"got {[s['chat_id'] for s in client.sends]}")
    url = "https://telegra.ph/weekly-1"
    check("ссылка в обоих сообщениях ведёт на одну страницу",
          all(url in s["text"] for s in client.sends),
          f"got {[s['text'][:60] for s in client.sends]}")
    check("возвращаемый текст пригоден для кэша (это тизер со ссылкой)",
          bool(results[0]) and url in results[0], f"got {(results[0] or '')[:60]!r}")


def scenario_prompt():
    print("\n[4] Недельный промпт: бюджет длины совпадает с обрезкой")
    prompt = GEN_CALLS[0]["prompt"]
    check("промпт помечен как weekly", GEN_CALLS[0]["kind"] == "weekly")

    ranges = set(re.findall(r"(\d{4})\s*[–-]\s*(\d{4})\s*символ", prompt))
    ceilings = {int(high) for _, high in ranges}
    check("промпт называет ОДИН диапазон объёма", len(ranges) == 1, f"got {sorted(ranges)}")
    check(f"верхняя граница не выше предела обрезки {S.WEEKLY_HTML_LIMIT}",
          ceilings and max(ceilings) <= S.WEEKLY_HTML_LIMIT, f"got {sorted(ceilings)}")
    check("прежние 8000–10000 из промпта убраны", not any(h == 10000 for h in ceilings),
          "конвейер режет на 9500, верхняя половина диапазона уезжала в отрез")
    check("нет второго, противоречащего лимита 4000-5000",
          "4000-5000" not in prompt and "4000–5000" not in prompt)

    print("\n[5] Недельный промпт не приказывает писать <br>")
    br_lines = [line.strip() for line in prompt.splitlines() if "<br>" in line]
    check("каждое упоминание <br> — это запрет, а не указание",
          all("ЗАПРЕЩЕН" in line for line in br_lines), f"got {br_lines}")
    check("прежнего «Используй HTML теги (<b>, <i>, <br>)» нет",
          "<br>)" not in prompt)
    check("сказано делать настоящие переводы строк",
          "переводами строк" in prompt or "переводы строк" in prompt.lower())

    print("\n[6] Правила оформления не спорят сами с собой")
    bans_markers = "ЗАПРЕЩЕНО использовать любые маркеры" in prompt
    allows_markers = "Разрешено использовать маркеры" in prompt
    template_markers = bool(re.search(r"^\s*\*\s{2,}\*\*", prompt, re.M))
    template_emoji_heads = "## ⚡️" in prompt or "## 🦷" in prompt
    check("маркеры не запрещены и разрешены одновременно", not (bans_markers and allows_markers),
          "две взаимоисключающие команды в одном промпте")
    check("маркеры не запрещены при шаблоне, который их требует",
          not (bans_markers and template_markers),
          "шаблон структуры расписывает кейсы через «*   **Суть:**»")
    check("эмодзи в начале строк не запрещены при эмодзи в шаблоне заголовков",
          not (bans_markers and template_emoji_heads),
          "каждый заголовок структуры начинается с эмодзи")

    bans_bold_names = "Имена и фамилии ЗАПРЕЩЕНО выделять жирным" in prompt
    wants_bold_names = "укажи его имя жирным" in prompt or "**Доктор Иванов**" in prompt
    check("про жирные имена сказано что-то одно", not (bans_bold_names and wants_bold_names),
          "правило 2 требует жирные имена, правило 2.1 их запрещает")

    print("\n[7] Анонимных авторов не приписывают реальному врачу")
    # Замер по stomat_archive.db: 18965 реплик из 117847 (16.1%) идут под именем
    # "Unknown", за ними 63 разных sender_id; имя Елисеева встречается 65 раз.
    check("имени Сергея Елисеева в промпте нет", "Елисеев" not in prompt)
    check("нет приказа «считай, что это пишет ...»", "считай, что это пишет" not in prompt)
    named = [line.strip() for line in prompt.splitlines() if "Unknown" in line]
    check("правило про Unknown сохранилось", bool(named), "иначе модель придумает имя сама")
    check("велено писать обезличенно",
          any("НЕ приписывай" in line or "обезличенно" in line for line in named),
          f"got {named}")
    check("выдумывать имя прямо запрещено",
          any("Придумывать" in line and "ЗАПРЕЩЕНО" in line for line in named), f"got {named}")


def scenario_platform():
    print("\n[8] Замер платформы: Telethon действительно вырезает <br>")
    from telethon.extensions import html as telethon_html

    plain, _ = telethon_html.parse("LineOne<br>LineTwo<br><br>End")
    check("<br> исчезает вместе с переносом", plain == "LineOneLineTwoEnd", f"got {plain!r}")
    kept, _ = telethon_html.parse("LineOne\nLineTwo")
    check("настоящий перевод строки сохраняется", kept == "LineOne\nLineTwo", f"got {kept!r}")
    check("значит структура на <br> склеилась бы в одну строку",
          "\n" not in plain and "\n" in kept)


async def scenario_pin_guard():
    print("\n[9] Закреп: доставленный выпуск не объявляется неудачей")
    install_stubs(SHORT_ARTICLE)
    real_send_once = S._send_message_once
    sent_labels = []

    async def send_returning_none(client, chat_id, topic_id, text, send_params, label):
        sent_labels.append(label)
        return None

    S._send_message_once = send_returning_none
    try:
        result = await S.process_weekly_batch(make_messages(), FakeClient(), -100333)
    finally:
        S._send_message_once = real_send_once

    check("тизер отправляли", sent_labels == ["weekly_teaser"], f"got {sent_labels}")
    check("выпуск не объявлен неудачей", result is not None,
          "возврат None заставляет планировщик опубликовать неделю второй раз")
    check("стадия закрыта как успех",
          current_status().get("stage") == "weekly_summary_done", f"got {current_status()}")
    check("падения на sent_msg.id не было",
          all(s.get("stage") != "weekly_summary_failed" for s in STAGES),
          f"got {[s.get('stage') for s in STAGES]}")


async def scenario_fallback_stage():
    print("\n[10] Аварийная ветка обновляет стадию для сторожа")
    install_stubs(SHORT_ARTICLE, telegraph_ok=False)
    client = FakeClient()
    result = await S.process_weekly_batch(make_messages(), client, -100444)

    check("Telegraph отказал, но выпуск ушёл в чат", len(client.sends) == 1, f"got {client.sends}")
    check("статья отправлена напрямую, а не тизер",
          "телеграм" not in (result or "").lower() and "telegra.ph" not in (result or ""),
          f"got {(result or '')[:60]!r}")
    seen = client.status_at_send[-1] if client.status_at_send else {}
    check("в момент отправки сторож видит telegram_send, а не telegraph_create",
          seen.get("stage") == "telegram_send", f"got {seen.get('stage')!r}")
    check("вид работы в отметке — weekly", seen.get("kind") == "weekly", f"got {seen}")
    check("в отметке есть длина отправляемого", seen.get("send_chars") == len(result),
          f"got {seen.get('send_chars')} vs {len(result or '')}")
    check("закреп сделан", len(client.pins) == 1, f"got {client.pins}")
    check("стадия закрыта как успех",
          current_status().get("stage") == "weekly_summary_done", f"got {current_status()}")


async def scenario_footer():
    print("\n[11] Подвал со счётчиком переживает обрезку")
    install_stubs(LONG_ARTICLE)
    messages = make_messages()
    await S.process_weekly_batch(messages, FakeClient(), -100555)
    html_sent = TELEGRAPH_CALLS[-1]["html"]

    check("статья действительно длиннее предела обрезки",
          len(S.clean_markdown_to_html(LONG_ARTICLE)) > S.WEEKLY_HTML_LIMIT,
          f"got {len(S.clean_markdown_to_html(LONG_ARTICLE))}")
    check("обрезка сработала", "Отчет сокращен" in html_sent, "иначе проверка ничего не стоит")
    check(f"счётчик сообщений на месте", f"Сообщений за неделю — {len(messages)}" in html_sent,
          f"хвост: {html_sent[-120:]!r}")
    check("счётчик стоит в самом конце", html_sent.rstrip().endswith("</i>"),
          f"хвост: {html_sent[-60:]!r}")
    check(f"общая длина в пределах {S.WEEKLY_HTML_LIMIT}", len(html_sent) <= S.WEEKLY_HTML_LIMIT,
          f"got {len(html_sent)}")

    print("\n[12] Короткая статья: подвал один и обрезки нет")
    install_stubs(SHORT_ARTICLE)
    await S.process_weekly_batch(make_messages(), FakeClient(), -100555)
    html_sent = TELEGRAPH_CALLS[-1]["html"]
    check("счётчик добавлен", "Сообщений за неделю — 6" in html_sent, f"got {html_sent[-80:]!r}")
    check("подвал ровно один", html_sent.count("Сообщений за неделю") == 1)
    check("обрезки не было", "Отчет сокращен" not in html_sent)

    print("\n[13] Свой счётчик от модели не дублируется")
    install_stubs(SHORT_ARTICLE + "\n\nСообщений за неделю — 6")
    await S.process_weekly_batch(make_messages(), FakeClient(), -100555)
    html_sent = TELEGRAPH_CALLS[-1]["html"]
    check("второй подвал не дописан", html_sent.count("Сообщений за неделю") == 1,
          f"got {html_sent.count('Сообщений за неделю')}")


def scenario_signature():
    print("\n[14] Контракт недельной сборки совпадает с дневной")
    weekly = inspect.signature(S.process_weekly_batch).parameters
    daily = inspect.signature(S.process_summary_batch).parameters
    check("недельная сборка принимает cached_message", "cached_message" in weekly)
    check("кэш необязателен", weekly.get("cached_message") and weekly["cached_message"].default is None)
    check("обе ветки принимают delivery_hook", "delivery_hook" in weekly and "delivery_hook" in daily)
    check("порядок прежних параметров не сломан",
          list(weekly)[:5] == ["messages", "client", "chat_id", "topic_id", "delivery_hook"],
          f"got {list(weekly)}")


async def main():
    await scenario_cache_fast_path()
    await scenario_two_targets()
    scenario_prompt()
    scenario_platform()
    await scenario_pin_guard()
    await scenario_fallback_stage()
    await scenario_footer()
    scenario_signature()


asyncio.run(main())

print("\n[15] Боевые файлы не тронуты")
live_after = snapshot()
for name in LIVE_FILES:
    check(f"{name} без изменений", LIVE_BEFORE[name] == live_after[name],
          f"{LIVE_BEFORE[name]} -> {live_after[name]}")
check("журнал уведён во временный каталог",
      os.environ["STOMCHAT_LOG_PATH"].startswith(TMP_DIR))
check("отметки сторожа не писались на диск", not os.path.exists(
    os.path.join(TMP_DIR, "bot_summary_status.json")))

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
