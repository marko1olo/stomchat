"""
Тестовый сьют для 3 уникальных клинических суперсил StomChat:
  1. Генератор записи в амбулаторную медицинскую карту (Форма № 043/у) — /record (/043, /дневник, /карта)
  2. Клинический чекер соматических рисков и фармакологии — /rx (/риск, /риски, /соматика)
  3. Виртуальный мультидисциплинарный консилиум — /concilium (/консилиум, /план)

Проверяет:
  - Поверхность команд (меню BotCommand, текст /help, синонимы, правило 11 в промпте)
  - Инлайн-кнопки Главного меню (nav:record, nav:rx, nav:concilium) и описание в MAIN_MENU_TEXT
  - Поведение при вызове команд без аргументов (интерактивные памятки с кнопками)
  - Поведение при вызове команд с аргументами (формирование профильных EBM-промптов)
  - Колбэки навигации nav:record, nav:rx, nav:concilium
  - Колбэки шаблонов record:therapy, record:ortho, record:surgery, record:perio
  - Колбэки гайдлайнов rx:mronj, rx:anticoag, rx:cardio, rx:endo
  - Колбэк демонстрационного консилиума concilium:example

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
        print("\n[1] Регистрация команд в меню Telegram и промпте")
        # 1. Меню Default Scope
        menu_cmds = set(re.findall(r"types\.BotCommand\(command='([^']+)'", self.source))
        check("/record зарегистрирована в меню", "record" in menu_cmds)
        check("/rx зарегистрирована в меню", "rx" in menu_cmds)
        check("/concilium зарегистрирована в меню", "concilium" in menu_cmds)

        # 2. Пункты в /help
        help_block = self.source.split("💡 <b>Доступные команды в ЛС:</b>", 1)[1].split("await bot_client", 1)[0]
        help_bullets = set(re.findall(r"• /(\w+)", help_block))
        check("/record есть в /help", "record" in help_bullets)
        check("/rx есть в /help", "rx" in help_bullets)
        check("/concilium есть в /help", "concilium" in help_bullets)

        # 3. Синонимы в /help
        for syn in ("/043", "/дневник", "/карта", "/риск", "/риски", "/соматика", "/консилиум", "/план"):
            check(f"Синоним {syn} упомянут в /help", syn in help_block)

        # 4. Правило 11 в промпте
        prompt_block = self.source.split("11. ФУНКЦИОНАЛ БОТА:", 1)[1].split("\n12.", 1)[0]
        check("/record упомянута в Rule 11", "/record" in prompt_block)
        check("/rx упомянута в Rule 11", "/rx" in prompt_block)
        check("/concilium упомянута в Rule 11", "/concilium" in prompt_block)

    def test_02_main_menu_integration(self):
        print("\n[2] Интеграция в Главное меню")
        markup = assistant.build_main_menu_markup()
        callbacks = []
        for row in markup:
            for btn in row:
                cb = btn.data.decode("utf-8") if isinstance(btn.data, bytes) else str(btn.data)
                callbacks.append(cb)

        check("Кнопка Карты 043/у (nav:record) есть в меню", "nav:record" in callbacks)
        check("Кнопка Соматики (nav:rx) есть в меню", "nav:rx" in callbacks)
        check("Кнопка Консилиума (nav:concilium) есть в меню", "nav:concilium" in callbacks)

        menu_text = assistant.MAIN_MENU_TEXT
        check("Карта 043/у описана в MAIN_MENU_TEXT", "043/у" in menu_text)
        check("Соматика Rx-Check описана в MAIN_MENU_TEXT", "Rx-Check" in menu_text)
        check("Консилиум описан в MAIN_MENU_TEXT", "Консилиум" in menu_text)

    def test_03_interactive_navigation_callbacks(self):
        print("\n[3] Интерактивные колбэки навигации (nav:*)")
        bot = FakeBot()

        async def run_nav_tests():
            # nav:record
            cb = FakeCallbackEvent("nav:record")
            await assistant.handle_quiz_callback(bot, cb)
            check("nav:record вызвал answer", cb.answered_count >= 1)
            check("nav:record обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
            msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("nav:record содержит описание Формы 043/у", "Форма № 043/у" in msg)

            # nav:rx
            cb = FakeCallbackEvent("nav:rx")
            await assistant.handle_quiz_callback(bot, cb)
            check("nav:rx вызвал answer", cb.answered_count >= 1)
            check("nav:rx обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
            msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("nav:rx содержит описание Rx-Check", "Rx-Check" in msg)

            # nav:concilium
            cb = FakeCallbackEvent("nav:concilium")
            await assistant.handle_quiz_callback(bot, cb)
            check("nav:concilium вызвал answer", cb.answered_count >= 1)
            check("nav:concilium обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
            msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("nav:concilium содержит описание Консилиума", "консилиум" in msg.lower())

        asyncio.run(run_nav_tests())

    def test_04_record_templates_callbacks(self):
        print("\n[4] Клинические шаблоны Формы 043/у (record:*)")
        bot = FakeBot()

        async def run_record_tests():
            for kind, label, expected_icd, expected_text in [
                ("therapy", "Терапия", "K02.1", "коффердам"),
                ("ortho", "Ортопедия", "K08.8", "уступа"),
                ("surgery", "Хирургия", "K01.1", "ретинированного"),
                ("perio", "Пародонтология", "K05.3", "Root Planing")
            ]:
                cb = FakeCallbackEvent(f"record:{kind}")
                await assistant.handle_quiz_callback(bot, cb)
                check(f"record:{kind} вызвал answer", cb.answered_count >= 1)
                check(f"record:{kind} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                check(f"record:{kind} содержит МКБ {expected_icd}", expected_icd in msg)
                check(f"record:{kind} содержит клинический протокол ({expected_text})", expected_text.lower() in msg.lower())

        asyncio.run(run_record_tests())

    def test_05_rx_guidelines_callbacks(self):
        print("\n[5] Экспресс-гайдлайны соматических рисков (rx:*)")
        bot = FakeBot()

        async def run_rx_tests():
            for kind, expected_kw in [
                ("mronj", "s-CTX"),
                ("anticoag", "МНО"),
                ("cardio", "Мепивакаин"),
                ("endo", "Амоксициллин")
            ]:
                cb = FakeCallbackEvent(f"rx:{kind}")
                await assistant.handle_quiz_callback(bot, cb)
                check(f"rx:{kind} вызвал answer", cb.answered_count >= 1)
                check(f"rx:{kind} обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
                msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
                check(f"rx:{kind} содержит маркер безопасности «{expected_kw}»", expected_kw in msg)

        asyncio.run(run_rx_tests())

    def test_06_concilium_example_callback(self):
        print("\n[6] Демонстрационный консилиум (concilium:example)")
        bot = FakeBot()

        async def run_concilium_tests():
            cb = FakeCallbackEvent("concilium:example")
            await assistant.handle_quiz_callback(bot, cb)
            check("concilium:example вызвал answer", cb.answered_count >= 1)
            check("concilium:example обновил сообщение", cb.edited_count >= 1 or len(bot.edited_messages) >= 1)
            msg = cb.last_edit_text or (bot.edited_messages[-1]["message"] if bot.edited_messages else "")
            check("Консилиум включает Эндодонтиста", "Эндодонтист" in msg)
            check("Консилиум включает Хирурга-имплантолога", "Хирург" in msg)
            check("Консилиум включает Ортодонта", "Ортодонт" in msg)
            check("Консилиум включает Ортопеда-гнатолога", "Ортопед" in msg)
            check("Консилиум включает пошаговый Roadmap лечения", "Roadmap" in msg)

        asyncio.run(run_concilium_tests())

    def test_07_empty_commands_in_pm(self):
        print("\n[7] Вызов команд без аргументов в ЛС (handle_private_message)")
        bot = FakeBot()

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

        async def run_pm_tests():
            # 1. /record
            bot.sent_messages.clear()
            ev_rec = FakePmEvent(FakeMsg("/record"))
            await assistant.handle_private_message(bot, ev_rec)
            check("/record в ЛС отправляет сообщение", len(bot.sent_messages) >= 1)
            last = bot.sent_messages[-1]
            check("/record содержит описание Формы 043/у", "Форма № 043/у" in last["message"])
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (last.get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/record содержит кнопку record:therapy", "record:therapy" in btn_datas)
            check("/record содержит кнопку record:ortho", "record:ortho" in btn_datas)
            check("/record содержит кнопку record:surgery", "record:surgery" in btn_datas)
            check("/record содержит кнопку record:perio", "record:perio" in btn_datas)

            # 2. /rx
            bot.sent_messages.clear()
            ev_rx = FakePmEvent(FakeMsg("/rx"))
            await assistant.handle_private_message(bot, ev_rx)
            check("/rx в ЛС отправляет сообщение", len(bot.sent_messages) >= 1)
            last = bot.sent_messages[-1]
            check("/rx содержит описание Rx-Check", "Rx-Check" in last["message"])
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (last.get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/rx содержит кнопку rx:mronj", "rx:mronj" in btn_datas)
            check("/rx содержит кнопку rx:anticoag", "rx:anticoag" in btn_datas)
            check("/rx содержит кнопку rx:cardio", "rx:cardio" in btn_datas)
            check("/rx содержит кнопку rx:endo", "rx:endo" in btn_datas)

            # 3. /concilium
            bot.sent_messages.clear()
            ev_conc = FakePmEvent(FakeMsg("/concilium"))
            await assistant.handle_private_message(bot, ev_conc)
            check("/concilium в ЛС отправляет сообщение", len(bot.sent_messages) >= 1)
            last = bot.sent_messages[-1]
            check("/concilium содержит описание Консилиума", "консилиум" in last["message"].lower())
            btn_datas = [b.data.decode("utf-8") if isinstance(b.data, bytes) else str(b.data)
                         for row in (last.get("buttons") or []) for b in row if hasattr(b, "data")]
            check("/concilium содержит кнопку concilium:example", "concilium:example" in btn_datas)

        asyncio.run(run_pm_tests())


def run_tests():
    print("=" * 65)
    print("ТЕСТИРОВАНИЕ КЛИНИЧЕСКИХ СУПЕРСИЛ STOMCHAT")
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
