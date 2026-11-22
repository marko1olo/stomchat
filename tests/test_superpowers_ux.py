"""
Тестовый сьют для Red Team UI/UX аудита клинических суперсил (StomChat & EndoChat):
  1. Интерактивная навигация между карточками (Cross-link mesh & Variant toggling)
  2. Удобство копирования протоколов в МИС в 1 тап через <code>...</code> (IDENT, Инфодент, D4W)
  3. Мгновенное снятие спиннера и лимиты Telegram (<= 4000 символов, балансировка HTML, Fallback)

Запуск: python -m unittest tests/test_superpowers_ux.py
"""
import asyncio
import io
import os
import re
import sys
import tempfile
import unittest

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import assistant
import html_safe
from telethon.extensions import html as th_html


class DummyEvent:
    def __init__(self, data_str: str, chat_id=12345):
        self.data = data_str.encode("utf-8") if isinstance(data_str, str) else data_str
        self.chat_id = chat_id
        self.sender_id = chat_id
        self.message_id = 999
        self.answers = []
        self.edits = []

    async def answer(self, message=None, alert=False):
        self.answers.append({"message": message, "alert": alert})
        return True

    async def edit(self, text, **kwargs):
        self.edits.append({"text": text, "kwargs": kwargs})
        return True

    async def get_sender(self):
        class DummySender:
            first_name = "Доктор"
            last_name = "Тестов"
            username = "dr_test"
        return DummySender()


class DummyBotClient:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []

    async def send_message(self, entity, message, **kwargs):
        self.sent_messages.append({"entity": entity, "message": message, "kwargs": kwargs})
        return True

    async def edit_message(self, entity, message_id, text, **kwargs):
        self.edited_messages.append({"entity": entity, "message_id": message_id, "text": text, "kwargs": kwargs})
        return True


