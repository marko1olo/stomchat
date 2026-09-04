import re
import sys
from typing import List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

_SECTION_HEADER_RE = re.compile(
    r'^(#{1,6}\s*|\*+\s*|[•\-*]\s*)?'
    r'(Специализация|Арсенал(?:\s+и\s+(?:оснащение|оборудование))?|Оснащение|Оборудование|'
    r'Клинические\s+протоколы|Протоколы|Кейсы|Клинические\s+кейсы|Клинический\s+кейс)'
    r'(\s*:\s*|\s*\*\*:?\s*|\s*:\s*\*\*|\s*$)',
    re.IGNORECASE
)

def _normalize_item(text: str) -> str:
    t = re.sub(r'^(?:[•\-*]|\d+[\.\)])\s*', '', text.strip())
    t = t.strip('\'"«» \t')
    t = re.sub(r'[.,;!?]+$', '', t).strip()
    return re.sub(r'\s+', ' ', t).lower()

def _split_into_sentences(text: str) -> List[str]:
    if not text:
        return []
    raw_sentences = re.split(r'(?<=[.!?])\s+(?=[А-ЯA-Z0-9«"—])', text.strip())
    return [s.strip() for s in raw_sentences if s.strip()]

def deduplicate_clinical_summary(summary: str) -> str:
    if not summary or not summary.strip():
        return ""

    lines = summary.strip().splitlines()
    seen_items = set()
    seen_headers = set()
    result_lines = []

    for line in lines:
        stripped_line = line.strip()
        if not stripped_line:
            if result_lines and result_lines[-1] != "":
                result_lines.append("")
            continue

        header_match = _SECTION_HEADER_RE.match(stripped_line)
        if header_match:
            header_name = header_match.group(2).lower()
            header_prefix = header_match.group(0).rstrip()
            inline_content = stripped_line[header_match.end():].strip()

            if not inline_content:
                if header_name in seen_headers:
                    continue
                seen_headers.add(header_name)
                result_lines.append(header_prefix)
            else:
                sentences = _split_into_sentences(inline_content)
                kept_sentences = []
                for s in sentences:
                    norm = _normalize_item(s)
                    if norm and norm not in seen_items:
                        seen_items.add(norm)
                        kept_sentences.append(s)
                if kept_sentences:
                    seen_headers.add(header_name)
                    result_lines.append(f"{header_prefix} {' '.join(kept_sentences)}")
                elif header_name not in seen_headers:
                    seen_headers.add(header_name)
                    result_lines.append(header_prefix)
            continue

        bullet_match = re.match(r'^([•\-*]|\d+[\.\)])\s*', stripped_line)
        bullet_prefix = bullet_match.group(0) if bullet_match else ""
        content_without_bullet = stripped_line[len(bullet_prefix):].strip()

        sentences = _split_into_sentences(content_without_bullet)
        kept_sentences = []
        for s in sentences:
            norm = _normalize_item(s)
            if norm and norm not in seen_items:
                seen_items.add(norm)
                kept_sentences.append(s)

        if kept_sentences:
            if bullet_prefix:
                result_lines.append(f"{bullet_prefix}{' '.join(kept_sentences)}")
            else:
                result_lines.append(" ".join(kept_sentences))

    while result_lines and result_lines[-1] == "":
        result_lines.pop()
    while result_lines and result_lines[0] == "":
        result_lines.pop(0)

    return "\n".join(result_lines)


# Test cases
text1 = """
Специализация:
- Стоматолог-ортопед, гнатолог
- Стоматолог-ортопед, гнатолог

Арсенал и оснащение:
- Дентальный микроскоп Carl Zeiss OPMI PROergo
- Ультразвуковой аппарат Woodpecker
- Дентальный микроскоп Carl Zeiss OPMI PROergo.

Клинические протоколы:
- Спиртовой протокол адгезии OptiBond FL
- Ирригация гипохлоритом натрия 3% с активацией
- Спиртовой протокол адгезии OptiBond FL;

Кейсы:
- Зуб 3.6: деструкция костной ткани у апекса дистального корня.
- Дентальный микроскоп Carl Zeiss OPMI PROergo
- Зуб 3.6: деструкция костной ткани у апекса дистального корня
"""

deduped = deduplicate_clinical_summary(text1)
print("=== DEDUPED OUTPUT ===")
print(deduped)
print("======================")

# Assertions
assert "Специализация:" in deduped
assert "Арсенал и оснащение:" in deduped
assert "Клинические протоколы:" in deduped
assert "Кейсы:" in deduped
assert deduped.count("Carl Zeiss") == 1, f"Expected 1 Carl Zeiss, got {deduped.count('Carl Zeiss')}"
assert deduped.count("OptiBond FL") == 1, f"Expected 1 OptiBond FL, got {deduped.count('OptiBond FL')}"
assert deduped.count("Зуб 3.6") == 1, f"Expected 1 Зуб 3.6, got {deduped.count('Зуб 3.6')}"
assert deduped.count("Стоматолог-ортопед") == 1, f"Expected 1 Стоматолог-ортопед, got {deduped.count('Стоматолог-ортопед')}"

