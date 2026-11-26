import os
import sys
import asyncio
import json
from datetime import datetime
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import summarizer
import tg_safety
import distiller
import protocol_extractor
import digest_pdf
import blocking_tools


# ---------------------------------------------------------------------------
# 1. summarizer.py: clinical media filtering, punctuation, nested bold, teaser
# ---------------------------------------------------------------------------

def test_collect_clinical_media_filters_nonclinical_and_prefers_fullres():
    media_map, media_captions, media_authors = {}, {}, {}

    # Prod non-clinical #1: Christmas tree stand (msg_id=185663)
    kept_tree = summarizer._collect_clinical_media_item(
        185663,
        "Доктор",
        "Бытовое изображение подставки под ёлку. Каких-либо стоматологических объектов не обнаружено.",
        "Ироничная бытовая иллюстрация из врачебного сообщества.",
        "https://img3.pixhost.to/images/100/185663.jpg",
        media_map,
        media_captions,
        media_authors,
    )
    assert kept_tree is False

    # Prod non-clinical #2: book shelf (msg_id=185710)
    kept_books = summarizer._collect_clinical_media_item(
        185710,
        "Доктор",
        "Книжная полка в кабинете. Объекты не являются клиническими данными.",
        "Подборка стоматологической литературы.",
        "https://img3.pixhost.to/images/100/185710.jpg",
        media_map,
        media_captions,
        media_authors,
    )
    assert kept_books is False

    # Prod thumb followed by full-res with identical caption -> full-res wins
    cap = "Рентгенограмма имплантата с завышенным контуром коронки"
    assert summarizer._collect_clinical_media_item(
        185682, "🇸🇦", "На визиограмме дентальный имплантат", cap,
        "https://t3.pixhost.to/thumbs/105/671225.jpg",
        media_map, media_captions, media_authors,
    ) is True
    assert summarizer._collect_clinical_media_item(
        185683, "🇸🇦", "На визиограмме дентальный имплантат", cap,
        "https://img3.pixhost.to/images/105/671225.jpg",
        media_map, media_captions, media_authors,
    ) is True

    assert 185682 not in media_map
    assert media_map[185683] == "https://img3.pixhost.to/images/105/671225.jpg"
    assert 185663 not in media_map
    assert 185710 not in media_map


def test_embed_media_preserves_trailing_periods_and_blocks_memes():
    src = (
        "<p>Пластика с учетом мягких тканей. [IMG_101]</p>\n\n"
        "<p>Зазор между сканбоди [IMG_102].</p>\n\n"
        "<p>Шутка про ёлку [IMG_103].</p>"
    )
    media_map = {
        101: "https://img3.pixhost.to/images/101.jpg",
        102: "https://img3.pixhost.to/images/102.jpg",
        103: "https://img3.pixhost.to/images/103.jpg",
    }
    media_captions = {
        101: "Контур мягких тканей вокруг формирователя десны",
        102: "Контрольная рентгенограмма посадки сканбоди",
        103: "Ироничная бытовая иллюстрация из врачебного сообщества.",
    }
    out = summarizer.embed_media_into_summary_html(src, media_map, media_captions, max_images=4)
    assert "с учетом мягких тканей." in out
    assert "между сканбоди." in out
    assert "103.jpg" not in out
    assert "[IMG_" not in out


def test_collapse_nested_emphasis_and_filter_useful_placeholders():
    raw = "<p><b>🦷 <b>КЛИНИЧЕСКИЕ КЕЙСЫ: РАЗБОР</b></b></p><p></p><p><b>Одинокий</b> текст</p>"
    collapsed = summarizer._collapse_nested_emphasis(raw)
    assert collapsed == "<p><b>🦷 КЛИНИЧЕСКИЕ КЕЙСЫ: РАЗБОР</b></p><p><b>Одинокий</b> текст</p>"

    msgs = [
        (1, "Вася", "v", "ахаха", "-", datetime(2026, 11, 25, 12, 0), None, None),
        (2, "Петя", "p", "согласен", "SKIP: мем", datetime(2026, 11, 25, 12, 1), None, None),
        (3, "Елена", "e", "Ретрит 36 зуба, протейперы, отлом файла в апикальной трети, как доставать?", None, datetime(2026, 11, 25, 12, 2), None, None),
    ]
    useful = summarizer.filter_useful_messages(msgs)
    assert [m[0] for m in useful] == [3]


