"""
tests/test_bookmarks_and_search_ux.py — Red Team Inquisitor Test Suite for:
1. Search snippets & 1-tap MIS protocols ergonomics (`/search`, `<code>...</code>`, `<b>...</b>`).
2. Clinical bookmarks UX (/bookmarks, render_bookmarks_page, empty guidance, no cold reload).
3. Automatic lifecycle & purge of user_interactive_states (> 1 hour inactivity).
4. Systemic prompt rules: zero markdown, strict allowed HTML tags (<b>, <i>, <code>, <pre>).
"""

import asyncio
import json
import os
import re
import sqlite3
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import config
import database
import clinical_cognitive_core
import assistant
from assistant import clean_html_formatting


@pytest.fixture(autouse=True)
async def setup_db_environment(tmp_path):
    """Изолированная тестовая база данных для предотвращения загрязнения боевой stomat_bot.db."""
    orig_db = getattr(config, "DB_PATH", "stomat_bot.db")
    orig_state = getattr(assistant, "STATE_PATH", "assistant_state.json")
    test_db = str(tmp_path / "test_bookmarks_search.db")
    config.DB_PATH = test_db
    assistant.STATE_PATH = str(tmp_path / "assistant_state.json")
    await database.init_db()
    yield
    config.DB_PATH = orig_db
    assistant.STATE_PATH = orig_state


class FakeEvent:
    def __init__(self, data_str=b"", sender_id=12345, message_id=100):
        self.data = data_str.encode("utf-8") if isinstance(data_str, str) else data_str
        self.sender_id = sender_id
        self.chat_id = sender_id
        self.message_id = message_id
        self.answers = []

    async def answer(self, message=None, alert=False):
        self.answers.append({"message": message, "alert": alert})


class FakeBot:
    def __init__(self):
        self.sent_messages = []
        self.edited_messages = []

    async def send_message(self, *args, **kw):
        entity = args[0] if len(args) > 0 else kw.get("entity")
        message = args[1] if len(args) > 1 else kw.get("message")
        self.sent_messages.append({"entity": entity, "message": message, "args": args, "kwargs": kw})
        msg = MagicMock()
        msg.id = 9999
        msg.text = message
        return msg

    async def edit_message(self, *args, **kw):
        entity = args[0] if len(args) > 0 else kw.get("entity")
        message_id = args[1] if len(args) > 1 else kw.get("message")
        msg_text = args[2] if len(args) > 2 else kw.get("text", kw.get("message"))
        self.edited_messages.append({
            "entity": entity,
            "message_id": message_id,
            "message": msg_text,
            "args": args,
            "kwargs": kw
        })
        msg = MagicMock()
        msg.id = message_id or 100
        msg.text = msg_text
        return msg


@pytest.mark.asyncio
async def test_search_protocol_1tap_code():
    """Проверяет выдачу клинического протокола в /search с тегом <code> для 1-тап копирования в МИС."""
    bot = FakeBot()
    chat_id = 998877

    msg = MagicMock()
    msg.message = "/search BOPT"
    msg.text = "/search BOPT"
    msg.id = 101
    msg.sender_id = chat_id
    msg.chat_id = chat_id
    msg.reply_to_msg_id = None

    event = MagicMock()
    event.message = msg
    event.chat_id = chat_id
    event.sender_id = chat_id

    await assistant.handle_private_message(bot, event)

    assert len(bot.sent_messages) >= 1
    last_sent = bot.sent_messages[-1]
    message_text = last_sent.get("message") or last_sent["kwargs"].get("message") or ""

    # 1. Заголовок протокола выделен <b>
    assert "<b>" in message_text
    assert "BOPT" in message_text
    # 2. Наличие блока <code> для 1-тап копирования в МИС
    assert "<code>" in message_text
    assert "</code>" in message_text
    assert "1-тап" in message_text or "1 тап" in message_text
    # 3. Никаких маркдаун заголовков
    assert "###" not in message_text
    assert "**" not in message_text


