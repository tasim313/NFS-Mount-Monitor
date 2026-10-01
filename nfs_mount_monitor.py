#!/usr/bin/env python3
"""
NFS mount watchdog.

/etc/fstab is the only source of NFS servers, export paths, local mount
points, filesystem types, mount options, and the number of mounts. This
process re-reads that file every cycle, checks each nfs/nfs4 entry
independently, and mounts only a missing mount point with:

    mount <mount-point>

A failure of one entry cannot stop the others. Failed entries are
retried with exponential backoff. The watchdog never edits /etc/fstab
and never unmounts anything.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

try:
    from config import (
        CHECK_INTERVAL,
        FINDMNT_TIMEOUT,
        FSTAB_PATH,
        LOG_LEVEL,
        MOUNT_TIMEOUT,
        RETRY_BACKOFF_FACTOR,
        RETRY_INITIAL_DELAY,
        RETRY_MAX_DELAY,
    )
except ImportError:  # pragma: no cover - used only when config.py is absent
    CHECK_INTERVAL = 30
    FINDMNT_TIMEOUT = 10
    FSTAB_PATH = "/etc/fstab"
    LOG_LEVEL = "INFO"
    MOUNT_TIMEOUT = 30
    RETRY_BACKOFF_FACTOR = 2
    RETRY_INITIAL_DELAY = 30
    RETRY_MAX_DELAY = 300


LOGGER = logging.getLogger("nfs-mount-monitor")
STOP_REQUESTED = False

NFS_TYPES = frozenset({"nfs", "nfs4"})
_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|credential|credentials)(\s*=\s*)(\S+)"
)
_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class NFSMount:
    """One nfs or nfs4 record discovered from fstab."""

    source: str
    mount_point: str
    fs_type: str
    options: str
    line_number: int


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class MountObservation:
    present: bool = False
    source: str = ""
    fs_type: str = ""
    target: str = ""
    error: str = ""

    def matches(self, item: NFSMount) -> bool:
        if not self.present or self.error:
            return False
        if not is_nfs_type(self.fs_type):
            return False
        if normalize_mount_point(self.target) != normalize_mount_point(item.mount_point):
            return False
        return sources_match(item.source, self.source)

    def occupied_by_other(self, item: NFSMount) -> bool:
        return self.present and not self.error and not self.matches(item)


@dataclass
class RetryState:
    failures: int = 0
    next_attempt_at: float = 0.0


@dataclass
class CycleStats:
    total: int = 0
    mounted: int = 0
    missing: int = 0
    deferred: int = 0

    def record(self, outcome: str) -> None:
        self.total += 1
        if outcome in {"mounted", "recovered"}:
            self.mounted += 1
            return
        self.missing += 1
        if outcome == "deferred":
            self.deferred += 1


class CommandRunner(Protocol):
    def run(self, args: list[str], timeout: int) -> CommandResult:
        """Run a command without a shell and return its output."""


class SubprocessRunner:
    """Run host commands. A timeout kills the whole process group."""

    def run(self, args: list[str], timeout: int) -> CommandResult:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(
                args,
                timeout,
                output=stdout,
                stderr=stderr,
            ) from exc
        return CommandResult(
            returncode=proc.returncode if proc.returncode is not None else 1,
            stdout=stdout or "",
            stderr=stderr or "",
        )


def configure_logging() -> None:
    level = getattr(logging, str(LOG_LEVEL).upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def signal_handler(signum: int, _frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    LOGGER.info("Received %s; stopping monitor cleanly", signal.Signals(signum).name)


def sleep_interruptible(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while not STOP_REQUESTED:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1\2***", text or "")


def format_field(value: object) -> str:
    text = redact(str(value)).replace("\n", " ").strip()
    if len(text) > 300:
        text = text[:300] + "..."
    if text == "" or any(char.isspace() or char in '"\\' for char in text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def kv(**fields: object) -> str:
    return " ".join(f"{key}={format_field(value)}" for key, value in fields.items())


def strip_comments(line: str) -> str:
    """Drop an unescaped '#' comment. fstab encodes a literal '#' as \\043."""
    result: list[str] = []
    escaped = False
    for char in line:
        if char == "#" and not escaped:
            break
        result.append(char)
        escaped = char == "\\" and not escaped
    return "".join(result)


def unescape_fstab_field(field: str) -> str:
    """Decode libmount octal escapes such as \\040 (space) and \\011 (tab)."""

    def replace(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 8))

    return _OCTAL_ESCAPE_RE.sub(replace, field)


def split_fstab_fields(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    for char in line:
        if char in " \t":
            if current:
                fields.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        fields.append("".join(current))
    return fields


def is_nfs_type(fs_type: str) -> bool:
    return fs_type.strip().casefold() in NFS_TYPES


def option_names(options: str) -> set[str]:
    names: set[str] = set()
    for part in options.split(","):
        name = part.strip().split("=", 1)[0].strip().casefold()
        if name:
            names.add(name)
    return names


def is_bind_options(options: str) -> bool:
    names = option_names(options)
    return "bind" in names or "rbind" in names


def normalize_mount_point(path: str) -> str:
    if path == "/":
        return "/"
    stripped = path.rstrip("/")
    return stripped or "/"


def split_nfs_source(source: str) -> Optional[tuple[str, str]]:
    """Split host:path. Bracketed IPv6 hosts are supported; no IP is assumed."""
    text = source.strip()
    if text.startswith("["):
        end = text.find("]")
        if end > 1 and text[end + 1 : end + 2] == ":":
            return text[: end + 1], text[end + 2 :]
        return None
    colon = text.find(":")
    if colon <= 0:
        return None
    return text[:colon], text[colon + 1 :]


def normalize_remote_path(path: str) -> str:
    if path != "/" and path.endswith("/"):
        return path.rstrip("/")
    return path


def sources_match(expected: str, actual: str) -> bool:
    left = split_nfs_source(expected)
    right = split_nfs_source(actual)
    if left and right:
        return (
            left[0].casefold() == right[0].casefold()
            and normalize_remote_path(left[1]) == normalize_remote_path(right[1])
        )
    return expected.strip() == actual.strip()


def parse_fstab_line(raw_line: str, line_number: int) -> Optional[NFSMount]:
    line = strip_comments(raw_line).strip()
    if not line:
        return None

    fields = split_fstab_fields(line)
    if len(fields) < 4:
        LOGGER.warning(
            "Ignoring malformed fstab line %s: missing mount options",
            line_number,
        )
        return None

    try:
        source = unescape_fstab_field(fields[0])
        mount_point = unescape_fstab_field(fields[1])
        fs_type = unescape_fstab_field(fields[2])
        options = unescape_fstab_field(fields[3]) if len(fields) >= 4 else ""
    except ValueError:
        LOGGER.warning("Ignoring malformed fstab line %s: invalid escape", line_number)
        return None

    if not is_nfs_type(fs_type):
        return None

    if is_bind_options(options):
        LOGGER.debug(
            "Ignoring bind mount on fstab line %s mount_point=%s",
            line_number,
            mount_point,
        )
        return None

    if (
        not source
        or not mount_point.startswith("/")
        or "\x00" in source
        or "\x00" in mount_point
        or "\n" in source
        or "\n" in mount_point
    ):
        LOGGER.warning(
            "Ignoring malformed fstab line %s: NFS source or mount point is unusable",
            line_number,
        )
        return None

    return NFSMount(
        source=source,
        mount_point=mount_point,
        fs_type=fs_type,
        options=options,
        line_number=line_number,
    )


def parse_fstab(path: str = FSTAB_PATH) -> list[NFSMount]:
    """Return the nfs/nfs4 entries in `path`, in parent-before-child order.

    Duplicate mount points are attempted once. The first entry is kept
    because `mount <mount-point>` uses the first matching fstab record.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        LOGGER.exception("Unable to decode %s as UTF-8", path)
        raise
    except OSError:
        LOGGER.exception("Unable to read %s", path)
        raise

    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")

    unique: dict[str, NFSMount] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        try:
            item = parse_fstab_line(raw_line, line_number)
        except Exception:
            LOGGER.exception("Ignoring malformed fstab line %s", line_number)
            continue
        if item is None:
            continue

        key = normalize_mount_point(item.mount_point)
        existing = unique.get(key)
        if existing is not None:
            LOGGER.warning(
                "Duplicate NFS mount point %s; keeping line %s and ignoring line %s",
                item.mount_point,
                existing.line_number,
                item.line_number,
            )
            continue
        unique[key] = item

    return sorted(
        unique.values(),
        key=lambda item: (
            normalize_mount_point(item.mount_point).count("/"),
            normalize_mount_point(item.mount_point),
        ),
    )


