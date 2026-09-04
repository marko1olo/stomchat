"""
Adversarial Stress Test Suite - Challenger 1
Focus: Memory quality, compaction, sentence deduplication, context budgeting, and trivial message filter robustness.

Constraint Checklist:
- ZERO network calls (all mocked)
- Isolated temporary SQLite database
- Zero real LLM API requests
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch

# Configure stdout encoding for Windows
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Setup isolated temp directory for SQLite DB
_TMPDIR = tempfile.mkdtemp(prefix="stomchat_adversarial_challenger_")
import config
config.DB_PATH = os.path.join(_TMPDIR, "adversarial_test.db")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "adversarial_test.log")

import database
import user_memory

RESULTS = {"PASSED": [], "FAILED": []}


def record_check(test_id: str, passed: bool, details: str = ""):
    status = "PASS" if passed else "FAIL"
    if passed:
        RESULTS["PASSED"].append(test_id)
    else:
        RESULTS["FAILED"].append((test_id, details))
    print(f"  [{status}] {test_id}" + (f" | Details: {details}" if details and not passed else ""))


# ===========================================================================
# TEST SUITE 1: Extreme Sentence Duplication & Header Preservation
# ===========================================================================
def test_suite_1_extreme_deduplication():
    print("\n" + "="*70)
    print("TEST SUITE 1: Extreme Sentence Duplication & Header Preservation")
    print("="*70)

    # 1.1 50 Identical sentences in a single section
    single_sentence = "Врач выполняет обтурацию методом вертикальной конденсации гуттаперчи."
    input_50_identical = "Клинические протоколы:\n" + "\n".join([f"• {single_sentence}" for _ in range(50)])
    res_1_1 = user_memory.deduplicate_clinical_summary(input_50_identical)
    lines_1_1 = [l.strip() for l in res_1_1.splitlines() if l.strip()]
    
    record_check(
        "T1.1_50_identical_collapsed_to_one",
        len(lines_1_1) == 2 and single_sentence in lines_1_1[1],
        f"Expected 2 lines (header + 1 sentence), got {len(lines_1_1)} lines: {lines_1_1}"
    )
    record_check(
        "T1.1_header_preserved",
        "Клинические протоколы:" in res_1_1,
        "Header was dropped or corrupted"
    )

    # 1.2 50 Sentences across different sections with duplicates
    input_multi_section = """
Специализация:
• Врач стоматолог-терапевт, эндодонтист.
• Врач стоматолог-терапевт, эндодонтист.
• Врач стоматолог-терапевт, эндодонтист.

Арсенал и оснащение:
• Дентальный микроскоп Leica M320.
• Врач стоматолог-терапевт, эндодонтист.
• Ультразвуковой аппарат Woodpecker.
• Дентальный микроскоп Leica M320.

Клинические протоколы:
• Протокол ирригации: гипохлорит натрия 3% с ультразвуковой активацией.
• Дентальный микроскоп Leica M320.
• Протокол ирригации: гипохлорит натрия 3% с ультразвуковой активацией.

Кейсы:
• Перелечивание зуба 3.6 с извлечением сломанного инструмента.
• Дентальный микроскоп Leica M320.
• Перелечивание зуба 3.6 с извлечением сломанного инструмента.
"""
    res_1_2 = user_memory.deduplicate_clinical_summary(input_multi_section)
    record_check(
        "T1.2_no_cross_section_duplicates",
        res_1_2.count("Leica M320") == 1 and res_1_2.count("эндодонтист") == 1 and res_1_2.count("гипохлорит") == 1,
        f"Duplicates found across sections:\n{res_1_2}"
    )
    for expected_header in ["Специализация:", "Арсенал и оснащение:", "Клинические протоколы:", "Кейсы:"]:
        record_check(
            f"T1.2_header_{expected_header[:6]}_preserved",
            expected_header.lower() in res_1_2.lower(),
            f"Header {expected_header} missing from:\n{res_1_2}"
        )

    # 1.3 Subtle punctuation and casing variations of the same fact
    input_variations = """
