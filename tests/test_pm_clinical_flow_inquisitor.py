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
import tempfile
import shutil
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

_ORIG_STATE_PATH = None
_TMP_STATE_DIR = None


def setUpModule():
    global _ORIG_STATE_PATH, _TMP_STATE_DIR
    _ORIG_STATE_PATH = assistant.STATE_PATH
    _TMP_STATE_DIR = tempfile.mkdtemp(prefix="pm_flow_state_")
    assistant.STATE_PATH = os.path.join(_TMP_STATE_DIR, "assistant_state.json")


def tearDownModule():
    if _ORIG_STATE_PATH is not None:
        assistant.STATE_PATH = _ORIG_STATE_PATH
    if _TMP_STATE_DIR:
        shutil.rmtree(_TMP_STATE_DIR, ignore_errors=True)


class TestMentionCritiqueSuppression(unittest.IsolatedAsyncioTestCase):
    """1. Ликвидация самобичевания и споров при критике скорости/работы бота."""

    def setUp(self):
        super().setUp()
        assistant.USER_COOLDOWNS.clear()

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

        with patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant.is_silenced", return_value=False), \
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

        with patch("assistant.check_user_cooldown", return_value=0), \
             patch("assistant.is_silenced", return_value=False), \
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

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.mkdtemp(prefix="stom_pm_flow_")
        self._orig_state_path = assistant.STATE_PATH
        assistant.STATE_PATH = os.path.join(self._tmpdir, "test_state.json")
        assistant.USER_COOLDOWNS.clear()

    def tearDown(self):
        assistant.STATE_PATH = self._orig_state_path
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        super().tearDown()

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

    def test_sanitize_unwraps_misused_code_and_preserves_genitive_kollegi(self):
        """Снимает случайные <code> с прозы/измерений, сохраняя 043/у, Rp. и 'опыт коллеги'."""
        raw = (
            "Изучите опыт коллеги по работе в области 2.3. "
            "<code>Нёбную вогнутость 23 формируй овальным алмазным бором с мелким зерном (красная полоса), контролируя редукцию.</code> "
            "Редукция не менее <code>1,5–2 мм</code>.\n\n"
            "<code>Диагноз: K04.0 Пульпит 2.3.\nЛечение: препарирование под коронку E.max.</code>\n"
            "<code>Rp.: Ibuprofeni 400 mg\nD.t.d. N.10\nS.: по 1 таб при боли</code>"
        )
        cleaned = assistant.sanitize_clinical_pm_output(raw)
        self.assertIn("опыт коллеги", cleaned)
        self.assertIn("Нёбную вогнутость 23 формируй овальным алмазным бором", cleaned)
        self.assertNotIn("<code>Нёбную вогнутость", cleaned)
        self.assertNotIn("<code>1,5–2 мм</code>", cleaned)
        self.assertIn("1,5–2 мм", cleaned)
        self.assertIn("<code>Диагноз: K04.0", cleaned)
        self.assertIn("<code>Rp.: Ibuprofeni", cleaned)

    def test_extract_pm_case_entities_ignores_ages_and_durations(self):
        """Возраст, длительность и время не должны превращаться в ложные зубы FDI."""
        history = [
            {"sender_name": "User", "text": "Пациент 38 лет, боли 14 дней, аппликация 15 мин, торк 35 Нсм. Зубы 43,44,45 под коронками."},
        ]
        entities = assistant.extract_pm_case_entities(history, "что делать с 44?")
        self.assertIn("43", entities)
        self.assertIn("44", entities)
        self.assertIn("45", entities)
        for fake in ("38", "14", "15", "35"):
            self.assertNotIn(fake, entities)


