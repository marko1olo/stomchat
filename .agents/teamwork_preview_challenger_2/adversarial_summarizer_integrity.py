"""
Adversarial Summarizer Pipeline Integrity & Prompt Regex Verification Harness
Challenger 2 — StomChat Clinician Memory & Summarizer Audit

Adversarially stress-tests summarizer.py:
1. Mixed batches of 8-element, 9-element, and 10-element tuples.
2. Adversarial / Corrupt sender_ids:
   - None, "", "   ", "not_a_number", "doctor_99", 0, -500, [123], {"id": 1}, 1234.56, 9876543210123
3. Corrupt tuple fields (None texts, None names, None dates, cyclic/missing reply_ids).
4. Assertions:
   - ZERO ValueError: too many values to unpack
   - ZERO unhandled exceptions or crashes
   - Active author counting & clinical profile integration work reliably.
5. Prompt regex guards verification against test_digest_formatting.py and test_fix_weekly.py.
"""

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

# Ensure stomchat root is on sys.path
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Setup isolated environment
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_adv_summ_")
test_db_path = os.path.join(_TMPDIR, "adv_summ.db")
test_log_path = os.path.join(_TMPDIR, "adv_summ.log")
os.environ["STOMCHAT_LOG_PATH"] = test_log_path

import config  # noqa: E402
config.DB_PATH = test_db_path

import runtime_guard  # noqa: E402
runtime_guard.HEARTBEAT_PATH = os.path.join(_TMPDIR, "hb.json")
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "status.json")
runtime_guard.WATCHDOG_DUMP_PATH = os.path.join(_TMPDIR, "dump.txt")
runtime_guard.start_watchdog = lambda *a, **k: None
runtime_guard.stop_watchdog = lambda *a, **k: None

import database  # noqa: E402
import summarizer  # noqa: E402

PASS = []
FAIL = []

def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        status = "OK  "
    else:
        FAIL.append(name)
        status = "FAIL"
    detail_str = f" -- {detail}" if detail and not cond else ""
    print(f"  [{status}] {name}{detail_str}")