class TestSuperpowersUX(unittest.IsolatedAsyncioTestCase):

    def test_043_record_templates_have_code_blocks(self):
        """Все 8 шаблонов 043/у и все их варианты содержат блок <code> для копирования в 1 тап."""
        templates = assistant.CLINICAL_RECORD_TEMPLATES
        self.assertEqual(len(templates), 8, "Ожидается ровно 8 основных клинических шаблонов 043/у")

        required_keys = ["therapy", "endo", "surgery", "ortho", "implant", "pediatric", "perio", "complication"]
        for rk in required_keys:
            self.assertIn(rk, templates, f"Шаблон {rk} отсутствует в CLINICAL_RECORD_TEMPLATES")
            card = templates[rk]
            base_text = card.get("text", "")

            # Проверяем наличие блока <code>...</code> и UX-подсказки
            self.assertIn("<code>", base_text, f"В базовом шаблоне {rk} нет тега <code>")
            self.assertIn("</code>", base_text, f"В базовом шаблоне {rk} нет закрывающего тега </code>")
            self.assertIn("💡 <i>Нажмите на текст ниже, чтобы скопировать", base_text,
                          f"В базовом шаблоне {rk} отсутствует подсказка для врача")

            # Проверяем валидность в парсере Telethon
            parsed_text, entities = th_html.parse(base_text)
            code_entities = [e for e in entities if type(e).__name__ == "MessageEntityCode"]
            self.assertTrue(len(code_entities) >= 1, f"Парсер Telethon не обнаружил MessageEntityCode в {rk}")

            # Проверяем варианты
            variants = card.get("variants", {})
            for vk, vd in variants.items():
                var_text = vd.get("text", "")
                self.assertIn("<code>", var_text, f"В варианте {rk}:{vk} нет тега <code>")
                self.assertIn("</code>", var_text, f"В варианте {rk}:{vk} нет тега </code>")
                self.assertIn("💡 <i>Нажмите на текст ниже, чтобы скопировать", var_text,
                              f"В варианте {rk}:{vk} отсутствует подсказка для врача")

                v_parsed_text, v_entities = th_html.parse(var_text)
                v_code_entities = [e for e in v_entities if type(e).__name__ == "MessageEntityCode"]
                self.assertTrue(len(v_code_entities) >= 1,
                                f"Парсер Telethon не обнаружил MessageEntityCode в {rk}:{vk}")

    def test_one_tap_copy_extracts_pure_protocol(self):
        """Проверяем, что тапом по <code> извлекается ТОЛЬКО чистый медицинский протокол без шапки."""
        therapy_text = assistant.CLINICAL_RECORD_TEMPLATES["therapy"]["text"]
        parsed_text, entities = th_html.parse(therapy_text)
        code_entity = [e for e in entities if type(e).__name__ == "MessageEntityCode"][0]

        # Telegram entity offsets рассчитаны в UTF-16 code units (MTProto стандарт)
        utf16_bytes = parsed_text.encode('utf-16-le')
        extracted_protocol = utf16_bytes[code_entity.offset * 2 : (code_entity.offset + code_entity.length) * 2].decode('utf-16-le')

        # В скопированном протоколе не должно быть эмодзи-заголовка Telegram и подсказки бота
        self.assertNotIn("📋 Шаблон 043/у", extracted_protocol)
        self.assertNotIn("💡 Нажмите на текст ниже", extracted_protocol)

        # Но должны быть обязательные поля для МИС (IDENT / Инфодент)
        self.assertIn("Диагноз (МКБ-10): K02.1 Кариес дентина.", extracted_protocol)
        self.assertIn("Жалобы:", extracted_protocol)
        self.assertIn("Status praesens:", extracted_protocol)
        self.assertIn("Протокол лечения:", extracted_protocol)
        self.assertIn("OptiBond FL / Universal", extracted_protocol)

    def test_crosslink_mesh_integrity_all_dictionaries(self):
        """Проверяем целостность всех 204 кросс-линков между всеми 6 суперсилами."""
        dicts = {
            "sos": assistant.CLINICAL_SOS_CARDS,
            "record": assistant.CLINICAL_RECORD_TEMPLATES,
            "rx": assistant.RX_RISK_CARDS,
            "concilium": assistant.CONCILIUM_CARDS,
            "trans": assistant.PATIENT_TRANSLATION_CARDS,
            "vs": assistant.MATERIAL_BATTLE_CARDS,
        }

        total_checked = 0
        for sec_name, card_dict in dicts.items():
            for card_key, card in card_dict.items():
                crosslinks = card.get("crosslinks", [])
                self.assertTrue(len(crosslinks) >= 3, f"Карточка {sec_name}:{card_key} содержит мало кросс-линков ({len(crosslinks)})")
                for target_sec, target_cb, label in crosslinks:
                    total_checked += 1
                    parts = target_cb.split(":")
                    prefix = parts[0]
                    tgt_slug = parts[1] if len(parts) > 1 else ""

                    if prefix in dicts:
                        self.assertIn(tgt_slug, dicts[prefix],
                                      f"Битая ссылка: {target_cb} в карточке {sec_name}:{card_key}")
                    elif prefix in ("calc", "nav"):
                        pass
                    else:
                        self.fail(f"Неизвестный префикс колбэка: {target_cb} в {sec_name}:{card_key}")

        self.assertGreaterEqual(total_checked, 200, "Должно быть проверено не менее 200 кросс-линков")

    def test_variant_toggle_back_to_overview(self):
        """Проверяем, что повторный клик по активному варианту переключает назад на общий протокол."""
        variants = assistant.CLINICAL_RECORD_TEMPLATES["therapy"]["variants"]

        # 1. Когда активного варианта нет
        markup_none = assistant.build_clinical_card_markup("record", "therapy", variants=variants, active_variant=None)
        row1_buttons = markup_none[0]
        for btn in row1_buttons:
            self.assertTrue(btn.text.startswith("🔹"), "Неактивная вкладка должна начинаться с 🔹")
            self.assertIn("record:therapy:v_", btn.data.decode("utf-8"))

        # 2. Когда выбран вариант v_deep_caries
        markup_active = assistant.build_clinical_card_markup("record", "therapy", variants=variants, active_variant="v_deep_caries")
        active_btn = [b for b in markup_active[0] if "1. Глубокий кариес" in b.text][0]
        self.assertTrue(active_btn.text.startswith("🔘"), "Активная вкладка должна начинаться с 🔘")
        # Клик по активной вкладке возвращает на record:therapy (общий протокол)
        self.assertEqual(active_btn.data.decode("utf-8"), "record:therapy",
                         "Повторный клик по активному варианту обязан вести на базовую карточку")

    async def test_spinner_removal_immediate(self):
        """handle_clinical_ai_generation обязан немедленно снять спиннер через event.answer."""
        client = DummyBotClient()
        event = DummyEvent("record:ai:therapy")

        # Мокаем generate_gemini_text_async для мгновенного выполнения теста
        orig_generate = assistant.generate_gemini_text_async
        async def mock_offline(prompt, ctx, timeout=45.0):
            return None, "offline"

        assistant.generate_gemini_text_async = mock_offline
        try:
            await assistant.handle_clinical_ai_generation(client, event, "record", "therapy")
            self.assertTrue(len(event.answers) >= 1, "Спиннер не был снят через event.answer")
            first_answer = event.answers[0]
            self.assertIn("Запускаю клинический ИИ", first_answer.get("message", ""))
        finally:
            assistant.generate_gemini_text_async = orig_generate

    async def test_fallback_preserves_card_and_variant_context(self):
        """При оффлайн-фоллбэке контекст карточки и варианта не должен сбрасываться в случайный."""
        client = DummyBotClient()
        event = DummyEvent("record:ai:therapy:v_deep_caries")

        orig_generate = assistant.generate_gemini_text_async
        async def mock_offline(prompt, ctx, timeout=45.0):
            return None, "offline"

        assistant.generate_gemini_text_async = mock_offline
        try:
            # Передаем вариант therapy:v_deep_caries
            await assistant.handle_clinical_ai_generation(client, event, "record", "therapy:v_deep_caries")

            # Проверяем отредактированное сообщение
            self.assertTrue(len(client.edited_messages) >= 1, "Сообщение не было отредактировано")
            final_msg = client.edited_messages[-1]["text"]

            self.assertIn("💡 <i>(Клинический архив StomChat — режим оффлайн-кэша)</i>", final_msg)
            self.assertIn("Глубокий кариес с лечебным лайнером Biodentine", final_msg,
                          "Контекст варианта v_deep_caries был утерян!")
            self.assertIn("<code>", final_msg)
        finally:
            assistant.generate_gemini_text_async = orig_generate

    async def test_no_duplicate_headers_on_ai_generation(self):
        """Заголовок '✨ Живой ИИ-разбор' не должен дублироваться."""
        client = DummyBotClient()
        event = DummyEvent("record:ai:therapy")

        orig_generate = assistant.generate_gemini_text_async
        async def mock_generate(prompt, ctx, timeout=45.0):
            class Resp:
                text = "✨ <b>Живой ИИ-разбор StomChat Superpowers</b>\n\n📋 <b>Протокол 043/у</b>\n\n<code>Диагноз: K02.1</code>"
            return Resp(), None

        assistant.generate_gemini_text_async = mock_generate
        try:
            await assistant.handle_clinical_ai_generation(client, event, "record", "therapy")
            final_msg = client.edited_messages[-1]["text"]
            count = final_msg.count("Живой ИИ-разбор")
            self.assertEqual(count, 1, f"Заголовок дублируется ({count} раз): {final_msg[:120]}")
        finally:
            assistant.generate_gemini_text_async = orig_generate

    def test_clean_html_preserves_pre_and_code(self):
        """clean_html_formatting не должен удалять теги <pre> и <code>."""
        sample = "<pre><code>Протокол операции имплантации: торк 35 Нсм</code></pre>"
        cleaned = assistant.clean_html_formatting(sample)
        self.assertIn("<pre>", cleaned, "Тег <pre> был ошибочно удален")
        self.assertIn("<code>", cleaned, "Тег <code> был ошибочно удален")

    def test_safe_truncate_html_limits_to_4000_without_breaking_tags(self):
        """safe_truncate_html строго ограничивает длину до 4000 и корректно закрывает теги."""
        long_html = "📋 <b>Шаблон 043/у</b>\n\n<code>" + ("Протокол стоматолога IDENT Инфодент\n" * 150) + "</code>"
        self.assertGreater(len(long_html), 4500)

        truncated = html_safe.safe_truncate_html(long_html, max_len=3950)
        self.assertLessEqual(len(truncated), 4000, f"Длина превышает 4000 символов: {len(truncated)}")
        self.assertNotIn("<br>", truncated, "Для Telegram недопустим тег <br>")

        parsed_text, entities = th_html.parse(truncated)
        self.assertTrue(len(entities) >= 1)
        self.assertEqual(truncated.count("<code>"), truncated.count("</code>"))
        self.assertEqual(truncated.count("<b>"), truncated.count("</b>"))


if __name__ == "__main__":
    unittest.main()
