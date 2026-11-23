# -*- coding: utf-8 -*-
"""
Unit and Integration Tests for Chairside Emergency Protocols & Ergonomics:
- Subcutaneous Emphysema (/emphysema)
- Sinus Perforation / OAC (/oac, /пазуха)
- Composite Repair Gold Standard (/composite_repair, /починка)
- NaOCl Accident with Telegram <code> tags for 1-tap clipboard copying into EHR/MIS
- Non-hanging spinners (await event.answer() everywhere)
- Navigation hierarchy and callback routing
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

import chairside_tools as ct


class TestChairsideEmergencyCards(unittest.TestCase):
    """Тестирование клинических карточек неотложных протоколов и тегов <code>."""

    def test_subcutaneous_emphysema_card_content_and_code_tags(self):
        """Карточка эмфиземы: четкие шаги, запреты и теги <code> для дозировок."""
        card = ct.format_subcutaneous_emphysema_card()

        # Заголовок и клинические симптомы
        self.assertIn("Подкожная эмфизема лица и шеи", card)
        self.assertIn("крепитац", card)
        self.assertIn("хруст сухого снега", card)

        # Категорические запреты (Red-team safety)
        self.assertIn("МАССАЖ И ДАВЛЕНИЕ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ", card)
        self.assertIn("пневмомедиастинум", card)

        # Теги <code> для быстрого копирования в МИС в 1 клик
        self.assertIn("<code>Дексаметазон 4–8 мг в/м</code>", card)
        self.assertIn("<code>Амоксиклав 875/125 мг по 1 таб 2 раза в день (7 дней)</code>", card)
        self.assertIn("<code>Кларитромицин 500 мг по 1 таб 2 раза в день (7 дней)</code>", card)
        self.assertIn("<code>Ибупрофен 400 мг по 1 таб каждые 8 часов</code>", card)

        # Маршрутизация при осложнениях
        self.assertIn("СМП (112)", card)
        self.assertIn("торакальный стационар", card)

    def test_sinus_perforation_card_content_and_code_tags(self):
        """Карточка перфорации пазухи (ОАС): проба Вальсальвы, пластика, медикаменты с <code>."""
        card = ct.format_sinus_perforation_card()

        # Диагностика и запреты
        self.assertIn("Перфорация дна гайморовой пазухи", card)
        self.assertIn("Вальсальва", card)
        self.assertIn("ГЛУБОКОЕ ЗОНДИРОВАНИЕ ЛУНКИ ЗАПРЕЩЕНО", card)

        # Размеры дефектов и тактика
        self.assertIn("Малый (<2 мм)", card)
        self.assertIn("Рехрману", card)
        self.assertIn("<code>Викрил 4-0</code>", card)

        # Препараты с <code> для копирования в МИС
        self.assertIn("<code>Амоксиклав 875/125 мг по 1 таб 2 раза в день (7 дней)</code>", card)
        self.assertIn("<code>Ксилометазолин 0.1% по 2–3 капли в ноздрю 3 раза в день (5 дней)</code>", card)
        self.assertIn("<code>Ибупрофен 400 мг по 1 таб каждые 8 часов</code>", card)
        self.assertIn("<code>Лоратадин 10 мг по 1 таб 1 раз в день (5 дней)</code>", card)

        # Памятка для пациента
        self.assertIn("Строгий запрет сморкаться 14 дней", card)
        self.assertIn("чихать строго с открытым ртом", card)

    def test_composite_repair_card_ebm_gold_standard(self):
        """Карточка починки композита: скос 45°, микроабразия Al2O3, силан 60с, DeOx."""
        card = ct.format_composite_repair_card()

        self.assertIn("Починка композитной реставрации у кресла", card)
        self.assertIn("<code>45° шириной 1.5–2.0 мм</code>", card)
        self.assertIn("<code>Оксид алюминия (Al2O3) 27–50 мкм</code>", card)
        self.assertIn("<code>37% ортофосфорная кислота 30–60 секунд</code>", card)
        self.assertIn("<code>Силан (Silane Coupling Agent) на 60 секунд</code>", card)
        self.assertIn("<code>50°C в течение 30 секунд</code>", card)
        self.assertIn("<code>OptiBond FL / Heliobond / Clearfil SE Bond</code>", card)
        self.assertIn("<code>DeOx</code>", card)

    def test_naocl_accident_card_code_tags(self):
        """Карточка гипохлоритовой аварии: все дозировки обернуты в <code>."""
        card = ct.format_naocl_accident_card()

        self.assertIn("Гипохлоритовая авария", card)
        self.assertIn("<code>0.9% стерильный физраствор 10–20 мл</code>", card)
        self.assertIn("<code>Артикаин 4% 1:200 000</code>", card)
        self.assertIn("<code>Дексаметазон 4–8 мг в/м</code>", card)
        self.assertIn("<code>Амоксиклав 875/125 мг по 1 таб 2 раза в день (7 дней)</code>", card)
        self.assertIn("<code>Ибупрофен 400 мг + Парацетамол 500 мг каждые 6–8 часов</code>", card)
        self.assertIn("ТЕПЛО КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО", card)


class TestChairsideMarkupAndRouting(unittest.TestCase):
    """Тестирование разметки инлайн-кнопок и валидации префиксов колбэков."""

    def test_chairside_hub_contains_new_tools(self):
        """Chairside Hub содержит кнопки починки композита и фонетики."""
        hub_markup = ct.build_chairside_hub_markup()
        all_callbacks = [btn.data.decode("utf-8") if isinstance(btn.data, bytes) else btn.data
                         for row in hub_markup for btn in row]

        self.assertIn("chairside:composite_repair", all_callbacks)
        self.assertIn("chairside:phonetics", all_callbacks)
        self.assertIn("sos:resuscitation_menu", all_callbacks)

    def test_resuscitation_markup_contains_emphysema_and_sinus(self):
        """Меню реанимации содержит кнопки подкожной эмфиземы и соустья пазухи."""
        sos_markup = ct.build_resuscitation_sos_markup()
        all_callbacks = [btn.data.decode("utf-8") if isinstance(btn.data, bytes) else btn.data
                         for row in sos_markup for btn in row]

        self.assertIn("sos:subcutaneous_emphysema", all_callbacks)
        self.assertIn("sos:sinus", all_callbacks)
        self.assertIn("sos:naocl", all_callbacks)
        self.assertIn("sos:anaphylaxis", all_callbacks)
        self.assertIn("sos:last", all_callbacks)

    def test_is_chairside_callback_classification(self):
        """Проверка точного разделения Chairside и сторонних колбэков."""
        # Должны определяться как Chairside
        self.assertTrue(ct.is_chairside_callback("sos:subcutaneous_emphysema"))
        self.assertTrue(ct.is_chairside_callback("sos:sinus"))
        self.assertTrue(ct.is_chairside_callback("sos:oac"))
        self.assertTrue(ct.is_chairside_callback("chairside:composite_repair"))
        self.assertTrue(ct.is_chairside_callback("chairside:emphysema"))
        self.assertTrue(ct.is_chairside_callback("chairside:sinus"))
        self.assertTrue(ct.is_chairside_callback("chairside:oac"))
        self.assertTrue(ct.is_chairside_callback("chairside:phonetics"))
        self.assertTrue(ct.is_chairside_callback("sos:resuscitation_menu"))

        # НЕ должны перехватываться Chairside (энциклопедия / суперсилы)
        self.assertFalse(ct.is_chairside_callback("sos:emphysema"))
        self.assertFalse(ct.is_chairside_callback("sos:sinus_perf"))
        self.assertFalse(ct.is_chairside_callback("sos:perf"))
        self.assertFalse(ct.is_chairside_callback("sos:bleed"))
        self.assertFalse(ct.is_chairside_callback("sos:aspiration"))

    def test_command_registration(self):
        """Проверка регистрации прямых команд Chairside в is_chairside_command."""
        commands = [
            "/emphysema", "/эмфизема",
            "/oac", "/пазуха", "/соустье", "/sinus",
            "/composite_repair", "/починка", "/скол",
            "/phonetics", "/внол"
        ]
        for cmd in commands:
            self.assertTrue(ct.is_chairside_command(cmd), f"Command '{cmd}' must be recognized as chairside command")


class TestChairsideAsyncDispatch(unittest.IsolatedAsyncioTestCase):
    """Асинхронные тесты диспетчеризации колбэков и проверки отсутствия зависающих спиннеров."""

    def _create_mock_event(self, data_str: str):
        """Создает мок Telegram Telethon event с отслеживанием answer() и edit()."""
        event = AsyncMock()
        event.data = data_str.encode("utf-8")
        event.sender_id = 123456789
        event.answer = AsyncMock()
        event.edit = AsyncMock()
        return event

    async def test_dispatch_emphysema_callback(self):
        """Колбэк sos:subcutaneous_emphysema редактирует сообщение и отвечает на event (нет спиннера)."""
        event = self._create_mock_event("sos:subcutaneous_emphysema")
        bot = MagicMock()

        handled = await ct.dispatch_chairside_callback(bot, event, "sos:subcutaneous_emphysema")
        self.assertTrue(handled)
        event.answer.assert_awaited()
        event.edit.assert_awaited()

        # Проверка текста
        edit_args = event.edit.call_args[0]
        self.assertIn("Подкожная эмфизема", edit_args[0])

    async def test_dispatch_sinus_callback(self):
        """Колбэк sos:sinus редактирует сообщение и отвечает на event."""
        event = self._create_mock_event("sos:sinus")
        bot = MagicMock()

        handled = await ct.dispatch_chairside_callback(bot, event, "sos:sinus")
        self.assertTrue(handled)
        event.answer.assert_awaited()
        event.edit.assert_awaited()

        edit_args = event.edit.call_args[0]
        self.assertIn("Перфорация дна гайморовой пазухи", edit_args[0])

    async def test_dispatch_composite_repair_callback(self):
        """Колбэк chairside:composite_repair редактирует сообщение и отвечает на event."""
        event = self._create_mock_event("chairside:composite_repair")
        bot = MagicMock()

        handled = await ct.dispatch_chairside_callback(bot, event, "chairside:composite_repair")
        self.assertTrue(handled)
        event.answer.assert_awaited()
        event.edit.assert_awaited()

        edit_args = event.edit.call_args[0]
        self.assertIn("Починка композитной реставрации", edit_args[0])

    async def test_dispatch_command_emphysema(self):
        """Прямая команда /emphysema отправляет карточку."""
        event = AsyncMock()
        event.chat_id = 123456789
        event.message = MagicMock()
        event.message.id = 999
        bot = MagicMock()
        bot.send_message = AsyncMock()

        handled = await ct.handle_if_chairside_command(bot, event, "/emphysema")
        self.assertTrue(handled)
        bot.send_message.assert_awaited()

        sent_text = bot.send_message.call_args[0][1]
        self.assertIn("Подкожная эмфизема", sent_text)

    async def test_dispatch_command_sinus(self):
        """Прямая команда /oac отправляет карточку перфорации пазухи."""
        event = AsyncMock()
        event.chat_id = 123456789
        event.message = MagicMock()
        event.message.id = 999
        bot = MagicMock()
        bot.send_message = AsyncMock()

        handled = await ct.handle_if_chairside_command(bot, event, "/oac")
        self.assertTrue(handled)
        bot.send_message.assert_awaited()

        sent_text = bot.send_message.call_args[0][1]
        self.assertIn("Перфорация дна гайморовой пазухи", sent_text)

    async def test_dispatch_command_composite_repair(self):
        """Прямая команда /починка отправляет карточку починки композита."""
        event = AsyncMock()
        event.chat_id = 123456789
        event.message = MagicMock()
        event.message.id = 999
        bot = MagicMock()
        bot.send_message = AsyncMock()

        handled = await ct.handle_if_chairside_command(bot, event, "/починка")
        self.assertTrue(handled)
        bot.send_message.assert_awaited()

        sent_text = bot.send_message.call_args[0][1]
        self.assertIn("Починка композитной реставрации", sent_text)


if __name__ == "__main__":
    unittest.main()
