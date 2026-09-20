"""
Тестовый сьют для 6 уникальных клинических суперсил StomChat (расширенная динамическая база):
  1. Генератор записи в амбулаторную карту (Форма № 043/у) — /record (/043, /дневник, /карта)
  2. Клинический чекер соматических рисков и фармакологии — /rx (/риск, /риски, /соматика)
  3. Виртуальный мультидисциплинарный консилиум — /concilium (/консилиум, /план)
  4. Экстренные протоколы при осложнениях у кресла (Chairside Rescue) — /sos (/осложнение, /факап, /спасите)
  5. Переводчик с «пациентского» на клинический язык — /translate (/переводчик, /пациент, /сленг)
  6. Батл стоматологических материалов и протоколов (Material Match) — /vs (/сравнить, /материал, /выбор)

Проверяет:
  - Поверхность команд (меню BotCommandScopeDefault, текст /help, все синонимы, Правило 11 в промпте)
  - Инлайн-кнопки Главного меню (nav:record, nav:rx, nav:concilium, nav:sos, nav:translate, nav:vs) и MAIN_MENU_TEXT
  - Поведение при вызове команд без аргументов (интерактивные памятки с кнопками и случайным выбором)
  - Поведение при вызове команд с аргументами (трансформация в практические, неакадемические EBM-промпты)
  - Колбэки навигации (nav:*)
  - Колбэки шаблонов 043/у (record:*)
  - Колбэки гайдлайнов соматических рисков (rx:*)
  - Колбэк демонстрационного консилиума (concilium:example)
  - Все 10 экстренных протоколов Chairside Rescue (sos:*) + динамическая ротация (sos:random)
  - Все 12 пациентских перлов и скриптов (trans:*) + динамическая ротация (trans:random)
  - Все 10 батлов стоматологических материалов (vs:*) + динамическая ротация (vs:random)
  - Наличие кнопок быстрой ротации «🔄 Другой вариант» на каждой карточке

Запуск: python -X utf8 test_clinical_superpowers.py
"""
import asyncio
import io
import os
import re
import sys
import tempfile
import shutil
import unittest

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_superpowers_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "t.log")

import config
config.DB_PATH = os.path.join(_TMPDIR, "test_superpowers.db")

import database
import assistant

assistant.STATE_PATH = os.path.join(_TMPDIR, "assistant_state.json")
assistant.STATE_TMP_PATH = assistant.STATE_PATH + ".tmp"
assistant.STATE_BAK_PATH = assistant.STATE_PATH + ".bak"

PASS, FAIL = [], []
USER = 123456

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []

    async def send_message(self, entity=None, message=None, parse_mode=None, buttons=None, **kw):
        self.sent_messages.append({"entity": entity, "message": message, "buttons": buttons, "kw": kw})
        return type("M", (), {"id": 1000 + len(self.sent_messages), "chat_id": entity or USER})()

    async def edit_message(self, chat_id, msg_id, message, buttons=None, **kw):
        self.edited_messages.append({"chat_id": chat_id, "msg_id": msg_id, "message": message, "buttons": buttons, "kw": kw})
        return type("M", (), {"id": msg_id, "chat_id": chat_id})()


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
        return self


class FakeMsg:
    def __init__(self, text):
        self.id = 701
        self.message = text
        self.text = text
        self.video = None
        self.photo = None
        self.document = None
        self.voice = None
        self.audio = None
        self.reply_to = None

    async def download_media(self, file=None):
        return None


class FakePmEvent:
    def __init__(self, msg, uid=USER):
        self.chat_id = uid
        self.sender_id = uid
        self.message = msg
        self.sender = type("Sender", (), {"id": uid, "username": "dr_dan", "first_name": "Dan"})()


