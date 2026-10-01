#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="nfs-mount-monitor.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
APP_DIR="/opt/nfs-mount-monitor"

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: run this uninstaller as root, for example: sudo ./uninstall.sh"
    exit 1
fi

systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

rm -f "${SERVICE_PATH}"
systemctl daemon-reload
systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true

rm -rf "${APP_DIR}"

echo "NFS mount monitor removed."
echo "/etc/fstab was not modified."
echo "NFS filesystems were not unmounted."
echo "NFS data was not deleted."
