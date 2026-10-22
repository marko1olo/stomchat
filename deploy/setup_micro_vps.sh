#!/usr/bin/env bash
# ==============================================================================
# setup_micro_vps.sh: Первоначальная настройка микро-VPS (1 vCPU, 1 GB RAM)
# для StomChat (@docendobot) на Debian 12 / Ubuntu 24.04
# ==============================================================================
set -euo pipefail

BOT_NAME="stomchat"
SERVICE_NAME="stomchat.service"
INSTALL_DIR="/opt/stomchat"
BOT_USER="botrunner"
SWAP_SIZE="2G"

echo "=========================================================="
echo " [1/7] Настройка Swapfile ($SWAP_SIZE) и тюнинг памяти"
echo "=========================================================="
if [ ! -f /swapfile ]; then
    echo "Создание /swapfile на $SWAP_SIZE..."
    fallocate -l "$SWAP_SIZE" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Настройка swappiness: использовать swap только при дефиците ОЗУ
sysctl vm.swappiness=10
sysctl vm.vfs_cache_pressure=50
cat <<EOF > /etc/sysctl.d/99-micro-vps.conf
vm.swappiness = 10
vm.vfs_cache_pressure = 50
EOF

echo "=========================================================="
echo " [2/7] Обновление пакетов и установка системных зависимостей"
echo "=========================================================="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 git curl ufw tar gzip tzdata

# Установка часового пояса Europe/Moscow для корректного расписания дайджестов (REPORT_HOUR=22)
timedatectl set-timezone Europe/Moscow 2>/dev/null || ln -sf /usr/share/zoneinfo/Europe/Moscow /etc/localtime

echo "=========================================================="
echo " [3/7] Создание изолированного пользователя $BOT_USER"
echo "=========================================================="
if ! id "$BOT_USER" &>/dev/null; then
    useradd -r -m -d "$INSTALL_DIR" -s /bin/bash "$BOT_USER"
fi

mkdir -p "$INSTALL_DIR"
chown -R "$BOT_USER:$BOT_USER" "$INSTALL_DIR"

echo "=========================================================="
echo " [4/7] Настройка Python venv в $INSTALL_DIR"
echo "=========================================================="
sudo -u "$BOT_USER" bash <<EOF
set -euo pipefail
cd "$INSTALL_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
fi
EOF

echo "=========================================================="
echo " [5/7] Установка systemd службы"
echo "=========================================================="
if [ -f "$INSTALL_DIR/deploy/$SERVICE_NAME" ]; then
    cp "$INSTALL_DIR/deploy/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
fi

# Настройка прав на .env (если уже скопирован)
if [ -f "$INSTALL_DIR/.env" ]; then
    chmod 600 "$INSTALL_DIR/.env"
    chown "$BOT_USER:$BOT_USER" "$INSTALL_DIR/.env"
fi

echo "=========================================================="
echo " [6/7] Настройка периодического резервного копирования (Cron)"
echo "=========================================================="
CRON_JOB="0 * * * * cd $INSTALL_DIR && ./venv/bin/python3 scripts/snapshot_backup.py --create --tag hourly >> /var/log/stomchat_backup.log 2>&1"
(crontab -u "$BOT_USER" -l 2>/dev/null | grep -v "snapshot_backup.py" || true; echo "$CRON_JOB") | crontab -u "$BOT_USER" -

echo "=========================================================="
echo " [7/7] Настройка файрвола (UFW)"
echo "=========================================================="
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh

# Preserve VPN ports if WireGuard / AmneziaWG are installed
if command -v wg &>/dev/null || [ -f /etc/wireguard/wg0.conf ]; then
    echo "Обнаружен WireGuard: разрешаю UDP 443..."
    ufw allow 443/udp comment 'WireGuard' || true
fi
if command -v awg &>/dev/null || [ -f /etc/amnezia/amneziawg/awg0.conf ]; then
    echo "Обнаружен AmneziaWG: разрешаю UDP 4433..."
    ufw allow 4433/udp comment 'AmneziaWG' || true
fi
ufw --force enable

echo "=========================================================="
echo " НАСТРОЙКА VPS ЗАВЕРШЕНА УСПЕШНО!"
echo " Для запуска службы выполните:"
echo "   sudo systemctl start $SERVICE_NAME"
echo "   sudo systemctl status $SERVICE_NAME"
echo "=========================================================="