def test_get_russian_date_handles_iso_date_and_none():
    assert summarizer.get_russian_date("2026-11-25") == "25 ноября 2026"
    assert summarizer.get_russian_date("2026-11-25T22:00:00") == "25 ноября 2026"
    assert summarizer.get_russian_date(None).endswith(str(datetime.now().year))


def test_prepare_telegram_direct_html_strips_telegraph_only_tags():
    telegraph_html = (
        "<h3>🦷 КЛИНИЧЕСКИЕ КЕЙСЫ</h3>\n"
        "<p>Разбор <b>ретрита</b> 36 зуба.</p>\n"
        "<figure><img src=\"https://img3.pixhost.to/images/1.jpg\"><figcaption>Снимок & контроль</figcaption></figure>\n"
        "<hr/>\n"
        "<ul><li>Шаг один</li><li>Шаг два</li></ul>"
    )
    tg = summarizer._prepare_telegram_direct_html(telegraph_html)
    for forbidden in ("<p>", "</p>", "<figure", "<img", "<h3", "<hr", "<ul", "<li"):
        assert forbidden not in tg.lower()
    assert "<b>🦷 КЛИНИЧЕСКИЕ КЕЙСЫ</b>" in tg
    assert '<a href="https://img3.pixhost.to/images/1.jpg">Снимок &amp; контроль</a>' in tg
    assert "• Шаг один" in tg


def test_daily_summary_long_titles_and_telegraph_pdf_fallback(tmp_path):
    long_article = (
        "<b>🎯 КЛИНИЧЕСКИЙ ФОКУС ДНЯ: Протокол одномоментной имплантации и немедленной нагрузки в эстетически значимой зоне верхней челюсти при тонком биотипе</b>\n"
        + ("Подробное обсуждение формирования профиля прорезывания и ССТ. " * 25)
        + "\n\n<b>Кейс 1: Восстановление 36 зуба после вертикальной трещины и периапикальной резорбции костной ткани у пациента со сложной окклюзией</b>\n"
        + ("Детальный клинический разбор и фотопротокол. " * 20)
    )
    msgs = [
        (200 + i, f"Врач {i}", f"doc{i}",
         "Коллеги, как вести перфорацию бифуркации 36 зуба и чем закрывать MTA?",
         None, datetime(2026, 11, 25, 11, i), None, None)
        for i in range(6)
    ]

    class StubClient:
        def __init__(self):
            self.sends = []
            self.files = []
            self.pins = []

        async def get_messages(self, chat_id, limit=None):
            return []

        async def send_message(self, chat_id, text, **params):
            self.sends.append((chat_id, text, params))
            return type("M", (), {"id": len(self.sends)})()

        async def send_file(self, chat_id, file_path, **params):
            self.files.append((chat_id, file_path, params))
            return type("F", (), {"id": 99})()

        async def pin_message(self, chat_id, message_id, notify=False):
            self.pins.append(message_id)

    pdf_file = tmp_path / "daily.pdf"
    pdf_file.write_bytes(b"%PDF-1.4\n" + b"0" * 1500)

    async def fake_gen(*args, **kwargs):
        return type("Resp", (), {"text": long_article})()

    async def fake_telegraph_fail(*args, **kwargs):
        return None, "timeout"

    async def fake_pdf_ok(**kwargs):
        return str(pdf_file)

    async def fake_pdf_fail(**kwargs):
        return None

    async def _run():
        client_ok = StubClient()
        with patch.object(summarizer, "_generate_text_singleflight", fake_gen), \
             patch.object(summarizer, "create_telegraph_page_async", fake_telegraph_fail), \
             patch("digest_pdf.generate_digest_pdf", fake_pdf_ok), \
             patch("runtime_guard.write_summary_status"), \
             patch("runtime_guard.clear_summary_status"), \
             patch("user_memory.format_users_chunk_context", return_value=""):
            teaser = await summarizer.process_summary_batch(msgs, client_ok, -100111)
        # Real long titles extracted (clipped with ellipsis) instead of generic boilerplate
        assert "🔹 " in teaser
        assert "…" in teaser
        # PDF delivered -> no raw fallback spam in chat
        assert len(client_ok.sends) == 1
        assert len(client_ok.files) == 1

        client_dual_fail = StubClient()
        with patch.object(summarizer, "_generate_text_singleflight", fake_gen), \
             patch.object(summarizer, "create_telegraph_page_async", fake_telegraph_fail), \
             patch("digest_pdf.generate_digest_pdf", fake_pdf_fail), \
             patch("runtime_guard.write_summary_status"), \
             patch("runtime_guard.clear_summary_status"), \
             patch("user_memory.format_users_chunk_context", return_value=""):
            await summarizer.process_summary_batch(msgs, client_dual_fail, -100111)
        assert len(client_dual_fail.sends) >= 2
        assert "<p>" not in client_dual_fail.sends[1][1]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. tg_safety.py: Telegram HTML sanitization & tag protection
