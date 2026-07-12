# bdwn (BackDown)

Encrypted server backups to a remote SFTP repository using restic, driven by a
systemd timer running under a hardened unit.

bdwn backs up a list of directories you choose to a remote SFTP server. Backups
are stored in restic format, encrypted with a passphrase. A daily systemd timer
runs the backup as root with a reduced capability set (so any path can be read),
dropping privileges to the unprivileged `bdwn` user for the SSH/SFTP connection,
made with a dedicated key. Retention (daily/weekly/monthly) is applied
automatically after each run. ZFS pools/datasets/volumes can optionally be
exported with `zfs send` and included in the backup.

## Dependencies

All are standard Debian packages, declared in `debian/control` and pulled in
automatically on install:

- `restic` - backup engine
- `openssh-client` - SSH/SFTP transport
- `adduser` - creates the `bdwn` system user

Optional (declared as a suggestion, not installed automatically):

- `zfsutils-linux` - only needed for [ZFS backup](#zfs-backup)

## Build

```sh
make deb      # build the package -> ./bdwn_<version>_all.deb
make help     # list available targets
make clean    # remove build artifacts
```

## Install

```sh
apt install ./bdwn_*.deb
```

The package `postinst` performs first-install setup:

- Creates the `bdwn` system group and user (home `/var/lib/bdwn`, shell
  `/usr/sbin/nologin`).
- Seeds `/etc/bdwn/bdwn.conf` from the example, owned `root:root`, mode `0600`
  (directory `/etc/bdwn` is `root:root`, `0700`). On upgrade from older
  versions, the looser `0640` root:bdwn permissions are tightened.
- Seeds `/var/lib/bdwn/.ssh/config` from the example, owned `bdwn:bdwn`,
  mode `0600`.
- Reloads systemd so it sees the units. The `bdwn.timer` is **not** enabled
  automatically: you enable it yourself once the config is ready.

Existing config and SSH files are never overwritten on upgrade.

After install you must edit the config, set up the SSH key, test a backup, then
enable the timer (both as root):

```
bdwn backup                          # test once
systemctl enable --now bdwn.timer    # start the daily schedule
```

## Configuration

Edit `/etc/bdwn/bdwn.conf`. It is plain shell syntax (sourced by the CLI), so
use no spaces around `=`.

| Variable            | Required | Meaning                                                                                      |
| ------------------- | -------- | -------------------------------------------------------------------------------------------- |
| `BDWN_REPO`         | yes      | Restic repository. Form `sftp:<ssh-host>:/path`, where `<ssh-host>` is a `Host` entry in the SSH config. |
| `RESTIC_PASSWORD`   | yes      | Encryption passphrase for the repository. Keep it secret.                                    |
| `BACKUP_PATHS`      | yes      | Space-separated directories/files to back up. Any path works (the service reads as root).    |
| `BDWN_EXCLUDES`     | no       | Space-separated exclude patterns passed to restic.                                           |
| `BDWN_KEEP_DAILY`   | no       | Daily snapshots to keep. Retention runs only if any keep value is set.                       |
| `BDWN_KEEP_WEEKLY`  | no       | Weekly snapshots to keep.                                                                     |
| `BDWN_KEEP_MONTHLY` | no       | Monthly snapshots to keep.                                                                    |
| `BDWN_CACHE_DIR`    | no       | Restic cache directory. Defaults to `/var/cache/bdwn`.                                        |
| `BDWN_ZFS_DATASETS` | no       | Space-separated ZFS pools/datasets/volumes to export and back up. See [ZFS backup](#zfs-backup). |
| `BDWN_ZFS_DUMP_DIR` | no       | Directory receiving the ZFS dumps. Defaults to `/var/backups/bdwn/zfs`. If changed, adjust `ReadWritePaths` in the systemd drop-in. |

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
bdwn restic restore latest --target /tmp/restore --include /var/backups/bdwn/zfs
zfs receive tank/vms/disk1 < /tmp/restore/var/backups/bdwn/zfs/tank_vms_disk1.zfs
```

## SSH setup

`BDWN_REPO` connects through a `Host` entry in `/var/lib/bdwn/.ssh/config`. To
prepare the connection:

1. Place the private key at `/var/lib/bdwn/.ssh/id_ed25519`:

   ```sh
   chown bdwn:bdwn /var/lib/bdwn/.ssh/id_ed25519
   chmod 0600 /var/lib/bdwn/.ssh/id_ed25519
   ```

2. Edit `/var/lib/bdwn/.ssh/config` to match your remote host. `User` is the
   server name passed to `setup-backup-user.sh` (e.g. `myserver`):

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
only. The `server/` directory ships `setup-backup-user.sh`, which creates a
system user per client server, named after it, chrooted to the shared
`/backups/servers` jail with no shell, no command execution and no forwarding:
it can only read/write its own restic repository over SFTP. Run it as root on
the backup server, once per client, with the server name and the client public
key:

```sh
./setup-backup-user.sh myserver id_ed25519.pub
```

The public key can also be passed inline instead of a file path, e.g.
`./setup-backup-user.sh myserver "ssh-ed25519 AAAA... root@myserver"`.

Because of the shared chroot, the repository path the client sees is
`/<SERVER_NAME>` (its private dir inside the jail), regardless of where it
lives on the backup server. With this setup, use:

```sh
BDWN_REPO="sftp:backup-remote:/$(hostname)"
```

See [server/README.md](server/README.md) for the full server-side procedure.

## Layout on the target system

| Path                              | Purpose                                          |
| --------------------------------- | ------------------------------------------------ |
| `/usr/bin/bdwn`                   | CLI wrapper around restic                        |
| `/etc/bdwn/bdwn.conf`             | Configuration (`0600` root:root)                 |
| `/var/lib/bdwn`                   | `bdwn` user home, holds `.ssh/` (config, key, known_hosts) |
| `/var/cache/bdwn`                 | Restic cache                                      |
| `/lib/systemd/system/bdwn.service`| Backup service (oneshot, hardened)               |
| `/lib/systemd/system/bdwn.timer`  | Daily timer                                       |
| `/usr/share/bdwn/*.example`       | Example config, SSH config and ZFS systemd drop-in |
| `/var/backups/bdwn`               | ZFS dump area (`0700` root:root, created by postinst, removed on purge) |

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
bdwn check           # verify repository integrity
bdwn restic <args>   # arbitrary restic command
```

Use `bdwn restic ...` for anything not wrapped, for example a restore:

```sh
bdwn restic restore latest --target /tmp/restore
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
  **not** applied by default: they are opt-in via the systemd drop-in described
  in [ZFS backup](#zfs-backup).

## Uninstall

```sh
apt remove bdwn     # stop and disable the timer, remove program files
apt purge bdwn      # also remove the bdwn user, /etc/bdwn, /var/lib/bdwn, /var/cache/bdwn, /var/backups/bdwn
```