# Test inline headers
text2 = """
Специализация: Стоматолог-ортопед. Стоматолог-ортопед.
Арсенал и оснащение: Микроскоп Zeiss. Микроскоп Zeiss.
Клинические протоколы: OptiBond FL.
Кейсы: Кейс 3.6. Микроскоп Zeiss.
"""
deduped2 = deduplicate_clinical_summary(text2)
print("\n=== DEDUPED 2 ===")
print(deduped2)
print("=================")
assert deduped2.count("Стоматолог-ортопед") == 1
assert deduped2.count("Zeiss") == 1
assert deduped2.count("OptiBond FL") == 1
# Test edge cases
assert deduplicate_clinical_summary("") == ""
assert deduplicate_clinical_summary("   \n\n  ") == ""

no_headers = "Врач терапевт. Врач терапевт."
assert deduplicate_clinical_summary(no_headers) == "Врач терапевт."

teeth_cases = """
Кейсы:
- Зуб 3.6: деструкция кости.
- Зуб 4.6: деструкция кости.
"""
deduped_teeth = deduplicate_clinical_summary(teeth_cases)
assert "Зуб 3.6" in deduped_teeth
assert "Зуб 4.6" in deduped_teeth

# Test format_users_chunk_context budgeting
async def test_format_budget():
    HEADER = (
        "=== НАКОПЛЕННЫЕ ПРОФИЛИ УЧАСТНИКОВ ОБСУЖДЕНИЯ (ИЗ БЕСЕДЫ) ===\n"
        "[Справочная информация для ассистента: это выжимка из накопленной памяти о врачах-участниках "
        "текущего обсуждения, составленная ИИ по их сообщениям в чате. Учитывай специализацию и "
        "клинический опыт собеседников]:"
    )

    class MockDB:
        @staticmethod
        async def get_users_memory_batch(uids):
            return {
                uid: {
                    "first_name": f"Доктор-{uid}",
                    "username": f"doc_{uid}",
                    "specialty": f"Специализация #{uid}",
                    "group_summary": f"Подробное описание практики доктора #{uid}. " * 5,
                }
                for uid in uids
            }

    async def format_users_chunk_context_impl(user_ids: List[int], max_chars: Optional[int] = 2000) -> str:
        if not user_ids:
            return ""
        unique_ids = list(dict.fromkeys(user_ids))[:20]
        batch = await MockDB.get_users_memory_batch(unique_ids)
        if not batch:
            return ""

        notes = []
        for uid in unique_ids:
            mem = batch.get(uid)
            if not mem:
                continue
            name_parts = []
            if mem.get("first_name"):
                name_parts.append(mem["first_name"])
            if mem.get("username"):
                name_parts.append(f"(@{mem['username']})")
            doc_label = " ".join(name_parts) if name_parts else f"Врач #{uid}"

            spec = mem.get("specialty", "").strip()
            grp_sum = mem.get("group_summary", "").strip() or mem.get("clinical_summary", "").strip()
            profile_text = ""
            if grp_sum:
                profile_text = grp_sum[:300].strip()
                if len(grp_sum) > 300:
                    profile_text += "..."

            desc_parts = []
            if spec:
                desc_parts.append(spec)
            if profile_text and profile_text != spec:
                desc_parts.append(profile_text)

            if desc_parts:
                notes.append(f"• {doc_label}: {'; '.join(desc_parts)}")

        if not notes:
            return ""

        if max_chars is not None and max_chars > 0:
            if len(HEADER) >= max_chars:
                return ""
            selected_notes = []
            for note in notes:
                candidate = f"{HEADER}\n" + "\n".join(selected_notes + [note])
                if len(candidate) > max_chars:
                    break
                selected_notes.append(note)

            if not selected_notes:
                return ""
            return f"{HEADER}\n" + "\n".join(selected_notes)
        else:
            return f"{HEADER}\n" + "\n".join(notes)

    uids = list(range(1, 21))
    res_2000 = await format_users_chunk_context_impl(uids, max_chars=2000)
    assert len(res_2000) <= 2000, f"Exceeded 2000: {len(res_2000)}"
    assert "Доктор-1" in res_2000

    # Header is 286 chars, note is ~260 chars, so 600 allows exactly 1 doctor
    res_600 = await format_users_chunk_context_impl(uids, max_chars=600)
    assert len(res_600) <= 600, f"Exceeded 600: {len(res_600)}"
    assert "Доктор-1" in res_600
    assert "Доктор-2" not in res_600

    # 400 is less than header + 1 note (286 + 260 = 546), so returns empty
    res_400 = await format_users_chunk_context_impl(uids, max_chars=400)
    assert res_400 == "", f"Expected empty for max_chars=400, got: {res_400}"

    res_100 = await format_users_chunk_context_impl(uids, max_chars=100)
    assert res_100 == "", f"Expected empty for max_chars=100, got: {res_100}"

    print("Budgeting test PASSED!")

import asyncio
asyncio.run(test_format_budget())

print("\nALL LOCAL ASSERTIONS PASSED!")
