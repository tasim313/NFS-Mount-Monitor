#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/nfs-mount-monitor"
SERVICE_NAME="nfs-mount-monitor.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: run this installer as root, for example: sudo ./install.sh"
    exit 1
fi

if [[ ! -d /run/systemd/system ]]; then
    echo "ERROR: systemd is not running on this system."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: Python 3 is required."
    exit 1
fi

echo "Checking NFS client utilities..."
if ! command -v mount.nfs >/dev/null 2>&1 || ! command -v findmnt >/dev/null 2>&1; then
    echo "Installing nfs-common..."
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-common
fi

install -d -m 0755 "${APP_DIR}"

install -m 0755 nfs_mount_monitor.py "${APP_DIR}/nfs_mount_monitor.py"
install -m 0644 config.py "${APP_DIR}/config.py"
install -m 0644 requirements.txt "${APP_DIR}/requirements.txt"

# Read NFS entries from /etc/fstab and create only missing local mount
# directories. This never edits /etc/fstab.
python3 - <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))
from nfs_mount_monitor import parse_fstab

for item in parse_fstab("/etc/fstab"):
    mount_point = Path(item.mount_point)
    if mount_point.exists():
        continue
    mount_point.mkdir(parents=True, exist_ok=True)
    print(f"Created mount directory: {mount_point}")
PY

install -m 0644 "systemd/${SERVICE_NAME}" "${SERVICE_PATH}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo
echo "Installation complete."
echo
systemctl --no-pager --full status "${SERVICE_NAME}" || true
echo
echo "Useful commands:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
echo "  findmnt -t nfs,nfs4"
