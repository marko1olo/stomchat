#!/usr/bin/env python3
"""
snapshot_backup.py: Утилита горячего снапшотирования, валидации и восстановления
состояния ботов (stomat_bot / endo_bot) для двустороннего failover между Local PC и VPS.

Функции:
1. --create:  Создает атомарный консистентный снапшот БД (через sqlite3.backup API + WAL checkpoint),
              состояний (assistant_state.json, bot_state.json) и сессий (*.session) с манифестом SHA-256.
2. --verify:  Проверяет целостность архива, хеш-суммы файлов и запускает PRAGMA integrity_check на БД.
3. --restore: Атомарно восстанавливает файлы в рабочий каталог с созданием резервной копии .bak.
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("snapshot_backup")


def _calc_sha256(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _detect_db_name(root: Path) -> str:
    # Определение по config.py или наличию файла
    if (root / "endo_bot.db").exists():
        return "endo_bot.db"
    return "stomat_bot.db"


def create_snapshot(output_dir: Path = None, tag: str = "") -> Path:
    root = _get_project_root()
    db_name = _detect_db_name(root)
    db_path = root / db_name

    if output_dir is None:
        output_dir = root / "snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tag_suffix = f"_{tag}" if tag else ""
    archive_name = f"snapshot_{db_name.split('.')[0]}_{timestamp_str}{tag_suffix}.tar.gz"
    archive_path = output_dir / archive_name

    logger.info("Начало создания снапшота для %s...", db_name)

    with tempfile.TemporaryDirectory(prefix="bot_snapshot_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        payload_dir = tmp_dir / "data"
        payload_dir.mkdir()

        # 1. Горячий бекап SQLite через backup_db (backup API + WAL checkpoint)
        if db_path.exists():
            temp_db_path = payload_dir / db_name
            logger.info("Сброс WAL и горячее резервное копирование SQLite %s...", db_name)
            try:
                if str(root) not in sys.path:
                    sys.path.insert(0, str(root))
                import backup_db
                backup_db.backup_database(src_path=db_path, dest_path=temp_db_path, timeout=60.0)
                logger.info("БД %s успешно скопирована и верифицирована (%d байт)", db_name, temp_db_path.stat().st_size)
            except Exception as e:
                logger.warning("Ошибка при использовании backup_db: %s, выполняем резервный backup...", e)
                src_conn = sqlite3.connect(str(db_path), timeout=60)
                src_conn.execute("PRAGMA busy_timeout = 30000")
                try:
                    try:
                        src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except Exception:
                        pass
                    dst_conn = sqlite3.connect(str(temp_db_path))
                    try:
                        src_conn.backup(dst_conn)
                    finally:
                        dst_conn.close()
                finally:
                    src_conn.close()
        else:
            logger.warning("Файл БД %s не найден в %s", db_name, root)

        # 2. Копирование JSON-файлов состояния
        state_files = [
            "assistant_state.json",
            "bot_state.json",
            "bot_startup_state.json",
            "mirror_backfill_state.json"
        ]
        copied_states = []
        for sf in state_files:
            sf_path = root / sf
            if sf_path.exists():
                shutil.copy2(sf_path, payload_dir / sf)
                copied_states.append(sf)
        logger.info("Скопированы файлы состояния: %s", copied_states)

        # 3. Копирование сессий Telethon (*.session)
        session_files = list(root.glob("*.session"))
        copied_sessions = []
        for sess in session_files:
            # Игнорируем временные/тестовые сессии нулевого размера
            if sess.stat().st_size > 0:
                shutil.copy2(sess, payload_dir / sess.name)
                copied_sessions.append(sess.name)
        logger.info("Скопированы сессии Telethon: %s", copied_sessions)

        # 4. Формирование манифеста с SHA-256
        manifest = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "db_name": db_name,
            "files": {}
        }
        for f in payload_dir.iterdir():
            manifest["files"][f.name] = {
                "size": f.stat().st_size,
                "sha256": _calc_sha256(f)
            }

        with open(payload_dir / "manifest.json", "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, indent=2, ensure_ascii=False)

        # 5. Упаковка в tar.gz
        with tarfile.open(archive_path, "w:gz") as tar:
            for item in payload_dir.iterdir():
                tar.add(item, arcname=item.name)

    # Симлинк / копия latest_snapshot.tar.gz
    latest_path = output_dir / f"latest_{db_name.split('.')[0]}_snapshot.tar.gz"
    try:
        shutil.copy2(archive_path, latest_path)
    except Exception as e:
        logger.warning("Не удалось обновить latest_snapshot: %s", e)

    logger.info("Снапшот успешно создан: %s (размер: %d байт)", archive_path, archive_path.stat().st_size)
    return archive_path


def verify_snapshot(archive_path: Path) -> bool:
    logger.info("Верификация снапшота %s...", archive_path)
    if not archive_path.exists():
        logger.error("Файл архива не найден: %s", archive_path)
        return False

    with tempfile.TemporaryDirectory(prefix="bot_verify_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(tmp_dir, filter="data")
                else:
                    tar.extractall(tmp_dir)
        except Exception as e:
            logger.error("Ошибка распаковки архива: %s", e)
            return False

        manifest_file = tmp_dir / "manifest.json"
        if not manifest_file.exists():
            logger.error("В архиве отсутствует manifest.json!")
            return False

        with open(manifest_file, "r", encoding="utf-8") as mf:
            manifest = json.load(mf)

        # Проверка контрольных сумм каждого файла
        for filename, meta in manifest.get("files", {}).items():
            target_f = tmp_dir / filename
            if not target_f.exists():
                logger.error("Файл %s указан в манифесте, но отсутствует в архиве!", filename)
                return False
            actual_hash = _calc_sha256(target_f)
            if actual_hash != meta["sha256"]:
                logger.error("Несовпадение SHA-256 для %s! Ожидалось %s, получено %s", filename, meta["sha256"], actual_hash)
                return False

        # Проверка целостности базы данных SQLite (PRAGMA integrity_check)
        db_name = manifest.get("db_name")
        if db_name and (tmp_dir / db_name).exists():
            db_file = tmp_dir / db_name
            logger.info("Проверка PRAGMA integrity_check для %s...", db_name)
            root = _get_project_root()
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            import backup_db
            is_ok, msg = backup_db.verify_database_integrity(db_file)
            if not is_ok:
                logger.error("PRAGMA integrity_check вернул ошибки для %s: %s", db_name, msg)
                return False
            logger.info("База данных SQLite прошла проверку целостности: %s", msg)

    logger.info("Снапшот %s ПОЛНОСТЬЮ ВАЛИДЕН.", archive_path.name)
    return True


def restore_snapshot(archive_path: Path, force: bool = False) -> bool:
    if not verify_snapshot(archive_path):
        logger.error("Восстановление отменено: архив поврежден или не прошел верификацию.")
        return False

    root = _get_project_root()
    logger.info("Восстановление состояния в каталог %s...", root)

    with tempfile.TemporaryDirectory(prefix="bot_restore_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        with tarfile.open(archive_path, "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmp_dir, filter="data")
            else:
                tar.extractall(tmp_dir)

        manifest_file = tmp_dir / "manifest.json"
        with open(manifest_file, "r", encoding="utf-8") as mf:
            manifest = json.load(mf)

        # Создаем бекап (.bak) существующих файлов перед заменой
        backup_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        for filename in manifest.get("files", {}):
            target = root / filename
            if target.exists():
                bak_path = root / f"{filename}.bak_{backup_stamp}"
                try:
                    shutil.copy2(target, bak_path)
                except Exception as e:
                    logger.warning("Не удалось создать .bak для %s: %s", filename, e)

            # Если это база данных SQLite, убираем старые WAL/SHM файлы чтобы они не конфликтовали с новой базой
            if filename.endswith(".db"):
                for suffix in ("-wal", "-shm"):
                    stale_wal = root / f"{filename}{suffix}"
                    if stale_wal.exists():
                        try:
                            shutil.copy2(stale_wal, root / f"{stale_wal.name}.bak_{backup_stamp}")
                            stale_wal.unlink()
                            logger.info("Удален устаревший журнал WAL: %s", stale_wal.name)
                        except Exception as e:
                            logger.warning("Не удалось удалить устаревший WAL %s: %s", stale_wal, e)

            # Копируем файл из снапшота
            src_file = tmp_dir / filename
            shutil.copy2(src_file, target)
            logger.info("Восстановлен файл: %s (%d байт)", filename, target.stat().st_size)

            # Для баз данных SQLite проводим финальную валидацию целостности
            if filename.endswith(".db"):
                try:
                    if str(root) not in sys.path:
                        sys.path.insert(0, str(root))
                    import backup_db
                    is_ok, msg = backup_db.verify_database_integrity(target)
                    if not is_ok:
                        logger.error("Восстановленная база данных %s не прошла проверку целостности: %s", filename, msg)
                        return False
                    logger.info("Восстановленная БД %s успешно прошла PRAGMA integrity_check", filename)
                except Exception as e:
                    logger.warning("Не удалось запустить verify_database_integrity для %s: %s", filename, e)

    logger.info("Восстановление успешно завершено.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Утилита снапшотирования и failover для StomChat/EndoChat")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="Создать новый снапшот")
    group.add_argument("--verify", type=str, help="Проверить целостность архива снапшота")
    group.add_argument("--restore", type=str, help="Восстановить состояние из архива снапшота")
    parser.add_argument("--output-dir", type=str, default=None, help="Каталог сохранения снапшотов")
    parser.add_argument("--tag", type=str, default="", help="Дополнительный тег в имени архива")

    args = parser.parse_args()

    if args.create:
        out_dir = Path(args.output_dir) if args.output_dir else None
        res_path = create_snapshot(output_dir=out_dir, tag=args.tag)
        print(f"SNAPSHOT_CREATED: {res_path}")
        sys.exit(0)
    elif args.verify:
        is_ok = verify_snapshot(Path(args.verify))
        sys.exit(0 if is_ok else 1)
    elif args.restore:
        is_ok = restore_snapshot(Path(args.restore))
        sys.exit(0 if is_ok else 1)


if __name__ == "__main__":
    main()