class TestClinicalSuperpowers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with io.open("assistant.py", encoding="utf-8") as f:
            cls.source = f.read()
        asyncio.run(database.init_db())

    @classmethod
    def tearDownClass(cls):
        try:
            database._DB_EXECUTOR.shutdown(wait=True)
        except Exception:
            pass
        shutil.rmtree(_TMPDIR, ignore_errors=True)

    def test_01_commands_surface_registration(self):
        print("\n[1] Регистрация 6 суперсил в меню Telegram, /help и Rule 11")
        # 1. Меню Default Scope
        menu_cmds = set(re.findall(r"types\.BotCommand\(command='([^']+)'", self.source))
        for cmd in ("record", "rx", "concilium", "sos", "translate", "vs"):
            check(f"/{cmd} зарегистрирована в меню Telegram", cmd in menu_cmds)

        # 2. Пункты в /help
        help_block = self.source.split("💡 <b>Доступные команды в ЛС:</b>", 1)[1].split("await bot_client", 1)[0]
        help_bullets = set(re.findall(r"• /(\w+)", help_block))
        for cmd in ("record", "rx", "concilium", "sos", "translate", "vs"):
            check(f"/{cmd} есть в буллетах /help", cmd in help_bullets)

        # 3. Все клинические синонимы в /help
        synonyms = [
            "/043", "/дневник", "/карта",
            "/риск", "/риски", "/соматика",
            "/консилиум", "/план",
            "/осложнение", "/факап", "/спасите",
            "/переводчик", "/пациент", "/сленг",
            "/сравнить", "/материал", "/выбор"
        ]
        for syn in synonyms:
            check(f"Синоним {syn} описан в /help", syn in help_block)

        # 4. Правило 11 в системном промпте
        prompt_block = self.source.split("11. ФУНКЦИОНАЛ БОТА:", 1)[1].split("\n12.", 1)[0]
        for cmd in ("/record", "/rx", "/concilium", "/sos", "/translate", "/vs"):
            check(f"{cmd} упомянута в Rule 11", cmd in prompt_block)

    def test_02_main_menu_integration(self):
        print("\n[2] Интеграция в Главное меню (кнопки и описание)")
        markup = assistant.build_main_menu_markup()
        callbacks = []
        for row in markup:
            for btn in row:
                cb = btn.data.decode("utf-8") if isinstance(btn.data, bytes) else str(btn.data)
                callbacks.append(cb)

        check("Кнопка Карты 043/у (nav:record) есть в меню", "nav:record" in callbacks)
        check("Кнопка Соматики (nav:rx) есть в меню", "nav:rx" in callbacks)
        check("Кнопка Консилиума (nav:concilium) есть в меню", "nav:concilium" in callbacks)
        check("Кнопка SOS-осложнений (nav:sos) есть в меню", "nav:sos" in callbacks)
        check("Кнопка Переводчика (nav:translate) есть в меню", "nav:translate" in callbacks)
        check("Кнопка Батла материалов (nav:vs) есть в меню", "nav:vs" in callbacks)

        menu_text = assistant.MAIN_MENU_TEXT
        check("Карта 043/у описана в MAIN_MENU_TEXT", "043/у" in menu_text)
        check("Соматика Rx-Check описана в MAIN_MENU_TEXT", "Rx-Check" in menu_text)
        check("Консилиум описан в MAIN_MENU_TEXT", "Консилиум" in menu_text)
        check("SOS-Спасение описано в MAIN_MENU_TEXT", "SOS-Спасение" in menu_text)
        check("Пациентский переводчик описан в MAIN_MENU_TEXT", "Пациентский переводчик" in menu_text)
        check("Батл материалов описан в MAIN_MENU_TEXT", "Батл материалов" in menu_text)

    def test_03_interactive_navigation_callbacks(self):
        print("\n[3] Интерактивные колбэки навигации (nav:*)")
        bot = FakeBot()

        async def run_nav_tests():
            nav_cases = [
                ("nav:record", "Форма № 043/у"),
                ("nav:rx", "Rx-Check"),
                ("nav:concilium", "консилиум"),
                ("nav:sos", "Chairside Rescue"),
                ("nav:translate", "Dental Translator"),
                ("nav:vs", "Material Match")
            ]
            for target, marker in nav_cases:
                cb = FakeCallbackEvent(target)
                await assistant.handle_quiz_callback(bot, cb)
                check(f"{target} вызвал answer", cb.answered_count >= 1)
                check(f"{target} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                check(f"{target} содержит «{marker}»", marker.lower() in msg.lower())

        asyncio.run(run_nav_tests())

    def test_04_record_templates_callbacks(self):
        print("\n[4] Клинические шаблоны Формы 043/у (8 шаблонов + record:random + кросс-линки)")
        bot = FakeBot()

        async def run_record_tests():
            cases = [
                ("therapy", "Терапия", "K02.1", "коффердам"),
                ("ortho", "Ортопедия", "K08.8", "уступ"),
                ("surgery", "Хирургия", "K01.1", "ретинированного"),
                ("perio", "Пародонтология", "K05.3", "Root Planing"),
                ("endo", "Эндодонтия", "K04.0", "MB2"),
                ("implant", "Имплантация", "K08.1", "имплантат"),
                ("pediatric", "Детство", "K04.0", "Biodentine"),
                ("complication", "Осложнение", "Y60.8", "Bypass")
            ]
            for kind, label, expected_icd, expected_text in cases:
                cb = FakeCallbackEvent(f"record:{kind}")
                await assistant.handle_quiz_callback(bot, cb)
                check(f"record:{kind} вызвал answer", cb.answered_count >= 1)
                check(f"record:{kind} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                check(f"record:{kind} содержит МКБ {expected_icd}", expected_icd in msg)
                check(f"record:{kind} содержит клинический протокол ({expected_text})", expected_text.lower() in msg.lower())

                # Проверка кнопки ротации и связанных кросс-линков
                last_btns = (bot.edited_messages[-1].get("buttons") if bot.edited_messages else None) or cb.last_edit_buttons
                btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                             for row in (last_btns or []) for b in row if hasattr(b, "data")]
                check(f"record:{kind} снабжен кнопкой ротации record:random", "record:random" in btn_datas)
                has_crosslink = any(d.startswith(("rx:", "vs:", "sos:", "trans:", "concilium:")) for d in btn_datas)
                check(f"record:{kind} содержит сквозные клинические кросс-линки", has_crosslink)

            # Проверка динамического случайного выбора
            cb_rnd = FakeCallbackEvent("record:random")
            await assistant.handle_quiz_callback(bot, cb_rnd)
            check("record:random вызвал answer", cb_rnd.answered_count >= 1)
            msg_rnd = cb_rnd.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("record:random вернул карточку Формы 043/у", "Шаблон 043/у" in msg_rnd)

        asyncio.run(run_record_tests())

    def test_05_rx_guidelines_callbacks(self):
        print("\n[5] Экспресс-гайдлайны соматических рисков (8 рисков + rx:random + кросс-линки)")
        bot = FakeBot()

        async def run_rx_tests():
            cases = [
                ("mronj", "s-CTX"),
                ("anticoag", "МНО"),
                ("cardio", "Мепивакаин"),
                ("endo", "Амоксициллин"),
                ("pregnancy", "триместр"),
                ("diabetes", "HbA1c"),
                ("asthma_allergy", "Видаля"),
                ("renal_liver", "гемодиализ")
            ]
            for kind, expected_kw in cases:
                cb = FakeCallbackEvent(f"rx:{kind}")
                await assistant.handle_quiz_callback(bot, cb)
                check(f"rx:{kind} вызвал answer", cb.answered_count >= 1)
                check(f"rx:{kind} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                check(f"rx:{kind} содержит маркер безопасности «{expected_kw}»", expected_kw.lower() in msg.lower())

                last_btns = (bot.edited_messages[-1].get("buttons") if bot.edited_messages else None) or cb.last_edit_buttons
                btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                             for row in (last_btns or []) for b in row if hasattr(b, "data")]
                check(f"rx:{kind} снабжен кнопкой ротации rx:random", "rx:random" in btn_datas)
                has_crosslink = any(d.startswith(("record:", "sos:", "calc:", "trans:", "vs:")) for d in btn_datas)
                check(f"rx:{kind} содержит сквозные клинические кросс-линки", has_crosslink)

            # Проверка динамического случайного выбора
            cb_rnd = FakeCallbackEvent("rx:random")
            await assistant.handle_quiz_callback(bot, cb_rnd)
            check("rx:random вызвал answer", cb_rnd.answered_count >= 1)
            msg_rnd = cb_rnd.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("rx:random вернул карточку соматических рисков", "EBM-Гайдлайн" in msg_rnd)

        asyncio.run(run_rx_tests())

    def test_06_concilium_example_callback(self):
        print("\n[6] Мультидисциплинарный консилиум (3 консилиума + concilium:random + кросс-линки)")
        bot = FakeBot()

        async def run_concilium_tests():
            cases = [
                ("example", ["Эндодонтист", "Хирург", "Ортодонт", "Ортопед", "Roadmap"]),
                ("endo_perio", ["Эндодонтист", "Пародонтолог", "Хирург", "Ортопед", "Roadmap"]),
                ("ortho_implant", ["Ортодонт", "Хирург", "Ортопед", "Roadmap"])
            ]
            for kind, kw_list in cases:
                cb = FakeCallbackEvent(f"concilium:{kind}")
                await assistant.handle_quiz_callback(bot, cb)
                check(f"concilium:{kind} вызвал answer", cb.answered_count >= 1)
                check(f"concilium:{kind} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                for kw in kw_list:
                    check(f"concilium:{kind} включает «{kw}»", kw.lower() in msg.lower())

                last_btns = (bot.edited_messages[-1].get("buttons") if bot.edited_messages else None) or cb.last_edit_buttons
                btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                             for row in (last_btns or []) for b in row if hasattr(b, "data")]
                check(f"concilium:{kind} снабжен кнопкой ротации concilium:random", "concilium:random" in btn_datas)
                has_crosslink = any(d.startswith(("record:", "vs:", "rx:", "sos:")) for d in btn_datas)
                check(f"concilium:{kind} содержит сквозные клинические кросс-линки", has_crosslink)

            # Проверка динамического случайного выбора
            cb_rnd = FakeCallbackEvent("concilium:random")
            await assistant.handle_quiz_callback(bot, cb_rnd)
            check("concilium:random вызвал answer", cb_rnd.answered_count >= 1)
            msg_rnd = cb_rnd.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("concilium:random вернул карточку консилиума", "Клинический консилиум" in msg_rnd)

        asyncio.run(run_concilium_tests())


    def test_07_sos_callbacks_and_random(self):
        print("\n[7] Расширенные протоколы осложнений у кресла (10 кейсов + sos:random)")
        bot = FakeBot()

        async def run_sos_tests():
            cases = [
                ("sos:file", ["Bypass", "C-Pilot", "EDTA", "Деэскалация"]),
                ("sos:perf", ["MTA", "Biodentine", "перфорация", "коллаген"]),
                ("sos:sealer", ["NaOCl accident", "Дексаметазон", "нижнечелюстной канал"]),
                ("sos:bleed", ["Транексамовой", "кюретаж", "ушивание", "давление"]),
                ("sos:aspiration", ["Геймлиха", "Тренделенбурга", "бронхоскопия"]),
                ("sos:anesthesia_failure", ["пульпит", "Gow-Gates", "PDL", "Citoject"]),
                ("sos:emphysema", ["крепитация", "хруст", "Амоксиклав"]),
                ("sos:sinus_perf", ["Вальсальвы", "соустья", "сморкания"]),
                ("sos:torque_loss", ["Spinning", "торк", "Undersizing"]),
                ("sos:dislocation", ["Гиппократа", "ВНЧС", "повязк"])
            ]
            for action, keywords in cases:
                cb = FakeCallbackEvent(action)
                await assistant.handle_quiz_callback(bot, cb)
                check(f"{action} вызвал answer", cb.answered_count >= 1)
                check(f"{action} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                for kw in keywords:
                    check(f"{action} содержит ключевой протокол «{kw}»", kw.lower() in msg.lower())
                # Проверяем наличие кнопки быстрой ротации
                last_btns = (bot.edited_messages[-1].get("buttons") if bot.edited_messages else None) or cb.last_edit_buttons
                btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                             for row in (last_btns or []) for b in row if hasattr(b, "data")]
                check(f"{action} снабжен кнопкой ротации sos:random", "sos:random" in btn_datas)
                has_crosslink = any(d.startswith(("record:", "vs:", "rx:", "trans:", "calc:", "concilium:")) for d in btn_datas)
                check(f"{action} содержит сквозные клинические кросс-линки", has_crosslink)

            # Проверка динамического случайного выбора sos:random
            cb_rnd = FakeCallbackEvent("sos:random")
            await assistant.handle_quiz_callback(bot, cb_rnd)
            check("sos:random вызвал answer", cb_rnd.answered_count >= 1)
            msg_rnd = cb_rnd.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("sos:random вернул полноценный протокол спасения", "SOS-Протокол" in msg_rnd)

        asyncio.run(run_sos_tests())

    def test_08_translate_callbacks_and_random(self):
        print("\n[8] Расширенный переводчик с «пациентского» (12 перлов + trans:random)")
        bot = FakeBot()

        async def run_trans_tests():
            cases = [
                ("trans:arsenic", ["K04.0", "As2O3", "мышьяк", "Рекорд"]),
                ("trans:laser", ["K02.1", "фотополимеризационн", "лазер", "длина волны"]),
                ("trans:bone", ["K05.3", "резорбция", "кость", "SRP"]),
                ("trans:nerve", ["K04.0", "сквозняк", "нерв", "пульпы"]),
                ("trans:calcium", ["K02.1", "кальций", "беременност", "токсикоз"]),
                ("trans:milk_teeth", ["молочный", "зачаток", "фолликул"]),
                ("trans:adrenalin_allergy", ["адреналин", "паническ", "аспирационн"]),
                ("trans:vodka_garlic", ["чеснок", "ожог", "спирт"]),
                ("trans:cement_forever", ["Силидонт", "цемент", "ортофосфорн"]),
                ("trans:why_so_expensive", ["коффердам", "стерилизаци", "ламп"]),
                ("trans:ultrasound_enamel", ["Моос", "кавитаци", "камень"]),
                ("trans:crown_superglue", ["Момент", "цианакрилат", "культ"])
            ]
            for action, keywords in cases:
                cb = FakeCallbackEvent(action)
                await assistant.handle_quiz_callback(bot, cb)
                check(f"{action} вызвал answer", cb.answered_count >= 1)
                check(f"{action} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                for kw in keywords:
                    check(f"{action} содержит термин/юмор «{kw}»", kw.lower() in msg.lower())
                last_btns = (bot.edited_messages[-1].get("buttons") if bot.edited_messages else None) or cb.last_edit_buttons
                btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                             for row in (last_btns or []) for b in row if hasattr(b, "data")]
                check(f"{action} снабжен кнопкой ротации trans:random", "trans:random" in btn_datas)
                has_crosslink = any(d.startswith(("record:", "vs:", "rx:", "sos:", "calc:", "concilium:")) for d in btn_datas)
                check(f"{action} содержит сквозные клинические кросс-линки", has_crosslink)

            # Проверка динамического случайного выбора trans:random
            cb_rnd = FakeCallbackEvent("trans:random")
            await assistant.handle_quiz_callback(bot, cb_rnd)
            check("trans:random вызвал answer", cb_rnd.answered_count >= 1)
            msg_rnd = cb_rnd.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("trans:random вернул карточку переводчика", "Клинический декодер" in msg_rnd)

        asyncio.run(run_trans_tests())

    def test_09_vs_callbacks_and_random(self):
        print("\n[9] Расширенные батлы материалов (10 батлов + vs:random)")
        bot = FakeBot()

        async def run_vs_tests():
            cases = [
                ("vs:ceramics", ["1200 МПа", "E.max", "10-MDP", "плавиковой"]),
                ("vs:adhesion", ["OptiBond FL", "Universal", "35–42 МПа", "10-MDP"]),
                ("vs:sealer", ["AH Plus", "BioRoot", "силикат кальция", "Single-Cone"]),
                ("vs:mta", ["MTA", "Biodentine", "12 минут", "дисколорит"]),
                ("vs:bopt", ["BOPT", "уступ", "десн", "биотип"]),
                ("vs:post", ["вкладка", "СВШ", "феррул", "200 ГПа"]),
                ("vs:implant_retention", ["винтовая", "цементн", "периимплантит"]),
                ("vs:airflow", ["сода", "эритритол", "глицин", "14 мкм"]),
                ("vs:isolation", ["коффердам", "слюн", "35–40 МПа"]),
                ("vs:gi_composite", ["СИЦ", "композит", "Fuji", "фтор"])
            ]
            for action, keywords in cases:
                cb = FakeCallbackEvent(action)
                await assistant.handle_quiz_callback(bot, cb)
                check(f"{action} вызвал answer", cb.answered_count >= 1)
                check(f"{action} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                for kw in keywords:
                    check(f"{action} содержит EBM-факт «{kw}»", kw.lower() in msg.lower())
                last_btns = (bot.edited_messages[-1].get("buttons") if bot.edited_messages else None) or cb.last_edit_buttons
                btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                             for row in (last_btns or []) for b in row if hasattr(b, "data")]
                check(f"{action} снабжен кнопкой ротации vs:random", "vs:random" in btn_datas)
                has_crosslink = any(d.startswith(("record:", "vs:", "rx:", "sos:", "trans:", "calc:", "concilium:")) for d in btn_datas)
                check(f"{action} содержит сквозные клинические кросс-линки", has_crosslink)


            # Проверка динамического случайного выбора vs:random
            cb_rnd = FakeCallbackEvent("vs:random")
            await assistant.handle_quiz_callback(bot, cb_rnd)
            check("vs:random вызвал answer", cb_rnd.answered_count >= 1)
            msg_rnd = cb_rnd.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("vs:random вернул карточку батла материалов", "Material Battle" in msg_rnd)

        asyncio.run(run_vs_tests())

    def test_10_empty_and_arg_commands_in_pm(self):
        print("\n[10] Вызовы всех 6 команд в ЛС (без аргументов и с аргументами)")
        bot = FakeBot()

        async def run_pm_tests():
            # 1. /record
            bot.sent_messages.clear()
            ev_rec = FakePmEvent(FakeMsg("/record"))
            await assistant.handle_private_message(bot, ev_rec)
            check("/record в ЛС отправляет сообщение", len(bot.sent_messages) >= 1)
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (bot.sent_messages[-1].get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/record содержит кнопки record:*", "record:therapy" in btn_datas)

            # 2. /rx
            bot.sent_messages.clear()
            ev_rx = FakePmEvent(FakeMsg("/rx"))
            await assistant.handle_private_message(bot, ev_rx)
            check("/rx в ЛС отправляет сообщение", len(bot.sent_messages) >= 1)
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (bot.sent_messages[-1].get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/rx содержит кнопки rx:*", "rx:mronj" in btn_datas)

            # 3. /concilium
            bot.sent_messages.clear()
            ev_conc = FakePmEvent(FakeMsg("/concilium"))
            await assistant.handle_private_message(bot, ev_conc)
            check("/concilium в ЛС отправляет сообщение", len(bot.sent_messages) >= 1)
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (bot.sent_messages[-1].get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/concilium содержит кнопку concilium:example", "concilium:example" in btn_datas)

            # 4. /sos (пустая)
            bot.sent_messages.clear()
            ev_sos = FakePmEvent(FakeMsg("/sos"))
            await assistant.handle_private_message(bot, ev_sos)
            check("/sos без аргументов отправляет меню SOS", len(bot.sent_messages) >= 1)
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (bot.sent_messages[-1].get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/sos содержит кнопку sos:random", "sos:random" in btn_datas)
            check("/sos содержит кнопку sos:file", "sos:file" in btn_datas)
            check("/sos содержит кнопку sos:aspiration", "sos:aspiration" in btn_datas)
            check("/sos содержит кнопку sos:anesthesia_failure", "sos:anesthesia_failure" in btn_datas)

            # 5. /translate (пустая)
            bot.sent_messages.clear()
            ev_trans = FakePmEvent(FakeMsg("/translate"))
            await assistant.handle_private_message(bot, ev_trans)
            check("/translate без аргументов отправляет меню переводчика", len(bot.sent_messages) >= 1)
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (bot.sent_messages[-1].get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/translate содержит кнопку trans:random", "trans:random" in btn_datas)
            check("/translate содержит кнопку trans:arsenic", "trans:arsenic" in btn_datas)
            check("/translate содержит кнопку trans:calcium", "trans:calcium" in btn_datas)
            check("/translate содержит кнопку trans:milk_teeth", "trans:milk_teeth" in btn_datas)

            # 6. /vs (пустая)
            bot.sent_messages.clear()
            ev_vs = FakePmEvent(FakeMsg("/vs"))
            await assistant.handle_private_message(bot, ev_vs)
            check("/vs без аргументов отправляет меню батлов", len(bot.sent_messages) >= 1)
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (bot.sent_messages[-1].get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/vs содержит кнопку vs:random", "vs:random" in btn_datas)
            check("/vs содержит кнопку vs:ceramics", "vs:ceramics" in btn_datas)
            check("/vs содержит кнопку vs:bopt", "vs:bopt" in btn_datas)
            check("/vs содержит кнопку vs:post", "vs:post" in btn_datas)

        asyncio.run(run_pm_tests())


def run_tests():
    print("=" * 65)
    print("ТЕСТИРОВАНИЕ КЛИНИЧЕСКИХ СУПЕРСИЛ STOMCHAT (EXPANDED DYNAMIC)")
    print("=" * 65)

    suite = unittest.TestLoader().loadTestsFromTestCase(TestClinicalSuperpowers)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)

    print("\n" + "=" * 65)
    print(f"ИТОГ СУПЕРСИЛ: PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    print("=" * 65)

    if FAIL:
        print("Проваленные проверки: " + ", ".join(FAIL))
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    run_tests()