@pytest.mark.asyncio
async def test_search_wiki_fact_snippets():
    """Проверяет подсветку ключевых слов через <b> и валидный HTML в сниппетах базы знаний."""
    bot = FakeBot()
    chat_id = 998877

    msg = MagicMock()
    msg.message = "/search гипохлорит"
    msg.text = "/search гипохлорит"
    msg.id = 102
    msg.sender_id = chat_id
    msg.chat_id = chat_id
    msg.reply_to_msg_id = None

    event = MagicMock()
    event.message = msg
    event.chat_id = chat_id
    event.sender_id = chat_id

    await assistant.handle_private_message(bot, event)

    assert len(bot.sent_messages) >= 1
    last_sent = bot.sent_messages[-1]
    message_text = last_sent.get("message") or last_sent["kwargs"].get("message") or ""

    # Проверяем подсветку ключевых слов через <b>
    assert "<b>" in message_text
    assert "гипохлорит" in message_text.lower()
    # Проверяем отсутствие сырых внутренних кодов базы вроде [1.1.6, 1.1.3]
    assert not re.search(r'\[\d+\.\d+\.\d+\]', message_text)
    # Проверяем, что разметка содержит и протокол с <code>, и <b>
    assert "<code>" in message_text


@pytest.mark.asyncio
async def test_search_empty_and_not_found():
    """Проверяет дружелюбные клинические подсказки при пустом запросе и отсутствии результатов."""
    bot = FakeBot()
    chat_id = 998877

    # Пустой запрос /search
    msg = MagicMock()
    msg.message = "/search"
    msg.text = "/search"
    msg.id = 103
    msg.sender_id = chat_id
    msg.chat_id = chat_id
    msg.reply_to_msg_id = None
    event = MagicMock()
    event.message = msg
    event.chat_id = chat_id
    event.sender_id = chat_id

    await assistant.handle_private_message(bot, event)
    assert len(bot.sent_messages) >= 1
    msg1 = bot.sent_messages[-1].get("message") or bot.sent_messages[-1]["kwargs"].get("message") or ""
    assert "укажите поисковый запрос" in msg1.lower()
    assert "<code>/search BOPT</code>" in msg1

    # Запрос без результатов
    msg.message = "/search xyzабвгд12345несуществующий"
    msg.text = "/search xyzабвгд12345несуществующий"
    await assistant.handle_private_message(bot, event)
    msg2 = bot.sent_messages[-1].get("message") or bot.sent_messages[-1]["kwargs"].get("message") or ""
    assert "ничего не найдено" in msg2.lower()
    assert "Попробуйте изменить формулировку" in msg2


@pytest.mark.asyncio
async def test_empty_bookmarks_guidance():
    """Проверяет отображение подробного клинического руководства при пустых закладках."""
    bot = FakeBot()
    test_user_id = 888777111

    # 1. Проверяем текстовую команду /bookmarks
    msg = MagicMock()
    msg.message = "/bookmarks"
    msg.text = "/bookmarks"
    msg.id = 104
    msg.sender_id = test_user_id
    msg.chat_id = test_user_id
    msg.reply_to_msg_id = None
    event = MagicMock()
    event.message = msg
    event.chat_id = test_user_id
    event.sender_id = test_user_id

    await assistant.handle_private_message(bot, event)
    assert len(bot.sent_messages) >= 1
    bm_msg = bot.sent_messages[-1].get("message") or bot.sent_messages[-1]["kwargs"].get("message") or ""
    assert "У вас пока нет сохраненных записей" in bm_msg
    assert "<code>/save</code>" in bm_msg
    assert "⭐ В закладки" in bm_msg

    # 2. Проверяем inline-рендер render_bookmarks_page
    cb_event = FakeEvent(data_str=b"bm:page:1", sender_id=test_user_id, message_id=100)
    await assistant.render_bookmarks_page(bot, cb_event, page=1)
    assert len(bot.edited_messages) >= 1
    edit_item = bot.edited_messages[-1]
    edit_text = edit_item.get("message") or edit_item["kwargs"].get("text") or ""
    assert "У вас пока нет сохраненных записей" in edit_text
    buttons = edit_item.get("buttons") or edit_item["kwargs"].get("buttons") or []
    button_datas = [btn.data.decode('utf-8') if isinstance(btn.data, bytes) else str(btn.data) 
                    for row in buttons for btn in row]
    assert "nav:wiki" in button_datas
    assert "nav:main" in button_datas


