# bdwn (BackDown)

Encrypted server backups to a remote SFTP repository using restic, driven by a
systemd timer running under a hardened unit.

bdwn backs up a list of directories you choose to a remote SFTP server. Backups
are stored in restic format, encrypted with a passphrase. A daily systemd timer
runs the backup as root with a reduced capability set (so any path can be read),
dropping privileges to the unprivileged `bdwn` user for the SSH/SFTP connection,
made with a dedicated key. Retention (daily/weekly/monthly) is applied
automatically after each run. ZFS pools/datasets/volumes can optionally be
exported with `zfs send` and included in the backup, or replicated straight
onto a dataset of a remote ZFS server with `bdwn zfs-sync` and its own timer,
see [ZFS replication](#zfs-replication-bdwn-zfs-sync). A local repository can be
mirrored onto an external disk with `bdwn export`, see
[Export to an external device](#export-to-an-external-device). An optional companion
package, `bdwn-libvirt`, adds cold backups of libvirt VMs into the same
repository, see [Libvirt VM backup](#libvirt-vm-backup-bdwn-libvirt).

This README documents the **client** side. The repository server side ships as
a separate package, `bdwn-server`, documented in
[server/README.md](server/README.md).

## Dependencies

All are standard Debian packages, declared in `debian/control` and pulled in
automatically on install:

- `restic` - backup engine
- `openssh-client` - SSH/SFTP transport
- `rsync` - mirrors a local repository in `bdwn export`
- `adduser` - creates the `bdwn` system user

Optional (declared as a suggestion, not installed automatically):

- `zfsutils-linux` - only needed for [ZFS backup](#zfs-backup) and
  [ZFS replication](#zfs-replication-bdwn-zfs-sync)

The `bdwn-libvirt` companion package additionally depends on `libvirt-clients`
and `qemu-utils`, both pulled in automatically when installing `bdwn-libvirt`.
The `bdwn-server` package depends on `openssh-server`, `openssh-client`,
`rsync` and `adduser`, and not on `bdwn`.

## Build

One source tree, three binary packages:

```sh
make deb      # build all three packages, see below
make help     # list available targets
make clean    # remove build artifacts
```

| Package        | Installed on   | Contents                                                                 |
| -------------- | -------------- | ------------------------------------------------------------------------ |
| `bdwn`         | backup client  | `bdwn` CLI, `bdwn.service`, `bdwn.timer`, examples                        |
| `bdwn-libvirt` | backup client  | `bdwn-libvirt` plus the per-VM unit templates. Depends on `bdwn`.         |
| `bdwn-server`  | backup server  | `bdwn-setup-backup-user`, `bdwn-export`, `bdwn-export.service`/`.timer`. Independent of `bdwn`. |

Output files are `./bdwn_<version>_all.deb`,
`./bdwn-libvirt_<version>_all.deb` and `./bdwn-server_<version>_all.deb`.

## Install

```sh
apt install ./bdwn_*.deb
```

`bdwn-server` is installed on the **backup server**, not on the client, and is
independent of `bdwn`: a backup server need not be a backup client. It ships
`bdwn-setup-backup-user` (one SFTP-only chrooted user and restic repository
per client), `bdwn-export` (mirror of the server repositories onto an
external device) and `bdwn-webui` (read-only status page showing the state and
the size of every repository). See [server/README.md](server/README.md).

When `bdwn-libvirt` is installed, pass both packages to apt in the same command
so they upgrade together:

```sh
apt install ./bdwn_*.deb ./bdwn-libvirt_*.deb
```

`bdwn-libvirt` depends on `bdwn` at its own version or newer. Upgrading `bdwn`
alone is fine; upgrading `bdwn-libvirt` alone, or installing a `bdwn-libvirt`
newer than the installed `bdwn`, is refused by apt. A `bdwn-libvirt` built
before this dependency was relaxed still requires the exact same `bdwn`
version, so the first upgrade past it must name both `.deb` files in one
command.

The package `postinst` performs first-install setup:

- Creates the `bdwn` system group and user (home `/var/lib/bdwn`, shell
  `/usr/sbin/nologin`).
- Seeds `/etc/bdwn/bdwn.conf` from the example, owned `root:root`, mode `0600`
  (directory `/etc/bdwn` is `root:root`, `0700`). On upgrade from older
  versions, the looser `0640` root:bdwn permissions are tightened.
- Seeds `/var/lib/bdwn/.ssh/config` from the example, owned `bdwn:bdwn`,
  mode `0600`. The host alias, IP and port placeholders are filled from the
  debconf values (or their defaults), see
  [Package configuration (debconf)](#package-configuration-debconf).
- Adds a `<ip> <alias>` line for the backup server to `/etc/hosts`, only if
  the alias is not already present.
- Reloads systemd so it sees the units. The `bdwn.timer` is **not** enabled
  automatically: you enable it yourself once the config is ready.

Existing config and SSH files are never overwritten on upgrade.

After install you must edit the config, set up the SSH key, test a backup, then
enable the timer (both as root):

```
bdwn backup                          # test once
systemctl enable --now bdwn.timer    # start the daily schedule
```

## Package configuration (debconf)

The first-install setup uses three values to fill the seeded SSH config, add
the `/etc/hosts` line and print the suggested nftables rule:

| Debconf key               | Default         | Purpose                                    |
| ------------------------- | --------------- | ------------------------------------------ |
| `bdwn/backup-server`      | `backup-srv`       | SSH host alias of the backup server        |
| `bdwn/backup-server-ip`   | `192.168.1.5` | IP written to `/etc/hosts` for the alias   |
| `bdwn/backup-server-port` | `2222`         | Port used for the SFTP connection          |

There is no interactive prompt. To override a default, preseed the values
before installing:

```sh
cat <<'EOF' | debconf-set-selections
bdwn bdwn/backup-server string mybackup
bdwn bdwn/backup-server-ip string 10.0.0.4
bdwn bdwn/backup-server-port string 22
EOF
apt install ./bdwn_*.deb
```

Any key not preseeded keeps its built-in default. Inspect the current values
with `debconf-show bdwn`.

Caveat: the values only matter on **first** install. The SSH config is seeded
once (never overwritten on upgrade) and the `/etc/hosts` line is added only if
missing. To change them afterwards, edit `/var/lib/bdwn/.ssh/config` and
`/etc/hosts` by hand.

## Configuration

Edit `/etc/bdwn/bdwn.conf`. It is plain shell syntax (sourced by the CLI), so
use no spaces around `=`.

| Variable            | Required | Meaning                                                                                      |
| ------------------- | -------- | -------------------------------------------------------------------------------------------- |
| `BDWN_REPO`         | yes      | Restic repository. Either `sftp:<ssh-host>:/path`, where `<ssh-host>` is a `Host` entry in the SSH config, or an absolute local path, see [Local repository](#local-repository). |
| `RESTIC_PASSWORD`   | yes      | Encryption passphrase for the repository. Keep it secret.                                    |
| `BACKUP_PATHS`      | yes      | Space-separated directories/files to back up. Any path works (the service reads as root).    |
| `BDWN_EXCLUDES`     | no       | Space-separated exclude patterns passed to restic.                                           |
| `BDWN_KEEP_DAILY`   | no       | Daily snapshots to keep. Retention runs only if any keep value is set.                       |
| `BDWN_KEEP_WEEKLY`  | no       | Weekly snapshots to keep.                                                                     |
| `BDWN_KEEP_MONTHLY` | no       | Monthly snapshots to keep.                                                                    |
| `BDWN_CACHE_DIR`    | no       | Restic cache directory. Defaults to `/var/cache/bdwn`.                                        |
| `BDWN_RESTORE_DIR`  | no       | Default target directory for `bdwn restore`. Defaults to `/var/backups/bdwn/restore`.         |
| `BDWN_EXPORT_UUID`  | no       | Filesystem UUID of the external device receiving the mirror. Unset by default; required only when `bdwn export` is called with no argument. See [Export to an external device](#export-to-an-external-device). |
| `BDWN_EXPORT_MOUNT` | no       | Mount point used for `BDWN_EXPORT_UUID`. Defaults to `/mnt/bdwn-export`.                      |
| `BDWN_EXPORT_SUBDIR`| no       | Directory created on the device to hold the mirror. Defaults to `bdwn-repo`.                  |
| `BDWN_ZFS_DATASETS` | no       | Space-separated ZFS pools/datasets/volumes to export and back up. See [ZFS backup](#zfs-backup). |
| `BDWN_ZFS_DUMP_DIR` | no       | Directory receiving the ZFS dumps. Defaults to `/var/backups/bdwn/zfs`. If changed, adjust `ReadWritePaths` in the systemd drop-in. |
| `BDWN_LIBVIRT_SHUTDOWN_TIMEOUT` | no | Seconds to wait for a clean VM shutdown before aborting. Defaults to `300`. Only used by `bdwn-libvirt`. |
| `BDWN_LIBVIRT_MERGE_MODE` | no | How the snapshot overlay is merged back: `auto` (default), `live` or `cold`. See [Merge modes](#merge-modes). Only used by `bdwn-libvirt`. |

Example:

```sh
BDWN_REPO="sftp:backup-remote:/$(hostname)"
RESTIC_PASSWORD="change-me-to-a-strong-passphrase"
BACKUP_PATHS="/etc /var/www /home"
BDWN_EXCLUDES="*.tmp /home/*/.cache"
BDWN_KEEP_DAILY="7"
BDWN_KEEP_WEEKLY="4"
BDWN_KEEP_MONTHLY="6"
```

## Local repository

`BDWN_REPO` can also be an absolute filesystem path, for a repository on a
mounted disk or NFS share:

```sh
BDWN_REPO="/srv/backups/restic"
```

In this mode restic reads and writes the repository directly as root: no SSH
config, key, known_hosts or backup server is needed. Skip the
[SSH setup](#ssh-setup) and [Backup server setup](#backup-server-setup)
sections entirely.

The hardened `bdwn.service` mounts the filesystem read-only
(`ProtectSystem=strict`, only `/var/lib/bdwn` and `/var/cache/bdwn` writable),
so the repository path must be opened up with a systemd drop-in. The package
ships an example:

```sh
install -d /etc/systemd/system/bdwn.service.d
cp /usr/share/bdwn/local-repo-dropin.conf.example /etc/systemd/system/bdwn.service.d/local-repo.conf
# edit ReadWritePaths in local-repo.conf to the parent directory of BDWN_REPO
systemctl daemon-reload
```

Running `bdwn backup` by hand as root works without the drop-in.

## Export to an external device

`bdwn export [DIRECTORY]` mirrors a **local** repository onto an external disk
with `rsync -a --delete --stats`, into the `BDWN_EXPORT_SUBDIR` directory of
the device (created `0700`). The copy is byte-for-byte the same restic
repository: same passphrase, objects deleted by retention removed, and later
exports only transfer the delta. After the copy bdwn runs `sync`, checks the
result with `restic -r <dest> cat config` and prints the restore command.

Runs as root only (it may mount a device and reads the whole repository).
There is no export from an `sftp:` repository: bdwn refuses it.

With a directory argument, you mount and unmount the disk yourself. The
directory must already be a mount point, otherwise bdwn refuses to run (guard
against mirroring onto the root filesystem):

```sh
mount /dev/sdb1 /mnt/usb
bdwn export /mnt/usb           # mirror into /mnt/usb/bdwn-repo
umount /mnt/usb
```

With `BDWN_EXPORT_UUID` set, bdwn mounts and unmounts the device itself,
including when the export fails. A mount point already mounted is left mounted:

```sh
lsblk -o NAME,SIZE,FSTYPE,UUID          # find the UUID of the external disk

cat >> /etc/bdwn/bdwn.conf <<'EOF'
BDWN_EXPORT_UUID="1234abcd-56ef-78ab-90cd-1234567890ab"
BDWN_EXPORT_MOUNT="/mnt/bdwn-export"
BDWN_EXPORT_SUBDIR="bdwn-repo"
EOF

bdwn export                             # mount, mirror, unmount
```

The copy is a standalone repository: restore from it on any machine with
restic, no bdwn needed (restic prompts for the passphrase):

```sh
restic -r /mnt/usb/bdwn-repo restore latest --target /tmp/restore
```

Caveat: `bdwn export` is a manual command only. The hardened `bdwn.service`
cannot run it (`SystemCallFilter` blocks `@mount` and `ProtectSystem=strict`
leaves the device read-only), so no timer or drop-in is provided for it.

## ZFS backup

When `BDWN_ZFS_DATASETS` is set, `bdwn backup` runs this sequence before the
restic backup, for each listed pool/dataset/volume:

1. Snapshot it recursively under the fixed name `@bdwn` (a stale `@bdwn`
   snapshot left by a crashed run is destroyed first).
2. Export every filesystem/volume below it with `zfs send` to a dump file in
   `BDWN_ZFS_DUMP_DIR` (dataset name with `/` replaced by `_`, extension
   `.zfs`).
3. Destroy the snapshot.

The dump directory is added to the paths backed up by restic, and the dump
files are deleted after a successful backup: restic deduplicates by content,
so keeping them locally buys nothing. Requires the `zfsutils-linux` package.

The hardened `bdwn.service` blocks ZFS by default (`PrivateDevices=yes` hides
`/dev/zfs`, no `CAP_SYS_ADMIN`). The package ships a drop-in example that
relaxes only what ZFS needs (`PrivateDevices=no` with a minimal `DeviceAllow`
list, `CAP_SYS_ADMIN`, `ReadWritePaths=/var/backups/bdwn`). To enable ZFS
backup under the systemd timer:

```sh
install -d /etc/systemd/system/bdwn.service.d
cp /usr/share/bdwn/zfs-dropin.conf.example /etc/systemd/system/bdwn.service.d/zfs.conf
systemctl daemon-reload
```

Running `bdwn backup` by hand as root works without the drop-in.

To restore, fetch the dump from the repository, then feed it to `zfs receive`:

```sh
bdwn restore --target /tmp/restore /var/backups/bdwn/zfs
zfs receive tank/vms/disk1 < /tmp/restore/var/backups/bdwn/zfs/tank_vms_disk1.zfs
```

## ZFS replication (bdwn zfs-sync)

`bdwn zfs-sync` replicates a local ZFS dataset onto a dataset of a remote
server with `zfs send` piped into `zfs receive` over SSH. It is the one command
that leaves restic aside: the target is a real ZFS dataset, not an encrypted
repository, so it is independent of `BDWN_REPO` and `RESTIC_PASSWORD` and works
on a machine where they are not even configured.

Use it when the backup target is a ZFS pool and you want a directly usable
copy (snapshots browsable, rollback, `zfs clone`). Use
[ZFS backup](#zfs-backup) instead when you want the datasets inside the
encrypted restic repository.

Each run:

1. Lists the sync snapshots of the dataset and asks the remote server whether
   it still holds the most recent one.
2. Snapshots the dataset as `<prefix>-<UTC timestamp>` (`bdwn-sync-20260819T031500Z`
   by default), recursively unless `BDWN_ZFS_SYNC_RECURSIVE="no"`.
3. Sends it, incrementally from the snapshot found in step 1 (`zfs send -I`, so
   every snapshot taken since the last sync is replicated too), or in full when
   the two sides share none: first run, or remote dataset recreated.
4. Destroys the local sync snapshots beyond the last `BDWN_ZFS_SYNC_KEEP`
   (default 3). The remote keeps its own copies; the ones kept locally only
   serve as bases for the next incremental stream.

If the transfer fails, the snapshot just created is destroyed and the command
exits non-zero, so the next run resumes incrementally from the last snapshot
the remote actually has, instead of falling back to a full stream.

### Configuration

Everything lives in `/etc/bdwn/bdwn.conf`, alongside the restic settings.
Three keys are required, the rest have defaults:

| Key | Default | Purpose |
| --- | --- | --- |
| `BDWN_ZFS_SYNC_LOCAL_DATASET` | *required* | Local dataset to replicate |
| `BDWN_ZFS_SYNC_REMOTE_DATASET` | *required* | Dataset receiving the copy; its parent must already exist |
| `BDWN_ZFS_SYNC_HOST` | *required* | Remote server: a `Host` entry of the ssh config, or a hostname/IP |
| `BDWN_ZFS_SYNC_SSH_USER` | ssh config | Dedicated SSH account on the remote server |
| `BDWN_ZFS_SYNC_LOCAL_USER` | `bdwn` | Local unprivileged user the SSH client is dropped to |
| `BDWN_ZFS_SYNC_SSH_DIR` | `/var/lib/bdwn/.ssh` | Holds `config`, the key and `known_hosts` |
| `BDWN_ZFS_SYNC_SSH_KEY` | ssh config | Private key (`IdentitiesOnly` is set with it) |
| `BDWN_ZFS_SYNC_PORT` | ssh config | SSH port |
| `BDWN_ZFS_SYNC_SSH_OPTS` | *(none)* | Extra ssh options |
| `BDWN_ZFS_SYNC_RECURSIVE` | `yes` | Replicate the children of the dataset (`snapshot -r`, `send -R`) |
| `BDWN_ZFS_SYNC_SEND_OPTS` | *(none)* | Extra `zfs send` options, e.g. `-L -e -c`, or `-w` to keep an encrypted dataset encrypted on the wire and on the remote |
| `BDWN_ZFS_SYNC_RECV_OPTS` | `-u -F` | Options of the remote `zfs receive` |
| `BDWN_ZFS_SYNC_REMOTE_ZFS` | `zfs` | Command invoked on the remote server, e.g. `sudo zfs` |
| `BDWN_ZFS_SYNC_KEEP` | `3` | Sync snapshots kept locally (minimum 1) |
| `BDWN_ZFS_SYNC_SNAP_PREFIX` | `bdwn-sync` | Prefix of the snapshots created here |

`-u` in the default `BDWN_ZFS_SYNC_RECV_OPTS` keeps the received dataset
unmounted, which is what lets an unprivileged remote account receive it at all.
`-F` rolls the remote copy back to its last snapshot before receiving, so a
stray write on the target does not break the next incremental stream; it also
means anything written to the remote copy is discarded.

A minimal configuration:

```sh
BDWN_ZFS_SYNC_LOCAL_DATASET="tank/data"
BDWN_ZFS_SYNC_REMOTE_DATASET="backup/srv1/data"
BDWN_ZFS_SYNC_HOST="backup-remote"
BDWN_ZFS_SYNC_SSH_USER="bdwn-zfs"
```

### Remote server setup

The dedicated account needs no shell privileges beyond ZFS. Create it, install
the public key of `/var/lib/bdwn/.ssh/`, create the parent dataset and delegate
the receive permissions:

```sh
adduser --system --shell /bin/sh --home /home/bdwn-zfs bdwn-zfs
install -d -o bdwn-zfs -g nogroup -m 0700 /home/bdwn-zfs/.ssh
cat >> /home/bdwn-zfs/.ssh/authorized_keys   # paste the client public key

zfs create -p backup/srv1
zfs allow -u bdwn-zfs create,receive,mount,mountpoint,destroy,rollback,snapshot,hold backup/srv1
```

`zfs allow` covers everything the receive needs. On Linux the `mount`
permission is not enough for an unprivileged user to actually mount a dataset,
which is why the default `-u` never asks for a mount. If you drop `-u`, run the
receive through `sudo` instead (`BDWN_ZFS_SYNC_REMOTE_ZFS="sudo zfs"`, with a
matching NOPASSWD rule).

On the client, record the host key once and test:

```sh
ssh-keyscan -p 22 zfs.example.com >> /var/lib/bdwn/.ssh/known_hosts
chown bdwn:bdwn /var/lib/bdwn/.ssh/known_hosts
bdwn zfs-sync
```

### Timer

The package ships `bdwn-zfs-sync.service` (oneshot, hardened like
`bdwn.service` plus what ZFS needs: `PrivateDevices=no` with a minimal
`DeviceAllow` list and `CAP_SYS_ADMIN`) and `bdwn-zfs-sync.timer` (daily,
persistent, 15 minutes of randomized delay). Nothing is enabled at install:

```sh
systemctl enable --now bdwn-zfs-sync.timer
systemctl start bdwn-zfs-sync.service    # run one now
journalctl -u bdwn-zfs-sync.service
```

Change the schedule with `systemctl edit bdwn-zfs-sync.timer` (`OnCalendar=hourly`
is a common choice for a replication).

Unlike `bdwn.service`, this unit needs no drop-in for ZFS: it is only ever used
for replication, so the `/dev/zfs` access and `CAP_SYS_ADMIN` are part of the
unit. It never holds the restic passphrase either, since `zfs-sync` does not
open the repository.

### Restoring

The remote copy is a plain dataset: browse its snapshots with
`zfs list -t snapshot backup/srv1/data`, and send one back the other way.

```sh
# on the remote server, sending back to the client
zfs send backup/srv1/data@bdwn-sync-20260819T031500Z | ssh srv1 zfs receive -F tank/data-restored
```

## Libvirt VM backup (bdwn-libvirt)

The `bdwn-libvirt` companion package (built from the same source, depends on
`bdwn` at that version or newer, `libvirt-clients` and `qemu-utils`) makes cold
backups of libvirt VMs into the bdwn repository. Repository, passphrase and SSH privilege
drop are all reused from the bdwn configuration.

```sh
apt install ./bdwn-libvirt_*.deb
```

For each VM, `/usr/bin/bdwn-libvirt <vm-name>` runs this sequence:

1. Clean shutdown via `virsh shutdown`, waiting up to
   `BDWN_LIBVIRT_SHUTDOWN_TIMEOUT` seconds (default 300). On timeout the
   backup aborts and the VM is restarted.
2. Cold external snapshot while the VM is off (`virsh snapshot-create-as
   --disk-only --atomic --no-metadata`): qcow2 overlays, the base images
   become stable.
3. VM restart. Downtime is the shutdown plus the snapshot, nothing more.
4. Export of the base disk images and the domain XML (dumped with
   `--inactive --security-info` to `/var/backups/bdwn/libvirt`) through
   `bdwn restic backup --tag libvirt-<vm>`.
5. Overlays merged back into the base images, according to
   `BDWN_LIBVIRT_MERGE_MODE` (see below). The persistent domain definition is
   then restored (`virsh define` of the pre-snapshot XML) and the overlay
   files removed.

A VM that is already shut off is exported directly, with no snapshot or
restart. If the script fails between the shutdown and the restart, a trap
restarts the VM. A run that dies between the snapshot and the merge leaves the
VM on its overlay: `bdwn-libvirt repair <vm-name>` cleans that up, see
[Repairing after a failed run](#repairing-after-a-failed-run).

### Merge modes

Merging the overlay back can be done live or with the VM stopped. Live is
free but needs qemu to reopen the base image read-write, which AppArmor
denies on most Debian hosts (see [Troubleshooting](#troubleshooting)).
`BDWN_LIBVIRT_MERGE_MODE` picks the strategy:

| Value | Behaviour |
| --- | --- |
| `auto` (default) | `virsh blockcommit --active --pivot`, falling back to a cold merge if it is refused. |
| `live` | Live merge only. The run fails if it is refused, leaving the VM up on its overlay. |
| `cold` | Cold merge only: stop the VM, `qemu-img commit` each overlay, restart the VM. |

The cold merge runs as root, outside the per-VM AppArmor profile, so it always
works. It costs a second downtime, short because an overlay only holds the
writes made since the snapshot. Set `cold` on a host where the live merge is
known to be denied: `auto` works there too, but wastes a failed attempt and
logs an error on every run.

The package ships template units `bdwn-libvirt@.service` (oneshot, hardened
like `bdwn.service`, plus `ReadWritePaths` for `/var/lib/libvirt` and
`/var/backups/bdwn`) and `bdwn-libvirt@.timer` (daily, persistent, one hour
of randomized delay to spread the VM downtime windows). Nothing is enabled by
the postinst; enable one instance per VM (the prerm stops and disables all
enabled instances on removal):

```sh
systemctl enable --now bdwn-libvirt@myvm.timer
```

Or run it by hand as root:

```sh
bdwn-libvirt myvm            # back up the VM
bdwn-libvirt repair myvm     # merge back the overlays left by a failed run
```

To restore, find the tagged snapshot, fetch it, then define the VM and put the
disk images back in place. `bdwn restore` has no `--tag` option, so the
snapshot is selected by the ID listed by `bdwn snapshots`:

```sh
bdwn snapshots --tag libvirt-myvm             # pick the snapshot ID
bdwn restore --snapshot 4a1b2c3d --target /tmp/restore
virsh define /tmp/restore/var/backups/bdwn/libvirt/myvm.xml
```

Retention: the keep policy of the main bdwn backup applies globally (restic
forget groups by host and paths), so the VM snapshot groups are pruned by the
daily bdwn run.

Caveats:

- Disk paths must not contain spaces.
- Cdrom devices are skipped.
- Works with file-backed disk images (qcow2/raw).
- `virsh shutdown` first tries the default mode (guest agent when a channel is
  defined in the domain XML), then falls back to a plain ACPI power button
  press if that call fails (e.g. a broken guest agent answering unparseable JSON).
- The guest only strictly needs to handle ACPI power button events.

### Repairing after a failed run

If a run dies between the snapshot and the merge (host reboot, killed job,
`live` merge refused), the VM keeps running on its `.bdwn` overlay and the next
backup aborts rather than stacking a second overlay on top.
`bdwn-libvirt repair <vm-name>` puts the VM back on its base images:

```sh
bdwn-libvirt repair myvm
```

It lists the domain disks, shuts the VM down if it is running
(`BDWN_LIBVIRT_SHUTDOWN_TIMEOUT` applies), runs `qemu-img commit` on each
leftover overlay, redefines the domain from the pre-snapshot XML, removes the
overlay files, and restarts the VM only if it was running before. It is
idempotent: running it twice, or on a healthy VM, is a no-op that prints
`myvm already runs on its base images, nothing to repair`.

Requirements and limits:

- The pre-snapshot definition `/var/backups/bdwn/libvirt/<vm>.xml` must exist.
  Every backup run writes it; without it the repair refuses, since nothing can
  point the domain back at its base images.
- A stale overlay file sitting next to a base image, while the domain already
  uses that base image, is **not** merged: the guest has been writing to the
  base since, so committing the overlay would corrupt the disk. The repair
  reports the file with the `qemu-img info` and `rm` commands to run, and
  stops. Deleting it stays a manual decision.

### Troubleshooting

**Symptom:** the export succeeds, then the live merge is refused with:

```
error: internal error: unable to execute QEMU command 'block-commit': Could not open '<base image>': Permission denied
```

With the default `auto` mode this is only a warning: the script logs
`live merge refused, falling back to a cold merge` and finishes the job with
the VM briefly stopped. In `live` mode the run fails there, and the VM keeps
running on its `.bdwn` overlay with the base image intact underneath. No data
is at risk either way.

**Cause:** for block-commit, qemu (running unprivileged as `libvirt-qemu`)
must reopen the base image read-write while it only had it read-only. On
Debian, `virt-aa-helper` regenerates the per-VM AppArmor profile at every
domain start and writes an explicit deny for each read-only backing file:

```sh
grep -B1 deny /etc/apparmor.d/libvirt/libvirt-$(virsh domuuid <vm>).files
  # don't audit writes to readonly files
  deny "/var/lib/libvirt/images/<disk>.qcow2" w,
```

The rule exists to silence audit noise, but an AppArmor `deny` is a hard
refusal that **no allow rule can override**. Adding permissions to
`/etc/apparmor.d/local/abstractions/libvirt-qemu` therefore has no effect,
and the file is rewritten at the next VM start anyway. The only durable ways
to lift it are host-wide: `security_driver = "none"` in
`/etc/libvirt/qemu.conf`, or a `complain` flag in
`/etc/apparmor.d/libvirt/TEMPLATE.qemu`. Both drop the confinement of every
VM on the host.

**Fix:** keep the confinement and use `BDWN_LIBVIRT_MERGE_MODE="cold"`, which
merges with `qemu-img` as root, outside the profile. Prefer it over `auto` on
such a host to avoid a failed attempt on every run.

If instead the denial is a plain permission problem (no `DENIED` line in
`journalctl -k`, base image not owned by `libvirt-qemu` in
`ls -l /var/lib/libvirt/images/`), fix it with `chown
libvirt-qemu:libvirt-qemu` and `chmod 0660` on the base image.

**Merging back** in `live` mode, once the VM is stuck on its overlay (the
failure message points at the same command):

```sh
bdwn-libvirt repair <vm>
```

Under the hood it stops the VM, `qemu-img commit`s each `.bdwn` overlay,
redefines the domain from `/var/backups/bdwn/libvirt/<vm>.xml`, removes the
overlay files and restarts the VM. See
[Repairing after a failed run](#repairing-after-a-failed-run).

**Symptom:** a backup aborts immediately with:

```
bdwn-libvirt: leftover overlay from a failed run: /var/lib/libvirt/images/<disk>.bdwn
Merge it back before retrying:
  bdwn-libvirt repair <vm>
```

**Cause:** a previous run died after the snapshot, so the domain still points
at the overlay. Snapshotting again would stack a second overlay on top.

**Fix:** run `bdwn-libvirt repair <vm>`, then retry the backup.

**Symptom:** the run aborts before the shutdown with:

```
bdwn-libvirt: orphaned overlay file from a previous run: /var/lib/libvirt/images/<disk>.bdwn
```

**Cause:** the VM runs on its base image (the overlay was merged back by hand,
or the merge step never removed it), but the overlay file is still on disk.
`virsh snapshot-create-as` refuses to overwrite an existing file, so the
script checks for it up front rather than shutting the VM down for nothing.

**Fix:** the overlay is stale, everything the guest wrote since the merge went
to the base image and merging it back now would corrupt the disk. Inspect it,
then delete it:

```sh
qemu-img info /var/lib/libvirt/images/<disk>.bdwn
rm /var/lib/libvirt/images/<disk>.bdwn
```

`bdwn-libvirt repair <vm>` detects and reports this case too (`nothing to
merge, but a stale overlay file is left over: ...`), but it never deletes the
file for you: removing it is a manual decision.

## SSH setup

This section and [Backup server setup](#backup-server-setup) only apply to
`sftp:` repositories; skip both for a [local repository](#local-repository).

`BDWN_REPO` connects through a `Host` entry in `/var/lib/bdwn/.ssh/config`. To
prepare the connection:

1. Place the private key at `/var/lib/bdwn/.ssh/id_ed25519`:

   ```sh
   chown bdwn:bdwn /var/lib/bdwn/.ssh/id_ed25519
   chmod 0600 /var/lib/bdwn/.ssh/id_ed25519
   ```

2. Edit `/var/lib/bdwn/.ssh/config` to match your remote host. `User` is the
   server name passed to `bdwn-setup-backup-user` (e.g. `myserver`):

   ```sshconfig
   Host backup-remote
       HostName backup.example.com
       Port 22
       User myserver
       IdentityFile /var/lib/bdwn/.ssh/id_ed25519
       IdentitiesOnly yes
   ```

3. Record the remote host key so restic can connect non-interactively
   (run as root):

   ```sh
   ssh-keyscan -p 22 backup.example.com >> /var/lib/bdwn/.ssh/known_hosts
   chown bdwn:bdwn /var/lib/bdwn/.ssh/known_hosts
   chmod 0600 /var/lib/bdwn/.ssh/known_hosts
   ```

The remote SFTP account must have the matching public key in its
`authorized_keys`.

## Backup server setup

The remote account that receives the backups should be locked down to SFTP
only. The `bdwn-server` package provides `bdwn-setup-backup-user`, which
creates a system user per client server, named after it, chrooted to the
shared `/backups/servers` jail with no shell, no command execution and no
forwarding: it can only read/write its own restic repository over SFTP. Run it
as root on the backup server, once per client, with the server name and the
client public key:

```sh
apt install ./bdwn-server_*.deb        # on the backup server
bdwn-setup-backup-user myserver id_ed25519.pub
```

The public key can also be passed inline instead of a file path, e.g.
`bdwn-setup-backup-user myserver "ssh-ed25519 AAAA... root@myserver"`. Without
the package, the same script runs from the source tree as
`./server/setup-backup-user.sh`.

Because of the shared chroot, the repository path the client sees is
`/<SERVER_NAME>` (its private dir inside the jail), regardless of where it
lives on the backup server. With this setup, use:

```sh
BDWN_REPO="sftp:backup-remote:/$(hostname)"
```

The same package also ships `bdwn-export`, which mirrors every client
repository held by the backup server onto an external device with rsync, with
no passphrase involved, and `bdwn-webui`, a read-only status page listing the
state, the size and the last snapshot date of every repository:

```sh
systemctl enable --now bdwn-webui                                 # on the server
ssh -L 8080:/run/bdwn-webui/bdwn-webui.sock root@backup-server    # from a workstation
```

then open `http://localhost:8080/`. See [server/README.md](server/README.md)
for the full server-side procedure, the `bdwn-server` package contents, the
export and the web UI.

## Layout on the target system

Client machine (`bdwn` and `bdwn-libvirt`). The files installed on the backup
server by `bdwn-server` are listed in [server/README.md](server/README.md).

| Path                              | Purpose                                          |
| --------------------------------- | ------------------------------------------------ |
| `/usr/bin/bdwn`                   | CLI wrapper around restic                        |
| `/etc/bdwn/bdwn.conf`             | Configuration (`0600` root:root)                 |
| `/var/lib/bdwn`                   | `bdwn` user home, holds `.ssh/` (config, key, known_hosts) |
| `/var/cache/bdwn`                 | Restic cache                                      |
| `/lib/systemd/system/bdwn.service`| Backup service (oneshot, hardened)               |
| `/lib/systemd/system/bdwn.timer`  | Daily timer                                       |
| `/lib/systemd/system/bdwn-zfs-sync.service` | ZFS replication service (oneshot, hardened) |
| `/lib/systemd/system/bdwn-zfs-sync.timer`   | Daily ZFS replication timer              |
| `/usr/share/bdwn/*.example`       | Example config, SSH config and ZFS systemd drop-in |
| `/var/backups/bdwn`               | ZFS dumps, libvirt XML staging and the default `bdwn restore` target (`0700` root:root, created by postinst, removed on purge) |
| `/usr/bin/bdwn-libvirt`           | Libvirt VM backup script (bdwn-libvirt package)   |
| `/lib/systemd/system/bdwn-libvirt@.service` | Per-VM backup service template (bdwn-libvirt package) |
| `/lib/systemd/system/bdwn-libvirt@.timer`   | Per-VM daily timer template (bdwn-libvirt package) |

## Usage

Once enabled (`systemctl enable --now bdwn.timer`), the timer runs a backup
daily with a randomized delay and no further manual action. On the first backup,
bdwn initializes the restic repository automatically.

Trigger a backup now:

```sh
systemctl start bdwn.service
journalctl -u bdwn.service    # view logs
```

Run commands by hand as root:

```sh
bdwn backup          # run a backup + retention
bdwn snapshots       # list snapshots
bdwn list            # list the files held by a snapshot
bdwn restore         # restore files out of a snapshot
bdwn export          # mirror a local repository onto an external device
bdwn zfs-sync        # replicate a ZFS dataset to the remote ZFS server
bdwn check           # verify repository integrity
bdwn restic <args>   # arbitrary restic command
```

`bdwn zfs-sync` is the only command that does not touch the restic repository,
and the only one that runs without `BDWN_REPO`, `RESTIC_PASSWORD` and
`BACKUP_PATHS` being set, see
[ZFS replication](#zfs-replication-bdwn-zfs-sync).

`bdwn export [DIRECTORY]` needs a local `BDWN_REPO` and an external device, see
[Export to an external device](#export-to-an-external-device):

```sh
bdwn export /mnt/usb     # or plain 'bdwn export' with BDWN_EXPORT_UUID set
```

### Listing snapshot contents

`bdwn list [snapshot-id] [path...]` wraps `restic ls --long`. The snapshot
defaults to `latest`: it is inserted automatically when the first argument is
missing, is a path (starts with `/`) or is an option (starts with `-`). Any
other argument is handed to `restic ls`.

```sh
bdwn list                        # files of the latest snapshot
bdwn list /etc/ssh               # descend into a subdirectory of the latest snapshot
bdwn list 4a1b2c3d /var/www      # same, in a given snapshot
bdwn list --tag libvirt-myvm     # latest snapshot carrying that tag
```

### Restoring

`bdwn restore [--snapshot ID] [--target DIR] [path...]` wraps
`restic restore`. `--snapshot` defaults to `latest` and `--target` to
`BDWN_RESTORE_DIR` (itself `/var/backups/bdwn/restore` by default). Each
positional path becomes a restic `--include` pattern, so naming a directory
brings back everything below it; with no path the whole snapshot is restored.

```sh
bdwn restore                                        # whole latest snapshot into the default target
bdwn restore /var/www                               # one directory, into the default target
bdwn restore --target /tmp/restore /etc/ssh         # one directory, elsewhere
bdwn restore --snapshot 4a1b2c3d /var/www           # from a given snapshot
bdwn restore --target / /etc/ssh                    # restore in place, overwriting the live files
```

The target directory is created mode `0700` when missing, because restored
files keep their original content. An existing directory is left untouched, so
`--target /` does not alter the mode of `/`.

Caveats:

- Paths must not contain spaces (word-splitting, same limitation as
  `BACKUP_PATHS`).
- `--target /` overwrites live files. There is no dry run.
- The default target lives under `/var/backups/bdwn`, which `apt purge bdwn`
  removes. Move restored data out of it, or pass a `--target` outside that
  tree.

Use `bdwn restic ...` for anything not wrapped, for example selecting a
snapshot by tag during a restore:

```sh
bdwn restic restore latest --tag libvirt-myvm --target /tmp/restore
```

## Security notes

- The backup runs as root under a hardened systemd unit (`ProtectSystem=strict`,
  `NoNewPrivileges`, syscall filtering, `SocketBindDeny=any`), with the
  capability set reduced to the minimum: `CAP_DAC_READ_SEARCH` (read any path in
  `BACKUP_PATHS`), `CAP_SETUID`/`CAP_SETGID` (privilege drop for SSH) and
  `CAP_KILL` (terminate the ssh child).
- The network-facing ssh process never runs as root: when the repository is
  `sftp:`, `/usr/bin/bdwn` spawns it via `runuser -u bdwn`, using bdwn's SSH
  config, key and known_hosts, with `RESTIC_PASSWORD` stripped from its
  environment so it never sees the passphrase. Run as a non-root user, `bdwn`
  behaves as before (no privilege drop).
- The passphrase is stored in `/etc/bdwn/bdwn.conf` (`0600` root:root, readable
  by root only). Keep this file secret and do not commit it anywhere.
- The relaxations needed by ZFS (`CAP_SYS_ADMIN`, access to `/dev/zfs`) are
  **not** applied to `bdwn.service` by default: they are opt-in via the systemd
  drop-in described in [ZFS backup](#zfs-backup). `bdwn-zfs-sync.service` does
  carry them, since ZFS is all it does; it drops `CAP_DAC_READ_SEARCH` in
  exchange, reads no backup path and never sees the restic passphrase.
- `bdwn zfs-sync` follows the same rule as the SFTP repository: `zfs send` runs
  as root, the ssh process receiving the stream runs as
  `BDWN_ZFS_SYNC_LOCAL_USER` via `runuser`, and `BatchMode=yes` keeps a
  misconfigured connection from hanging on a prompt.
- The remote ZFS server holds unencrypted data unless the source dataset is
  itself encrypted and `BDWN_ZFS_SYNC_SEND_OPTS="-w"` sends a raw stream.
  Unlike the restic repository, the replication target is only as private as
  the remote pool.

## Uninstall

```sh
apt remove bdwn-libvirt   # if installed: stop and disable all bdwn-libvirt@<vm> timers, remove the VM backup script and units (bdwn cannot be removed while bdwn-libvirt depends on it)
apt remove bdwn           # stop and disable the bdwn and bdwn-zfs-sync timers, remove program files
apt purge bdwn            # also remove the bdwn user, /etc/bdwn, /var/lib/bdwn, /var/cache/bdwn, /var/backups/bdwn
```