# ---------------------------------------------------------------------------

def test_tg_safety_sanitizes_p_br_markdown_and_protects_code():
    raw = (
        "<b>Протокол</b>\n<p></p><p><b>EBM:</b> используйте **хлоргексидин** 2%<br>"
        "<code>foo(**kwargs)</code> и <a href=\"https://ex.com/**p**\">ссылка</a></p>\n"
        "* первый пункт\n<b>незакрытый"
    )
    cleaned = tg_safety._sanitize_telegram_html(raw)
    assert "<p" not in cleaned.lower()
    assert "<br" not in cleaned.lower()
    assert "<b>хлоргексидин</b>" in cleaned
    assert "<code>foo(**kwargs)</code>" in cleaned
    assert 'href="https://ex.com/**p**"' in cleaned
    assert "• первый пункт" in cleaned
    assert cleaned.count("<b>") == cleaned.count("</b>")


# ---------------------------------------------------------------------------
# 3. distiller.py: UTF-8 mojibake fix in _extract_facts_by_regex
# ---------------------------------------------------------------------------

def test_distiller_regex_fallback_preserves_cyrillic_with_escapes():
    broken_json = (
        '[{"c":"ENDO","f":"Использовать NaOCl 3%\\nТемпература 50\\u00b0C, \\"активация\\" звуком","s":[12345]}'
        ',{"c":"ORTHO","f":"Сломанный хвост без закрытия"'
    )
    facts = distiller._extract_facts_by_regex(broken_json)
    assert len(facts) == 1
    assert facts[0]["c"] == "ENDO"
    assert "Использовать NaOCl 3%" in facts[0]["f"]
    assert "\nТемпература 50°C" in facts[0]["f"]
    assert '"активация"' in facts[0]["f"]
    assert "Ð" not in facts[0]["f"]


# ---------------------------------------------------------------------------
# 4. protocol_extractor.py: JSON outer object, steps check, HTML safety
# ---------------------------------------------------------------------------

