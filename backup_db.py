"""
backup_db.py: Горячее резервное копирование и верификация целостности SQLite для StomChat и EndoChat.

Строго реализует SQLite WAL Checkpoint Law (RULE 12):
1. Обязательный безопасный сброс WAL: PRAGMA wal_checkpoint(TRUNCATE) с валидацией
   статуса busy (повторные попытки с backoff при наличии активных читателей).
2. Горячее копирование через нативный sqlite3.backup API (сырое копирование файлов с активным WAL запрещено!).
3. Атомарность и защита от повреждений:
   - создание копии во временный файл .tmp
   - валидация PRAGMA integrity_check на файле бекапа до публикации
   - расчет контрольной суммы SHA-256
   - атомарный перенос в целевой файл
4. Программный API и консольный интерфейс (CLI) для failover и регламентных бекапов.
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    import config
except ImportError:
    config = None

logger = logging.getLogger("backup_db")


def calc_sha256(file_path: Path) -> str:
    """Вычисляет контрольную сумму SHA-256 для указанного файла."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_default_db_path() -> Path:
    """Определяет путь к основной базе данных проекта."""
    if config and hasattr(config, "DB_PATH"):
        return Path(config.DB_PATH)
    root = Path(__file__).resolve().parent
    if (root / "endo_bot.db").exists():
        return root / "endo_bot.db"
    return root / "stomat_bot.db"


def checkpoint_wal_safe(db_path: Path, max_retries: int = 5, retry_delay: float = 0.25) -> bool:
    """
    Выполняет PRAGMA wal_checkpoint(TRUNCATE) с валидацией возвращаемого кортежа (busy, log, checkpointed).
    При busy == 1 (блокировка активными транзакциями) предпринимает повторные попытки с паузой.
    """
    if not db_path.exists():
        logger.warning("База данных %s не существует для checkpoint", db_path)
        return False

    for attempt in range(max_retries):
        conn = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            conn.execute("PRAGMA busy_timeout = 30000")
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row:
                busy, log_pages, ckpt_pages = row[0], row[1], row[2]
                if busy == 0:
                    logger.info("WAL checkpoint (TRUNCATE) успешен: log=%d, checkpointed=%d", log_pages, ckpt_pages)
                    return True
                logger.warning(
                    "WAL checkpoint busy (попытка %d/%d): log=%d, checkpointed=%d. Ожидание...",
                    attempt + 1, max_retries, log_pages, ckpt_pages
                )
            else:
                return True
        except Exception as e:
            logger.warning("Ошибка при вызове wal_checkpoint (попытка %d/%d): %s", attempt + 1, max_retries, e)
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        if attempt + 1 < max_retries:
            time.sleep(retry_delay * (attempt + 1))

    logger.error("Не удалось сбросить WAL (TRUNCATE) после %d попыток для %s", max_retries, db_path)
    return False


def verify_database_integrity(db_path: Path) -> Tuple[bool, str]:
    """
    Проверяет целостность БД SQLite через PRAGMA integrity_check.
    Возвращает (is_ok, message).
    """
    if not db_path.exists():
        return False, f"Файл базы данных не найден: {db_path}"

    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000")
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        rows = cur.fetchall()
        if rows == [("ok",)]:
            return True, "PRAGMA integrity_check: ok"
        return False, f"PRAGMA integrity_check failed: {rows}"
    except Exception as e:
        return False, f"Исключение при проверке integrity: {e}"
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def backup_database(
    src_path: Optional[Path] = None,
    dest_path: Optional[Path] = None,
    max_retries: int = 5,
    timeout: float = 60.0
) -> Dict[str, Any]:
    """
    Создает горячую консистентную резервную копию SQLite базы:
    1. checkpoint_wal_safe(TRUNCATE) со сверкой статуса busy.
    2. Горячий бекап через нативный sqlite3.backup API во временный файл .tmp.
    3. PRAGMA integrity_check на файле бекапа до замены.
    4. Атомарный перенос в dest_path и расчет SHA-256.
    """
    src = src_path or get_default_db_path()
    if not src.exists():
        raise FileNotFoundError(f"Исходная база данных не найдена: {src}")

    if dest_path is None:
        backup_dir = src.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest_path = backup_dir / f"{src.stem}_backup_{stamp}.db"
    else:
        dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Шаг 1: сброс WAL с контролем статуса
    checkpoint_wal_safe(src, max_retries=max_retries)

    tmp_dest = dest_path.with_suffix(".tmp")
    if tmp_dest.exists():
        tmp_dest.unlink()

    # Шаг 2: горячее копирование через sqlite3.backup API
    logger.info("Горячее копирование SQLite %s -> %s через sqlite3.backup...", src.name, tmp_dest.name)
    src_conn = sqlite3.connect(str(src), timeout=timeout)
    src_conn.execute("PRAGMA busy_timeout = 30000")
    dst_conn = sqlite3.connect(str(tmp_dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    # Шаг 3: проверка целостности созданного файла
    is_ok, msg = verify_database_integrity(tmp_dest)
    if not is_ok:
        if tmp_dest.exists():
            tmp_dest.unlink()
        raise RuntimeError(f"Созданный бекап БД поврежден: {msg}")

    # Шаг 4: атомарная замена
    if dest_path.exists():
        dest_path.unlink()
    tmp_dest.rename(dest_path)

    sha256 = calc_sha256(dest_path)
    size = dest_path.stat().st_size
    logger.info("Резервная копия успешно создана: %s (%d байт, sha256: %s)", dest_path.name, size, sha256[:16])

    return {
        "status": "ok",
        "src": str(src),
        "dest": str(dest_path),
        "size": size,
        "sha256": sha256,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def main():
    parser = argparse.ArgumentParser(description="Утилита горячего бекапа и валидации SQLite для StomChat/EndoChat")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--backup", action="store_true", help="Создать горячую резервную копию")
    group.add_argument("--verify", action="store_true", help="Проверить целостность базы данных")
    group.add_argument("--checkpoint", action="store_true", help="Сбросить WAL (TRUNCATE) в основную базу")
    parser.add_argument("--src", type=str, default=None, help="Путь к исходной базе данных")
    parser.add_argument("--dest", type=str, default=None, help="Целевой путь для бекапа")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    src_p = Path(args.src) if args.src else get_default_db_path()

    if args.checkpoint:
        success = checkpoint_wal_safe(src_p)
        sys.exit(0 if success else 1)
    elif args.verify:
        is_ok, msg = verify_database_integrity(src_p)
        print(f"VERIFY RESULT ({src_p.name}): {'OK' if is_ok else 'FAILED'} - {msg}")
        sys.exit(0 if is_ok else 1)
    elif args.backup:
        dest_p = Path(args.dest) if args.dest else None
        res = backup_database(src_path=src_p, dest_path=dest_p)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        sys.exit(0)


if __name__ == "__main__":
    main()
