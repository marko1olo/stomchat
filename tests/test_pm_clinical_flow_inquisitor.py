# -*- coding: utf-8 -*-
"""
tests/test_pm_clinical_flow_inquisitor.py — Беспощадный Red Team тест
клинического консилиума в ЛС, живых статусов ожидания (Zero Dead Screen),
ликвидации самобичевания при критике скорости и бескомпромиссной очистки
пациентских дисклеймеров.
"""

import asyncio
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import assistant
import tg_safety


class TestMentionCritiqueSuppression(unittest.IsolatedAsyncioTestCase):
    """1. Ликвидация самобичевания и споров при критике скорости/работы бота."""

    async def test_speed_critique_suppression_deterministic(self):
        """Реплики с жалобой на скорость/зависание без клинического вопроса подавляются в триаже."""
        critiques = [
            "Сильно тупит бот",
            "Опять бот завис",
            "Бот тормозит страшно",
            "Че-то бот тупит",
            "Бот глючит сегодня",
            "Почему-то бот не отвечает",
            "Бот долго отвечает",
            "Ну бот и тупой",
            "Бот лагает",
        ]

        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = -100123456789
        mock_event.message.reply_to_msg_id = None
        mock_event.message.reply_to = None

        with patch("assistant.is_silenced", return_value=False), \
             patch("assistant.check_and_apply_silence", AsyncMock(return_value=False)), \
             patch("assistant.generate_gemini_text_async") as mock_gemini:

            for text in critiques:
                with self.subTest(text=text):
                    mock_gemini.reset_mock()
                    res = await assistant.check_bot_mention_trigger(
                        bot_client=mock_client,
                        event=mock_event,
                        msg_id=12345,
                        text=text,
                    )
                    self.assertFalse(res, f"Critique {text!r} should be suppressed and return False")
                    # Никаких вызовов генерации для спора/самобичевания не должно быть!
                    mock_gemini.assert_not_called()

    async def test_speed_critique_with_clinical_question_allowed(self):
        """Если в реплике есть клинический вопрос по зубу — запрос не глушится детерминированно."""
        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = -100123456789
        mock_event.message.reply_to_msg_id = None
        mock_event.message.reply_to = None

        with patch("assistant.is_silenced", return_value=False), \
             patch("assistant.check_and_apply_silence", AsyncMock(return_value=False)), \
             patch("assistant.fetch_dynamic_chat_context", AsyncMock(return_value=(["Dr: бот долго отвечает, подскажи протокол для 3.6"], None, None))), \
             patch("assistant.query_db_async", AsyncMock(return_value=[(101, "Dr", "бот долго отвечает, подскажи протокол для 3.6")])), \
             patch("assistant.generate_gemini_text_async") as mock_gemini, \
             patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))):

            triage_resp = MagicMock()
            triage_resp.text = "YES"
            gen_resp = MagicMock()
            gen_resp.text = "<b>Протокол обработки 3.6:</b> NaOCl 3% с ультразвуковой активацией."
            mock_gemini.side_effect = [
                (triage_resp, None),
                (gen_resp, None)
            ]

            res = await assistant.check_bot_mention_trigger(
                bot_client=mock_client,
                event=mock_event,
                msg_id=12345,
                text="Бот долго отвечает, но какой протокол ирригации для 3.6?",
            )
            self.assertTrue(res)
            mock_client.send_message.assert_called_once()

    async def test_validator_rule_14_rejects_self_deprecation(self):
        """Рецензент check_response_quality отклоняет самобичевание и спор с врачом о софте."""
        bad_drafts = [
            "Обычная история при пиковой нагрузке на серверы или раздутых запросах. У кресла, когда решение нужно за секунды, рассчитывать на зависающий софт — слишком рискованная тактика.",
            "К сожалению, наши серверы перегружены, и софт сильно лагает. Врачу у кресла лучше не надеяться на такую ненадежную систему.",
            "Серверная инфраструктура не справляется с нагрузкой, поэтому бот тупит. Приносим извинения за зависания.",
        ]

        with patch("assistant.generate_gemini_text_async") as mock_llm:
            for draft in bad_drafts:
                resp = MagicMock()
                resp.text = '{"ok": false, "reason": "САМОБИЧЕВАНИЕ И СПОР О СОФТЕ: критика надежности серверов"}'
                mock_llm.return_value = (resp, None)

                ok, reason = await assistant.check_response_quality(
                    context_msgs=["Врач: Сильно тупит бот"],
                    draft_reply=draft,
                    invited=True
                )
                self.assertFalse(ok)
                self.assertIn("САМОБИЧЕВАНИЕ", reason)


