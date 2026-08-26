"""
Тестовый сьют Zero-Slash & Menu Navigation:
Проверка естественного языка врачей, генерации меню, диспетчеризации колбэков и роутинга в ЛС.

Тест 1: Классификатор намерений detect_user_intent (20+ фраз стоматологов: статьи, анестезия, квиз, кейс, закладки, стиль).
Тест 2: Генерация инлайн-карточек и меню (build_main_menu_markup, build_reply_keyboard).
Тест 3: Диспетчер колбэков nav:* (nav:main, nav:help, nav:calc, nav:quiz, nav:style, nav:bookmarks с event.edit и event.answer).
Тест 4: Интеграционный тест обработки сообщения на естественном языке в ЛС без слэшей.

Запуск: python test_zero_slash_and_menu.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from telethon import types, Button

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_zero_slash_")
config.DB_PATH = os.path.join(_TMPDIR, "test_zero_slash.db")

import database
import assistant

# Изоляция файла состояния
assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

PASS, FAIL = [], []
USER = 778899


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SENT, EDITED, DELETED = [], [], []


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []

    async def send_message(self, entity=None, message=None, parse_mode=None, buttons=None, **kw):
        SENT.append({"entity": entity, "message": message, "buttons": buttons, "kw": kw})
        self.sent_messages.append(message)
        return type("M", (), {"id": 1000 + len(SENT), "chat_id": entity or USER})()

    async def edit_message(self, chat_id, msg_id, message, buttons=None, **kw):
        EDITED.append({"chat_id": chat_id, "msg_id": msg_id, "message": message, "buttons": buttons, "kw": kw})
        self.edited_messages.append(message)
        return type("M", (), {"id": msg_id, "chat_id": chat_id})()

    async def delete_messages(self, chat_id, msg_id):
        DELETED.append(msg_id)

    def action(self, chat_id, kind):
        return _Typing()


class FakeMessage:
    def __init__(self, text="", video=None, photo=None, document=None):
        self.id = 101
        self.message = text
        self.video = video
        self.photo = photo
        self.document = document
        self.sticker = None
        self.voice = None
        self.audio = None
        self.reply_to = None

    async def download_media(self, file=None):
        return None


class FakeEvent:
    def __init__(self, message, chat_id=USER, sender_id=USER):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message = message


class FakeCallbackEvent:
    def __init__(self, data_str, chat_id=USER, sender_id=USER, message_id=505):
        self.data = data_str.encode("utf-8") if isinstance(data_str, str) else data_str
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message_id = message_id
        self.answered_count = 0
        self.edited_count = 0
        self.last_answer_text = None
        self.last_edit_text = None
        self.last_edit_buttons = None

    async def answer(self, text=None, alert=False):
        self.answered_count += 1
        self.last_answer_text = text

    async def edit(self, text=None, buttons=None, parse_mode=None, link_preview=False):
        self.edited_count += 1
        self.last_edit_text = text
        self.last_edit_buttons = buttons
        EDITED.append({"chat_id": self.chat_id, "msg_id": self.message_id, "message": text, "buttons": buttons})


def reset():
    SENT.clear()
    EDITED.clear()
    DELETED.clear()
    assistant.USER_COOLDOWNS.clear()


async def stub_llm(prompt, status_ctx=None, timeout=None):
    return type("R", (), {"text": "Клинический разбор доказательной стоматологии по вашему запросу."})(), None


async def empty_corpus(keywords):
    return "", ""


async def run_all_tests():
    await database.init_db()
    assistant.generate_gemini_text_async = stub_llm
    assistant.search_knowledge_corpus = empty_corpus

    print("\n" + "=" * 65)
    print("ТЕСТ 1: Классификатор намерений detect_user_intent (20+ фраз)")
    print("=" * 65)

    test_phrases = [
        # Поиск статей / PubMed
        ("погугли биодентин перфорация", assistant.INTENT_WEB_SEARCH),
        ("найди статьи про BOPT", assistant.INTENT_WEB_SEARCH),
        ("что говорит pubmed о винирах на депульпированные зубы", assistant.INTENT_WEB_SEARCH),
        ("какие свежие исследования по ирригации каналов", assistant.INTENT_WEB_SEARCH),
        ("поищи в интернете протокол фиксации emax", assistant.INTENT_WEB_SEARCH),
        ("найди протокол вертипрепа", assistant.INTENT_WEB_SEARCH),
        ("загугли новые адгезивы 8 поколения", assistant.INTENT_WEB_SEARCH),
        ("что пишет pubmed про гидроксид кальция", assistant.INTENT_WEB_SEARCH),

        # Расчет анестезии
        ("посчитай анестезию", assistant.INTENT_CALCULATOR),
        ("сколько карпул артикаина на 70 кг", assistant.INTENT_CALCULATOR),
        ("дозировка скандонеста ребенку", assistant.INTENT_CALCULATOR),
        ("рассчитай дозу", assistant.INTENT_CALCULATOR),
        ("калькулятор анестезии", assistant.INTENT_CALCULATOR),
        ("калькулятор", assistant.INTENT_CALCULATOR),
        ("сколько карпул убистезина можно взрослому 80 кг", assistant.INTENT_CALCULATOR),
        ("дозировка артикаина 4%", assistant.INTENT_CALCULATOR),
        ("посчитай дозировку мепивакаина", assistant.INTENT_CALCULATOR),

        # Запрос квиза / викторины
        ("хочу квиз", assistant.INTENT_QUIZ),
        ("давай викторину", assistant.INTENT_QUIZ),
        ("проверь мои знания", assistant.INTENT_QUIZ),
        ("дай вопрос", assistant.INTENT_QUIZ),
        ("запусти викторину", assistant.INTENT_QUIZ),
        ("проверь меня", assistant.INTENT_QUIZ),
        ("задай клинический вопрос", assistant.INTENT_QUIZ),
        ("хочу тест", assistant.INTENT_QUIZ),

        # Симуляция кейса
        ("давай кейс", assistant.INTENT_CASE),
        ("хочу клинический случай", assistant.INTENT_CASE),
        ("сыграем в диагностику", assistant.INTENT_CASE),
        ("запусти симулятор", assistant.INTENT_CASE),
        ("клинический кейс", assistant.INTENT_CASE),
        ("клинический симулятор", assistant.INTENT_CASE),
        ("поиграем в кейс", assistant.INTENT_CASE),
        ("начать кейс", assistant.INTENT_CASE),

        # Закладки
        ("мои закладки", assistant.INTENT_BOOKMARKS),
        ("что я сохранил", assistant.INTENT_BOOKMARKS),
        ("покажи сохраненки", assistant.INTENT_BOOKMARKS),
        ("закладки", assistant.INTENT_BOOKMARKS),
        ("сохраненные посты", assistant.INTENT_BOOKMARKS),
        ("что я сохранила", assistant.INTENT_BOOKMARKS),
        ("открой закладки", assistant.INTENT_BOOKMARKS),
        ("покажи мои закладки", assistant.INTENT_BOOKMARKS),

        # Сброс и настройка стиля
        ("смени стиль", assistant.INTENT_STYLE),
        ("настройки стиля", assistant.INTENT_STYLE),
        ("хочу другой тон", assistant.INTENT_STYLE),
        ("измени стиль общения", assistant.INTENT_STYLE),
        ("поменяй стиль", assistant.INTENT_STYLE),
        ("выбрать стиль", assistant.INTENT_STYLE),
        ("настройка тона", assistant.INTENT_STYLE),

        # Меню и помощь
        ("меню", (assistant.INTENT_MENU, assistant.INTENT_HELP)),
        ("главное меню", (assistant.INTENT_MENU, assistant.INTENT_HELP)),
        ("открой главное меню", (assistant.INTENT_MENU, assistant.INTENT_HELP)),
        ("помощь", (assistant.INTENT_MENU, assistant.INTENT_HELP)),
        ("что ты умеешь", (assistant.INTENT_MENU, assistant.INTENT_HELP)),
        ("инструкция", (assistant.INTENT_MENU, assistant.INTENT_HELP)),

        # Отрицательные клинические тесты (НЕ должны перехватываться!)
        ("первая помощь при анафилактическом шоке", None),
        ("алгоритм помощи при травме зуба", None),
        ("клинический случай: пациент 35 лет с болью в 46 зубе", None),
        ("у меня кейс сложный в клинике, 26 зуб перфорация", None),
        ("мой ответ А", None),
        ("чем травить emax перед фиксацией?", None),
        ("какой цемент выбрать для циркония?", None),
    ]

    for text, expected in test_phrases:
        intent = assistant.detect_user_intent(text)
        actual = intent.name if intent else None
        if isinstance(expected, tuple):
            ok = actual in expected
        else:
            ok = (actual == expected)
        check(f"Интент «{text}» -> {actual}", ok, f"ожидалось: {expected}, получено: {actual}")

    print("\n" + "=" * 65)
    print("ТЕСТ 2: Генерация инлайн-карточек меню и клавиатур")
    print("=" * 65)

    inline_markup = assistant.build_main_menu_markup()
    check("build_main_menu_markup возвращает список строк кнопок", isinstance(inline_markup, list) and len(inline_markup) >= 3)

    all_inline_buttons = []
    for row in inline_markup:
        all_inline_buttons.extend(row)

    inline_callbacks = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data) for b in all_inline_buttons if hasattr(b, "data")]
    check("Кнопка Препаратов и доз (nav:calc) присутствует", "nav:calc" in inline_callbacks)
    check("Кнопка Поиска в сети (nav:web) присутствует", "nav:web" in inline_callbacks)
    check("Кнопка Разбора снимков (nav:xray) присутствует", "nav:xray" in inline_callbacks)
    check("Кнопка Клинического вопроса (nav:chat) присутствует", "nav:chat" in inline_callbacks)
    check("Кнопка Закладок (nav:bookmarks) присутствует", "nav:bookmarks" in inline_callbacks)
    check("Кнопка Настроек (nav:settings) присутствует", "nav:settings" in inline_callbacks)

    reply_kb = assistant.build_reply_keyboard()
    check("build_reply_keyboard возвращает ReplyKeyboardMarkup", isinstance(reply_kb, types.ReplyKeyboardMarkup))
    check("ReplyKeyboardMarkup имеет флаг resize=True", bool(reply_kb.resize))

    reply_texts = []
    for row in reply_kb.rows:
        for btn in row.buttons:
            reply_texts.append(btn.text)

    check("Нижняя кнопка «💊 Препараты и дозы» есть в ReplyKeyboardMarkup", any("Препараты" in t or "Калькулятор" in t for t in reply_texts))
    check("Нижняя кнопка «🔍 Найти статью» есть в ReplyKeyboardMarkup", any("Найти" in t or "Поиск" in t for t in reply_texts))
    check("Нижняя кнопка «⭐ Закладки» есть в ReplyKeyboardMarkup", any("Закладки" in t for t in reply_texts))
    check("Нижняя кнопка «⌨️ Меню» есть в ReplyKeyboardMarkup", any("меню" in t.lower() for t in reply_texts))

    print("\n" + "=" * 65)
    print("ТЕСТ 3: Диспетчер колбэков nav:* (in-place навигация)")
    print("=" * 65)

    bot = FakeBot()

    # 1. nav:main
    reset()
    cb_main = FakeCallbackEvent("nav:main")
    await assistant.handle_quiz_callback(bot, cb_main)
    check("nav:main вызвал event.answer()", cb_main.answered_count >= 1)
    check("nav:main обновил сообщение (event.edit / edit_message)", len(EDITED) >= 1 or cb_main.edited_count >= 1)
    last_msg = EDITED[-1]["message"] if EDITED else (cb_main.last_edit_text or "")
    check("nav:main содержит приветствие меню", any(w in last_msg for w in ("StomChat", "Клинический навигатор", "База Знаний", "Добро пожаловать")))

    # 2. nav:help
    reset()
    cb_help = FakeCallbackEvent("nav:help")
    await assistant.handle_quiz_callback(bot, cb_help)
    check("nav:help вызвал event.answer()", cb_help.answered_count >= 1)
    check("nav:help обновил сообщение", len(EDITED) >= 1 or cb_help.edited_count >= 1)
    last_msg = EDITED[-1]["message"] if EDITED else (cb_help.last_edit_text or "")
    check("nav:help содержит памятку по возможностям", "Памятка" in last_msg or "Естественный язык" in last_msg or "команд" in last_msg.lower())

    # 3. nav:calc
    reset()
    cb_calc = FakeCallbackEvent("nav:calc")
    await assistant.handle_quiz_callback(bot, cb_calc)
    check("nav:calc вызвал event.answer()", cb_calc.answered_count >= 1)
    check("nav:calc обновил сообщение", len(EDITED) >= 1 or cb_calc.edited_count >= 1)
    last_msg = EDITED[-1]["message"] if EDITED else (cb_calc.last_edit_text or "")
    check("nav:calc содержит расчет анестезии и потолки", "калькулятор" in last_msg.lower() and "500 мг" in last_msg)

    # 4. nav:quiz
    reset()
    cb_quiz = FakeCallbackEvent("nav:quiz")
    await assistant.handle_quiz_callback(bot, cb_quiz)
    check("nav:quiz вызвал event.answer()", cb_quiz.answered_count >= 1)
    check("nav:quiz обновил сообщение", len(EDITED) >= 1 or cb_quiz.edited_count >= 1)
    last_msg = EDITED[-1]["message"] if EDITED else (cb_quiz.last_edit_text or "")
    check("nav:quiz содержит информацию о викторине", "квиз" in last_msg.lower() or "викторин" in last_msg.lower())

    # 5. nav:style
    reset()
    cb_style = FakeCallbackEvent("nav:style")
    await assistant.handle_quiz_callback(bot, cb_style)
    check("nav:style вызвал event.answer()", cb_style.answered_count >= 1)
    check("nav:style обновил сообщение", len(EDITED) >= 1 or cb_style.edited_count >= 1)
    last_msg = EDITED[-1]["message"] if EDITED else (cb_style.last_edit_text or "")
    check("nav:style содержит выбор стиля общения", "стил" in last_msg.lower())

    # 6. nav:bookmarks (пустые и с данными)
    reset()
    cb_bm = FakeCallbackEvent("nav:bookmarks")
    await assistant.handle_quiz_callback(bot, cb_bm)
    check("nav:bookmarks вызвал event.answer()", cb_bm.answered_count >= 1)
    check("nav:bookmarks обновил сообщение", len(EDITED) >= 1 or cb_bm.edited_count >= 1)
    last_msg = EDITED[-1]["message"] if EDITED else (cb_bm.last_edit_text or "")
    check("nav:bookmarks отображает раздел закладок", "заклад" in last_msg.lower())

    print("\n" + "=" * 65)
    print("ТЕСТ 4: Интеграционный тест сообщений в ЛС без единого слэша")
    print("=" * 65)

    # 1. Запрос калькулятора на естественном языке
    reset()
    msg_calc = FakeEvent(FakeMessage(text="посчитай анестезию артикаин 4% ребенку 20 кг"))
    await assistant.handle_private_message(bot, msg_calc)
    check("Запрос калькулятора без слэша обработан", len(SENT) >= 1)
    sent_text = SENT[-1]["message"] if SENT else ""
    check("В ответе калькулятора есть расчет дозировки", "карпул" in sent_text.lower() or "мг" in sent_text.lower() or "артикаин" in sent_text.lower())

    # 2. Запрос смены стиля на естественном языке
    reset()
    msg_style = FakeEvent(FakeMessage(text="смени стиль"))
    await assistant.handle_private_message(bot, msg_style)
    check("Запрос «смени стиль» открыл настройку стиля", len(SENT) >= 1)
    sent_text = SENT[-1]["message"] if SENT else ""
    check("В ответе есть варианты стилей", "стил" in sent_text.lower() and ("Коллега" in sent_text or "факты" in sent_text))

    # 3. Запрос вызова главного меню на естественном языке
    reset()
    msg_menu = FakeEvent(FakeMessage(text="открой главное меню"))
    await assistant.handle_private_message(bot, msg_menu)
    check("Запрос «открой главное меню» вернул меню", len(SENT) >= 1)
    menu_messages = [s["message"] for s in SENT]
    has_menu_text = any("StomChat" in m or "Клинический навигатор" in m or "База Знаний" in m or "меню" in m.lower() for m in menu_messages)
    has_buttons = any(s.get("buttons") is not None for s in SENT)
    check("Главное меню содержит приветствие", has_menu_text)
    check("Главное меню отправлено с кнопками", has_buttons)

    # 4. Запрос квиза на естественном языке
    reset()
    msg_quiz = FakeEvent(FakeMessage(text="хочу викторину"))
    await assistant.handle_private_message(bot, msg_quiz)
    check("Запрос «хочу викторину» сгенерировал задачу", len(SENT) >= 1)
    sent_text = SENT[-1]["message"] if SENT else ""
    check("В ответе сформирован клинический вопрос", "викторина" in sent_text.lower() or "клиническая" in sent_text.lower() or "ответ" in sent_text.lower())

    # 5. Запрос закладок на естественном языке
    reset()
    msg_bm = FakeEvent(FakeMessage(text="покажи мои закладки"))
    await assistant.handle_private_message(bot, msg_bm)
    check("Запрос «покажи мои закладки» отвечен", len(SENT) >= 1)
    bm_messages = [s["message"] for s in SENT]
    check("В ответе есть информация о закладках", any("заклад" in m.lower() for m in bm_messages))

    # 6. Обычный клинический вопрос стоматолога (должен уйти в консультацию LLM, а не перехватываться системными командами)
    reset()
    msg_consult = FakeEvent(FakeMessage(text="пациент 45 лет, глубокий кариес 36 зуба, вскрыта точка рога пульпы, кровоточит, чем покрыть?"))
    await assistant.handle_private_message(bot, msg_consult)
    check("Клиническая консультация обработана", len(SENT) >= 1)
    sent_text = SENT[-1]["message"] if SENT else ""
    check("Ответ содержит разбор клинической ситуации", len(sent_text) > 20)


try:
    asyncio.run(run_all_tests())
finally:
    try:
        database._DB_EXECUTOR.shutdown(wait=True)
    except Exception:
        pass
    shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'=' * 65}\nРЕЗУЛЬТАТЫ СЬЮТА ZERO-SLASH & MENU:\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Проваленные проверки: " + ", ".join(FAIL))
    sys.exit(1)
else:
    print("Все тесты пройдены на 100% (exit code 0)!")
    sys.exit(0)