@pytest.mark.asyncio
async def test_nav_bookmarks_delegates_to_render_bookmarks_page():
    """Проверяет, что колбэк nav:bookmarks вызывает render_bookmarks_page без устаревшего дублирования."""
    bot = FakeBot()
    cb_event = FakeEvent(data_str=b"nav:bookmarks", sender_id=555666777, message_id=100)

    with patch("assistant.render_bookmarks_page", new_callable=AsyncMock) as mock_render:
        await assistant.handle_quiz_callback(bot, cb_event)
        mock_render.assert_called_once_with(bot, cb_event, page=1)


@pytest.mark.asyncio
async def test_user_interactive_states_auto_expiry():
    """Проверяет автоматическое удаление зависших интерактивных состояний (> 1 часа)."""
    user_id = 777123456
    
    # 1. Записываем устаревшее состояние (last_updated = 3700 сек назад)
    old_ts = time.time() - 3700
    history_payload = {
        "last_updated": old_ts,
        "messages": [{"role": "user", "content": "старый вопрос"}]
    }
    await database.set_user_interactive_state(
        user_id=user_id,
        state_type="case",
        current_step=2,
        case_id="case_old_test",
        history=json.dumps(history_payload)
    )

    # 2. Чтение через get_user_interactive_state должно автоматически обнаружить просрочку (>1ч),
    # удалить запись из базы и вернуть None, не блокируя работу врача!
    state = await database.get_user_interactive_state(user_id)
    assert state is None, "Устаревшее интерактивное состояние (> 1 часа) должно автоматически очищаться!"

    # 3. Записываем свежее состояние (last_updated = 30 сек назад)
    fresh_ts = time.time() - 30
    fresh_payload = {
        "last_updated": fresh_ts,
        "messages": [{"role": "user", "content": "актуальный шаг"}]
    }
    await database.set_user_interactive_state(
        user_id=user_id,
        state_type="case",
        current_step=1,
        case_id="case_fresh_test",
        history=json.dumps(fresh_payload)
    )

    fresh_state = await database.get_user_interactive_state(user_id)
    assert fresh_state is not None
    assert fresh_state["case_id"] == "case_fresh_test"

    # 4. Проверяем пакетный cleanup_stale_interactive_states
    deleted = await database.cleanup_stale_interactive_states(max_age_seconds=3600)
    # Свежее состояние не должно быть удалено
    still_fresh = await database.get_user_interactive_state(user_id)
    assert still_fresh is not None

    # Очищаем за собой
    await database.clear_user_interactive_state(user_id)


def test_prompt_systemic_rules_and_mis_ergonomics():
    """Проверяет строгие правила системных промптов: 1-тап МИС <code>, <b>, запрет Markdown."""
    arch = clinical_cognitive_core.MASTER_COGNITIVE_ARCHITECTURE
    pm_dir = assistant.PM_CLINICAL_COGNITIVE_DIRECTIVE

    # 1. Наличие директивы 1-тап копирования в МИС (IDENT / Инфодент) в ядре и в PM
    assert "1-ТАП КОПИРОВАНИЕ В IDENT И ИНФОДЕНТ" in arch
    assert "<code>...</code>" in arch
    assert "043/у" in arch

    assert "<code>...</code>" in pm_dir
    assert "043/у" in pm_dir
    assert ("IDENT" in pm_dir or "ИНФОДЕНТ" in pm_dir)

    # 2. Запрет Markdown и запрет регулярочных заплаток
    assert "ТОТАЛЬНЫЙ ЗАПРЕТ MARKDOWN" in arch
    assert "Никаких регулярочных заплаток" in arch
    assert "<b>, <i>, <code>, <pre>" in arch


