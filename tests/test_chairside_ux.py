# -*- coding: utf-8 -*-
"""
tests/test_chairside_ux.py — Всесторонний UI/UX и регрессионный аудит Chairside-помощника.

Проверяет:
1. Зависающие спиннеры в Telegram (Hanging Spinners):
   - Проверка КАЖДОГО callback в chairside_tools.py (dispatch_chairside_callback,
     fdi:*, files:*, file_sys:*, aae:*, pres_st:*, anes_sos:*, pharma:*, sos:*, noop).
   - Вызов await event.answer() на всех путях исполнения, включая ошибки и повторные нажатия (MessageNotModifiedError).
   - Защита от сбоев при поврежденных/неполных аргументах (ValueError при unpack).
2. Ловушки навигации (Navigation Traps & Back Buttons):
   - Строгая иерархия: Конкретный инструмент -> Меню раздела -> Chairside Hub -> Главное меню.
   - Полное отсутствие тупиков (Dead Ends) в мастере AAE (Шаги 1, 2, 3), фармакотерапии, анестезии, формуле FDI.
   - Корректность кнопки «Назад» при гипохлоритовой аварии (возврат в меню реанимации, а не к отломкам).
3. Читаемость у кресла (Chairside Ergonomics):
   - Высококонтрастные числа: торк (N·cm), обороты (об/мин), длины (мм), дозировки (мг, мл, карпулы).
   - Отсутствие сырых неэкранированных тегов, ломающих parse_mode='html'.
4. Делегация колбэков в assistant.py (is_chairside_callback):
   - Корректная маршрутизация экстренной реанимации (sos:anaphylaxis, sos:last, sos:naocl) в Chairside,
     а энциклопедических карточек (sos:perf, sos:bleed) в общий консилиум.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

# Обеспечиваем импорт корня репозитория
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import chairside_tools as ct
import anesthesia_calc as ac


class MockEvent:
    """Mock-объект CallbackQuery события Telethon для тестирования UI."""
    def __init__(self, data_str: str):
        self.data = data_str.encode("utf-8") if isinstance(data_str, str) else data_str
        self.chat_id = -1001987654321
        self.sender_id = 987654321
        self.edit = AsyncMock()
        self.answer = AsyncMock()


class TestChairsideSpinnersAndCallbacks(unittest.IsolatedAsyncioTestCase):
    """1. Тесты на предотвращение зависающих спиннеров в Telegram."""

    async def test_all_fdi_callbacks_answer(self):
        """Проверяет все callback-запросы зубной формулы FDI."""
        bot = MagicMock()
        callbacks = [
            "fdi:menu",
            "fdi:main",
            "fdi:top_molars",
            "fdi:quad:1",
            "fdi:quad:2",
            "fdi:quad:3",
            "fdi:quad:4",
            "fdi:tooth:16",
            "fdi:tooth:21",
            "fdi:tooth:36",
            "fdi:tooth:48",
            "fdi:tooth:99",   # Несуществующий зуб (fallback)
            "fdi:quad:99",    # Несуществующий квадрант
            "fdi:unknown_cmd" # Неизвестный экшен
        ]
        for cb in callbacks:
            ev = MockEvent(cb)
            handled = await ct.dispatch_chairside_callback(bot, ev, cb)
            self.assertTrue(handled, f"Callback {cb} must be handled")
            self.assertTrue(ev.answer.called, f"event.answer() must be called for {cb}")
            self.assertTrue(ev.edit.called, f"event.edit() must be called for {cb}")

    async def test_all_files_callbacks_answer(self):
        """Проверяет все callback-запросы каталога эндо-систем."""
        bot = MagicMock()
        callbacks = [
            "files:menu",
            "files:photo_prompt",
            "file_sys:protaper_ultimate",
            "file_sys:waveone_gold",
            "file_sys:vdw_rotate",
            "file_sys:reciproc_blue",
            "file_sys:soco_sc_pro",
            "file_sys:rogin_super",
            "file_sys:denco_super",
            "file_sys:edgetaper_platinum",
            "file_sys:xp_endo_shaper",
            "file_sys:non_existent_sys", # Несуществующая система (fallback)
            "files:unknown_action"       # Неизвестный экшен
        ]
        for cb in callbacks:
            ev = MockEvent(cb)
            handled = await ct.dispatch_chairside_callback(bot, ev, cb)
            self.assertTrue(handled, f"Callback {cb} must be handled")
            self.assertTrue(ev.answer.called, f"event.answer() must be called for {cb}")
            self.assertTrue(ev.edit.called, f"event.edit() must be called for {cb}")

    async def test_all_resuscitation_and_sos_callbacks_answer(self):
        """Проверяет протоколы экстренной реанимации и скорой помощи."""
        bot = MagicMock()
        callbacks = [
            "sos:resuscitation_menu",
            "sos:anaphylaxis",
            "sos:last",
            "sos:collapse",
            "sos:hypoglycemia",
            "sos:hypertension",
            "sos:naocl",
            "sos:needle",
            "sos:adrenaline",
            "sos:pregnancy",
            "sos:file_matrix",
            "sos:anesthesia_buffering",
            "sos:irrigation",
            "chairside:irrigation",
            "file_frag:coronal_orifice",
            "file_frag:middle_straight",
            "file_frag:middle_curved",
            "file_frag:apical_beyond",
            "file_frag:naocl",
            "file_frag:unknown_level" # Неизвестный уровень отломка (fallback)
        ]
        for cb in callbacks:
            ev = MockEvent(cb)
            handled = await ct.dispatch_chairside_callback(bot, ev, cb)
            self.assertTrue(handled, f"Callback {cb} must be handled")
            self.assertTrue(ev.answer.called, f"event.answer() must be called for {cb}")
            self.assertTrue(ev.edit.called, f"event.edit() must be called for {cb}")

    async def test_all_anesthesia_rescue_callbacks_answer(self):
        """Проверяет протоколы преодоления резистентности анестезии."""
        bot = MagicMock()
        callbacks = [
            "anes_sos:menu",
            "anes_sos:buffering",
            "anes_sos:gow_gates",
            "anes_sos:vazirani_akinosi",
            "anes_sos:intraosseous",
            "anes_sos:pdl",
            "anes_sos:unknown_tech" # Неизвестная методика (fallback)
        ]
        for cb in callbacks:
            ev = MockEvent(cb)
            handled = await ct.dispatch_chairside_callback(bot, ev, cb)
            self.assertTrue(handled, f"Callback {cb} must be handled")
            self.assertTrue(ev.answer.called, f"event.answer() must be called for {cb}")
            self.assertTrue(ev.edit.called, f"event.edit() must be called for {cb}")

    async def test_all_pharma_callbacks_answer(self):
        """Проверяет 1-tap калькуляторы фармакотерапии."""
        bot = MagicMock()
        callbacks = [
            "pharma:menu",
            "pharma:kid:20",
            "pharma:teen:35",
            "pharma:adult:70",
            "pharma:allergy:70",
            "pharma:severe:75",
            "pharma:prophylaxis:70",
            "pharma:prophylaxis_all:70",
            "pharma:analgesia",
            "pharma:dexa",
            "pharma:unknown_sub" # Неизвестный подраздел (fallback)
        ]
        for cb in callbacks:
            ev = MockEvent(cb)
            handled = await ct.dispatch_chairside_callback(bot, ev, cb)
            self.assertTrue(handled, f"Callback {cb} must be handled")
            self.assertTrue(ev.answer.called, f"event.answer() must be called for {cb}")
            self.assertTrue(ev.edit.called, f"event.edit() must be called for {cb}")

    async def test_all_aae_callbacks_answer(self):
        """Проверяет все шаги калькулятора сложности AAE."""
        bot = MagicMock()
        callbacks = [
            "aae:start",
            "aae:start:16",
            "aae:s1:1:16",
            "aae:s1:2:16",
            "aae:s1:3:16",
            "aae:s2:1:1:16",
            "aae:s2:2:2:16",
            "aae:s2:3:3:16",
            "aae:s3:1:1:1:16",
            "aae:s3:2:2:2:16",
            "aae:s3:3:3:3:16",
            "aae:corrupted_data" # Поврежденный callback (fallback)
        ]
        for cb in callbacks:
            ev = MockEvent(cb)
            handled = await ct.dispatch_chairside_callback(bot, ev, cb)
            self.assertTrue(handled, f"Callback {cb} must be handled")
            self.assertTrue(ev.answer.called, f"event.answer() must be called for {cb}")
            self.assertTrue(ev.edit.called, f"event.edit() must be called for {cb}")

    async def test_preservation_calc_safe_parsing(self):
        """Проверяет калькулятор сохранения зуба и защиту от некорректных параметров."""
        bot = MagicMock()
        # Корректные параметры
        ev_norm = MockEvent("pres_st:1.2:0:40:0")
        ok = await ct.dispatch_chairside_callback(bot, ev_norm, "pres_st:1.2:0:40:0")
        self.assertTrue(ok)
        self.assertTrue(ev_norm.edit.called)

        # Поврежденная строка pres_st (меньше 5 параметров) — не должна падать с ValueError
        ev_bad = MockEvent("pres_st:corrupted")
        ok_bad = await ct.dispatch_chairside_callback(bot, ev_bad, "pres_st:corrupted")
        self.assertTrue(ok_bad)
        self.assertTrue(ev_bad.answer.called)
        self.assertTrue(ev_bad.edit.called)

        # Поврежденная строка pres_eval
        ev_eval_bad = MockEvent("pres_eval:invalid:data")
        ok_eval = await ct.dispatch_chairside_callback(bot, ev_eval_bad, "pres_eval:invalid:data")
        self.assertTrue(ok_eval)
        self.assertTrue(ev_eval_bad.answer.called)
        self.assertTrue(ev_eval_bad.edit.called)

    async def test_noop_callback_toast(self):
        """Проверяет обработку информационных заголовков noop."""
        bot = MagicMock()
        ev = MockEvent("noop")
        handled = await ct.dispatch_chairside_callback(bot, ev, "noop")
        self.assertTrue(handled)
        self.assertTrue(ev.answer.called)
        call_args = ev.answer.call_args[0]
        self.assertTrue(len(call_args) > 0 and "клинический параметр" in call_args[0].lower())

    async def test_message_not_modified_safe_handling(self):
        """Проверяет, что повторный клик (MessageNotModifiedError) не крашит обработчик."""
        bot = MagicMock()
        ev = MockEvent("files:menu")
        # Симулируем исключение Telethon при повторном нажатии той же кнопки
        ev.edit.side_effect = Exception("MessageNotModifiedError: Content of the message was not modified")

        handled = await ct.dispatch_chairside_callback(bot, ev, "files:menu")
        self.assertTrue(handled, "Must safely absorb MessageNotModifiedError and return True")
        self.assertTrue(ev.answer.called)


class TestChairsideNavigationHierarchy(unittest.TestCase):
    """2. Тесты на строгую иерархию навигации и устранение тупиков (Navigation Traps)."""

    def test_hub_markup_has_exit_to_main_menu(self):
        """Chairside Hub обязан содержать выход в главное меню бота."""
        markup = ct.build_chairside_hub_markup()
        all_datas = [b.data.decode() if isinstance(b.data, bytes) else b.data for row in markup for b in row]
        self.assertIn("nav:main", all_datas)

    def test_aae_wizard_zero_dead_ends(self):
        """В мастере AAE на шагах 2 и 3 врач обязан иметь кнопки «Назад» и выход в Хаб."""
        # Шаг 1: выход в Hub и главное меню
        m1 = ct.build_aae_markup(step=1, tooth_str="16")
        datas_1 = [b.data.decode() if isinstance(b.data, bytes) else b.data for row in m1 for b in row]
        self.assertIn("chairside:hub", datas_1)

        # Шаг 2: кнопка возврата на Шаг 1 и выход в Hub
        m2 = ct.build_aae_markup(step=2, s1=2, tooth_str="16")
        datas_2 = [b.data.decode() if isinstance(b.data, bytes) else b.data for row in m2 for b in row]
        self.assertIn("aae:start:16", datas_2, "Step 2 must allow returning to Step 1")
        self.assertIn("chairside:hub", datas_2, "Step 2 must allow returning to Hub")

        # Шаг 3: кнопка возврата на Шаг 2 с сохранением оценки s1 и выход в Hub
        m3 = ct.build_aae_markup(step=3, s1=2, s2=1, tooth_str="16")
        datas_3 = [b.data.decode() if isinstance(b.data, bytes) else b.data for row in m3 for b in row]
        self.assertIn("aae:s1:2:16", datas_3, "Step 3 must allow returning to Step 2 with s1 preserved")
        self.assertIn("chairside:hub", datas_3, "Step 3 must allow returning to Hub")

    def test_section_menus_have_hub_and_main_links(self):
        """Все меню разделов должны вести в Chairside Hub и главное меню."""
        menus = [
            ("FDI Quadrants", ct.build_fdi_quadrant_selector_markup()),
            ("Files Catalog", ct.build_files_catalog_markup()),
            ("Anesthesia SOS", ct.build_anesthesia_sos_markup()),
            ("Broken File Matrix", ct.build_broken_file_matrix_markup()),
            ("Resuscitation SOS", ct.build_resuscitation_sos_markup()),
            ("Pharma Quick", ct.build_pharma_quick_markup()),
        ]
        for name, markup in menus:
            datas = [b.data.decode() if isinstance(b.data, bytes) else b.data for row in markup for b in row]
            self.assertIn("chairside:hub", datas, f"{name} must have a back button to chairside:hub")

    def test_naocl_accident_returns_to_resuscitation_not_file_matrix(self):
        """При аварии гипохлорита возврат должен вести в меню реанимации, а не к отломкам файлов."""
        # Проверяем кнопки под карточкой NaOCl аварии в dispatch
        ev = MockEvent("sos:naocl")
        async def _run():
            await ct.dispatch_chairside_callback(MagicMock(), ev, "sos:naocl")
        import asyncio
        asyncio.run(_run())
        self.assertTrue(ev.edit.called)
        _, kwargs = ev.edit.call_args
        buttons = kwargs.get("buttons") or ev.edit.call_args[0][1]
        btn_datas = [b.data.decode() if isinstance(b.data, bytes) else b.data for row in buttons for b in row]
        self.assertIn("sos:resuscitation_menu", btn_datas, "NaOCl accident must link to resuscitation menu")
        self.assertNotIn("sos:file_matrix", btn_datas, "NaOCl accident should not have irrelevant file matrix link")

    def test_fdi_quadrant_teeth_navigation(self):
        """В сетке зубов квадранта есть переход ко всем квадрантам и в Hub."""
        for q in (1, 2, 3, 4):
            markup = ct.build_fdi_quadrant_teeth_markup(q)
            datas = [b.data.decode() if isinstance(b.data, bytes) else b.data for row in markup for b in row]
            self.assertIn("fdi:menu", datas, f"Quadrant {q} must link to fdi:menu")
            self.assertIn("chairside:hub", datas, f"Quadrant {q} must link to chairside:hub")


    def test_fdi_tooth_actions_has_quadrant_back(self):
        """Под карточкой конкретного зуба должен быть возврат в его квадрант."""
        markup = ct.build_fdi_tooth_actions_markup(16)
        datas = [b.data.decode() if isinstance(b.data, bytes) else b.data for row in markup for b in row]
        self.assertIn("fdi:quad:1", datas, "Tooth 16 actions must link back to quadrant 1")


class TestChairsideErgonomicsAndFormatting(unittest.TestCase):
    """3. Тесты читаемости у кресла, контрастности чисел и корректности HTML."""

    def test_file_system_card_numbers_contrast(self):
        """Параметры эндо-систем (RPM, N·cm, taper) должны быть выделены в <code>...</code>."""
        data = ct.lookup_file_system("protaper")
        self.assertIsNotNone(data)
        card = ct.format_file_system_card(data)
        self.assertIn("<code>", card)
        self.assertIn("N·cm</code>", card)
        self.assertIn("об/мин</code>", card)
        self.assertIn("ProTaper Ultimate", card)

    def test_tooth_card_length_and_anatomy_contrast(self):
        """Длина зуба и анатомия должны иметь четкие числовые ориентиры."""
        anat = ct.get_tooth_anatomy(16)
        self.assertIsNotNone(anat)
        card = ct.format_tooth_card(anat)
        self.assertIn(f"<code>{anat.avg_length_mm} мм</code>", card)
        self.assertIn("DANGER ZONE", card)
        self.assertIn("MB2", card)

    def test_anesthesia_carpule_math_clarity(self):
        """Расчет карпул анестезии должен четко указывать число карпул и потолок мг."""
        card_70 = ac.calculate_anesthesia_quick_card(70.0)
        self.assertIn("490 мг", card_70)
        self.assertIn("7 карп.", card_70)
        self.assertIn("Лимит адреналина", card_70)

        card_20 = ac.calculate_anesthesia_quick_card(20.0)
        self.assertIn("100 мг", card_20)
        self.assertIn("1 карп.", card_20)

    def test_irrigation_protocol_safety_warnings(self):
        """Протокол ирригации обязан содержать строгие предупреждения по PCA и NaOCl."""
        card = ct.format_irrigation_protocol_card()
        self.assertIn("парахлоранилина (PCA)", card)
        self.assertIn("КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО", card)
        self.assertIn("17% ЭДТА", card)


class TestChairsideAssistantCallbackRouting(unittest.TestCase):
    """4. Тесты функции is_chairside_callback для маршрутизации в assistant.py."""

    def test_chairside_callbacks_identified_correctly(self):
        """Все Chairside действия должны распознаваться как chairside."""
        valid_chairside = [
            "chairside:hub",
            "chairside:menu",
            "fdi:menu",
            "fdi:tooth:16",
            "files:menu",
            "file_sys:waveone_gold",
            "pres_st:2.0:0:80:0",
            "pres_eval:2.0:0:80:0",
            "preserve:tooth:16",
            "anes_sos:buffering",
            "anes_sos:gow_gates",
            "file_frag:middle_curved",
            "pharma:kid:20",
            "pharma:prophylaxis:70",
            "aae:start",
            "aae:s1:1:16",
            "noop",
            # Экстренные протоколы реанимации Chairside
            "sos:resuscitation_menu",
            "sos:anaphylaxis",
            "sos:last",
            "sos:collapse",
            "sos:hypoglycemia",
            "sos:hypertension",
            "sos:needle",
            "sos:adrenaline",
            "sos:pregnancy",
            "sos:naocl",
            "sos:file_matrix",
            "sos:anesthesia_buffering",
            "sos:irrigation"
        ]
        for cb in valid_chairside:
            self.assertTrue(ct.is_chairside_callback(cb), f"'{cb}' must be recognized as chairside callback")

    def test_non_chairside_callbacks_not_intercepted(self):
        """Общие энциклопедические и викторинные колбэки НЕ должны перехватываться Chairside."""
        external_callbacks = [
            "sos:perf",
            "sos:bleed",
            "sos:aspiration",
            "sos:emphysema",
            "sos:sinus_perf",
            "sos:torque_loss",
            "sos:dislocation",
            "sos:anesthesia_failure",
            "sos:ai",
            "sos:random",
            "nav:main",
            "nav:wiki",
            "quiz:ans:1",
            "case:opt:1",
            "bm:save:123"
        ]
        for cb in external_callbacks:
            self.assertFalse(ct.is_chairside_callback(cb), f"'{cb}' must NOT be intercepted by chairside")


if __name__ == "__main__":
    unittest.main()