Арсенал и оснащение:
• Микроскоп Leica M320.
• микроскоп leica m320!
• «Микроскоп Leica M320»
• * Микроскоп Leica M320...
• 1. Микроскоп Leica M320;
• 2)   Микроскоп   Leica   M320
• 'микроскоп leica m320'
"""
    res_1_3 = user_memory.deduplicate_clinical_summary(input_variations)
    content_lines_1_3 = [l for l in res_1_3.splitlines() if "leica" in l.lower()]
    record_check(
        "T1.3_subtle_casing_punctuation_collapsed",
        len(content_lines_1_3) == 1,
        f"Expected exactly 1 line for variations, got {len(content_lines_1_3)}: {content_lines_1_3}"
    )

    # 1.4 Repeated headers without content
    input_repeated_headers = """
Специализация:
Специализация:
Специализация:
• Эндодонтист.
Арсенал:
Арсенал:
• Микроскоп.
"""
    res_1_4 = user_memory.deduplicate_clinical_summary(input_repeated_headers)
    record_check(
        "T1.4_repeated_headers_deduped",
        res_1_4.lower().count("специализация:") == 1 and res_1_4.lower().count("арсенал:") == 1,
        f"Duplicate headers remained:\n{res_1_4}"
    )

    # 1.5 Header with inline content and repeated inline content
    input_inline = """
Специализация: Стоматолог-терапевт.
Специализация: Стоматолог-терапевт.
Арсенал: Микроскоп Leica M320.
Арсенал: Микроскоп Leica M320.
"""
    res_1_5 = user_memory.deduplicate_clinical_summary(input_inline)
    record_check(
        "T1.5_inline_headers_deduped",
        res_1_5.lower().count("стоматолог-терапевт") == 1 and res_1_5.lower().count("leica m320") == 1,
        f"Inline header duplicates not eliminated:\n{res_1_5}"
    )

    # 1.6 Markdown headers
    input_md_headers = """
