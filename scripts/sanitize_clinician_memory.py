#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sanitize_clinician_memory.py: Санитария и очистка долгосрочной памяти врачей и профилей.

Строго реализует SQLite WAL Checkpoint Law (RULE 12):
1. Обязательный безопасный сброс WAL: PRAGMA wal_checkpoint(TRUNCATE).
2. Горячий бэкап базы данных перед модификацией с валидацией PRAGMA integrity_check.
3. Полная санитария user_memories и user_profiles:
   - Вычищает висячие «. Специализация:» и заикания ({X}. Специализация: {X}) из profile_portrait.
   - Преобразует specialty='неизвестно', 'стоматолог', 'нет данных' в пустую строку "" либо
     восстанавливает узкую специализацию из подтвержденных клинических фактов/досье.
   - Прогоняет facts_json через clean_clinician_facts(), удаляя мета-факты про ботов, чаты,
     голосования, опросы, троллинг и отрицания.
   - Очищает clinical_summary и group_summary от шаблонных фраз
     «Недостаточно сообщений в общей группе для анализа клинического профиля...».
4. 100% паритет и совместимость между StomChat и EndoChat.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import backup_db
import user_memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sanitize_clinician_memory")

_TEMPLATE_SUMMARY_RE = re.compile(
    r'(?i)[•\-*]?\s*недостаточно\s+сообщений\s+в\s+общей\s+группе\s+для\s+анализа\s+клинического\s+профиля[^\n.!?]*[.!?]?',
    re.UNICODE
)

_TEMPLATE_FORMING_RE = re.compile(
    r'(?i)[•\-*]?\s*клинический\s+профиль\s+доктора\s+формируется[^\n.!?]*[.!?]?',
    re.UNICODE
)

_INVALID_SPECIALTY_STRINGS = {
    "неизвестно", "стоматолог", "нет данных", "не определена", "не указана",
    "врач-стоматолог", "доктор", "стоматология", "неизвестна", "клиницист"
}

_GENERIC_SPECIALTY_STRINGS = {
    "стоматолог", "врач-стоматолог", "врач", "стоматолог общей практики",
    "доктор", "клиницист", "практикующий стоматолог", "стоматология",
    "неизвестно", "нет данных", "не определена", "не указана", ""
}


def clean_summary_text(summary: Optional[str]) -> str:
    """Очищает summary от шаблонных мета-текстов бота."""
    if not summary:
        return ""
    text = summary.strip()
    text = _TEMPLATE_SUMMARY_RE.sub("", text).strip()
    text = _TEMPLATE_FORMING_RE.sub("", text).strip()

    # Проверяем, остался ли содержательный текст помимо пустых заголовков
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    content_lines = []
    for l in lines:
        if re.match(r'^(?:#+\s*|\*+\s*|[•\-*]\s*|\d+[\.\)]\s*)?(?:специализация|арсенал(?:(?:\s+и\s+)?(?:оснащение|оборудование))?|клинические\s+протоколы|протоколы|кейсы|клинический\s+профиль)\s*:?\*?\s*$', l, re.IGNORECASE):
            continue
        content_lines.append(l)

    if not content_lines:
        return ""

    return user_memory.deduplicate_clinical_summary(text)


def recover_clinician_specialty(
    current_spec: str,
    clin_summary: str,
    grp_summary: str,
    facts: list
) -> str:
    """
    Преобразует невалидные специальности ('неизвестно', 'стоматолог', 'нет данных') в пустую строку "",
    либо восстанавливает строго валидную узкую специализацию из подтвержденного клинического досье/фактов.
    Если текущая специальность уже была пустой или валидной — оставляет её без необоснованных изменений.
    """
    cur = (current_spec or "").strip()
    cur_lower = cur.lower()

    # Если специальность уже валидна или изначально пустая — не трогаем
    if cur_lower not in _INVALID_SPECIALTY_STRINGS:
        return cur

    # Попытка восстановить строго узкую специализацию для невалидных записей ('неизвестно', 'стоматолог', 'нет данных')
    # 1. Поиск в clinical_summary явного раздела специализации
    if clin_summary:
        m = re.search(r'(?i)(?:#+\s*|\*+\s*)?специализация\s*:\s*([^\n.;]+)', clin_summary)
        if m:
            cand = re.sub(r'[*_#]+', '', m.group(1)).strip()
            cand_l = cand.lower()
            if (
                user_memory.is_valid_specialty_candidate(cand)
                and cand_l not in _INVALID_SPECIALTY_STRINGS
                and cand_l not in _GENERIC_SPECIALTY_STRINGS
                and "(" not in cand
                and any(r in cand_l for r in user_memory._SPECIFIC_CLINICAL_ROOTS)
            ):
                return cand

    # 2. Эвристический вывод из подтвержденного текста group_summary
    if grp_summary:
        first_line = grp_summary.strip().splitlines()[0].lower()
        if "терапевтического" in first_line and "эндодонт" in first_line:
            return "Стоматолог-терапевт / эндодонтист"
        elif "эндодонтическ" in first_line:
            return "Стоматолог-эндодонтист"
        elif "ортопедическ" in first_line or "ортопед" in first_line:
            return "Стоматолог-ортопед"
        elif "хирург" in first_line or "имплант" in first_line:
            return "Стоматолог-хирург"
        elif "детск" in first_line:
            return "Детский стоматолог"

    # Если узкая специализация не подтверждена — строго пустая строка ""
    return ""