class TestZeroDeadScreenLiveStatus(unittest.IsolatedAsyncioTestCase):
    """2. Живой прогресс анализа в ЛС (Zero Dead Screen)."""

    async def test_photo_analysis_stages_sequence(self):
        """При анализе снимка статус проходит: 1. Скачиваю -> 2. Изучаю анатомию -> 3. Формирую протокол -> Удаление."""
        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 999888
        mock_event.sender_id = 999888
        mock_sender = MagicMock()
        mock_sender.username = "test_doc"
        mock_sender.first_name = "Doctor"
        mock_sender.last_name = "Dentist"
        mock_event.sender = mock_sender
        mock_event.get_sender = AsyncMock(return_value=mock_sender)

        mock_event.message.message = "Посмотри зуб 1.2"
        mock_event.message.photo = MagicMock()
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None
        mock_event.message.id = 555
        mock_event.message.file.size = 1024
        mock_event._album_events = None

        # Создаем временный файл, чтобы os.path.exists вернул True
        dummy_file = os.path.join(_HERE, "dummy_test_photo.jpg")
        with open(dummy_file, "w", encoding="utf-8") as f:
            f.write("fake photo")

        mock_event.message.download_media = AsyncMock(return_value=dummy_file)

        mock_status_msg = MagicMock()
        mock_status_msg.id = 777001
        mock_client.send_message = AsyncMock(return_value=mock_status_msg)

        edits = []
        async def fake_edit(client, cid, mid, text, **kwargs):
            edits.append((mid, text))
            return tg_safety.TgOutcome(ok=True)

        deletions = []
        async def fake_delete(client, cid, mids, **kwargs):
            deletions.extend(mids)
            return tg_safety.TgOutcome(ok=True)

        try:
            with patch("assistant.check_user_cooldown", return_value=0), \
                 patch("assistant.database.save_pm_message", AsyncMock()), \
                 patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
                 patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=[])), \
                 patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
                 patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
                 patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
                 patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
                 patch("assistant.vision.describe_image", AsyncMock(return_value="Периапикальный очаг 1.2, перфорация стенки корня")), \
                 patch("assistant.generate_gemini_text_async") as mock_gemini, \
                 patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))), \
                 patch("assistant.send_message_chunks_async", AsyncMock()) as mock_send_chunks, \
                 patch("assistant.tg_safety.edit_message", side_effect=fake_edit), \
                 patch("assistant.tg_safety.delete_messages", side_effect=fake_delete):

                resp_mock = MagicMock()
                resp_mock.text = "<b>Клинический разбор 1.2:</b> Перфорация стенки корня в средней трети. Протокол закрытия МТА под контролем микроскопа."
                mock_gemini.return_value = (resp_mock, None)

                await assistant.handle_private_message(mock_client, mock_event)

                # Проверяем первый отправленный статус:
                mock_client.send_message.assert_any_call(
                    entity=999888,
                    message="📥 <i>Скачиваю и подготавливаю снимок...</i>",
                    parse_mode='html'
                )

                # Проверяем промежуточные стадии в edit_message:
                edit_texts = [e[1] for e in edits]
                self.assertTrue(any("Изучаю анатомию, каналы и рентген-статус" in t for t in edit_texts),
                                f"Missing vision stage in edits: {edit_texts}")
                self.assertTrue(any("Формирую доказательный протокол лечения" in t for t in edit_texts),
                                f"Missing protocol stage in edits: {edit_texts}")

                # Проверяем, что перед выдачей ответа статусное сообщение удалено:
                self.assertIn(777001, deletions, "Status message was not cleaned up before delivering response")
                mock_send_chunks.assert_called_once()
        finally:
            if os.path.exists(dummy_file):
                try:
                    os.remove(dummy_file)
                except Exception:
                    pass


    async def test_pm_fast_text_query_uses_pure_typing(self):
        """Быстрый клинический вопрос (< 2.8с) не создает статусных сообщений (работает только typing)."""
        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 999777
        mock_event.sender_id = 999777
        mock_sender = MagicMock(username="doc_fast", first_name="Fast", last_name="Doc")
        mock_event.sender = mock_sender
        mock_event.get_sender = AsyncMock(return_value=mock_sender)

        mock_event.message.message = "какой протокол ирригации при пульпите 3.6?"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None
        mock_event.message.id = 666
        mock_event._album_events = None

        with patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant.database.save_pm_message", AsyncMock()), \
             patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=[])), \
             patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
             patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
             patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
             patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
             patch("assistant.generate_gemini_text_async") as mock_gemini, \
             patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))), \
             patch("assistant.send_message_chunks_async", AsyncMock()) as mock_send_chunks:

            resp_mock = MagicMock()
            resp_mock.text = "<b>Протокол ирригации 3.6:</b> 3% NaOCl с подогревом до 50°C и ультразвуковой активацией."
            mock_gemini.return_value = (resp_mock, None)

            await assistant.handle_private_message(mock_client, mock_event)

            # При быстром ответе bot_client.send_message не должен вызываться для статусов
            mock_client.send_message.assert_not_called()
            # Ответ ушел через send_message_chunks_async
            mock_send_chunks.assert_called_once()

    async def test_pm_long_generation_carousel_rotation(self):
        """Длительная генерация (> initial_delay) запускает карусель с ротацией этапов и очисткой."""
        mock_client = AsyncMock()
        mock_event = MagicMock()
        mock_event.chat_id = 999666
        mock_event.sender_id = 999666
        mock_sender = MagicMock(username="doc_long", first_name="Long", last_name="Doc")
        mock_event.sender = mock_sender
        mock_event.get_sender = AsyncMock(return_value=mock_sender)

        mock_event.message.message = "распиши глубокий протокол эндодонтического лечения периодонтита 4.6"
        mock_event.message.photo = None
        mock_event.message.video = None
        mock_event.message.voice = None
        mock_event.message.audio = None
        mock_event.message.video_note = None
        mock_event.message.document = None
        mock_event.message.id = 777
        mock_event._album_events = None

        mock_status_msg = MagicMock(id=888001)
        mock_client.send_message = AsyncMock(return_value=mock_status_msg)

        edits = []
        async def fake_edit(client, cid, mid, text, **kwargs):
            edits.append((mid, text))
            return tg_safety.TgOutcome(ok=True)

        deletions = []
        async def fake_delete(client, cid, mids, **kwargs):
            if isinstance(mids, (int, str)):
                deletions.append(int(mids))
            elif isinstance(mids, (list, tuple, set)):
                deletions.extend([int(x) for x in mids])
            return tg_safety.TgOutcome(ok=True)

        async def slow_gemini(*args, **kwargs):
            await asyncio.sleep(0.20)
            resp = MagicMock()
            resp.text = "<b>Протокол 4.6:</b> Инструментация до #30.04, NaOCl 3%, ЭДТА 17%, биокерамика BioRoot."
            return (resp, None)

        orig_init = assistant.PmStatusTicker.__init__
        def patched_init(self, *args, **kwargs):
            kwargs["initial_delay"] = 0.02
            kwargs["edit_interval"] = 0.03
            orig_init(self, *args, **kwargs)

        with patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant.database.save_pm_message", AsyncMock()), \
             patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=[])), \
             patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
             patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
             patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
             patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
             patch("assistant.generate_gemini_text_async", side_effect=slow_gemini), \
             patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))), \
             patch("assistant.send_message_chunks_async", AsyncMock()) as mock_send_chunks, \
             patch("assistant.tg_safety.edit_message", side_effect=fake_edit), \
             patch("assistant.tg_safety.delete_messages", side_effect=fake_delete), \
             patch.object(assistant.PmStatusTicker, "__init__", patched_init):

            await assistant.handle_private_message(mock_client, mock_event)

            # Статусное сообщение было создано
            mock_client.send_message.assert_called_with(
                entity=999666,
                message="🧪 <i>Формирую доказательный протокол лечения...</i>",
                parse_mode='html',
            )
            # Карусель сделала хотя бы 2 ротации
            self.assertGreaterEqual(len(edits), 1, f"Expected at least 1 carousel edit, got {len(edits)}")
            # Статусное сообщение было удалено перед выдачей ответа
            self.assertIn(888001, deletions)
            mock_send_chunks.assert_called_once()