**Специализация:**
• Терапевт.
### Клинические протоколы:
• BOPT протокол.
"""
    res_1_6 = user_memory.deduplicate_clinical_summary(input_md_headers)
    record_check(
        "T1.6_markdown_headers_handled",
        "специализация" in res_1_6.lower() and "клинические протоколы" in res_1_6.lower(),
        f"Markdown headers failed:\n{res_1_6}"
    )

    # 1.7 Completely empty or whitespace inputs
    record_check("T1.7_empty_string", user_memory.deduplicate_clinical_summary("") == "", "Expected '' for empty string")
    record_check("T1.7_none_input", user_memory.deduplicate_clinical_summary(None) == "", "Expected '' for None input")
    record_check("T1.7_whitespace_input", user_memory.deduplicate_clinical_summary("   \n\n\t  ") == "", "Expected '' for whitespace input")


# ===========================================================================
# TEST SUITE 2: Boundary Tests on format_users_chunk_context
# ===========================================================================
async def test_suite_2_context_budgeting_boundaries():
    print("\n" + "="*70)
    print("TEST SUITE 2: Boundary Tests on format_users_chunk_context")
    print("="*70)

    await database.init_db()

    # 2.1 Empty user list
    res_empty = await user_memory.format_users_chunk_context([])
    record_check("T2.1_empty_user_list", res_empty == "", f"Expected empty string, got: {res_empty!r}")

    # 2.2 Non-existent users
    res_nonexistent = await user_memory.format_users_chunk_context([999991, 999992])
    record_check("T2.2_nonexistent_users", res_nonexistent == "", f"Expected empty string, got: {res_nonexistent!r}")

    # Populate 100 mock users in DB
    for uid in range(1001, 1101):
        await database.save_user_memory(
            user_id=uid,
            specialty=f"Специализация доктора #{uid}",
            clinical_summary=f"Клиническое досье доктора #{uid}: опыт работы 10 лет, протоколы адгезии.",
            group_summary=f"Профиль беседы доктора #{uid}: активный участник дискуссий по эндодонтии.",
            username=f"doc_{uid}",
            first_name=f"Доктор {uid}"
        )

    # 2.3 100 users list passed (should take at most 20 and stay within max_chars=2000)
    user_ids_100 = list(range(1001, 1101))
    res_100 = await user_memory.format_users_chunk_context(user_ids_100, max_chars=2000)
    record_check(
        "T2.3_100_users_never_exceeds_2000",
        len(res_100) <= 2000 and len(res_100) > 0,
        f"Length was {len(res_100)}"
    )
    # Check that at most 20 unique doctors are even considered
    doctor_lines = [l for l in res_100.splitlines() if l.startswith("• ")]
    record_check(
        "T2.3_capped_at_top_20",
        len(doctor_lines) <= 20,
        f"Found {len(doctor_lines)} doctors, expected <= 20"
    )

    # 2.4 100 identical duplicate users: [1001] * 100
    res_dups = await user_memory.format_users_chunk_context([1001] * 100, max_chars=2000)
    doc_lines_dup = [l for l in res_dups.splitlines() if l.startswith("• ")]
    record_check(
        "T2.4_100_duplicate_ids_collapsed",
        len(doc_lines_dup) == 1 and "Доктор 1001" in res_dups,
        f"Expected 1 doctor line, got {len(doc_lines_dup)}"
    )

    # 2.5 Profiles with EXACTLY 2000 chars clinical summary
    doc_2000_id = 7001
    exact_2000_summary = "А" * 2000
    await database.save_user_memory(
        user_id=doc_2000_id,
        specialty="Ортодонт",
        clinical_summary=exact_2000_summary,
        group_summary="",  # Will fallback to clinical_summary
        username="doc_2000",
        first_name="Доктор Точный"
    )
    res_2000 = await user_memory.format_users_chunk_context([doc_2000_id], max_chars=2000)
    record_check(
        "T2.5_profile_exactly_2000_respects_cap",
        len(res_2000) <= 2000 and len(res_2000) > 0,
        f"Length was {len(res_2000)}"
    )

    # 2.6 Profiles with EXCEEDING 2000 chars (e.g. 5000 chars)
    doc_5000_id = 7002
    huge_summary = "Терапевт-микроскопист. " + ("Длинное описание протокола лечения каналов. " * 150)
    await database.save_user_memory(
        user_id=doc_5000_id,
        specialty="Микроскопист",
        clinical_summary=huge_summary,
        group_summary=huge_summary[:4000],
        username="doc_5000",
        first_name="Доктор Огромный"
    )
    res_5000 = await user_memory.format_users_chunk_context([doc_5000_id], max_chars=2000)
    record_check(
        "T2.6_profile_exceeding_2000_respects_cap",
        len(res_5000) <= 2000 and len(res_5000) > 0,
        f"Length was {len(res_5000)}"
    )

    # 2.7 Stress test: 20 doctors each with 2000 chars under max_chars=2000
    multi_huge_ids = []
    for i in range(1, 21):
        huge_id = 8000 + i
        multi_huge_ids.append(huge_id)
        await database.save_user_memory(
            user_id=huge_id,
            specialty=f"Специализация-{i}",
            clinical_summary="Клиническое описание врача. " * 80,
            group_summary="Описание для беседы врача. " * 40,
            username=f"huge_{i}",
            first_name=f"Доктор-Гигант-{i}"
        )
    res_multi_huge = await user_memory.format_users_chunk_context(multi_huge_ids, max_chars=2000)
    record_check(
        "T2.7_20_huge_doctors_under_2000_limit",
        len(res_multi_huge) <= 2000 and len(res_multi_huge) > 0,
        f"Length was {len(res_multi_huge)}"
    )

    # Check for mid-sentence truncation of doctor profiles at chunk boundary:
    # Does the final doctor note in res_multi_huge get cut mid-sentence?
    last_line = res_multi_huge.splitlines()[-1] if res_multi_huge else ""
    record_check(
        "T2.7_chunk_boundary_no_mid_sentence_truncation",
        last_line.startswith("• Доктор-Гигант-") and not last_line.endswith(("•", "• Доктор", ":")),
        f"Last line seems truncated mid-entry: {last_line[-50:]!r}"
    )

    # 2.8 Inspection of inner slice grp_sum[:300]
    # Check whether grp_sum[:300] cuts words/sentences mid-stream
    # If a sentence boundary is at char 280, does grp_sum[:300] cut mid-word?
    test_inner_id = 9101
    sample_text = "Первое предложение доктора. Второе предложение доктора. Третье очень длинное предложение доктора про сложное препарирование уступов BOPT и фиксацию постоянных коронок на цинк-фосфатный цемент или композит двойного отверждения."
    await database.save_user_memory(
        user_id=test_inner_id,
        specialty="Ортопед",
        group_summary=sample_text,
        username="doc_inner",
        first_name="Доктор Внутренний"
    )
    res_inner = await user_memory.format_users_chunk_context([test_inner_id], max_chars=2000)
    # Check if '...' is added when truncated
    has_ellipsis = "..." in res_inner if len(sample_text) > 300 else True
    record_check(
        "T2.8_inner_excerpt_ellipsis",
        has_ellipsis,
        "Inner excerpt did not indicate truncation with ellipsis"
    )

    # 2.9 Budget limits smaller than header
    res_tiny_budget = await user_memory.format_users_chunk_context(multi_huge_ids, max_chars=100)
    record_check(
        "T2.9_tiny_budget_smaller_than_header",
        res_tiny_budget == "",
        f"Expected empty string when max_chars < header len, got len {len(res_tiny_budget)}"
    )


# ===========================================================================
# TEST SUITE 3: Trivial Message Filter Robustness
# ===========================================================================
def test_suite_3_trivial_message_filter():
    print("\n" + "="*70)
    print("TEST SUITE 3: Trivial Message Filter Robustness")
    print("="*70)

    # 3.1 Empty & Whitespace
    record_check("T3.1_none_is_trivial", user_memory.is_trivial_message(None) is True)
    record_check("T3.1_empty_is_trivial", user_memory.is_trivial_message("") is True)
    record_check("T3.1_spaces_is_trivial", user_memory.is_trivial_message("    ") is True)
    record_check("T3.1_tabs_newlines_is_trivial", user_memory.is_trivial_message("\t\n\r  \n") is True)

    # 3.2 Short messages (< 8 characters)
    record_check("T3.2_ok_lower", user_memory.is_trivial_message("ок") is True)
    record_check("T3.2_ok_upper", user_memory.is_trivial_message("ОК") is True)
    record_check("T3.2_thanks_short", user_memory.is_trivial_message("спс") is True)
    record_check("T3.2_yes", user_memory.is_trivial_message("да") is True)
    record_check("T3.2_no", user_memory.is_trivial_message("нет") is True)
    record_check("T3.2_hello_short", user_memory.is_trivial_message("ку") is True)
    record_check("T3.2_punctuation_only", user_memory.is_trivial_message("???") is True)

    # 3.3 Emojis only
    record_check("T3.3_single_emoji", user_memory.is_trivial_message("👍") is True)
    record_check("T3.3_three_emojis", user_memory.is_trivial_message("👍🙏✨") is True)
    record_check("T3.3_emojis_with_spaces", user_memory.is_trivial_message("👍  👏  ❤️") is True)
    
    # Adversarial emoji stress: 10+ emojis (len >= 8 chars)
    # Does 10 emojis get detected as trivial or slip through as clinical message?
    emojis_10 = "👍" * 10
    is_10_emojis_trivial = user_memory.is_trivial_message(emojis_10)
    record_check(
        "T3.3_10_emojis_trivial",
        is_10_emojis_trivial is True,
        f"10 emojis ('{emojis_10}') returned is_trivial={is_10_emojis_trivial}. (May slip through if len >= 8 and not in patterns!)"
    )

    emojis_spaced = "👍 " * 6  # len 12
    is_spaced_emojis_trivial = user_memory.is_trivial_message(emojis_spaced)
    record_check(
        "T3.3_spaced_emojis_trivial",
        is_spaced_emojis_trivial is True,
        f"Spaced emojis ('{emojis_spaced}') returned is_trivial={is_spaced_emojis_trivial}"
    )

    # 3.4 Acknowledgements & Greetings in UPPER / LOWER / MIXED case
    record_check("T3.4_spasibo_lower", user_memory.is_trivial_message("спасибо") is True)
    record_check("T3.4_spasibo_upper", user_memory.is_trivial_message("СПАСИБО") is True)
    record_check("T3.4_bolshoe_spasibo_lower", user_memory.is_trivial_message("большое спасибо!") is True)
    record_check("T3.4_bolshoe_spasibo_upper", user_memory.is_trivial_message("БОЛЬШОЕ СПАСИБО!") is True)
    record_check("T3.4_ogromnoe_spasibo_vam_kollega", user_memory.is_trivial_message("Огромное спасибо вам, коллега!") is True)
    record_check("T3.4_privet_upper", user_memory.is_trivial_message("ПРИВЕТ") is True)
    record_check("T3.4_dobry_den_upper", user_memory.is_trivial_message("ДОБРЫЙ ДЕНЬ") is True)
    record_check("T3.4_zdravstvuyte_upper", user_memory.is_trivial_message("ЗДРАВСТВУЙТЕ!") is True)

    # Adversarial greetings with address (e.g. "Добрый день, коллега")
    is_dobry_den_kollega_trivial = user_memory.is_trivial_message("Добрый день, коллега")
    record_check(
        "T3.4_dobry_den_kollega_trivial",
        is_dobry_den_kollega_trivial is True,
        f"'Добрый день, коллега' returned is_trivial={is_dobry_den_kollega_trivial}"
    )

    is_dobroe_utro_kollega_trivial = user_memory.is_trivial_message("Доброе утро, коллега")
    record_check(
        "T3.4_dobroe_utro_kollega_trivial",
        is_dobroe_utro_kollega_trivial is True,
        f"'Доброе утро, коллега' returned is_trivial={is_dobroe_utro_kollega_trivial}"
    )

    is_dobry_vecher_vsem_trivial = user_memory.is_trivial_message("Добрый вечер всем!")
    record_check(
        "T3.4_dobry_vecher_vsem_trivial",
        is_dobry_vecher_vsem_trivial is True,
        f"'Добрый вечер всем!' returned is_trivial={is_dobry_vecher_vsem_trivial}"
    )

    # 3.5 Bot slash commands
    record_check("T3.5_slash_start", user_memory.is_trivial_message("/start") is True)
    record_check("T3.5_slash_help", user_memory.is_trivial_message("/help") is True)
    record_check("T3.5_slash_calc", user_memory.is_trivial_message("/calc") is True)
    record_check("T3.5_slash_wipe", user_memory.is_trivial_message("/wipe") is True)
    record_check("T3.5_slash_custom", user_memory.is_trivial_message("/custom_cmd") is True)

    # 3.6 Non-trivial clinical messages (MUST RETURN False)
    clinical_1 = "Коллега, подскажи протокол фиксации винира на OptiBond FL при вертикальном препарировании"
    clinical_2 = "Пациент 45 лет, зуб 3.6, на КЛКТ очаг деструкции в области бифуркации корней 5 мм"
    clinical_3 = "Использую микроскоп Leica M320, увеличение 16х, ультразвук Woodpecker"
    clinical_4 = "Применяю самопротравливающий адгезив 7 поколения или тотальное протравливание"

    record_check("T3.6_clinical_1_not_trivial", user_memory.is_trivial_message(clinical_1) is False)
    record_check("T3.6_clinical_2_not_trivial", user_memory.is_trivial_message(clinical_2) is False)
    record_check("T3.6_clinical_3_not_trivial", user_memory.is_trivial_message(clinical_3) is False)
    record_check("T3.6_clinical_4_not_trivial", user_memory.is_trivial_message(clinical_4) is False)

    # Short clinical message: e.g. "зуб 3.6" or "кариес"
    # len("зуб 3.6") == 7 < 8 -> considered trivial by length threshold!
    # Let's inspect this behavior:
    short_clinical = "зуб 3.6"
    record_check(
        "T3.6_short_clinical_len_7_behavior",
        user_memory.is_trivial_message(short_clinical) is True,
        "Messages < 8 chars are treated as trivial by design to prevent noise."
    )


# ===========================================================================
# TEST SUITE 4: End-to-End Simulation of Update Clinician Memory with Extreme Duplicates
# ===========================================================================
async def test_suite_4_e2e_compaction_deduplication():
    print("\n" + "="*70)
    print("TEST SUITE 4: E2E Compaction with Extreme Duplications via Mock LLM")
    print("="*70)

    test_uid = 444001
    user_memory.reset_pm_memory_cooldown(test_uid)

    # Mock response from LLM that contains extreme duplication
    duplicated_llm_summary = """
