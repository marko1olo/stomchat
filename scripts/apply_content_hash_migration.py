#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_content_hash_migration.py: Миграция stomat_wiki.db с вычислением content_hash.

Строго реализует SQLite WAL Checkpoint Law (RULE 12):
1. Выполняет PRAGMA wal_checkpoint(TRUNCATE) до и после миграции.
2. Создает резервную копию базы данных через backup_db перед изменениями схемы.
3. Добавляет колонку content_hash, вычисляет SHA-256 для существующих строк.
4. Дедуплицирует записи и создает UNIQUE INDEX idx_content_hash.
5. Финальная верификация PRAGMA integrity_check.
"""

import hashlib
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import backup_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("apply_content_hash_migration")


def compute_hash(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").lower().strip())
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def main():
    db_path = _PROJECT_ROOT / "stomat_wiki.db"
    if not db_path.exists():
        logger.warning("База данных stomat_wiki.db не найдена в %s, пропуск миграции", _PROJECT_ROOT)
        return

    logger.info("=== Запуск миграции content_hash для %s ===", db_path.name)

    # 1. Сброс WAL до начала работ
    backup_db.checkpoint_wal_safe(db_path, max_retries=5)

    # 2. Создание резервной копии перед миграцией
    try:
        bak_res = backup_db.backup_database(src_path=db_path)
        logger.info("Создана резервная копия wiki перед миграцией: %s", bak_res.get("dest"))
    except Exception as e:
        logger.error("Не удалось создать резервную копию перед миграцией: %s. Отмена.", e)
        sys.exit(1)

    # 3. Применение миграции
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    cursor = conn.cursor()

    try:
        cursor.execute("PRAGMA table_info(distilled_facts);")
        cols = [row[1] for row in cursor.fetchall()]

        if "content_hash" not in cols:
            logger.info("Добавление колонки 'content_hash' в distilled_facts...")
            cursor.execute("ALTER TABLE distilled_facts ADD COLUMN content_hash TEXT;")

        cursor.execute("SELECT id, content FROM distilled_facts WHERE content_hash IS NULL OR content_hash = '';")
        rows = cursor.fetchall()
        logger.info("Вычисление content_hash для %d строк...", len(rows))

        for row_id, content in rows:
            chash = compute_hash(content)
            cursor.execute("UPDATE distilled_facts SET content_hash = ? WHERE id = ?;", (chash, row_id))

        conn.commit()

        # Дедупликация
        cursor.execute("SELECT content_hash, COUNT(*) FROM distilled_facts GROUP BY content_hash HAVING COUNT(*) > 1;")
        dups = cursor.fetchall()

        if dups:
            logger.info("Найдено %d групп дубликатов, сохраняем наименьший ID...", len(dups))
            deleted_dups = 0
            for chash, count in dups:
                cursor.execute("SELECT id FROM distilled_facts WHERE content_hash = ? ORDER BY id ASC;", (chash,))
                ids = [r[0] for r in cursor.fetchall()]
                remove_ids = ids[1:]
                placeholders = ",".join("?" for _ in remove_ids)
                cursor.execute(f"DELETE FROM distilled_facts WHERE id IN ({placeholders});", remove_ids)
                deleted_dups += len(remove_ids)
            logger.info("Удалено %d дубликатов.", deleted_dups)
            conn.commit()

        # Создание UNIQUE INDEX
        logger.info("Создание UNIQUE INDEX idx_content_hash...")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_content_hash ON distilled_facts(content_hash);")
        conn.commit()
    finally:
        conn.close()

    # 4. Финальный сброс WAL и валидация
    backup_db.checkpoint_wal_safe(db_path, max_retries=5)
    is_ok, msg = backup_db.verify_database_integrity(db_path)
    if not is_ok:
        logger.error("Миграция нарушила целостность stomat_wiki.db: %s", msg)
        sys.exit(1)

    logger.info("OK: Миграция content_hash успешно завершена и верифицирована!")


if __name__ == "__main__":
    main()