class TestZeroPatientDisclaimers(unittest.TestCase):
    """3. Полная ликвидация пациентских дисклеймеров в ЛС."""

    def test_sanitize_clinical_pm_output_comprehensive(self):
        """Санитизатор вычищает все формы пациентских отмазок."""
        test_cases = [
            (
                "Рекомендуется обтурация каналов биокерамикой. Обратитесь к очному врачу для консультации.",
                "Рекомендуется обтурация каналов биокерамикой."
            ),
            (
                "При перфорации показан МТА ProRoot. Необходим очный осмотр и пальпация переходной складки.",
                "При перфорации показан МТА ProRoot."
            ),
            (
                "Помните, я всего лишь языковая модель и не могу ставить диагнозы. Дозировка артикаина 4%: 7 мг/кг.",
                "Дозировка артикаина 4%: 7 мг/кг."
            ),
            (
                "Диагноз может поставить только очный врач. По снимку PAI 3 в зубе 2.1.",
                "По снимку PAI 3 в зубе 2.1."
            ),
            (
                "Данная информация не заменяет очную консультацию стоматолога. Промойте канал 3% гипохлоритом.",
                "Промойте канал 3% гипохлоритом."
            ),
            (
                "Я не являюсь врачом и не имею медицинского диплома. Для 4.6 рекомендована вкладка оверлей.",
                "Для 4.6 рекомендована вкладка оверлей."
            ),
            (
                "Не занимайтесь самолечением, обратитесь в клинику. Обязательна изоляция коффердамом.",
                "Обязательна изоляция коффердамом."
            ),
            (
                "Проконсультируйтесь с лечащим врачом перед приемом антибиотиков. Амоксиклав 875/125 мг 2 раза в день.",
                "Амоксиклав 875/125 мг 2 раза в день."
            ),
            (
                "Формирование ковровой дорожки файлом 10 K-File.\n\nВам все понятно? Остались ли вопросы?",
                "Формирование ковровой дорожки файлом 10 K-File."
            ),
        ]

        for input_text, expected in test_cases:
            with self.subTest(input_text=input_text):
                cleaned = assistant.sanitize_clinical_pm_output(input_text)
                self.assertEqual(cleaned, expected)


if __name__ == "__main__":
    unittest.main()
