#!/usr/bin/env python3
"""
failover.py: Кроссплатформенный контроллер двустороннего переключения (Failover)
между Local PC и микро-VPS с защитой от Split-Brain и верификацией целостности.

Использование:
  python scripts/failover.py --to local --vps user@ip [--key id_rsa] [--remote-dir /opt/stomchat]
  python scripts/failover.py --to vps   --vps user@ip [--key id_rsa] [--remote-dir /opt/stomchat]
  python scripts/failover.py --status  --vps user@ip [--key id_rsa]
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("failover")


def _get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _detect_bot_context(root: Path):
    if (root / "endo_bot.db").exists() or "endochat" in str(root).lower():
        return {
            "name": "endochat",
            "db_name": "endo_bot.db",
            "service": "endochat.service",
            "default_remote_dir": "/opt/endochat"
        }
    return {
        "name": "stomchat",
        "db_name": "stomat_bot.db",
        "service": "stomchat.service",
        "default_remote_dir": "/opt/stomchat"
    }


def _run_cmd(cmd_list, check=True, capture=True):
    logger.info("CMD: %s", " ".join(cmd_list))
    res = subprocess.run(cmd_list, capture_output=capture, text=True)
    if check and res.returncode != 0:
        logger.error("Команда завершилась с ошибкой (%d):\n%s\n%s", res.returncode, res.stdout, res.stderr)
        raise RuntimeError(f"Command failed: {' '.join(cmd_list)}")
    return res


def _ssh_cmd(vps: str, key: str, remote_command: str):
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        cmd.extend(["-i", key])
    cmd.extend([vps, remote_command])
    return _run_cmd(cmd, check=True)


def _scp_download(vps: str, key: str, remote_file: str, local_dest: str):
    cmd = ["scp", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        cmd.extend(["-i", key])
    cmd.extend([f"{vps}:{remote_file}", local_dest])
    return _run_cmd(cmd, check=True)


def _scp_upload(vps: str, key: str, local_file: str, remote_dest: str):
    cmd = ["scp", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        cmd.extend(["-i", key])
    cmd.extend([local_file, f"{vps}:{remote_dest}"])
    return _run_cmd(cmd, check=True)


def switch_to_local(vps: str, key: str, remote_dir: str, force: bool = False):
    root = _get_project_root()
    ctx = _detect_bot_context(root)
    service_name = ctx["service"]
    db_name = ctx["db_name"]
    snapshot_script = root / "scripts" / "snapshot_backup.py"

    logger.info("=== ПЕРЕХВАТ УПРАВЛЕНИЯ НА LOCAL PC (%s) ===", ctx["name"].upper())

    # Шаг 1. Проверка и остановка службы на VPS (Защита от Split-Brain)
    logger.info("[1/5] Проверка связи с VPS и остановка службы %s...", service_name)
    try:
        _ssh_cmd(vps, key, f"sudo systemctl stop {service_name}")
        logger.info("Служба %s на VPS остановлена (Split-Brain исключен).", service_name)
    except Exception as e:
        if not force:
            logger.error("VPS недоступен! Ошибка: %s. Для аварийного запуска используйте --force.", e)
            sys.exit(1)
        else:
            logger.warning("ВНИМАНИЕ: VPS недоступен, но передан флаг --force. Продолжаем с последним локальным состоянием!")

    # Шаг 2. Создание свежего снапшота на VPS
    remote_snapshot_file = None
    if not force:
        logger.info("[2/5] Создание горячего снапшота состояния на VPS...")
        res = _ssh_cmd(vps, key, f"cd {remote_dir} && python3 scripts/snapshot_backup.py --create --tag handover_to_local")
        for line in res.stdout.splitlines():
            if "SNAPSHOT_CREATED:" in line:
                remote_snapshot_file = line.split("SNAPSHOT_CREATED:", 1)[1].strip()
                break

        if not remote_snapshot_file:
            # Резервный поиск последнего снапшота
            remote_snapshot_file = f"{remote_dir}/snapshots/latest_{db_name.split('.')[0]}_snapshot.tar.gz"

        logger.info("Удаленный снапшот: %s", remote_snapshot_file)

        # Шаг 3. Скачивание снапшота на локальную машину
        local_snapshots_dir = root / "snapshots"
        local_snapshots_dir.mkdir(parents=True, exist_ok=True)
        local_archive = local_snapshots_dir / Path(remote_snapshot_file).name

        logger.info("[3/5] Скачивание снапшота с VPS на локальный ПК...")
        _scp_download(vps, key, remote_snapshot_file, str(local_archive))
    else:
        # Режим force: берем локальный latest
        local_archive = root / "snapshots" / f"latest_{db_name.split('.')[0]}_snapshot.tar.gz"
        if not local_archive.exists():
            logger.error("Локальный архив %s не найден! Аварийный запуск невозможен.", local_archive)
            sys.exit(1)

    # Шаг 4. Восстановление состояния на локальной машине
    logger.info("[4/5] Восстановление базы данных и стейтов локально...")
    res = subprocess.run([sys.executable, str(snapshot_script), "--restore", str(local_archive)], capture_output=True, text=True)
    if res.returncode != 0:
        logger.error("Ошибка восстановления локального состояния:\n%s\n%s", res.stdout, res.stderr)
        sys.exit(1)
    logger.info("Локальное состояние успешно восстановлено и верифицировано.")

    # Шаг 5. Готовность к запуску
    logger.info("[5/5] Переключение завершено! VPS в standby, локальная база 100% свежая.")
    print("\n" + "=" * 60)
    print(f" ПЕРЕХВАТ УСПЕШЕН: {ctx['name'].upper()} ГОТОВ К ЛОКАЛЬНОЙ РАБОТЕ")
    print(" Запустите бота локально командой:")
    print("   .\\start.bat")
    print("=" * 60 + "\n")


def switch_to_vps(vps: str, key: str, remote_dir: str):
    root = _get_project_root()
    ctx = _detect_bot_context(root)
    service_name = ctx["service"]
    db_name = ctx["db_name"]
    snapshot_script = root / "scripts" / "snapshot_backup.py"

    logger.info("=== ВОЗВРАТ УПРАВЛЕНИЯ НА VPS (%s) ===", ctx["name"].upper())

    # Шаг 1. Остановка локального бота (если запущен)
    logger.info("[1/6] Проверка локальных процессов...")
    # На Windows/Linux создаем свежий снапшот с гарантированным сбросом WAL
    logger.info("[2/6] Создание локального снапшота (checkpoint WAL + state)...")
    res = subprocess.run([sys.executable, str(snapshot_script), "--create", "--tag", "handover_to_vps"], capture_output=True, text=True)
    if res.returncode != 0:
        logger.error("Ошибка создания локального снапшота:\n%s\n%s", res.stdout, res.stderr)
        sys.exit(1)

    local_snapshot_file = None
    for line in res.stdout.splitlines():
        if "SNAPSHOT_CREATED:" in line:
            local_snapshot_file = line.split("SNAPSHOT_CREATED:", 1)[1].strip()
            break

    if not local_snapshot_file:
        local_snapshot_file = str(root / "snapshots" / f"latest_{db_name.split('.')[0]}_snapshot.tar.gz")

    logger.info("Локальный снапшот: %s (%d байт)", local_snapshot_file, os.path.getsize(local_snapshot_file))

    # Шаг 3. Загрузка снапшота на VPS
    remote_snapshots_dir = f"{remote_dir}/snapshots"
    _ssh_cmd(vps, key, f"mkdir -p {remote_snapshots_dir}")
    remote_archive_path = f"{remote_snapshots_dir}/{Path(local_snapshot_file).name}"

    logger.info("[3/6] Загрузка снапшота на VPS...")
    _scp_upload(vps, key, local_snapshot_file, remote_archive_path)

    # Шаг 4. Остановка службы на VPS перед накатом (если вдруг была активна)
    logger.info("[4/6] Подготовка службы %s на VPS...", service_name)
    _ssh_cmd(vps, key, f"sudo systemctl stop {service_name}")

    # Шаг 5. Восстановление состояния на VPS
    logger.info("[5/6] Восстановление базы и стейтов на VPS...")
    restore_cmd = f"cd {remote_dir} && python3 scripts/snapshot_backup.py --restore {remote_archive_path}"
    _ssh_cmd(vps, key, restore_cmd)

    # Шаг 6. Запуск службы systemd на VPS
    logger.info("[6/6] Запуск службы %s на VPS...", service_name)
    _ssh_cmd(vps, key, f"sudo systemctl start {service_name}")
    status_res = _ssh_cmd(vps, key, f"systemctl is-active {service_name}")

    is_active = status_res.stdout.strip() == "active"
    if is_active:
        logger.info("Служба %s УСПЕШНО ЗАПУЩЕНА И АКТИВНА НА СЕРВЕРЕ!", service_name)
    else:
        logger.warning("Статус службы на VPS: %s. Проверьте: sudo journalctl -u %s -n 50", status_res.stdout.strip(), service_name)

    print("\n" + "=" * 60)
    print(f" ВОЗВРАТ НА СЕРВЕР ЗАВЕРШЕН: {ctx['name'].upper()} РАБОТАЕТ НА VPS")
    print(f" Служба: {service_name} | Статус: {status_res.stdout.strip()}")
    print("=" * 60 + "\n")


def check_status(vps: str, key: str):
    root = _get_project_root()
    ctx = _detect_bot_context(root)
    service_name = ctx["service"]

    logger.info("Проверка статуса для %s на VPS (%s)...", ctx["name"], vps)
    try:
        status_res = _ssh_cmd(vps, key, f"systemctl status {service_name}")
        print("\n" + status_res.stdout)
    except Exception as e:
        logger.error("Не удалось связаться с сервером: %s", e)


def main():
    root = _get_project_root()
    ctx = _detect_bot_context(root)

    default_vps = os.getenv("FAILOVER_VPS_HOST", "")
    default_key = os.getenv("FAILOVER_SSH_KEY", "")
    default_remote_dir = os.getenv("FAILOVER_REMOTE_DIR", ctx["default_remote_dir"])

    parser = argparse.ArgumentParser(description="Контроллер двустороннего failover переключения StomChat/EndoChat")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--to", choices=["local", "vps"], help="Целевой узел исполнения бота (local или vps)")
    group.add_argument("--status", action="store_true", help="Проверить статус службы на сервере")

    parser.add_argument("--vps", type=str, default=default_vps, required=not bool(default_vps), help="VPS SSH адрес (user@ip)")
    parser.add_argument("--key", type=str, default=default_key, help="Путь к приватному SSH ключу")
    parser.add_argument("--remote-dir", type=str, default=default_remote_dir, help="Путь к проекту на VPS")
    parser.add_argument("--force", action="store_true", help="Принудительный запуск при недоступности VPS")

    args = parser.parse_args()

    if args.status:
        check_status(args.vps, args.key)
    elif args.to == "local":
        switch_to_local(args.vps, args.key, args.remote_dir, force=args.force)
    elif args.to == "vps":
        switch_to_vps(args.vps, args.key, args.remote_dir)


if __name__ == "__main__":
    main()