@pytest.mark.asyncio
async def test_instant_search_clinical_keywords():
    """
    Проверяет мгновенный полнотекстовый поиск клинических заметок и протоколов
    по ключевым клиническим запросам сообщества:
    - препарирование
    - фиксация
    - апекс
    - сломанный инструмент
    - VDO
    """
    # 1. препарирование
    prep_res = await database.get_clinical_protocols(search="препарирование")
    assert len(prep_res) >= 1, "Поиск по 'препарирование' должен вернуть протокол"
    assert any("bopt" in p["title"].lower() or "vertiprep" in p["title"].lower() or "препарирован" in p["title"].lower() or "уступ" in p["title"].lower() for p in prep_res)

    # 2. фиксация
    fix_res = await database.get_clinical_protocols(search="фиксация")
    assert len(fix_res) >= 1, "Поиск по 'фиксация' должен вернуть протокол"
    assert any("e.max" in p["title"].lower() or "фиксаци" in p["title"].lower() or "адгезив" in p["title"].lower() for p in fix_res)

    # 3. апекс
    apex_res = await database.get_clinical_protocols(search="апекс")
    assert len(apex_res) >= 1, "Поиск по 'апекс' должен вернуть протокол"
    assert any("апекс" in p["title"].lower() or "гипохлорит" in p["title"].lower() or "дезинфекция" in p["title"].lower() or "апикальн" in p["title"].lower() for p in apex_res)

    # 4. сломанный инструмент
    broken_res = await database.get_clinical_protocols(search="сломанный инструмент")
    assert len(broken_res) >= 1, "Поиск по 'сломанный инструмент' должен вернуть протокол"
    assert any("сломан" in p["title"].lower() or "байпас" in p["title"].lower() or "извлечение" in p["title"].lower() or "отломок" in p["title"].lower() for p in broken_res)

    # 5. VDO
    vdo_res = await database.get_clinical_protocols(search="VDO")
    assert len(vdo_res) >= 1, "Поиск по 'VDO' должен вернуть протокол"
    assert any("vdo" in p["title"].lower() or "высота прикуса" in p["title"].lower() or "окклюзи" in p["title"].lower() for p in vdo_res)

    # Проверка клинической структуры протоколов: steps_json, materials, key_nuances
    for p in (prep_res[0], fix_res[0], apex_res[0], broken_res[0], vdo_res[0]):
        assert p["title"], "Заголовок протокола не должен быть пустым"
        assert p["steps_json"], "Шаги протокола не должны быть пустыми"
        assert p["materials"], "Материалы протокола не должны быть пустыми"
        assert p["key_nuances"], "Ключевые нюансы протокола не должны быть пустыми"


@pytest.mark.asyncio
async def test_search_clinical_notes_unified():
    """Проверяет объединенный поиск клинических заметок и протоколов search_clinical_notes."""
    user_id = 82719283
    await database.save_clinical_bookmark(
        saved_by_user_id=user_id,
        msg_id=5501,
        chat_id=-10055,
        sender_name="Д-р Николаев",
        text="Протокол фиксации коронки e.max: плавиковая кислота 20 сек, силан 60 сек, Variolink.",
        has_media=False,
        media_description="",
        date="2026-11-23 14:00:00",
        tags="#ортопедия #фиксация"
    )

    notes = await database.search_clinical_notes(query="фиксация", user_id=user_id, limit=10)
    assert len(notes) >= 2, "Должны быть найдены и протоколы, и закладки врача"
    sources = [n["source"] for n in notes]
    assert "protocol" in sources
    assert "bookmark" in sources
    # Проверка наличия готового МИС-экспорта в выдаче
    for n in notes:
        assert "mis_export" in n
        assert "<code>" in n["mis_export"]
        assert "</code>" in n["mis_export"]


