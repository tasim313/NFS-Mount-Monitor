#!/usr/bin/env python3
"""Tests for the dynamic NFS watchdog.

Mutation checks use temporary fstab files. The real /etc/fstab is only
read, then compared to confirm this test did not change it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nfs_mount_monitor as mon

logging.getLogger("nfs-mount-monitor").addHandler(logging.NullHandler())


# Environment-specific names that must not appear in the application.
# They are a denylist, not mount configuration.
BANNED_SNIPPETS = (
    "192.168.1.10",
    "aikhanlab",
    "call_recording",
    "requisition",
    "notice_attachment",
    "NAS_IP",
    "MOUNT_POINTS",
)


class ManualClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRunner:
    def __init__(self) -> None:
        self.mounted: dict[str, tuple[str, str]] = {}
        self.desired: dict[str, tuple[str, str]] = {}
        self.fail: set[str] = set()
        self.timeouts: set[str] = set()
        self.stderr: dict[str, str] = {}
        self.boom_on: set[str] = set()
        self.calls: list[list[str]] = []

    def run(self, args: list[str], timeout: int) -> mon.CommandResult:
        self.calls.append(list(args))
        if not args:
            return mon.CommandResult(2, "", "empty command")
        if args[0] == "findmnt":
            if "--target" in args or "--mountpoint" not in args:
                return mon.CommandResult(2, "", "findmnt must use --mountpoint")
            mount_point = args[args.index("--mountpoint") + 1]
            if mount_point in self.boom_on:
                raise RuntimeError(f"findmnt failed for {mount_point}")
            info = self.mounted.get(mount_point)
            if info is None:
                return mon.CommandResult(1, "", "")
            source, fs_type = info
            stdout = f'SOURCE="{source}" FSTYPE="{fs_type}" TARGET="{mount_point}"\n'
            return mon.CommandResult(0, stdout, "")
        if args[0] == "mount":
            if "-a" in args or args[1:2] == ["-a"]:
                raise AssertionError(args)
            if len(args) != 2:
                return mon.CommandResult(2, "", f"unexpected mount arguments: {args}")
            mount_point = args[1]
            if mount_point in self.timeouts:
                raise subprocess.TimeoutExpired(args, timeout)
            if mount_point in self.fail:
                return mon.CommandResult(
                    32,
                    "",
                    self.stderr.get(mount_point, "mount.nfs: No route to host"),
                )
            spec = self.desired.get(mount_point)
            if spec is None:
                return mon.CommandResult(32, "", "fstab has no matching mount point")
            self.mounted[mount_point] = spec
            return mon.CommandResult(0, "", "")
        return mon.CommandResult(127, "", "unknown command")

    @property
    def mount_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call and call[0] == "mount"]


def write_fstab(path: Path, entries: list[tuple[str, str, str, str]], runner: FakeRunner | None = None) -> None:
    lines = [
        "# synthetic fstab for the watchdog test\n",
        "\n",
        "/dev/mapper/root / ext4 defaults 0 1\n",
        "tmpfs /tmp tmpfs defaults 0 0\n",
    ]
    if runner is not None:
        runner.desired.clear()
    for source, mount_point, fs_type, options in entries:
        lines.append(f"{source} {mount_point} {fs_type} {options} 0 0\n")
        if runner is not None and mon.is_nfs_type(fs_type):
            runner.desired[mount_point] = (source, fs_type)
    path.write_text("".join(lines), encoding="utf-8")


def make_watchdog(
    fstab: Path,
    runner: FakeRunner,
    clock: ManualClock | None = None,
) -> mon.NFSWatchdog:
    return mon.NFSWatchdog(
        fstab_path=str(fstab),
        runner=runner,
        clock=clock or ManualClock(),
        mount_timeout=5,
        findmnt_timeout=2,
        retry_initial=30,
        retry_max=300,
        retry_factor=2,
    )


class ParserTests(unittest.TestCase):
    def test_ignores_comments_blanks_and_non_nfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fstab = Path(tmp) / "fstab"
            mount_point = str(Path(tmp) / "data")
            fstab.write_text(
                "\n".join(
                    [
                        "# comment",
                        "   ",
                        "\t# tab comment",
                        "/dev/disk/by-uuid/abc / ext4 defaults 0 1",
                        "/dev/sdb1 /mnt/xfs xfs defaults 0 0",
                        "/dev/sdc1 /mnt/fat vfat defaults 0 0",
                        "/swap.img none swap sw 0 0",
                        "tmpfs /tmp tmpfs defaults 0 0",
                        "proc /proc proc defaults 0 0",
                        "sysfs /sys sysfs defaults 0 0",
                        "overlay /overlay overlay defaults 0 0",
                        f"/data {Path(tmp) / 'bind'} none bind 0 0",
                        f"nas.example:/export {Path(tmp) / 'nfs-bind'} nfs bind,_netdev 0 0",
                        "nfsd /proc/fs/nfsd nfsd defaults 0 0",
                        f"#nas.example:/hidden {Path(tmp) / 'hidden'} nfs defaults 0 0",
                        f"nas-server.example:/export/data {mount_point} nfs defaults,_netdev 0 0 # note",
                        "only-two fields",
                        "",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertLogs("nfs-mount-monitor", level="WARNING") as captured:
                entries = mon.parse_fstab(fstab)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].source, "nas-server.example:/export/data")
            self.assertEqual(entries[0].mount_point, mount_point)
            self.assertEqual(entries[0].fs_type, "nfs")
            self.assertEqual(entries[0].options, "defaults,_netdev")
            self.assertTrue(any("malformed" in line for line in captured.output))

    def test_multiple_servers_types_and_options_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries_in = [
                ("nas-a.example:/export/a", str(root / "a"), "nfs", "defaults,_netdev,nofail,nfsvers=4"),
                ("nas-b.example:/export/b", str(root / "b"), "nfs4", "defaults,_netdev"),
                (
                    "nas.example.local:/export/c",
                    str(root / "c"),
                    "NFS",
                    "rw,_netdev,nofail,timeo=10,retrans=2",
                ),
                ("[2001:db8::20]:/export/v6", str(root / "v6"), "nfs4", "defaults,_netdev"),
            ]
            fstab = root / "fstab"
            write_fstab(fstab, entries_in)
            parsed = mon.parse_fstab(fstab)
            self.assertEqual(
                [(item.source, item.fs_type, item.options) for item in parsed],
                [
                    ("nas-a.example:/export/a", "nfs", "defaults,_netdev,nofail,nfsvers=4"),
                    ("nas-b.example:/export/b", "nfs4", "defaults,_netdev"),
                    ("nas.example.local:/export/c", "NFS", "rw,_netdev,nofail,timeo=10,retrans=2"),
                    ("[2001:db8::20]:/export/v6", "nfs4", "defaults,_netdev"),
                ],
            )

    def test_tabs_and_escaped_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_point = str(root / "my data")
            source = "nas.example:/export/my data"
            escaped_mount = mount_point.replace(" ", "\\040")
            escaped_source = source.replace(" ", "\\040")
            fstab = root / "fstab"
            fstab.write_text(
                f"{escaped_source}\t{escaped_mount}\tnfs\trw,_netdev\t0\t0\n",
                encoding="utf-8",
            )
            parsed = mon.parse_fstab(fstab)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].source, source)
            self.assertEqual(parsed[0].mount_point, mount_point)
            self.assertEqual(parsed[0].options, "rw,_netdev")

    def test_duplicate_mount_point_is_kept_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_point = str(root / "dup")
            fstab = root / "fstab"
            fstab.write_text(
                "\n".join(
                    [
                        f"nas-a.example:/export/first {mount_point} nfs defaults,_netdev 0 0",
                        f"nas-b.example:/export/second {mount_point}/ nfs rw,_netdev 0 0",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertLogs("nfs-mount-monitor", level="WARNING"):
                parsed = mon.parse_fstab(fstab)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].source, "nas-a.example:/export/first")

    def test_parents_are_ordered_before_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = str(root / "tree")
            child = str(root / "tree" / "child")
            fstab = root / "fstab"
            write_fstab(
                fstab,
                [
                    ("nas.example:/export/child", child, "nfs", "defaults,_netdev"),
                    ("nas.example:/export/parent", parent, "nfs", "defaults,_netdev"),
                ],
            )
            parsed = mon.parse_fstab(fstab)
            points = [item.mount_point for item in parsed]
            self.assertLess(points.index(parent), points.index(child))

    def test_zero_nfs_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fstab = Path(tmp) / "fstab"
            fstab.write_text("/dev/sda1 / ext4 defaults 0 1\n", encoding="utf-8")
            self.assertEqual(mon.parse_fstab(fstab), [])

    def test_source_matching_accepts_hostnames_and_ipv6(self) -> None:
        self.assertTrue(
            mon.sources_match(
                "NAS.Example.Local:/export/data/",
                "nas.example.local:/export/data",
            )
        )
        self.assertTrue(
            mon.sources_match(
                "[2001:DB8::20]:/export/v6",
                "[2001:db8::20]:/export/v6/",
            )
        )
        self.assertFalse(
            mon.sources_match(
                "nas-a.example:/export/a",
                "nas-b.example:/export/a",
            )
        )


class WatchdogTests(unittest.TestCase):
    def test_mount_uses_only_the_fstab_mount_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_point = str(root / "data")
            source = "nas-server.example:/export/data"
            fstab = root / "fstab"
            runner = FakeRunner()
            write_fstab(
                fstab,
                [(source, mount_point, "nfs", "rw,_netdev,timeo=10,retrans=2")],
                runner,
            )
            before = fstab.read_bytes()
            fstab.chmod(0o444)
            stats = make_watchdog(fstab, runner).run_cycle()
            self.assertEqual(stats.mounted, 1)
            self.assertEqual(stats.missing, 0)
            self.assertEqual(runner.mount_calls, [["mount", mount_point]])
            self.assertNotIn(source, runner.mount_calls[0])
            self.assertEqual(fstab.read_bytes(), before)
            for call in runner.calls:
                self.assertNotIn("-a", call)
                self.assertNotIn("--target", call)

    def test_one_failure_does_not_stop_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            points = [str(root / name) for name in ("a", "b", "c")]
            fstab = root / "fstab"
            runner = FakeRunner()
            write_fstab(
                fstab,
                [
                    ("nas-a.example:/export/a", points[0], "nfs", "defaults,_netdev"),
                    ("nas-b.example:/export/b", points[1], "nfs", "defaults,_netdev"),
                    ("nas-c.example.local:/export/c", points[2], "nfs4", "defaults,_netdev"),
                ],
                runner,
            )
            runner.fail.add(points[1])
            stats = make_watchdog(fstab, runner).run_cycle()
            self.assertEqual(stats.total, 3)
            self.assertEqual(stats.mounted, 2)
            self.assertEqual(stats.missing, 1)
            self.assertEqual([call[1] for call in runner.mount_calls], points)
            self.assertIn(points[0], runner.mounted)
            self.assertNotIn(points[1], runner.mounted)
            self.assertIn(points[2], runner.mounted)

    def test_exception_on_one_entry_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            points = [str(root / name) for name in ("a", "b", "c")]
            fstab = root / "fstab"
            runner = FakeRunner()
            write_fstab(
                fstab,
                [
                    (f"nas-{name}.example:/export/{name}", point, "nfs", "defaults,_netdev")
                    for name, point in zip("abc", points)
                ],
                runner,
            )
            runner.boom_on.add(points[1])
            with self.assertLogs("nfs-mount-monitor", level="ERROR"):
                stats = make_watchdog(fstab, runner).run_cycle()
            self.assertEqual(stats.total, 3)
            self.assertIn(points[0], runner.mounted)
            self.assertIn(points[2], runner.mounted)

    def test_backoff_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_point = str(root / "down")
            fstab = root / "fstab"
            runner = FakeRunner()
            clock = ManualClock()
            write_fstab(
                fstab,
                [("nas-down.example:/export/down", mount_point, "nfs", "defaults,_netdev")],
                runner,
            )
            runner.fail.add(mount_point)
            runner.stderr[mount_point] = "mount.nfs: password=hunter2 No route to host"
            watchdog = make_watchdog(fstab, runner, clock)

            with self.assertLogs("nfs-mount-monitor", level="INFO") as captured:
                watchdog.run_cycle()
            logs = "\n".join(captured.output)
            self.assertIn("NFS mount missing", logs)
            self.assertIn("source=nas-down.example:/export/down", logs)
            self.assertIn(f"mount_point={mount_point}", logs)
            self.assertIn("result=failure", logs)
            self.assertIn("retry_in=30s", logs)
            self.assertNotIn("hunter2", logs)
            self.assertIn("password=***", logs)
            self.assertEqual(len(runner.mount_calls), 1)

            clock.advance(29)
            watchdog.run_cycle()
            self.assertEqual(len(runner.mount_calls), 1)
            self.assertEqual(watchdog.run_cycle().deferred, 1)

            clock.advance(1)
            watchdog.run_cycle()
            self.assertEqual(len(runner.mount_calls), 2)

            clock.advance(60)
            watchdog.run_cycle()
            self.assertEqual(len(runner.mount_calls), 3)

            clock.advance(120)
            watchdog.run_cycle()
            self.assertEqual(len(runner.mount_calls), 4)

            clock.advance(240)
            watchdog.run_cycle()
            self.assertEqual(len(runner.mount_calls), 5)

            clock.advance(299)
            watchdog.run_cycle()
            self.assertEqual(len(runner.mount_calls), 5)
            clock.advance(1)
            watchdog.run_cycle()
            self.assertEqual(len(runner.mount_calls), 6)
            state = watchdog._retry[mon.normalize_mount_point(mount_point)]
            self.assertEqual(state.failures, 6)
            self.assertEqual(watchdog.retry_delay(state.failures), 300)

            runner.fail.clear()
            clock.advance(300)
            with self.assertLogs("nfs-mount-monitor", level="INFO") as recovered:
                stats = watchdog.run_cycle()
            self.assertEqual(stats.mounted, 1)
            self.assertIn("result=success", "\n".join(recovered.output))
            self.assertEqual(
                watchdog._retry[mon.normalize_mount_point(mount_point)].failures,
                0,
            )

            del runner.mounted[mount_point]
            watchdog.run_cycle()
            self.assertEqual(len(runner.mount_calls), 8)
            self.assertIn(mount_point, runner.mounted)

    def test_fstab_reload_add_remove_change_and_non_nfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fstab = root / "fstab"
            runner = FakeRunner()
            clock = ManualClock()
            watchdog = make_watchdog(fstab, runner, clock)
            first = str(root / "first")
            second = str(root / "second")
            third = str(root / "third")
            fourth = str(root / "fourth")
            fifth = str(root / "fifth")
            changed = str(root / "changed")
            ignored = str(root / "ext4")

            write_fstab(
                fstab,
                [("server-a.example:/export/first", first, "nfs", "defaults,_netdev")],
                runner,
            )
            watchdog.run_cycle()
            self.assertEqual(runner.mount_calls, [["mount", first]])

            write_fstab(
                fstab,
                [
                    ("server-a.example:/export/first", first, "nfs", "defaults,_netdev"),
                    ("server-b.example:/export/second", second, "nfs4", "defaults,_netdev"),
                ],
                runner,
            )
            runner.mounted[first] = runner.desired[first]
            watchdog.run_cycle()
            self.assertEqual(runner.mount_calls[-1], ["mount", second])

            write_fstab(
                fstab,
                [("server-b.example:/export/second", second, "nfs4", "defaults,_netdev")],
                runner,
            )
            runner.mounted[second] = runner.desired[second]
            before_calls = len(runner.calls)
            watchdog.run_cycle()
            later_calls = runner.calls[before_calls:]
            self.assertFalse(any(first in call for call in later_calls))
            self.assertNotIn(mon.normalize_mount_point(first), watchdog._retry)

            write_fstab(
                fstab,
                [("another-server.example:/export/new", changed, "nfs", "rw,_netdev")],
                runner,
            )
            with self.assertLogs("nfs-mount-monitor", level="INFO") as captured:
                watchdog.run_cycle()
            logs = "\n".join(captured.output)
            self.assertIn("source=another-server.example:/export/new", logs)
            self.assertIn(f"mount_point={changed}", logs)
            self.assertNotIn("server-b.example:/export/second", logs)
            self.assertEqual(runner.mount_calls[-1], ["mount", changed])

            write_fstab(
                fstab,
                [
                    ("another-server.example:/export/new", changed, "nfs", "rw,_netdev"),
                    ("/dev/sdd1", ignored, "ext4", "defaults"),
                    ("server-c.example:/export/third", third, "nfs", "defaults,_netdev"),
                    ("server-d.example:/export/fourth", fourth, "nfs", "defaults,_netdev"),
                    ("files.example:/export/fifth", fifth, "nfs4", "defaults,_netdev"),
                ],
                runner,
            )
            runner.mounted[changed] = runner.desired[changed]
            stats = watchdog.run_cycle()
            self.assertEqual(stats.total, 4)
            mounted_now = [call[1] for call in runner.mount_calls]
            self.assertIn(third, mounted_now)
            self.assertIn(fourth, mounted_now)
            self.assertIn(fifth, mounted_now)
            self.assertNotIn(ignored, mounted_now)

    def test_duplicate_entries_mount_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_point = str(root / "once")
            fstab = root / "fstab"
            runner = FakeRunner()
            fstab.write_text(
                "\n".join(
                    [
                        f"nas-a.example:/export/first {mount_point} nfs defaults,_netdev 0 0",
                        f"nas-b.example:/export/second {mount_point} nfs rw,_netdev 0 0",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            runner.desired[mount_point] = ("nas-a.example:/export/first", "nfs")
            with self.assertLogs("nfs-mount-monitor", level="WARNING"):
                stats = make_watchdog(fstab, runner).run_cycle()
            self.assertEqual(stats.total, 1)
            self.assertEqual(runner.mount_calls, [["mount", mount_point]])

    def test_occupied_mount_is_not_covered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_point = str(root / "busy")
            fstab = root / "fstab"
            runner = FakeRunner()
            write_fstab(
                fstab,
                [("nas.example:/export/new", mount_point, "nfs", "defaults,_netdev")],
                runner,
            )
            runner.mounted[mount_point] = ("/dev/sdb1", "ext4")
            stats = make_watchdog(fstab, runner).run_cycle()
            self.assertEqual(runner.mount_calls, [])
            self.assertEqual(stats.missing, 1)
            self.assertEqual(runner.mounted[mount_point], ("/dev/sdb1", "ext4"))

    def test_timeout_does_not_block_the_next_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slow = str(root / "slow")
            healthy = str(root / "healthy")
            fstab = root / "fstab"
            runner = FakeRunner()
            write_fstab(
                fstab,
                [
                    ("nas-slow.example:/export/slow", slow, "nfs", "defaults,_netdev"),
                    ("nas-ok.example:/export/ok", healthy, "nfs", "defaults,_netdev"),
                ],
                runner,
            )
            runner.timeouts.add(slow)
            started = time.monotonic()
            stats = make_watchdog(fstab, runner).run_cycle()
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(stats.mounted, 1)
            self.assertEqual(stats.missing, 1)
            self.assertIn(healthy, runner.mounted)

    def test_secrets_in_options_are_not_logged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_point = str(root / "secret")
            fstab = root / "fstab"
            runner = FakeRunner()
            write_fstab(
                fstab,
                [(
                    "nas.example:/export/secret",
                    mount_point,
                    "nfs",
                    "rw,password=hunter2,_netdev",
                )],
                runner,
            )
            with self.assertLogs("nfs-mount-monitor", level="DEBUG") as captured:
                make_watchdog(fstab, runner).run_cycle()
            self.assertNotIn("hunter2", "\n".join(captured.output))

    def test_retry_delay_schedule(self) -> None:
        watchdog = mon.NFSWatchdog(retry_initial=30, retry_max=300, retry_factor=2)
        self.assertEqual(
            [watchdog.retry_delay(count) for count in range(1, 8)],
            [30, 60, 120, 240, 300, 300, 300],
        )

    def test_mount_command_timeout_kills_the_process_group(self) -> None:
        runner = mon.SubprocessRunner()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "child.sh"
            parent = root / "parent.sh"
            child.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
            parent.write_text(f"#!/bin/sh\n{child} &\nwait\n", encoding="utf-8")
            child.chmod(0o755)
            parent.chmod(0o755)
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.run([str(parent)], 1)
            self.assertLess(time.monotonic() - started, 5)
            processes = subprocess.run(
                ["ps", "-eo", "cmd"],
                check=False,
                capture_output=True,
                text=True,
            )
            leaked = [line for line in processes.stdout.splitlines() if str(root) in line]
            self.assertEqual(leaked, [])


class SourceContractTests(unittest.TestCase):
    def test_application_has_no_environment_specific_nfs_configuration(self) -> None:
        root = Path(__file__).resolve().parents[1]
        offenders: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".sh", ".service", ".md", ".conf", ".txt"}:
                continue
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for snippet in BANNED_SNIPPETS:
                if snippet in text:
                    offenders.append(f"{path.relative_to(root)} contains {snippet}")
            if "mount -a" in text and path.suffix == ".py":
                offenders.append(f"{path.relative_to(root)} contains mount -a")
        self.assertEqual(offenders, [])


class LiveFstabTests(unittest.TestCase):
    def test_actual_fstab_is_read_only_and_nfs_entries_match_findmnt(self) -> None:
        fstab = Path("/etc/fstab")
        if not os.access(fstab, os.R_OK):
            self.skipTest("/etc/fstab is not readable")
        if mon.shutil.which("findmnt") is None:
            self.skipTest("findmnt is not installed")

        before = fstab.read_bytes()
        text = fstab.read_text(encoding="utf-8")
        entries = mon.parse_fstab(str(fstab))
        watchdog = mon.NFSWatchdog(fstab_path=str(fstab))

        self.assertGreater(len(entries), 0)
        active_text = "\n".join(
            raw for raw in text.splitlines() if not raw.lstrip().startswith("#")
        )
        for entry in entries:
            self.assertTrue(mon.is_nfs_type(entry.fs_type))
            self.assertTrue(entry.mount_point.startswith("/"))
            self.assertIn(entry.options, text)
            self.assertIn(entry.mount_point, active_text)
            self.assertIn(entry.source, active_text)
            observation = watchdog.observe(entry.mount_point)
            self.assertFalse(observation.error, observation)
            if observation.present:
                self.assertTrue(
                    observation.matches(entry),
                    f"{entry.mount_point} observed as "
                    f"{observation.source} {observation.fs_type}",
                )

        for raw in text.splitlines():
            line = mon.strip_comments(raw).strip()
            if not line:
                continue
            fields = mon.split_fstab_fields(line)
            if len(fields) < 3:
                continue
            fs_type = mon.unescape_fstab_field(fields[2])
            mount_point = mon.unescape_fstab_field(fields[1])
            if mon.is_nfs_type(fs_type):
                continue
            self.assertNotIn(
                mon.normalize_mount_point(mount_point),
                {mon.normalize_mount_point(entry.mount_point) for entry in entries},
            )

        parent = watchdog.observe("/etc")
        self.assertFalse(parent.present)
        self.assertEqual(fstab.read_bytes(), before)

    def test_copy_of_actual_fstab_picks_up_added_and_removed_entries(self) -> None:
        fstab = Path("/etc/fstab")
        if not os.access(fstab, os.R_OK):
            self.skipTest("/etc/fstab is not readable")
        before = fstab.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy = root / "fstab"
            copy.write_bytes(before)
            self.assertEqual(
                [
                    (entry.source, entry.mount_point, entry.fs_type, entry.options)
                    for entry in mon.parse_fstab(copy)
                ],
                [
                    (entry.source, entry.mount_point, entry.fs_type, entry.options)
                    for entry in mon.parse_fstab(str(fstab))
                ],
            )
            added = str(root / "added")
            with copy.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\nnew-server.example:/export/added {added} nfs defaults,_netdev 0 0\n"
                    f"/dev/sde1 {root / 'ignored'} ext4 defaults 0 1\n"
                )
            parsed = mon.parse_fstab(copy)
            self.assertIn(added, [entry.mount_point for entry in parsed])
            self.assertNotIn(str(root / "ignored"), [entry.mount_point for entry in parsed])
            copy.write_bytes(before)
            self.assertNotIn(
                added,
                [entry.mount_point for entry in mon.parse_fstab(copy)],
            )
        self.assertEqual(fstab.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
