"""Generic watchdog settings.

NFS servers, export paths, mount points, filesystem types, and mount
options are not configured here. Those values are read from /etc/fstab
on every monitoring cycle.
"""

import os


def _setting_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    return max(minimum, value)


# Path of the fstab file to read. This is not a list of mounts.
FSTAB_PATH = os.getenv("NFS_MONITOR_FSTAB", "/etc/fstab")

CHECK_INTERVAL = _setting_int("NFS_MONITOR_CHECK_INTERVAL", 30, 5)
MOUNT_TIMEOUT = _setting_int("NFS_MONITOR_MOUNT_TIMEOUT", 30, 5)
FINDMNT_TIMEOUT = _setting_int("NFS_MONITOR_FINDMNT_TIMEOUT", 10, 1)

# After a failed mount of one entry: 30s, 60s, 120s, then cap at 300s.
RETRY_INITIAL_DELAY = _setting_int("NFS_MONITOR_RETRY_INITIAL_DELAY", 30, 1)
RETRY_MAX_DELAY = max(
    RETRY_INITIAL_DELAY,
    _setting_int("NFS_MONITOR_RETRY_MAX_DELAY", 300, 1),
)
RETRY_BACKOFF_FACTOR = _setting_int("NFS_MONITOR_RETRY_BACKOFF_FACTOR", 2, 2)

LOG_LEVEL = os.getenv("NFS_MONITOR_LOG_LEVEL", "INFO").upper()