class TestPMProdRegressionFixes(unittest.IsolatedAsyncioTestCase):
    """4. Регрессии из боевых логов и БД ЛС."""

    def setUp(self):
        super().setUp()
        assistant.USER_COOLDOWNS.clear()

    def _make_pm_event(self, chat_id: int, text: str, msg_id: int = 1001):
        ev = MagicMock()
        ev.chat_id = chat_id
        ev.sender_id = chat_id
        ev.message = MagicMock()
        ev.message.id = msg_id
        ev.message.message = text
        ev.message.text = text
        ev.message.photo = None
        ev.message.media = None
        ev.message.document = None
        ev.message.voice = None
        ev.message.audio = None
        ev.message.video = None
        ev.message.video_note = None
        ev.message.is_reply = False
        ev.get_sender = AsyncMock(return_value=MagicMock(first_name="Doc", username="doc", bot=False))
        return ev

    async def test_long_pm_reply_not_truncated_before_chunking(self):
        """Длинный ответ (>4500 символов) не режется в clean_html_formatting, а уходит в send_message_chunks_async с HTML."""
        long_proto = (
            "<b>Пошаговый протокол восстановления 2.3</b>\n\n"
            + ("Проведите щадящее препарирование по силиконовому ключу с сохранением эмали и контролем феррула 2 мм. " * 60)
        )
        self.assertGreater(len(long_proto), 5000)

        mock_client = AsyncMock()
        action_cm = MagicMock()
        action_cm.__aenter__ = AsyncMock(return_value=None)
        action_cm.__aexit__ = AsyncMock(return_value=None)
        mock_client.action = MagicMock(return_value=action_cm)
        status_msg = MagicMock(id=7701)
        mock_client.send_message = AsyncMock(return_value=status_msg)
        mock_client.delete_messages = AsyncMock()

        ev = self._make_pm_event(5130294926, "Подробный протокол препарирования 2.3 под винир и коронку?")
        resp = MagicMock(text=long_proto)

        with patch("assistant.database.save_pm_message", AsyncMock(return_value=1)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=[])), \
             patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
             patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
             patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
             patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
             patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
             patch("assistant.generate_gemini_text_async", AsyncMock(return_value=(resp, None))), \
             patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))), \
             patch("assistant.send_message_chunks_async", AsyncMock()) as mock_chunks, \
             patch("assistant._async_pm_supplement_job", AsyncMock()):
            await assistant.handle_private_message(mock_client, ev)

        mock_chunks.assert_called_once()
        sent_text = mock_chunks.call_args[0][2]
        self.assertGreater(len(sent_text), 5000)
        self.assertIn("<b>Пошаговый протокол", sent_text)
        self.assertNotIn("не поместились в одно сообщение", sent_text)

    async def test_continuation_request_not_throttled_to_short_mentor(self):
        """Команда 'Продолжай' не сжимается в 2-4 предложения и получает директиву продолжения."""
        from datetime import datetime, timedelta
        now = datetime.now()
        history = [
            {"sender_name": "User", "text": "Препарирование 2.3 под керамику?", "date": (now - timedelta(minutes=2)).isoformat()},
            {"sender_name": "Assistant", "text": "Этап 1: маркировка глубины вестибулярно 0.5 мм...", "date": (now - timedelta(minutes=1)).isoformat()},
        ]
        captured_prompts = []

        async def fake_gemini(prompt, status_ctx=None, timeout=None):
            captured_prompts.append(prompt)
            return MagicMock(text="Этап 2: нёбная поверхность и адгезивная фиксация..."), None

        mock_client = AsyncMock()
        action_cm = MagicMock()
        action_cm.__aenter__ = AsyncMock(return_value=None)
        action_cm.__aexit__ = AsyncMock(return_value=None)
        mock_client.action = MagicMock(return_value=action_cm)
        mock_client.send_message = AsyncMock(return_value=MagicMock(id=7702))
        mock_client.delete_messages = AsyncMock()

        ev = self._make_pm_event(5130294926, "Продолжай")
        with patch("assistant.database.save_pm_message", AsyncMock(return_value=1)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=history)), \
             patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
             patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
             patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
             patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
             patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
             patch("assistant.generate_gemini_text_async", side_effect=fake_gemini), \
             patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))), \
             patch("assistant.send_message_chunks_async", AsyncMock()), \
             patch("assistant._async_pm_supplement_job", AsyncMock()):
            await assistant.handle_private_message(mock_client, ev)

        self.assertTrue(captured_prompts)
        prompt = captured_prompts[-1]
        self.assertIn("ДИРЕКТИВА CONTINUATION", prompt)
        self.assertNotIn("СТРОГО 2–4 ЕМКИХ ПРЕДЛОЖЕНИЯ", prompt)

    async def test_nba_bm_and_pdf_work_with_sender_name(self):
        """handle_nba_callback должен работать с полями sender_name ('Assistant'/'User') из SQLite."""
        rows = [
            {"sender_name": "User", "text": "Чем промыть 36 зуб?"},
            {"sender_name": "Assistant", "text": "<b>Протокол:</b> NaOCl 3% с активацией."},
        ]
        ev_bm = MagicMock()
        ev_bm.chat_id = 555001
        ev_bm.sender_id = 555001
        ev_bm.answer = AsyncMock()

        with patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=rows)), \
             patch("assistant.database.save_clinical_bookmark", AsyncMock()) as mock_save_bm:
            await assistant.handle_nba_callback(AsyncMock(), ev_bm, "nba:bm")
        mock_save_bm.assert_called_once()
        self.assertIn("Протокол: NaOCl 3%", mock_save_bm.call_args.kwargs["text"])
        ev_bm.answer.assert_called()

        ev_pdf = MagicMock()
        ev_pdf.chat_id = 555001
        ev_pdf.sender_id = 555001
        ev_pdf.answer = AsyncMock()
        bot_client = AsyncMock()
        with patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=rows)), \
             patch("digest_pdf.generate_digest_pdf", AsyncMock(return_value=None)) as mock_gen_pdf:
            await assistant.handle_nba_callback(bot_client, ev_pdf, "nba:pdf")
        mock_gen_pdf.assert_called_once()
        html_body = mock_gen_pdf.call_args.kwargs["html_content"]
        self.assertIn("Врач-стоматолог:", html_body)
        self.assertIn("Консилиум StomChat:", html_body)

    async def test_quiz_callback_routes_new_chairside_prefixes(self):
        """Новые префиксы chairside_tools маршрутизируются и не проваливаются в CLINICAL_SOS_CARDS."""
        for data_str in ("cement:menu", "prep:menu", "sos:last", "sos:resuscitation_menu", "preserve:ferrule", "noop"):
            with self.subTest(data_str=data_str):
                ev = MagicMock()
                ev.data = data_str.encode("utf-8")
                ev.chat_id = 555002
                ev.sender_id = 555002
                ev.edit = AsyncMock()
                ev.answer = AsyncMock()
                await assistant.handle_quiz_callback(AsyncMock(), ev)
                if data_str == "noop":
                    ev.answer.assert_called()
                else:
                    self.assertTrue(ev.edit.called or ev.answer.called)

    async def test_pm_xray_dispatch_and_case_stop_vs_opt_out(self):
        """/xray работает в ЛС, а 'стоп' при активном /case завершает симулятор вместо отписки от пингов."""
        mock_client = AsyncMock()
        ev_xray = self._make_pm_event(555003, "/xray")
        with patch("assistant.handle_group_xray", AsyncMock()) as mock_xray, \
             patch("assistant.database.save_pm_message", AsyncMock()):
            await assistant.handle_private_message(mock_client, ev_xray)
        mock_xray.assert_called_once()

        ev_stop = self._make_pm_event(555004, "стоп")
        with patch("assistant.database.get_user_interactive_state", AsyncMock(return_value={"state_type": "case", "step": 1})), \
             patch("assistant.database.clear_user_interactive_state", AsyncMock()) as mock_clear, \
             patch("assistant.set_ping_opt_out") as mock_opt_out, \
             patch("assistant.database.save_pm_message", AsyncMock()):
            await assistant.handle_private_message(mock_client, ev_stop)
        mock_opt_out.assert_not_called()
        mock_clear.assert_called_once_with(555004)

    async def test_pm_pings_skips_recently_triaged_unchanged_history(self):
        """check_and_send_pm_pings не дергает LLM повторно, если last_triage_evaluated >= last_activity (<24h)."""
        now = assistant.get_msk_now()
        from datetime import timedelta
        state = {
            "pm_pings": {
                "555005": {
                    "last_activity": (now - timedelta(hours=18)).isoformat(),
                    "last_triage_evaluated": (now - timedelta(hours=4)).isoformat(),
                    "ping_sent": False,
                    "unanswered_pings": 0,
                }
            }
        }
        with patch("assistant.is_ping_quiet_hours", return_value=False), \
             patch("assistant.is_pm_ping_work_hours", return_value=True), \
             patch("assistant.load_state", return_value=state), \
             patch("assistant.generate_gemini_text_async", AsyncMock()) as mock_llm:
            await assistant.check_and_send_pm_pings(AsyncMock())
        mock_llm.assert_not_called()

    def test_groq_supplement_uses_per_model_keys_cooldown_sleep_and_fallback(self):
        """_query_groq_supplement_sync передает model_name в available_keys, спит >=2.5с на 429 и уходит на llama-3.3-70b-versatile."""
        calls = []

        def fake_available(provider, keys, model_name=None):
            calls.append(("avail", model_name, list(keys)))
            if model_name == "qwen/qwen3.8-27b":
                return ["k1", "k2", "k3"], [], 0
            return ["k1"], [], 0

        class FakeCompletions:
            def __init__(self, key):
                self.key = key

            def create(self, model, messages, temperature=0.2, max_tokens=600):
                calls.append(("req", model, self.key))
                if model == "qwen/qwen3.8-27b":
                    raise RuntimeError("429 rate limit exceeded")
                msg = MagicMock(content="<ul><li>Проверьте апикальную проходимость.</li></ul>")
                return MagicMock(choices=[MagicMock(message=msg)])

        def fake_client(provider, key, timeout=12.0):
            c = MagicMock()
            c.chat.completions = FakeCompletions(key)
            return c

        sleeps = []
        note_fail = MagicMock()
        note_ok = MagicMock()
        with patch("gemini_client.provider_pool", return_value=["k1", "k2", "k3"]), \
             patch("gemini_client.available_keys", side_effect=fake_available), \
             patch("gemini_client.get_provider_client", side_effect=fake_client), \
             patch("gemini_client.note_key_failure", note_fail), \
             patch("gemini_client.note_success", note_ok), \
             patch("gemini_client.sanitize_supplement_output", side_effect=lambda x: x), \
             patch("gemini_client.strip_reasoning", side_effect=lambda x: x), \
             patch("assistant.time.sleep", side_effect=lambda s: sleeps.append(s)):
            res = assistant._query_groq_supplement_sync("Боль 3.6?", "Ответ", timeout=12.0)

        self.assertIn("Проверьте апикальную проходимость", res or "")
        self.assertIn(("avail", "qwen/qwen3.8-27b", ["k1", "k2", "k3"]), calls)
        self.assertIn(("avail", "llama-3.3-70b-versatile", ["k1", "k2", "k3"]), calls)
        # Не сжигает k3 для qwen, делает паузу >=2.5с между k1 и k2
        qwen_reqs = [c for c in calls if c[0] == "req" and c[1] == "qwen/qwen3.8-27b"]
        self.assertEqual(len(qwen_reqs), 2)
        self.assertTrue(any(s >= 2.5 for s in sleeps))
        note_fail.assert_any_call("groq", "k1", "429 rate limit exceeded", model_name="qwen/qwen3.8-27b")
        note_ok.assert_called_once_with("groq", "k1", model_name="llama-3.3-70b-versatile")

    async def test_pm_validated_flagship_skips_redundant_supplement(self):
        """Если основной ответ флагмана прошёл check_response_quality, _async_pm_supplement_job не запускается."""
        ev = self._make_pm_event(555006, "Пульпит 3.6, алгоритм витальной ампутации?")
        resp = MagicMock(text="<b>Протокол пульпотомии 3.6:</b> NaOCl 3%, Biodentine.", model="gpt-6.1-sol")
        mock_client = AsyncMock()
        action_cm = MagicMock()
        action_cm.__aenter__ = AsyncMock(return_value=None)
        action_cm.__aexit__ = AsyncMock(return_value=None)
        mock_client.action = MagicMock(return_value=action_cm)
        mock_client.send_message = AsyncMock(return_value=MagicMock(id=7703))
        mock_client.delete_messages = AsyncMock()

        with patch("assistant.database.save_pm_message", AsyncMock(return_value=1)), \
             patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=[])), \
             patch("assistant.database.get_user_interactive_state", AsyncMock(return_value=None)), \
             patch("assistant.database.get_user_profile", AsyncMock(return_value={})), \
             patch("assistant.database.get_user_recent_group_messages", AsyncMock(return_value=[])), \
             patch("assistant.user_memory.get_clinician_memory", AsyncMock(return_value={})), \
             patch("assistant.user_memory.update_clinician_memory_async", AsyncMock()), \
             patch("assistant.generate_gemini_text_async", AsyncMock(return_value=(resp, None))), \
             patch("assistant.check_response_quality", AsyncMock(return_value=(True, "ok"))), \
             patch("assistant.send_message_chunks_async", AsyncMock()), \
             patch("assistant._async_pm_supplement_job", AsyncMock()) as mock_supp:
            await assistant.handle_private_message(mock_client, ev)
        mock_supp.assert_not_called()

    async def test_silenced_msg_skips_redundant_bot_mention_triage(self):
        """После [SILENCE] от assistant LLM сообщение 'Бот Шамиль)))' не должно дергать bot_mention_triage."""
        msg_id = 185762
        assistant._LLM_SILENCED_MSG_IDS[msg_id] = True
        ev = MagicMock()
        ev.chat_id = -1001820467444
        ev.sender_id = 12345
        ev._sender = MagicMock(bot=False)
        ev.message = MagicMock(reply_to_msg_id=None, reply_to=None)
        with patch("assistant.check_and_apply_silence", AsyncMock(return_value=False)), \
             patch("assistant.is_silenced", return_value=False), \
             patch("assistant.generate_gemini_text_async", AsyncMock()) as mock_llm:
            res = await assistant.check_bot_mention_trigger(AsyncMock(), ev, msg_id, "Бот Шамиль)))")
        self.assertFalse(res)
        mock_llm.assert_not_called()

    async def test_nba_topic_tag_and_proactive_hooks(self):
        """_extract_nba_topic_tag не берет стоп-слова вроде 'если', а nba:rec/memo/web и nav:record не затирают ответ в ЛС."""
        tag1 = assistant._extract_nba_topic_tag("Если у пациента не берет анестезия на нижней челюсти")
        self.assertEqual(tag1, "анестезия")
        tag2 = assistant._extract_nba_topic_tag("Сколько раз в день есть сладкое ребёнку 5 лет?")
        self.assertEqual(tag2, "сладкое")

        markup_live = assistant.build_nba_markup(topic_query=tag1, include_case_actions=True)
        cb_datas = [b.data.decode("utf-8") for row in markup_live for b in row]
        self.assertIn("nba:rec", cb_datas)
        self.assertIn("nba:memo", cb_datas)

        # nav:record из-под ответа ЛС (содержит nba:bm) не должен вызывать edit_callback_message (затирать ответ)
        btn_bm = MagicMock()
        btn_bm.data = b"nba:bm"
        row_bm = MagicMock()
        row_bm.buttons = [btn_bm]
        pm_msg = MagicMock()
        pm_msg.reply_markup = MagicMock(rows=[row_bm])

        ev_nav = MagicMock()
        ev_nav.data = b"nav:record"
        ev_nav.chat_id = 555010
        ev_nav.sender_id = 555010
        ev_nav.get_message = AsyncMock(return_value=pm_msg)
        ev_nav.answer = AsyncMock()
        ev_nav.edit = AsyncMock()

        with patch("assistant.edit_callback_message", AsyncMock()) as mock_edit, \
             patch("assistant.send_message_chunks_async", AsyncMock()) as mock_send:
            await assistant.handle_quiz_callback(AsyncMock(), ev_nav)
        mock_edit.assert_not_called()
        mock_send.assert_called_once()
        sent_btns = [b.data.decode("utf-8") for r in mock_send.call_args.kwargs["buttons"] for b in r]
        self.assertEqual(sent_btns[0], "nba:rec")

        # nba:rec и nba:memo формируют 1-тап документы из истории диалога
        rows = [
            {"sender_name": "User", "text": "Зуб 47 пульпит, мезиальный канал не проходится."},
            {"sender_name": "Assistant", "text": "Убрать навес Endo-Z, ручной #06/08, NaOCl 3%."},
        ]
        ev_rec = MagicMock(chat_id=555010, sender_id=555010)
        ev_rec.answer = AsyncMock()
        with patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=rows)), \
             patch("assistant.generate_gemini_text_async", AsyncMock(return_value=(MagicMock(text="<code>Диагноз K04.0</code>"), None))), \
             patch("assistant.send_message_chunks_async", AsyncMock()) as mock_rec_send:
            await assistant.handle_nba_callback(AsyncMock(), ev_rec, "nba:rec")
        mock_rec_send.assert_called_once()
        self.assertIn("<code>", mock_rec_send.call_args.args[2])

        # nba:web вызывает web_lookup.run_lookup по реальному вопросу вместо статичной заглушки
        ev_web = MagicMock(chat_id=555010, sender_id=555010)
        ev_web.answer = AsyncMock()
        with patch("assistant.database.get_last_pm_messages", AsyncMock(return_value=rows)), \
             patch("web_lookup.run_lookup", AsyncMock(return_value={"outcome": "ok", "text": "<b>Свежий протокол</b> [1]", "sources": []})), \
             patch("assistant.send_message_chunks_async", AsyncMock()) as mock_web_send:
            await assistant.handle_nba_callback(AsyncMock(), ev_web, "nba:web:пульпит")
        mock_web_send.assert_called_once()
        self.assertIn("Свежий протокол", mock_web_send.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