def parse_findmnt_pairs(stdout: str) -> dict[str, str]:
    line = stdout.strip().splitlines()
    if not line:
        return {}
    try:
        tokens = shlex.split(line[0])
    except ValueError:
        LOGGER.warning("Unable to parse findmnt output")
        return {}
    parsed: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        parsed[key] = value
    return parsed


class NFSWatchdog:
    def __init__(
        self,
        fstab_path: str = FSTAB_PATH,
        runner: Optional[CommandRunner] = None,
        clock: Callable[[], float] = time.monotonic,
        mount_timeout: int = MOUNT_TIMEOUT,
        findmnt_timeout: int = FINDMNT_TIMEOUT,
        retry_initial: int = RETRY_INITIAL_DELAY,
        retry_max: int = RETRY_MAX_DELAY,
        retry_factor: int = RETRY_BACKOFF_FACTOR,
    ) -> None:
        self.fstab_path = fstab_path
        self.runner = runner or SubprocessRunner()
        self.clock = clock
        self.mount_timeout = mount_timeout
        self.findmnt_timeout = findmnt_timeout
        self.retry_initial = retry_initial
        self.retry_max = max(retry_initial, retry_max)
        self.retry_factor = max(2, retry_factor)
        self._retry: dict[str, RetryState] = {}
        self._entry_signatures: dict[str, tuple[str, str, str]] = {}
        self._signature: Optional[tuple[tuple[str, str, str], ...]] = None

    def retry_delay(self, failures: int) -> float:
        delay = float(self.retry_initial)
        steps = max(0, failures - 1)
        for _ in range(steps):
            delay *= self.retry_factor
            if delay >= self.retry_max:
                return float(self.retry_max)
        return min(delay, float(self.retry_max))

    def observe(self, mount_point: str) -> MountObservation:
        # --mountpoint requires the path itself to be a mount.
        # --target would also match a parent filesystem such as /.
        try:
            result = self.runner.run(
                [
                    "findmnt",
                    "-n",
                    "-P",
                    "-o",
                    "SOURCE,FSTYPE,TARGET",
                    "--mountpoint",
                    mount_point,
                ],
                self.findmnt_timeout,
            )
        except subprocess.TimeoutExpired:
            return MountObservation(error=f"findmnt timed out after {self.findmnt_timeout}s")
        except OSError as exc:
            return MountObservation(error=str(exc))

        if result.returncode == 1:
            return MountObservation(present=False)
        if result.returncode != 0:
            detail = redact((result.stderr or result.stdout).strip())
            return MountObservation(
                error=detail or f"findmnt exited {result.returncode}",
            )

        parsed = parse_findmnt_pairs(result.stdout)
        if not parsed.get("TARGET"):
            return MountObservation(error="findmnt returned no mount target")
        return MountObservation(
            present=True,
            source=parsed.get("SOURCE", ""),
            fs_type=parsed.get("FSTYPE", ""),
            target=parsed.get("TARGET", ""),
        )

    def _schedule_retry(self, state: RetryState) -> float:
        state.failures += 1
        delay = self.retry_delay(state.failures)
        state.next_attempt_at = self.clock() + delay
        return delay

    def _reset_retry(self, mount_point: str) -> None:
        state = self._retry.get(normalize_mount_point(mount_point))
        if state is None:
            return
        state.failures = 0
        state.next_attempt_at = 0.0

    def _ensure_mountpoint_exists(self, item: NFSMount) -> tuple[bool, str]:
        path = Path(item.mount_point)
        if path.exists():
            if path.is_dir():
                return True, ""
            return False, "mount point exists and is not a directory"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"cannot create mount directory: {exc}"
        LOGGER.info(
            "Created missing mount directory %s",
            kv(mount_point=item.mount_point),
        )
        return True, ""

    def _mount(self, item: NFSMount) -> tuple[bool, str]:
        """Mount one fstab mount point. The NFS source stays in /etc/fstab."""
        ready, detail = self._ensure_mountpoint_exists(item)
        if not ready:
            return False, detail
        try:
            result = self.runner.run(
                ["mount", item.mount_point],
                self.mount_timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"mount timed out after {self.mount_timeout}s"
        except OSError as exc:
            return False, f"unable to execute mount: {exc}"

        detail = redact((result.stderr or result.stdout).strip())
        if result.returncode != 0:
            return False, detail or f"mount exited {result.returncode}"
        return True, detail

    def _defer_if_waiting(self, item: NFSMount, state: RetryState) -> bool:
        now = self.clock()
        if now >= state.next_attempt_at:
            return False
        LOGGER.debug(
            "NFS mount deferred %s",
            kv(
                source=item.source,
                mount_point=item.mount_point,
                retry_in=f"{state.next_attempt_at - now:.0f}s",
                failures=state.failures,
            ),
        )
        return True

    def _log_failure(self, item: NFSMount, detail: str) -> None:
        state = self._retry.setdefault(
            normalize_mount_point(item.mount_point),
            RetryState(),
        )
        delay = self._schedule_retry(state)
        LOGGER.error(
            "NFS mount result=failure %s",
            kv(
                source=item.source,
                mount_point=item.mount_point,
                retry_in=f"{delay:.0f}s",
                detail=detail,
            ),
        )

    def process_mount(self, item: NFSMount) -> str:
        key = normalize_mount_point(item.mount_point)
        state = self._retry.setdefault(key, RetryState())
        observation = self.observe(item.mount_point)

        if not observation.error and observation.matches(item):
            if state.failures:
                LOGGER.info(
                    "NFS mount result=success %s",
                    kv(
                        source=item.source,
                        mount_point=item.mount_point,
                        detail="mount is present",
                    ),
                )
            self._reset_retry(item.mount_point)
            return "mounted"

        # Re-check a mount that became healthy on every cycle. Retry the
        # expensive mount command only after this entry's backoff has elapsed.
        if self._defer_if_waiting(item, state):
            return "deferred"

        if observation.error:
            self._log_failure(item, observation.error)
            return "failed"

        if observation.occupied_by_other(item):
            self._log_failure(
                item,
                "mount point is already mounted "
                f"from {observation.source or 'unknown'} "
                f"fstype={observation.fs_type or 'unknown'}",
            )
            return "failed"

        LOGGER.warning(
            "NFS mount missing %s",
            kv(
                source=item.source,
                mount_point=item.mount_point,
                fs_type=item.fs_type,
            ),
        )
        command_ok, detail = self._mount(item)
        verified = self.observe(item.mount_point)
        if not verified.error and verified.matches(item):
            LOGGER.info(
                "NFS mount result=success %s",
                kv(source=item.source, mount_point=item.mount_point),
            )
            self._reset_retry(item.mount_point)
            return "recovered"

        if not command_ok and detail:
            failure_detail = detail
        elif verified.error:
            failure_detail = verified.error
        elif verified.present:
            failure_detail = (
                "verification failed: "
                f"found source={verified.source or 'unknown'} "
                f"fstype={verified.fs_type or 'unknown'}"
            )
        else:
            failure_detail = detail or "verification failed"
        self._log_failure(item, failure_detail)
        return "failed"

    def _log_reload(self, mounts: list[NFSMount]) -> None:
        signature = tuple(
            (item.mount_point, item.source, item.fs_type) for item in mounts
        )
        if signature == self._signature:
            return
        self._signature = signature
        LOGGER.info(
            "NFS fstab reloaded %s",
            kv(path=self.fstab_path, count=len(mounts)),
        )
        for item in mounts:
            LOGGER.info(
                "NFS entry %s",
                kv(
                    source=item.source,
                    mount_point=item.mount_point,
                    fs_type=item.fs_type,
                ),
            )

    def run_cycle(self) -> CycleStats:
        stats = CycleStats()
        try:
            mounts = parse_fstab(self.fstab_path)
        except Exception:
            LOGGER.exception("Unable to reload %s; skipping cycle", self.fstab_path)
            return stats

        self._log_reload(mounts)
        seen: set[str] = set()

        for item in mounts:
            if STOP_REQUESTED:
                break
            key = normalize_mount_point(item.mount_point)
            seen.add(key)
            entry_signature = (item.source, item.fs_type, item.options)
            previous_signature = self._entry_signatures.get(key)
            if previous_signature is not None and previous_signature != entry_signature:
                # A changed fstab definition is a new desired mount. Do not
                # carry the previous definition's failure delay forward.
                self._reset_retry(item.mount_point)
            self._entry_signatures[key] = entry_signature
            try:
                outcome = self.process_mount(item)
            except Exception:
                LOGGER.exception(
                    "NFS mount result=failure %s",
                    kv(
                        source=item.source,
                        mount_point=item.mount_point,
                        detail="unexpected error",
                    ),
                )
                outcome = "failed"
            stats.record(outcome)

        for key in list(self._retry):
            if key not in seen:
                del self._retry[key]
        for key in list(self._entry_signatures):
            if key not in seen:
                del self._entry_signatures[key]

        LOGGER.info(
            "NFS status %s",
            kv(
                total=stats.total,
                mounted=stats.mounted,
                missing=stats.missing,
                deferred=stats.deferred,
            ),
        )
        return stats


def main() -> int:
    configure_logging()

    if os.geteuid() != 0:
        LOGGER.error("This service must run as root.")
        return 1

    for tool in ("findmnt", "mount"):
        if shutil.which(tool) is None:
            LOGGER.error("Required executable not found: %s", tool)
            return 1

    watchdog = NFSWatchdog()
    LOGGER.info(
        "NFS mount monitor started %s",
        kv(
            interval=f"{CHECK_INTERVAL}s",
            mount_timeout=f"{MOUNT_TIMEOUT}s",
            retry_initial=f"{RETRY_INITIAL_DELAY}s",
            retry_max=f"{RETRY_MAX_DELAY}s",
            fstab=FSTAB_PATH,
        ),
    )

    while not STOP_REQUESTED:
        started = time.monotonic()
        try:
            watchdog.run_cycle()
        except Exception:
            LOGGER.exception("Unexpected monitoring-cycle failure")

        if STOP_REQUESTED:
            break
        elapsed = time.monotonic() - started
        sleep_interruptible(max(0.0, float(CHECK_INTERVAL) - elapsed))

    LOGGER.info("NFS mount monitor stopped")
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    raise SystemExit(main())