def detect_db_path(custom_path: Optional[str] = None) -> Path:
    if custom_path:
        p = Path(custom_path)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        return p
    return backup_db.get_default_db_path()


def run_sanitization(db_path: Path, apply_changes: bool = False, verbose: bool = False) -> Dict[str, int]:
    logger.info("=" * 70)
    logger.info("  REMEDIAL CLINICIAN MEMORY & FACT SANITIZATION")
    logger.info("  Target DB: %s", db_path)
    logger.info("  Mode:      %s", "APPLY (WRITING CHANGES)" if apply_changes else "DRY-RUN (AUDIT ONLY)")
    logger.info("=" * 70)

    if not db_path.exists():
        logger.error("Database file not found: %s", db_path)
        sys.exit(1)

    # 1. SQLite WAL Checkpoint Law: безопасный сброс WAL
    logger.info("[Pass 1] Выполнение PRAGMA wal_checkpoint(TRUNCATE)...")
    if not backup_db.checkpoint_wal_safe(db_path, max_retries=5):
        logger.warning("WAL checkpoint вернул busy статус, продолжаем с осторожностью.")

    # 2. Создание резервной копии перед любыми изменениями (если apply)
    if apply_changes:
        bak_path = db_path.with_name(f"{db_path.name}.bak")
        logger.info("[Pass 2] Создание резервной копии базы данных %s -> %s...", db_path.name, bak_path.name)
        backup_db.backup_database(db_path, bak_path, timeout=60.0)
        logger.info("[Pass 2] Резервная копия создана и верифицирована (размер: %d байт)", bak_path.stat().st_size)

    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.execute("PRAGMA busy_timeout = 30000")
    cur = conn.cursor()

    stats = {
        "total_memories": 0,
        "memories_updated": 0,
        "specialties_cleared": 0,
        "specialties_recovered": 0,
        "facts_cleaned": 0,
        "summaries_cleaned": 0,
        "total_profiles": 0,
        "profiles_updated": 0,
        "portraits_hanging_fixed": 0,
        "portraits_stutter_fixed": 0,
    }

    # ==================== ОЧИСТКА USER_MEMORIES ====================
    logger.info("\n[Pass 3] Анализ и санитария таблицы user_memories...")
    cur.execute("SELECT user_id, specialty, clinical_summary, group_summary, facts_json FROM user_memories")
    memory_rows = cur.fetchall()
    stats["total_memories"] = len(memory_rows)

    memories_to_update = []

    for uid, spec, clin, grp, facts_raw in memory_rows:
        orig_spec = spec or ""
        orig_clin = clin or ""
        orig_grp = grp or ""
        orig_facts_raw = facts_raw or "[]"

        # 1. Facts Sanitization
        facts_list = []
        try:
            if orig_facts_raw:
                facts_list = json.loads(orig_facts_raw)
                if not isinstance(facts_list, list):
                    facts_list = []
        except Exception:
            facts_list = []

        cleaned_facts = user_memory.clean_clinician_facts(facts_list)
        new_facts_raw = json.dumps(cleaned_facts, ensure_ascii=False) if cleaned_facts else "[]"

        # 2. Specialty Resolution
        new_spec = recover_clinician_specialty(orig_spec, orig_clin, orig_grp, cleaned_facts)

        # 3. Summaries Sanitization
        new_clin = clean_summary_text(orig_clin)
        new_grp = clean_summary_text(orig_grp)

        # Проверяем, изменились ли данные
        spec_changed = (new_spec != orig_spec)
        facts_changed = (new_facts_raw != orig_facts_raw)
        clin_changed = (new_clin != orig_clin)
        grp_changed = (new_grp != orig_grp)

        if spec_changed or facts_changed or clin_changed or grp_changed:
            stats["memories_updated"] += 1
            if spec_changed:
                if orig_spec.lower() in ("неизвестно", "стоматолог", "нет данных", "не определена", "не указана") and not new_spec:
                    stats["specialties_cleared"] += 1
                elif new_spec and new_spec != orig_spec:
                    stats["specialties_recovered"] += 1
            if facts_changed:
                stats["facts_cleaned"] += (len(facts_list) - len(cleaned_facts))
            if clin_changed or grp_changed:
                stats["summaries_cleaned"] += 1

            if verbose or stats["memories_updated"] <= 10:
                changes = []
                if spec_changed:
                    changes.append(f"spec: '{orig_spec}' -> '{new_spec}'")
                if facts_changed:
                    changes.append(f"facts: {len(facts_list)} -> {len(cleaned_facts)}")
                if clin_changed:
                    changes.append(f"clin_len: {len(orig_clin)} -> {len(new_clin)}")
                if grp_changed:
                    changes.append(f"grp_len: {len(orig_grp)} -> {len(new_grp)}")
                logger.info("  User %-12d: %s", uid, ", ".join(changes))

            memories_to_update.append((new_spec, new_clin, new_grp, new_facts_raw, uid))

    # ==================== ОЧИСТКА USER_PROFILES ====================
    logger.info("\n[Pass 4] Анализ и санитария таблицы user_profiles...")
    cur.execute("SELECT p.user_id, p.profile_portrait, m.specialty, m.clinical_summary FROM user_profiles p LEFT JOIN user_memories m ON p.user_id = m.user_id")
    profile_rows = cur.fetchall()
    stats["total_profiles"] = len(profile_rows)

    profiles_to_update = []

    for uid, port, spec, clin in profile_rows:
        orig_port = port or ""
        cleaned_port = user_memory.clean_profile_portrait(orig_port, specialty=(spec or ""), summary=(clin or ""))

        if cleaned_port != orig_port.strip():
            stats["profiles_updated"] += 1
            if "специализация:" in orig_port.lower():
                if orig_port.strip().endswith("Специализация:"):
                    stats["portraits_hanging_fixed"] += 1
                else:
                    stats["portraits_stutter_fixed"] += 1

            if verbose or stats["profiles_updated"] <= 10:
                logger.info("  Profile %-12d BEFORE: '%s'", uid, orig_port)
                logger.info("  Profile %-12d AFTER:  '%s'", uid, cleaned_port)

            profiles_to_update.append((cleaned_port, uid))

    # ==================== ПРИМЕНЕНИЕ ИЗМЕНЕНИЙ ====================
    if apply_changes:
        logger.info("\n[Pass 5] Применение транзакции обновления в БД...")
        with conn:
            if memories_to_update:
                conn.executemany(
                    """
                    UPDATE user_memories 
                    SET specialty = ?, clinical_summary = ?, group_summary = ?, facts_json = ? 
                    WHERE user_id = ?
                    """,
                    memories_to_update
                )
            if profiles_to_update:
                conn.executemany(
                    """
                    UPDATE user_profiles 
                    SET profile_portrait = ? 
                    WHERE user_id = ?
                    """,
                    profiles_to_update
                )
        logger.info("[Pass 5] Успешно зафиксировано: %d memories, %d profiles", len(memories_to_update), len(profiles_to_update))

        # Финальный сброс WAL и валидация целостности
        logger.info("[Pass 6] Финальный WAL checkpoint и PRAGMA integrity_check...")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        if integrity != "ok":
            logger.error("FATAL: PRAGMA integrity_check failed after sanitization: %s", integrity)
            conn.close()
            sys.exit(1)
        logger.info("[Pass 6] PRAGMA integrity_check: OK (БД абсолютно целостна)")
    else:
        logger.info("\n[Pass 5] DRY-RUN завершен. Изменения НЕ записаны в БД (используйте --apply).")

    conn.close()

    # Сводный отчет
    logger.info("\n" + "=" * 70)
    logger.info("  ИТОГОВЫЙ ОТЧЕТ САНИТАРИИ:")
    logger.info("  • Всего записей памяти (user_memories):   %d", stats["total_memories"])
    logger.info("  • Памятей обновлено:                     %d", stats["memories_updated"])
    logger.info("    - Невалидных специальностей сброшено:  %d", stats["specialties_cleared"])
    logger.info("    - Узких специальностей восстановлено:  %d", stats["specialties_recovered"])
    logger.info("    - Мета-фактов / дубликатов удалено:    %d", stats["facts_cleaned"])
    logger.info("    - Шаблонных текстов очищено:           %d", stats["summaries_cleaned"])
    logger.info("  • Всего профилей (user_profiles):        %d", stats["total_profiles"])
    logger.info("  • Портретов обновлено:                   %d", stats["profiles_updated"])
    logger.info("    - Висячих «. Специализация:» исправлено:%d", stats["portraits_hanging_fixed"])
    logger.info("    - Заиканий специализации исправлено:   %d", stats["portraits_stutter_fixed"])
    logger.info("=" * 70)

    return stats


def main():
    parser = argparse.ArgumentParser(description="Санитария памяти врачей и профилей StomChat/EndoChat")
    parser.add_argument("--apply", action="store_true", help="Применить изменения в базу данных (по умолчанию dry-run)")
    parser.add_argument("--db", type=str, default="", help="Путь к файлу базы данных SQLite")
    parser.add_argument("--verbose", "-v", action="store_true", help="Подробный вывод всех измененных записей")
    args = parser.parse_args()

    db_path = detect_db_path(args.db if args.db else None)
    run_sanitization(db_path, apply_changes=args.apply, verbose=args.verbose)


if __name__ == "__main__":
    main()