@pytest.mark.asyncio
async def test_clinical_bookmarks_tags_and_tag_search():
    """
    Проверяет сохранение клинических закладок с тегами, поиск по тегам,
    извлечение хештегов из текста и IDOR-защиту.
    """
    doctor_id = 918273645
    other_doctor_id = 112233445

    # 1. Сохранение с явными тегами (список)
    await database.save_clinical_bookmark(
        saved_by_user_id=doctor_id,
        msg_id=701,
        chat_id=-1001234567,
        sender_name="Д-р Иванов",
        text="Разбор препарирования под оверлей: уступ 1.0 мм, сглаживание углов.",
        has_media=False,
        media_description="",
        date="2026-11-20 12:00:00",
        tags=["#ортопедия", "#препарирование", "#оверлей"]
    )

    # 2. Сохранение с автоизвлечением хештегов из текста
    await database.save_clinical_bookmark(
        saved_by_user_id=doctor_id,
        msg_id=702,
        chat_id=-1001234567,
        sender_name="Д-р Петров",
        text="Пассивная ультразвуковая ирригация NaOCl 3% #эндо #апекс #ирригация",
        has_media=False,
        media_description="",
        date="2026-11-21 15:30:00"
    )

    # 3. Сохранение другим врачом
    await database.save_clinical_bookmark(
        saved_by_user_id=other_doctor_id,
        msg_id=703,
        chat_id=-1001234567,
        sender_name="Д-р Сидоров",
        text="Имплантация Nobel #хирургия #торк",
        has_media=False,
        media_description="",
        date="2026-11-22 10:00:00",
        tags="#хирургия, #имплантация"
    )

    # 4. Проверка получения списка тегов врача
    tags = await database.get_user_bookmark_tags(doctor_id)
    assert "#ортопедия" in tags
    assert "#препарирование" in tags
    assert "#эндо" in tags
    assert "#апекс" in tags
    assert "#хирургия" not in tags  # Тег чужого врача не должен присутствовать!

    # 5. Поиск по тегу
    endo_bms = await database.get_clinical_bookmarks(doctor_id, tag="апекс", include_id=True, include_tags=True)
    assert len(endo_bms) == 1
    assert endo_bms[0][1] == 702  # msg_id 702
    assert "#апекс" in endo_bms[0][7]  # tags column

    # 6. Счётчик закладок по тегу
    count_apex = await database.count_clinical_bookmarks(doctor_id, tag="апекс")
    assert count_apex == 1

    # 7. Обновление тегов с IDOR-защитой
    bm_id = endo_bms[0][0]
    # Чужой врач не может обновить теги
    res_idor = await database.update_clinical_bookmark_tags(bm_id, ["#взлом"], saved_by_user_id=other_doctor_id)
    assert res_idor is False

    # Владелец успешно обновляет теги
    res_ok = await database.update_clinical_bookmark_tags(bm_id, ["#эндо", "#апекс", "#ревизия"], saved_by_user_id=doctor_id)
    assert res_ok is True

    updated_bm = await database.get_clinical_bookmark_dict(bm_id, saved_by_user_id=doctor_id)
    assert "#ревизия" in updated_bm["tags"]

    # 8. Проверка структурированного поиска search_clinical_bookmarks
    search_res = await database.search_clinical_bookmarks(doctor_id, query="оверлей")
    assert len(search_res) == 1
    assert search_res[0]["msg_id"] == 701
    assert "#ортопедия" in search_res[0]["tags"]