async def run_adversarial_summarizer_tests():
    print("=" * 80)
    print("  ADVERSARIAL TEST: Summarizer Pipeline Integrity & Prompt Regex Guards")
    print(f"  Target Database: {test_db_path}")
    print("=" * 80)

    await database.init_db()

    # Pre-populate doctor profiles for user IDs 101, 102, 103
    await database.save_user_memory(
        user_id=101,
        specialty="Стоматолог-ортопед",
        clinical_summary="Врач специализируется на безметалловой керамике, винирах и адгезивной фиксации.",
        group_summary="Активный эксперт по препарированию под виниры и коронки E.max.",
        username="doc_ortho",
        first_name="Иван Иванов"
    )
    await database.save_user_memory(
        user_id=102,
        specialty="Эндодонтист-микроскопист",
        clinical_summary="Микроскоп Leica M320, извлечение отломков инструментов, обтурация горячей гуттаперчей.",
        group_summary="Консультирует по сложным корневым каналам и анатомии MB2.",
        username="doc_endo",
        first_name="Елена Смирнова"
    )

    captured_prompts = []

    async def mock_generate_text_singleflight(prompt, kind, chat_id, topic_id, msg_count, prompt_chars):
        captured_prompts.append({"kind": kind, "prompt": prompt})
        mock_resp = type("MockResponse", (), {"text": "<h2>РЕЗУЛЬТАТ ДАЙДЖЕСТА</h2><p>Тестовый контент</p>"})()
        return mock_resp

    mock_client = AsyncMock()
    mock_client.send_message = AsyncMock(return_value=type("SentMsg", (), {"id": 99999})())
    mock_client.pin_message = AsyncMock()

    now = datetime.now(timezone.utc)

    # -----------------------------------------------------------------------
    # TEST 1: Batch of purely 8-element legacy tuples
    # -----------------------------------------------------------------------
    print("\n[Test 1] Purely 8-element legacy tuples (backwards compatibility)")
    batch_8 = [
        (1, "Доктор А", "doc_a", "Коллеги, какой протокол фиксации виниров на релевантный цемент?", None, now, None, None),
        (2, "Доктор Б", "doc_b", "Использую Variolink Esthetic LC с праймером Monobond Plus.", None, now, 1, None),
        (3, "Доктор В", "doc_c", "Обязательно пескоструй оксидом алюминия 50 мкм и протравка плавиковой кислотой 20 сек.", None, now, 2, None),
    ]

    captured_prompts.clear()
    with patch("summarizer._generate_text_singleflight", side_effect=mock_generate_text_singleflight), \
         patch("summarizer._send_message_once", return_value=type("SentMsg", (), {"id": 99999})()), \
         patch("summarizer.create_telegraph_page_async", new=AsyncMock(return_value=("https://telegra.ph/p1", None))):
        try:
            res_8 = await summarizer.process_summary_batch(
                messages=batch_8,
                client=mock_client,
                chat_id=-1001234567,
            )
            check("8-element batch processed without ValueError: too many values to unpack", res_8 is not None)
            check("8-element batch triggered prompt generation", len(captured_prompts) > 0)
        except Exception as e:
            check("8-element batch processed without ValueError", False, f"Exception: {type(e).__name__}: {e}")

    # -----------------------------------------------------------------------
    # TEST 2: Batch of purely 9-element tuples (new contract with sender_id)
    # -----------------------------------------------------------------------
    print("\n[Test 2] Purely 9-element tuples with valid sender_ids")
    batch_9 = [
        (11, "Иван Иванов", "doc_ortho", "Обсуждаем препарирование зуба 1.1 под винир, граница на уровне десны?", None, now, None, None, 101),
        (12, "Елена Смирнова", "doc_endo", "Не забывайте про сохранение эмалевого ободка для надёжной адгезии!", None, now, 11, None, 102),
        (13, "Иван Иванов", "doc_ortho", "Да, адгезия к эмали на OptiBond FL даёт максимальную силу сцепления.", None, now, 12, None, 101),
    ]

    captured_prompts.clear()
    with patch("summarizer._generate_text_singleflight", side_effect=mock_generate_text_singleflight), \
         patch("summarizer._send_message_once", return_value=type("SentMsg", (), {"id": 99999})()), \
         patch("summarizer.create_telegraph_page_async", new=AsyncMock(return_value=("https://telegra.ph/p2", None))):
        try:
            res_9 = await summarizer.process_summary_batch(
                messages=batch_9,
                client=mock_client,
                chat_id=-1001234567,
            )
            check("9-element batch processed without ValueError", res_9 is not None)
            check("9-element batch generated prompt", len(captured_prompts) > 0)
            if captured_prompts:
                p_text = captured_prompts[-1]["prompt"]
                check("Prompt contains clinical profile for active author 101 (Иван Иванов)", "Иван Иванов" in p_text or "doc_ortho" in p_text)
                check("Prompt contains clinical profile for active author 102 (Елена Смирнова)", "Елена Смирнова" in p_text or "doc_endo" in p_text)
        except Exception as e:
            check("9-element batch processed without ValueError", False, f"Exception: {type(e).__name__}: {e}")

    # -----------------------------------------------------------------------
    # TEST 3: Mixed batch of 8-element, 9-element, and 10-element tuples
    # -----------------------------------------------------------------------
    print("\n[Test 3] Mixed batch: 8-element, 9-element, and 10-element tuples")
    batch_mixed = [
        # 8-element
        (21, "Врач 8-1", "user8_1", "Вопрос по эндомотору Woodpecker: какие настройки торка для протейперов?", None, now, None, None),
        # 9-element
        (22, "Елена Смирнова", "doc_endo", "Для F1 торк 1.5 Нсм, скорость 300 об/мин с постоянной ирригацией NaOCl 3%.", None, now, 21, None, 102),
        # 10-element (forward-compatible extra field)
        (23, "Иван Иванов", "doc_ortho", "В сложных каналах реципрокный режим безопаснее против поломки файлов.", None, now, 22, None, 101, "extra_metadata_field"),
        # 8-element
        (24, "Врач 8-2", "user8_2", "Спасибо за ценные рекомендации по торку!", None, now, 23, None),
        # 9-element
        (25, "Врач 9-1", "user9_1", "Какую ультразвуковую активацию гипохлорита используете?", None, now, 22, None, 103),
    ]

    captured_prompts.clear()
    with patch("summarizer._generate_text_singleflight", side_effect=mock_generate_text_singleflight), \
         patch("summarizer._send_message_once", return_value=type("SentMsg", (), {"id": 99999})()), \
         patch("summarizer.create_telegraph_page_async", new=AsyncMock(return_value=("https://telegra.ph/p3", None))):
        try:
            res_mixed = await summarizer.process_summary_batch(
                messages=batch_mixed,
                client=mock_client,
                chat_id=-1001234567,
            )
            check("Mixed 8/9/10-element batch processed without ValueError: too many values to unpack", res_mixed is not None)
        except Exception as e:
            check("Mixed 8/9/10-element batch processed without ValueError", False, f"Exception: {type(e).__name__}: {e}")

    # -----------------------------------------------------------------------
    # TEST 4: Highly Corrupt / Malformed sender_ids
    # -----------------------------------------------------------------------
    print("\n[Test 4] Adversarial / Malformed sender_id values")
    adversarial_sender_ids = [
        None,
        "",
        "   ",
        "corrupted_non_numeric_id",
        "doctor_404",
        0,
        -100,
        -999999999,
        [101, 102],
        {"user_id": 101},
        12345.678,
        "  101  ",  # string with spaces
        "9876543210123",  # large 64-bit ID
    ]

    batch_corrupt_senders = []
    for idx, bad_sid in enumerate(adversarial_sender_ids):
        msg_id = 100 + idx
        batch_corrupt_senders.append(
            (msg_id, f"Author {idx}", f"user_{idx}", f"Клинический вопрос #{idx}: протокол адгезии и полимеризации?", None, now, None, None, bad_sid)
        )

    captured_prompts.clear()
    with patch("summarizer._generate_text_singleflight", side_effect=mock_generate_text_singleflight), \
         patch("summarizer._send_message_once", return_value=type("SentMsg", (), {"id": 99999})()), \
         patch("summarizer.create_telegraph_page_async", new=AsyncMock(return_value=("https://telegra.ph/p4", None))):
        try:
            res_corrupt = await summarizer.process_summary_batch(
                messages=batch_corrupt_senders,
                client=mock_client,
                chat_id=-1001234567,
            )
            check("Corrupt sender_ids processed without TypeError, ValueError, or crash", res_corrupt is not None)
        except Exception as e:
            check("Corrupt sender_ids processed without crash", False, f"Exception: {type(e).__name__}: {e}")

    # -----------------------------------------------------------------------
    # TEST 5: Edge-Case message fields (None text, None name, None date, cyclic replies)
    # -----------------------------------------------------------------------
    print("\n[Test 5] Adversarial message field edge-cases")
    batch_edge_fields = [
        # text is None with media description
        (201, None, None, None, "Снимок КЛКТ зуба 3.6 с периапикальным очагом", now, None, "http://img.test/1.jpg", 101),
        # Empty string text with media
        (202, "", "", "", "Фото препарирования зуба", now, 201, "http://img.test/2.jpg", 102),
        # Cyclic reply_to_msg_id (msg 203 replies to 203)
        (203, "Dr Self", "self_doc", "Вопрос к самому себе: стоит ли делать резекцию верхушки корня?", None, now, 203, None, 103),
        # Reply to nonexistent ID
        (204, "Dr Lost", "lost_doc", "Ответ в пустоту: адгезив 7 поколения показал деградацию.", None, now, 8888888, None, 104),
        # Date as ISO string instead of datetime object
        (205, "Dr Date", "date_doc", "Клинический протокол распломбировки: ультразвук и растворитель эвкалиптол.", None, "2026-09-04 14:00:00", None, None, 105),
    ]

    captured_prompts.clear()
    with patch("summarizer._generate_text_singleflight", side_effect=mock_generate_text_singleflight), \
         patch("summarizer._send_message_once", return_value=type("SentMsg", (), {"id": 99999})()), \
         patch("summarizer.create_telegraph_page_async", new=AsyncMock(return_value=("https://telegra.ph/p5", None))):
        try:
            res_edge = await summarizer.process_summary_batch(
                messages=batch_edge_fields,
                client=mock_client,
                chat_id=-1001234567,
            )
            check("Edge-case message fields processed cleanly without crash", res_edge is not None)
        except Exception as e:
            check("Edge-case message fields processed cleanly", False, f"Exception: {type(e).__name__}: {e}")

    # -----------------------------------------------------------------------
    # TEST 6: Prompt Regex Guards Verification
    # -----------------------------------------------------------------------
    print("\n[Test 6] Prompt Regex Guards Verification (test_digest_formatting & test_fix_weekly)")

    # 6.0 Verify summarizer.py SOURCE CODE against test_digest_formatting.py check
    with open(os.path.join(_PROJECT_ROOT, "summarizer.py"), encoding="utf-8") as f:
        _SUMM_SRC = f.read()
    daily_prompt_src = _SUMM_SRC.split("=== ПРАВИЛА ОФОРМЛЕНИЯ (ЖЕСТКО) ===", 1)[1].split('"""', 1)[0]
    daily_numbers_src = {int(n) for n in re.findall(r"\{DAILY_CHAR_BUDGET\}|(\d{4,5})\s*символ", daily_prompt_src) if n}
    check(
        "Source code summarizer.py has ZERO hardcoded digits before 'символ' in daily prompt template",
        not daily_numbers_src,
        f"Found hardcoded numbers: {daily_numbers_src}"
    )

    # 6.1 Daily Prompt Runtime Regex Check
    captured_prompts.clear()
    with patch("summarizer._generate_text_singleflight", side_effect=mock_generate_text_singleflight), \
         patch("summarizer._send_message_once", return_value=type("SentMsg", (), {"id": 99999})()), \
         patch("summarizer.create_telegraph_page_async", new=AsyncMock(return_value=("https://telegra.ph/p6", None))):
        await summarizer.process_summary_batch(
            messages=batch_9,
            client=mock_client,
            chat_id=-1001234567,
        )

    check("Daily prompt was generated and captured", len(captured_prompts) > 0)
    if captured_prompts:
        daily_prompt_text = captured_prompts[-1]["prompt"]

        daily_hardcoded_matches = re.findall(r"(\d{4,5})\s*символ", daily_prompt_text)
        daily_numbers = {int(n) for n in daily_hardcoded_matches if n}
        check(
            f"Daily prompt at runtime matches exactly {{DAILY_CHAR_BUDGET}} ({summarizer.DAILY_CHAR_BUDGET})",
            daily_numbers == {summarizer.DAILY_CHAR_BUDGET},
            f"Found numbers: {daily_numbers}"
        )
        check(
            "Daily prompt does NOT contain literal '2000 символов'",
            "2000 символов" not in daily_prompt_text and "2000 символ" not in daily_prompt_text,
            "Found '2000 символов' in prompt!"
        )
        check(
            "Daily prompt does NOT contain obsolete ranges '4000-5000' or '7000-9000'",
            "4000-5000" not in daily_prompt_text and "7000-9000" not in daily_prompt_text
        )

    # 6.2 Weekly Prompt Regex Check
    weekly_captured_prompts = []
    async def mock_weekly_generate(prompt, kind, chat_id, topic_id, msg_count, prompt_chars):
        weekly_captured_prompts.append({"kind": kind, "prompt": prompt})
        mock_resp = type("MockResponse", (), {"text": "<h1>WEEKLY DIGEST</h1><p>Содержание недели</p>"})()
        return mock_resp

    with patch("summarizer._generate_text_singleflight", side_effect=mock_weekly_generate), \
         patch("summarizer.create_telegraph_page_async", new=AsyncMock(return_value=("https://telegra.ph/weekly", None))), \
         patch("summarizer._send_message_once", return_value=type("SentMsg", (), {"id": 88888})()):
        try:
            weekly_res = await summarizer.process_weekly_batch(
                messages=batch_9,
                client=mock_client,
                chat_id=-1001234567,
            )
            check("process_weekly_batch executed without crash", weekly_res is not None)
        except Exception as e:
            check("process_weekly_batch executed without crash", False, f"Exception: {type(e).__name__}: {e}")

    check("Weekly prompt was captured", len(weekly_captured_prompts) > 0)
    if weekly_captured_prompts:
        weekly_prompt_text = weekly_captured_prompts[-1]["prompt"]

        # Regex from test_fix_weekly.py:
        # numbers = {int(n) for n in re.findall(r"(\d{4,5})\s*символ", prompt)}
        # check("цифра взята из константы, а не зашита", numbers == {S.WEEKLY_CHAR_BUDGET})
        # check("объём в промпте — одна и та же цифра во всех упоминаниях", len(numbers) == 1)
        weekly_numbers = {int(n) for n in re.findall(r"(\d{4,5})\s*символ", weekly_prompt_text)}
        check(
            "Weekly prompt matches exactly {WEEKLY_CHAR_BUDGET} in regex (len(numbers) == 1)",
            weekly_numbers == {summarizer.WEEKLY_CHAR_BUDGET},
            f"Expected {{{summarizer.WEEKLY_CHAR_BUDGET}}}, got {weekly_numbers}"
        )
        check(
            "Weekly prompt does NOT contain literal '2000 символов' or extraneous numbers",
            "2000 символов" not in weekly_prompt_text and "2000 символ" not in weekly_prompt_text
        )

    # -----------------------------------------------------------------------
    # TEST 7: Adversarial injection of doctor clinical dossier
    # -----------------------------------------------------------------------
    print("\n[Test 7] Doctor dossier containing potential regex collision string")
    await database.save_user_memory(
        user_id=777,
        specialty="Хирург-имплантолог",
        clinical_summary="Провел более 1500 операций по синус-лифтингу и установке имплантатов Straumann.",
        group_summary="Клинический разбор сложных случаев субантральной аугментации.",
        username="doc_surgeon",
        first_name="Сергей Хирургов"
    )
    batch_with_adversarial_doctor = [
        (301, "Сергей Хирургов", "doc_surgeon", "При проведении открытого синус-лифтинга используем мембрану Bio-Gide и Bio-Oss.", None, now, None, None, 777),
        (302, "Иван Иванов", "doc_ortho", "И последующая нагрузка через 6 месяцев после интеграции костного графта.", None, now, 301, None, 101),
    ]

    captured_prompts.clear()
    with patch("summarizer._generate_text_singleflight", side_effect=mock_generate_text_singleflight), \
         patch("summarizer._send_message_once", return_value=type("SentMsg", (), {"id": 99999})()), \
         patch("summarizer.create_telegraph_page_async", new=AsyncMock(return_value=("https://telegra.ph/p7", None))):
        res_adv_doc = await summarizer.process_summary_batch(
            messages=batch_with_adversarial_doctor,
            client=mock_client,
            chat_id=-1001234567,
        )
        check("Batch with doctor memory containing numbers processed cleanly", res_adv_doc is not None)
        if captured_prompts:
            adv_prompt = captured_prompts[-1]["prompt"]
            check("Doctor 777 injected into prompt", "Сергей Хирургов" in adv_prompt or "Хирург-имплантолог" in adv_prompt)
            # Ensure no regex false positive
            daily_hardcoded_matches_adv = re.findall(r"(\d{4,5})\s*символ", adv_prompt)
            check(
                f"Injected dossier does not trigger false positive regex matches (matches only {{{summarizer.DAILY_CHAR_BUDGET}}})",
                set(int(n) for n in daily_hardcoded_matches_adv) == {summarizer.DAILY_CHAR_BUDGET},
                f"Matches: {daily_hardcoded_matches_adv}"
            )

    print("\n" + "=" * 80)
    print(f"  SUMMARIZER INTEGRITY RESULTS: PASSED={len(PASS)}  FAILED={len(FAIL)}")
    print("=" * 80)

    if FAIL:
        print("  FAILURES DETECTED:")
        for f in FAIL:
            print(f"    - {f}")
        sys.exit(1)
    else:
        print("  ALL SUMMARIZER PIPELINE & REGEX INTEGRITY CHECKS PASSED EMPIRICALLY!")

if __name__ == "__main__":
    asyncio.run(run_adversarial_summarizer_tests())
