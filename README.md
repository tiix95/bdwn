# bdwn (BackDown)

Encrypted server backups to a remote SFTP repository using restic, driven by a
systemd timer running as an unprivileged system user.

bdwn backs up a list of directories you choose to a remote SFTP server. Backups
are stored in restic format, encrypted with a passphrase. A daily systemd timer
runs the backup as the unprivileged `bdwn` user, connecting over SSH with a
dedicated key. Retention (daily/weekly/monthly) is applied automatically after
each run.

## Dependencies

All are standard Debian packages, declared in `debian/control` and pulled in
automatically on install:

- `restic` - backup engine
- `openssh-client` - SSH/SFTP transport
- `adduser` - creates the `bdwn` system user

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
- Seeds `/etc/bdwn/bdwn.conf` from the example, owned `root:bdwn`, mode `0640`.
- Seeds `/var/lib/bdwn/.ssh/config` from the example, owned `bdwn:bdwn`,
  mode `0600`.
- Reloads systemd so it sees the units. The `bdwn.timer` is **not** enabled
  automatically: you enable it yourself once the config is ready.

Existing config and SSH files are never overwritten on upgrade.

After install you must edit the config, set up the SSH key, test a backup, then
enable the timer:

```
sudo -u bdwn HOME=/var/lib/bdwn bdwn backup   # test once
systemctl enable --now bdwn.timer             # start the daily schedule
```

## Configuration

Edit `/etc/bdwn/bdwn.conf`. It is plain shell syntax (sourced by the CLI), so
use no spaces around `=`.

| Variable            | Required | Meaning                                                                                      |
| ------------------- | -------- | -------------------------------------------------------------------------------------------- |
| `BDWN_REPO`         | yes      | Restic repository. Form `sftp:<ssh-host>:/path`, where `<ssh-host>` is a `Host` entry in the SSH config. |
| `RESTIC_PASSWORD`   | yes      | Encryption passphrase for the repository. Keep it secret.                                    |
| `BACKUP_PATHS`      | yes      | Space-separated directories/files to back up. Must be readable by the `bdwn` user.           |
| `BDWN_EXCLUDES`     | no       | Space-separated exclude patterns passed to restic.                                           |
| `BDWN_KEEP_DAILY`   | no       | Daily snapshots to keep. Retention runs only if any keep value is set.                       |
| `BDWN_KEEP_WEEKLY`  | no       | Weekly snapshots to keep.                                                                     |
| `BDWN_KEEP_MONTHLY` | no       | Monthly snapshots to keep.                                                                    |
| `BDWN_CACHE_DIR`    | no       | Restic cache directory. Defaults to `/var/cache/bdwn`.                                        |

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
| `/etc/bdwn/bdwn.conf`             | Configuration (`0640` root:bdwn)                 |
| `/var/lib/bdwn`                   | `bdwn` user home, holds `.ssh/` (config, key, known_hosts) |
| `/var/cache/bdwn`                 | Restic cache                                      |
| `/lib/systemd/system/bdwn.service`| Backup service (oneshot, hardened)               |
| `/lib/systemd/system/bdwn.timer`  | Daily timer                                       |
| `/usr/share/bdwn/*.example`       | Example config and SSH config                    |

## Usage

Once enabled (`systemctl enable --now bdwn.timer`), the timer runs a backup
daily with a randomized delay and no further manual action. On the first backup,
bdwn initializes the restic repository automatically.

Trigger a backup now:

```sh
systemctl start bdwn.service
journalctl -u bdwn.service    # view logs
```

Run commands by hand as the `bdwn` user (set `HOME` so SSH finds its config):

```sh
sudo -u bdwn HOME=/var/lib/bdwn bdwn backup       # run a backup + retention
sudo -u bdwn HOME=/var/lib/bdwn bdwn snapshots    # list snapshots
sudo -u bdwn HOME=/var/lib/bdwn bdwn check        # verify repository integrity
sudo -u bdwn HOME=/var/lib/bdwn bdwn restic <args># arbitrary restic command
```

Use `bdwn restic ...` for anything not wrapped, for example a restore:

```sh
sudo -u bdwn HOME=/var/lib/bdwn bdwn restic restore latest --target /tmp/restore
```

## Security notes

- The backup runs as the unprivileged `bdwn` user under a hardened systemd unit
  (`ProtectSystem=strict`, empty capability set, `NoNewPrivileges`, syscall
  filtering).
- Every path in `BACKUP_PATHS` must be readable by the `bdwn` user. Files it
  cannot read are skipped.
- The passphrase is stored in `/etc/bdwn/bdwn.conf` (`0640` root:bdwn). Keep
  this file secret and do not commit it anywhere.
- To back up root-only files, grant the service read access with a systemd
  override (advanced, optional):

  ```sh
  systemctl edit bdwn.service
  ```

  ```ini
  [Service]
  CapabilityBoundingSet=CAP_DAC_READ_SEARCH
  AmbientCapabilities=CAP_DAC_READ_SEARCH
  ```

  This lets restic read any file regardless of permissions. Grant it only if you
  understand the trade-off.

## Uninstall

```sh
apt remove bdwn     # stop and disable the timer, remove program files
apt purge bdwn      # also remove the bdwn user, /etc/bdwn, /var/lib/bdwn, /var/cache/bdwn
```