@pytest.mark.asyncio
async def test_mis_export_format_clinical_records():
    """
    Проверяет экспорт клинических заметок, закладок и протоколов в формате МИС
    (моноширинный <code>...</code>) для вставки в один клик в карту 043/у / IDENT / Инфодент.
    """
    # 1. Форматирование клинической заметки для МИС
    mis_note = database.format_clinical_note_for_mis(
        title="Препарирование зуба 1.6 под оверлей",
        text="Проведено препарирование жевательной поверхности с редукцией бугров на 1.5 мм. Изоляция коффердамом.",
        doctor="Д-р Смирнов",
        tooth="1.6",
        diagnosis="К02.1 Кариес дентина",
        tags="#ортопедия #оверлей"
    )
    assert "<code>" in mis_note
    assert "</code>" in mis_note
    assert "1-тап" in mis_note
    assert "IDENT" in mis_note or "ИНФОДЕНТ" in mis_note or "043/У" in mis_note
    assert "Д-р Смирнов" in mis_note
    assert "1.6" in mis_note
    assert "**" not in mis_note
    assert "###" not in mis_note

    # 2. Форматирование протокола для МИС
    protos = await database.get_clinical_protocols(search="BOPT")
    assert len(protos) >= 1
    bopt_proto = protos[0]

    mis_proto = database.format_protocol_mis(bopt_proto)
    assert "<code>" in mis_proto
    assert "</code>" in mis_proto
    assert "<b>" in mis_proto
    assert "BOPT" in mis_proto
    assert "1-тап" in mis_proto

    # 3. Асинхронный экспорт протокола по ID
    exported_proto = await database.export_protocol_mis_async(bopt_proto["id"])
    assert exported_proto is not None
    assert "<code>" in exported_proto
    assert bopt_proto["title"] in exported_proto

    # 4. Асинхронный экспорт закладки в МИС
    doc_id = 991122
    await database.save_clinical_bookmark(
        saved_by_user_id=doc_id,
        msg_id=8881,
        chat_id=-10099,
        sender_name="Д-р Эксперт",
        text="Адгезивный протокол: травление эмали 30с, силан 60с, Variolink Esthetic.",
        has_media=False,
        media_description="",
        date="2026-11-23 10:00:00",
        tags="#адгезия, #фиксация"
    )
    bms = await database.get_clinical_bookmarks(doc_id, include_id=True)
    bm_id = bms[0][0]

    exported_bm = await database.export_bookmark_to_mis_async(bm_id, saved_by_user_id=doc_id)
    assert exported_bm is not None
    assert "<code>" in exported_bm
    assert "Д-р Эксперт" in exported_bm
    assert "Variolink" in exported_bm


@pytest.mark.asyncio
async def test_database_reliability_invariants_and_wal_checkpoint():
    """
    Проверяет инварианты надежности базы данных:
    1. SQLite WAL mode (PRAGMA journal_mode = WAL).
    2. synchronous = NORMAL (1).
    3. busy_timeout = 30000.
    4. Автоматический PRAGMA wal_checkpoint(TRUNCATE).
    5. Защита от дедлоков при конкурентном асинхронном доступе.
    """
    # 1. Проверка инвариантов
    inv = await database.verify_database_invariants_async()
    assert inv["is_wal"] is True
    assert inv["is_normal_sync"] is True
    assert inv["busy_timeout"] >= 30000
    assert inv["quick_check"] == "ok"
    assert inv["is_safe"] is True

    # 2. PRAGMA wal_checkpoint(TRUNCATE)
    ckpt_ok = await database.checkpoint_wal_async(mode="TRUNCATE")
    assert ckpt_ok is True

    # 3. Конкурентный доступ: 15 параллельных асинхронных операций чтения и записи
    async def worker_write(idx):
        user_id = 900000 + idx
        await database.save_clinical_bookmark(
            saved_by_user_id=user_id,
            msg_id=idx,
            chat_id=-10000,
            sender_name=f"Doctor {idx}",
            text=f"Клиническая заметка {idx} под нагрузкой",
            has_media=False,
            media_description="",
            date="2026-11-23 12:00:00",
            tags=f"#тест{idx}"
        )
        bms = await database.get_clinical_bookmarks(user_id, include_id=True)
        assert len(bms) >= 1
        return len(bms)

    tasks = [worker_write(i) for i in range(1, 16)]
    results = await asyncio.gather(*tasks)
    assert len(results) == 15
    assert all(r >= 1 for r in results)

    # 4. Проверка закрытия базы со сбросом WAL
    close_ok = database.close_db(checkpoint=True)
    assert close_ok is True