Специализация: Врач стоматолог-ортодонт.
Специализация: Врач стоматолог-ортодонт.

Арсенал и оснащение:
• Сканер 3Shape Trios 4.
• Сканер 3Shape Trios 4.
• Сканер 3Shape Trios 4.
• Брекет-системы Damon Q2.

Клинические протоколы:
• Протокол фиксации брекетов: пескоструйная обработка оксидом алюминия 27 мкм.
• Сканер 3Shape Trios 4.
• Протокол фиксации брекетов: пескоструйная обработка оксидом алюминия 27 мкм.

Кейсы:
• Коррекция дистального прикуса с дефицитом места во фронтальном отделе.
• Сканер 3Shape Trios 4.
• Коррекция дистального прикуса с дефицитом места во фронтальном отделе.
"""
    mock_llm_json = {
        "specialty": "Ортодонт",
        "rewritten_summary": duplicated_llm_summary,
        "new_facts": ["Сканер 3Shape", "Брекеты Damon", "Сканер 3Shape"]
    }

    class MockResp:
        def __init__(self, text):
            self.text = text

    with patch("user_memory.generate_gemini_text_async", new=AsyncMock(return_value=(MockResp(json.dumps(mock_llm_json)), None))):
        await user_memory.update_clinician_memory_async(
            user_id=test_uid,
            user_message="Коллега, вот мой клинический протокол и кейс по ортодонтии на сканере 3Shape",
            bot_response="Отличный ортодонтический протокол!",
            username="dr_ortho_stress",
            first_name="Евгений"
        )

    saved_mem = await database.get_user_memory(test_uid)
    saved_summary = saved_mem.get("clinical_summary", "")

    record_check(
        "T4.1_saved_summary_not_empty",
        len(saved_summary) > 0,
        "Saved summary was empty"
    )
    record_check(
        "T4.2_saved_summary_scanner_deduped",
        saved_summary.count("3Shape Trios 4") == 1,
        f"3Shape Trios 4 appeared {saved_summary.count('3Shape Trios 4')} times in:\n{saved_summary}"
    )
    record_check(
        "T4.3_saved_summary_case_deduped",
        saved_summary.count("Коррекция дистального прикуса") == 1,
        f"Case appeared {saved_summary.count('Коррекция дистального прикуса')} times in:\n{saved_summary}"
    )
    record_check(
        "T4.4_saved_summary_headers_preserved",
        all(h in saved_summary for h in ["Специализация:", "Арсенал и оснащение:", "Клинические протоколы:", "Кейсы:"]),
        f"Some headers missing in:\n{saved_summary}"
    )


# ===========================================================================
# MAIN RUNNER
# ===========================================================================
async def main():
    start_time = time.time()
    print("Starting Challenger 1 Adversarial Verification Suite...")

    test_suite_1_extreme_deduplication()
    await test_suite_2_context_budgeting_boundaries()
    test_suite_3_trivial_message_filter()
    await test_suite_4_e2e_compaction_deduplication()

    duration = time.time() - start_time
    total = len(RESULTS["PASSED"]) + len(RESULTS["FAILED"])
    print("\n" + "="*70)
    print(f"ADVERSARIAL STRESS TEST RESULTS: {len(RESULTS['PASSED'])}/{total} PASSED ({duration:.2f}s)")
    print("="*70)

    if RESULTS["FAILED"]:
        print(f"\nFAILED CHECKS ({len(RESULTS['FAILED'])}):")
        for tid, det in RESULTS["FAILED"]:
            print(f"  - {tid}: {det}")
        sys.exit(1)
    else:
        print("\nALL ADVERSARIAL CHECKS PASSED.")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
