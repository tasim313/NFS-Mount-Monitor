# NFS Mount Monitor

## GitHub repository and contribution workflow

The GitHub repository for this project is:

<https://github.com/tasim313/NFS-Mount-Monitor>

The `main` branch is the stable branch. Make and push development changes on
`dev`, then open a pull request from `dev` into `main` and merge it after
review. Do not push routine development work directly to `main`.

### Initialize a local checkout

For a new local project directory, initialize Git and create the first commit
on `main`:

```bash
git init
git add README.md
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/tasim313/NFS-Mount-Monitor.git
git push -u origin main
```

If this directory already contains the project files, stage the files you want
in the initial repository commit (instead of only `README.md`). If the remote
`main` branch already has commits, fetch and reconcile those commits before
your first push.

### Develop on `dev` and open a pull request

Create the development branch from the current `main`, make your changes, and
commit them:

```bash
git switch main
git pull origin main
git switch -c dev
# Edit and review project files.
git add README.md nfs_mount_monitor.py config.py tests
git commit -m "Describe the development change"
git push -u origin dev
```

Open a pull request on GitHub with **base** set to `main` and **compare** set
to `dev`. Review and merge the pull request on GitHub. To continue working
after a merge, update local `main` and bring it into `dev`:

```bash
git switch main
git pull origin main
git switch dev
git merge main
```

The commands in this section are workflow examples. Replace the staged paths
and commit message with the files and summary for each change. Git commits and
pushes require Git identity and GitHub authentication to be configured.

A systemd watchdog that keeps the NFS and NFS4 filesystems in `/etc/fstab` mounted.

`/etc/fstab` is the only source for the NFS server, the remote export, the local mount point, the filesystem type, the mount options, and how many of those filesystems exist. The Python program contains the monitoring loop, mount check, verification, logging, and retry logic. It does not contain a copy of the machine's NFS configuration.

## Architecture

```text
/etc/fstab
      |
      v
Dynamic NFS parser
      |
      v
NFS entry list
      |
      +---- source
      +---- mount point
      +---- filesystem type
      +---- mount options
      |
      v
Mount state check (findmnt --mountpoint)
      |
      +---- Mounted as specified -> do nothing
      |
      +---- Missing -> mount <mount-point>
      |
      v
Verification
      |
      v
Logging and per-mount retry/backoff
      |
      v
Next cycle, which reads /etc/fstab again
```

Each cycle re-reads `/etc/fstab`. Adding, removing, or editing an NFS entry is picked up on the next cycle without restarting the process or changing Python code. Zero, one, or many entries are all handled the same way. Entries can point at different servers, hostnames, or addresses.

The service is a recovery layer. systemd still generates the normal boot mounts from `/etc/fstab`.

## What the watchdog reads

A line is used only when its filesystem type is `nfs` or `nfs4`. Other types are ignored, including `ext4`, `xfs`, `vfat`, `swap`, `tmpfs`, `proc`, `sysfs`, `overlay`, and bind mounts.

The parser accepts blank lines, comments, mixed spaces and tabs, and fstab octal escapes such as `\040` for a space. A malformed line is logged and skipped. Duplicate mount points are attempted once; the first entry is kept because `mount <mount-point>` uses the first matching fstab record.

The options already written by the administrator are left untouched. The watchdog does not add `nofail`, `nfsvers=4`, or any other option, and it does not rewrite `/etc/fstab`.

## Mount repair

A missing entry is repaired with the mount point taken from that fstab record:

```bash
mount /path/from/fstab
```

`mount` reads the server, export, type, and options from `/etc/fstab`. The watchdog does not assemble a `server:/export` command, and it does not run `mount -a`.

`findmnt --mountpoint` then has to show that same path mounted with an NFS type and the fstab source. `findmnt --target` is not used, because that query also matches a parent filesystem.

Entries are handled one at a time. One failure does not stop the remaining entries, and it does not stop the watchdog.

## Retry backoff

Each mount point keeps its own failure count:

| Failure | Next attempt |
| --- | --- |
| 1st | 30 seconds |
| 2nd | 60 seconds |
| 3rd | 120 seconds |
| later | 300 seconds maximum |

The check loop still runs on its normal interval, re-reads `/etc/fstab`, and notices a mount that has already recovered. The `mount` command itself waits until that entry's backoff has elapsed. A successful verification clears the failure count for that mount point.

## Requirements

- Linux with systemd
- Python 3
- `nfs-common` (`mount` and `findmnt`)
- root privileges for the service

No third-party Python packages are required.

## Install

From this directory:

```bash
sudo ./install.sh
```