@pytest.mark.asyncio
async def test_real_clinician_queries_fts_and_ranking():
    """
    Проверяет полнотекстовый поиск FTS5 и семантическое ранжирование
    по реальным клиническим запросам врачей из чата:
    1. «сладкое» -> купирование гиперестезии дентина и боли от сладкого
    2. «паста с фтором» -> фторпрофилактика, концентрации 1450–5000 ppm
    3. «шепелявость» -> фонетическая проба по Сильверману, окклюзия
    4. «звук с» -> фонетическое расстояние Closest Speaking Space
    5. «шарик в центре коронки» -> дентикли/пульполиты пульповой камеры
    6. «феррул» -> оценка и создание кругового феррул-эффекта 1.5–2 мм
    7. «препарирование» -> вертикальное препарирование / BOPT
    8. «фиксация e.max» -> адгезивная фиксация дисиликата лития
    Все результаты должны содержать точный сниппет с блоком <code>...</code> для 1-тап копирования в МИС.
    """
    real_queries_expectations = [
        ("сладкое", "сладк"),
        ("паста с фтором", "фтор"),
        ("шепелявость", "шепеляв"),
        ("звук с", "сильверман"),
        ("шарик в центре коронки", "дентикл"),
        ("феррул", "феррул"),
        ("препарирование", "препарирован"),
        ("фиксация e.max", "дисиликат"),
    ]

    for q, expected_marker in real_queries_expectations:
        protos = await database.get_clinical_protocols(search=q, limit=5)
        assert len(protos) >= 1, f"Запрос '{q}' должен вернуть хотя бы 1 протокол"
        top_title = protos[0]["title"].lower()
        top_content = (protos[0]["title"] + " " + protos[0].get("materials", "") + " " + protos[0].get("key_nuances", "")).lower()
        assert expected_marker in top_content, f"Для запроса '{q}' ожидался маркер '{expected_marker}' в топе, получено: '{protos[0]['title']}'"

        # Проверка МИС-экспорта и 1-тап сниппета
        mis_snippet = database.format_protocol_mis(protos[0])
        assert "<code>" in mis_snippet, "Сниппет должен содержать <code> блок"
        assert "</code>" in mis_snippet, "Сниппет должен содержать </code> закрывающий тег"
        assert "1-тап" in mis_snippet or "1 тап" in mis_snippet, "Сниппет должен содержать маркер 1-тап"
        assert "**" not in mis_snippet, "В сниппете не должно быть Markdown '**'"
        assert "###" not in mis_snippet, "В сниппете не должно быть Markdown '###'"