def test_protocol_extractor_fixes():
    rabberdam_proto = (
        "Клинический алгоритм изоляции рабочего поля с применением системы раббердам:\n"
        "1. Этап подбора кламмера и разметки латексного платка под группу зубов.\n"
        "2. Финальная проверка прилегания платка в межзубных промежутках и флоссинг десневой манжеты перед работой."
    )
    assert protocol_extractor.is_protocol_candidate(rabberdam_proto) is True

    # Prefers outer object over nested dict with "title" + handles trailing comma
    messy = (
        'Вот протокол:\n'
        '{"is_protocol": true, "title": "Внешний протокол", "category": "Эндодонтия", '
        '"steps": [{"step": 1, "title": "Внутренний шаг", "description": "А"}, '
        '{"step": 2, "title": "Шаг 2", "description": "Б"}],}\nКонец.'
    )
    parsed = protocol_extractor._clean_json_str(messy)
    assert parsed is not None
    assert parsed["title"] == "Внешний протокол"
    assert len(parsed["steps"]) == 2

    # Balanced tags in _safe_html_text
    safe = protocol_extractor._safe_html_text("**жирный *незакрытый <script>")
    assert "&lt;script&gt;" in safe
    assert safe.count("<b>") == safe.count("</b>")

    # Escaped author & category in format_protocol_catalog + exact short title button
    text, buttons = protocol_extractor.format_protocol_catalog(
        [{"id": 7, "title": "Короткий", "category": "Терапия & <EBM>", "author_doctor": "Dr <X>/Team"}],
        category_filter="Терапия & <EBM>",
    )
    assert "&lt;EBM&gt;" in text
    assert "&lt;X&gt;" in text
    all_btn_texts = [b.text for row in buttons for b in row]
    assert "📖 #1 Короткий" in all_btn_texts

    # Validate non-list steps rejected
    async def fake_llm(*args, **kwargs):
        return type("R", (), {"text": '{"is_protocol": true, "title": "T", "steps": "not a list"}'})(), None

    with patch("blocking_tools.generate_gemini_text_async", fake_llm):
        res = asyncio.run(protocol_extractor.extract_protocol_from_text_async("Протокол коффердам шаг 1 шаг 2"))
    assert res is None

    # Spelling fix in built-in LAST protocol
    last_proto = [p for p in protocol_extractor._DEFAULT_COMMUNITY_PROTOCOLS if "LAST" in p["title"]][0]
    assert "онемение языка" in json.dumps(last_proto, ensure_ascii=False)
    assert "онимение" not in json.dumps(last_proto, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 5. digest_pdf.py: microtypography, card boundaries, date escape, loop-safe sem
# ---------------------------------------------------------------------------

def test_digest_pdf_microtypography_case_boundary_and_semaphore():
    sample_html = (
        '<p><b>Кейс 1: Восстановление 11 зуба</b></p>\n'
        '<p>Постановка пломбы на 11 зуб и по протоколу из статьи. '
        'Обратите внимание на <a href="https://example.com/a b">ссылку в тексте</a>, п к.</p>\n'
        '<p><b>Другие клинические ситуации:</b></p>\n'
        '<p>Краткий обзор 46 зуба.</p>'
    )
    doc = digest_pdf._build_journal_html(
        sample_html, "Заголовок", "Подзаголовок", msg_count=5, date_str="<25 ноября>"
    )
    assert "&lt;25 ноября&gt;" in doc
    assert "на&nbsp;11" in doc
    assert "по&nbsp;протоколу" in doc
    assert "из&nbsp;статьи" in doc
    # Single letter 'п' must NOT get &nbsp; attached!
    assert "п&nbsp;" not in doc
    # href attribute untouched
    assert 'href="https://example.com/a b"' in doc
    # "Другие клинические ситуации" is NOT swallowed inside case-card
    case_end = doc.find("Другие клинические ситуации")
    assert doc.rfind("</div>", 0, case_end) > doc.find('class="case-card"')

    # Semaphore works across two separate asyncio.run calls
    async def acquire():
        sem = digest_pdf._get_pdf_semaphore()
        async with sem:
            return id(sem)

    s1 = asyncio.run(acquire())
    s2 = asyncio.run(acquire())
    assert s1 != s2


# ---------------------------------------------------------------------------
# 6. blocking_tools.py: DB_PATH=None, Telegraph non-JSON HTTP 502, pace lock
# ---------------------------------------------------------------------------

def test_blocking_tools_telegraph_none_db_and_502_html():
    import config

    class BadResp:
        status_code = 502
        text = "<html><body>502 Bad Gateway</body></html>"

        def json(self):
            raise ValueError("Expecting value")

    with patch.object(config, "DB_PATH", None), \
         patch.object(config, "TELEGRAPH_TOKEN", "tok123"), \
         patch("requests.post", return_value=BadResp()):
        with pytest.raises(RuntimeError, match="non-JSON HTTP 502"):
            blocking_tools._create_telegraph_page_sync("Test", "<p>Body</p>")


def test_blocking_tools_pace_lock_survives_multiple_event_loops():
    async def pace_once():
        blocking_tools._LAST_GEMINI_CALL_START = 0.0
        await blocking_tools._pace_gemini_calls()
        return True

    blocking_tools._GEMINI_PACE_LOCK = None
    blocking_tools._GEMINI_PACE_LOCK_OWNED = None
    blocking_tools._GEMINI_PACE_LOCK_LOOP = None
    with patch.object(blocking_tools, "_GEMINI_MIN_INTERVAL_SECONDS", 0.0):
        assert asyncio.run(pace_once()) is True
        assert asyncio.run(pace_once()) is True