The installer checks systemd and Python 3, installs `nfs-common` when `mount.nfs` or `findmnt` is missing, copies the program to `/opt/nfs-mount-monitor`, creates any missing local directories for NFS entries already present in `/etc/fstab`, and enables the service.

It does not edit `/etc/fstab`.

## Generic settings

Defaults:

- check interval: 30 seconds
- mount command timeout: 30 seconds
- `findmnt` timeout: 10 seconds
- retry delays: 30, 60, 120, then at most 300 seconds
- log level: INFO

```text
NFS_MONITOR_CHECK_INTERVAL=30
NFS_MONITOR_MOUNT_TIMEOUT=30
NFS_MONITOR_FINDMNT_TIMEOUT=10
NFS_MONITOR_RETRY_INITIAL_DELAY=30
NFS_MONITOR_RETRY_MAX_DELAY=300
NFS_MONITOR_RETRY_BACKOFF_FACTOR=2
NFS_MONITOR_LOG_LEVEL=INFO
NFS_MONITOR_FSTAB=/etc/fstab
```

`NFS_MONITOR_FSTAB` selects which fstab file to read. It is not a list of servers or mount points.

## Service commands

```bash
sudo systemctl status nfs-mount-monitor
sudo systemctl restart nfs-mount-monitor
sudo systemctl stop nfs-mount-monitor
sudo systemctl start nfs-mount-monitor
sudo journalctl -u nfs-mount-monitor -f
```

## Example fstab entries

The following lines are examples of the file format only. This program does not read them, and they are not the configuration of any particular machine. The running watchdog uses whatever `nfs` and `nfs4` lines are in `/etc/fstab`.

```fstab
# EXAMPLE ONLY — not used by nfs-mount-monitor.
nas-server.example:/export/data /mnt/data nfs defaults,_netdev 0 0
backup.example.local:/export/archive /mnt/archive nfs4 defaults,_netdev 0 0
files.example:/export/projects /srv/projects nfs rw,_netdev,timeo=10,retrans=2 0 0
```

After an administrator edits the real `/etc/fstab`:

```bash
sudo systemctl daemon-reload
findmnt -t nfs,nfs4
```

The next watchdog cycle reads the new file. A removed entry is no longer checked. The watchdog does not unmount it.

## Logging

Logs go to stdout and are collected by journald. Mount messages use the source and mount point from the current fstab entry, for example:

```text
NFS fstab reloaded path=/etc/fstab count=2
NFS entry source=nas-server.example:/export/data mount_point=/mnt/data fs_type=nfs
NFS mount missing source=nas-server.example:/export/data mount_point=/mnt/data fs_type=nfs
NFS mount result=success source=nas-server.example:/export/data mount_point=/mnt/data
NFS mount result=failure source=backup.example.local:/export/archive mount_point=/mnt/archive retry_in=30s detail=mount.nfs: No route to host
NFS status total=2 mounted=1 missing=1 deferred=0
```

Passwords, credentials, and other secrets are not written to the log.

## Safety

The watchdog:

- only reads `/etc/fstab`;
- mounts a single missing mount point per attempt;
- verifies that mount before reporting success;
- keeps monitoring when one entry fails;
- creates a missing empty mount directory when a newly added entry needs one;
- stops on SIGTERM and SIGINT.

It does not edit `/etc/fstab`, unmount a filesystem, delete data, reboot the machine, or run shell commands with `shell=True`.

Mounting requires root. The service's only filesystem change is mounting an entry that is already defined in `/etc/fstab`, plus creating an empty directory when that mount point does not exist yet.

## Tests

Unit tests use temporary fstab files and do not modify `/etc/fstab`:

```bash
python3 -m unittest discover -s tests -t . -v
```

A read-only check of the real `/etc/fstab` is included. It parses the file and queries `findmnt`. It does not mount or unmount anything.

## Uninstall

```bash
sudo ./uninstall.sh
```

Uninstall removes the application and the systemd unit. It leaves `/etc/fstab` and any NFS mounts in place.

## Troubleshooting

```bash
findmnt -t nfs,nfs4
findmnt -n -P --mountpoint /path/from/fstab
journalctl -u nfs-mount-monitor --since "10 minutes ago"
```

If `findmnt` reports that the path is mounted from a different source than the current fstab line, the watchdog logs that and leaves the existing mount in place. Remounting is an administrator action.

A changed `/etc/fstab` is reread automatically. `systemctl daemon-reload` is still required before systemd's own boot units pick up the same edit.

```bash
sudo apt install nfs-common
```

installs the NFS client tools when they are missing.