@pytest.mark.asyncio
async def test_media_safe_handling_and_fallbacks(tmp_path):
    """
    Проверяет отказоустойчивость методов базы данных при работе с битыми и отсутствующими файлами:
    - validate_media_file_safe защищает от пустых/несуществующих/0-байтовых путей
    - safe_get_media_url очищает битую запись и возвращает безопасный fallback (None)
    """
    # 1. Валидация несуществующих и пустых файлов
    assert database.validate_media_file_safe(None) is False
    assert database.validate_media_file_safe("") is False
    assert database.validate_media_file_safe("   ") is False
    assert database.validate_media_file_safe("totally_missing_file.jpg") is False

    # 2. Валидация поврежденного файла (0 байт)
    empty_file = tmp_path / "zero_bytes.jpg"
    empty_file.write_bytes(b"")
    assert database.validate_media_file_safe(str(empty_file)) is False

    # 3. Валидация корректного файла
    valid_file = tmp_path / "valid_image.jpg"
    valid_file.write_bytes(b"JFIF" + b"\x00" * 200)
    assert database.validate_media_file_safe(str(valid_file), min_bytes=50) is True

    # 4. Проверка безопасной санитизации в БД (safe_get_media_url)
    msg_id = 887766
    await database.save_message(
        msg_id=msg_id,
        sender_id=12345,
        sender_name="Doctor",
        sender_username="doc",
        text="Снимок с битым локальным файлом",
        date="2026-11-23 12:00:00",
        has_media=True,
        media_type="photo"
    )
    # Записываем битый локальный путь
    await database.update_media_remote_url(msg_id, "totally_missing_file.jpg")

    # safe_get_media_url обнаруживает, что файл отсутствует, очищает запись и возвращает None!
    url_res = await database.safe_get_media_url(msg_id)
    assert url_res is None

    # Проверяем, что битая ссылка зачищена в БД
    def check_cleaned():
        with database._connection() as db:
            row = db.execute("SELECT media_remote_url, has_media FROM messages WHERE msg_id = ?", (msg_id,)).fetchone()
            return row
    cleaned_row = await database._run_db(check_cleaned)
    assert cleaned_row[0] is None
    assert cleaned_row[1] == 0


@pytest.mark.asyncio
async def test_concurrency_stress_and_deadlock_protection():
    """
    Стресс-тест многопоточности и защиты от SQLITE_BUSY:
    Запускает 25 одновременных асинхронных операций чтения, записи, обновления тегов
    и фонового сброса WAL (PASSIVE) без взаимных блокировок и сбоев целостности.
    """
    async def worker_task(worker_id):
        user_id = 800000 + worker_id
        # 1. Запись сообщения
        await database.save_message(
            msg_id=user_id,
            sender_id=user_id,
            sender_name=f"Stress Doc {worker_id}",
            sender_username=f"doc_{worker_id}",
            text=f"Клиническое обсуждение в стресс-тесте {worker_id} #stress",
            date="2026-11-23 15:00:00"
        )
        # 2. Запись клинической закладки
        await database.save_clinical_bookmark(
            saved_by_user_id=user_id,
            msg_id=user_id,
            chat_id=-100999,
            sender_name=f"Stress Doc {worker_id}",
            text=f"Закладка стресс-теста #{worker_id}",
            has_media=False,
            media_description="",
            date="2026-11-23 15:00:00",
            tags=[f"#стресс_{worker_id}", "#высокая_нагрузка"]
        )
        # 3. Обновление тегов
        bms = await database.get_clinical_bookmarks(user_id, include_id=True)
        assert len(bms) >= 1
        bm_id = bms[0][0]
        await database.update_clinical_bookmark_tags(bm_id, [f"#тег_обновлен_{worker_id}"], saved_by_user_id=user_id)

        # 4. Поиск протоколов под нагрузкой
        protos = await database.get_clinical_protocols(search="препарирование", limit=3)
        assert len(protos) >= 1

        # 5. Фоновый сброс WAL во время параллельной работы
        if worker_id % 5 == 0:
            await database.checkpoint_wal_async(mode="PASSIVE")

        return True

    # Запускаем 25 параллельных конкурентных воркеров
    tasks = [worker_task(i) for i in range(1, 26)]
    results = await asyncio.gather(*tasks)
    assert len(results) == 25
    assert all(r is True for r in results)

    # Итоговая проверка целостности БД
    inv = await database.verify_database_invariants_async()
    assert inv["quick_check"] == "ok"
    assert inv["is_safe"] is True
