# bdwn-server (backup server side)

Server-side tooling for bdwn, shipped as the `bdwn-server` Debian package:

- `bdwn-setup-backup-user` locks down SSH users on the repository server so
  they can only receive bdwn (restic) backups over SFTP, chrooted to a shared
  jail with one private directory per client server.
- `bdwn-export` mirrors those repositories onto an external device with rsync,
  for an offline copy kept out of the building.

The users get no interactive shell, no command execution and no forwarding of
any kind. Access is confined to a chroot jail. Note this is not literally
"upload-only": restic needs read, write and delete inside the repository to
create snapshots and prune old ones. The lockdown restricts *where* the client
can act (its own dir in the jail), not the SFTP operations it can perform
there.

## Install

```sh
apt install ./bdwn-server_*.deb
```

`bdwn-server` is built from the same source tree as the `bdwn` client and
`bdwn-libvirt` (`make deb` produces the three `.deb` files). It does **not**
depend on `bdwn`: a backup server need not be a backup client. Its
dependencies are `openssh-server`, `openssh-client`, `rsync` and `adduser`.

What the package installs:

| Path                                            | Purpose                                             |
| ----------------------------------------------- | --------------------------------------------------- |
| `/usr/bin/bdwn-setup-backup-user`               | Creates one SFTP-only chrooted user per backup client |
| `/usr/bin/bdwn-export`                          | Mirrors the repositories onto an external device      |
| `/etc/bdwn/bdwn-export.conf`                    | Export configuration (`0600` root:root)               |
| `/usr/share/bdwn-server/bdwn-export.conf.example` | Shipped example, source of the seeded config        |
| `/usr/share/bdwn-server/sshd_bdwn-backup.conf.example` | Reference copy of the sshd drop-in, for review only |
| `/lib/systemd/system/bdwn-export.service`       | Oneshot export service (no argument)                  |
| `/lib/systemd/system/bdwn-export.timer`         | Weekly export timer, **not** enabled by the package   |

The `postinst` creates `/etc/bdwn` (`0700` root:root, shared with the `bdwn`
client package when both are installed) and seeds
`/etc/bdwn/bdwn-export.conf` from the example on first install only. It
reloads systemd but enables nothing: the timer only makes sense when the
external device stays plugged in. It then prints a numbered "Next steps" block
summarising the setup (sshd `Include` line, `bdwn-setup-backup-user`, nftables
rule, export, timer).

`apt purge bdwn-server` removes only `/etc/bdwn/bdwn-export.conf`. The
repositories under `/backups/servers`, the backup users and the sshd drop-in
are left untouched on purpose.

Without the package, the script is still runnable straight from the source
tree as `./setup-backup-user.sh` (same behaviour, same arguments). Every
example below uses the installed command name.

## What it does

All backup clients share a single chroot jail at `/backups/servers`. Each
client server gets its own system user, named after the server, member of the
shared `bdwn` group, with a private repository dir inside the jail.

`bdwn-setup-backup-user` performs, in order (run once per client server):

1. Creates the shared system group `bdwn` if missing.
2. Creates the system user `<server-name>`, shell `/usr/sbin/nologin`, member
   of `bdwn`, home set to `/<server-name>` so SFTP lands directly in the
   server's dir after chroot.
3. Creates the chroot dir `/backups/servers` owned `root:root` mode `0755`
   (sshd refuses to chroot into a dir writable by group/others).
4. Creates the per-server repository dir `/backups/servers/<server-name>`
   owned `<server-name>:bdwn`, mode `0700`. This holds the restic repository.
   Inside the chroot it appears as `/<server-name>`.
5. Installs the client public key OUTSIDE the chroot at
   `/etc/ssh/authorized_keys/<server-name>` (owned root, read by sshd before
   chroot).
6. Installs an sshd drop-in at `/etc/ssh/sshd_config.d/bdwn-backup.conf` with a
   `Match Group bdwn` block forcing SFTP-only, chrooted, no-forwarding access.
   One block covers every server user. The block also sets `AllowGroups bdwn`
   so the backup users are accepted even on servers hardened with a global
   `AllowGroups` in `sshd_config` (see Hardening notes).
7. Runs `sshd -t` and reloads ssh.

## Requirements

- Run as `root` on the backup (repository) server.
- OpenSSH server installed and running.
- The main `sshd_config` must keep the Debian default line:

  ```
  Include /etc/ssh/sshd_config.d/*.conf
  ```

  The drop-in that `bdwn-setup-backup-user` writes there is shipped for review
  as `/usr/share/bdwn-server/sshd_bdwn-backup.conf.example`. It is a reference
  copy only: nothing installs it, the script writes the real file itself.

- The client's SSH public key, either as a file copied to this server or
  passed inline as the second argument.

## Usage

1. On the CLIENT (the machine being backed up), generate the key pair used by
   the `bdwn` user:

   ```sh
   ssh-keygen -t ed25519 -f /var/lib/bdwn/.ssh/id_ed25519
   ```

2. Copy the public key to the backup server (optional if you pass the key
   inline in step 3):

   ```sh
   scp /var/lib/bdwn/.ssh/id_ed25519.pub server:/root/id_ed25519.pub
   ```

3. On the SERVER, run the setup command as root, once per client server. The
   first argument is the server name (it becomes the SFTP user name, charset
   `a-z 0-9 _ -`, must not start with `-`). The second argument is the client
   public key, either as a path to the key file:

   ```sh
   bdwn-setup-backup-user myserver /root/id_ed25519.pub
   ```

   or as the key itself, inline (quote it):

   ```sh
   bdwn-setup-backup-user myserver "ssh-ed25519 AAAA... root@myserver"
   ```

   In both cases the key is validated with `ssh-keygen` before any change is
   made to the system; an invalid key aborts the script.

The command prints the exact `BDWN_REPO` value to set on the client when done.
Run it again with a different name and key for each additional client server.

## Resulting layout

Default values (`BACKUP_GROUP=bdwn`, `CHROOT_BASE=/backups/servers`), for a
server named `myserver`:

| Path                                  | Owner           | Mode   | Purpose                                             |
| ------------------------------------- | --------------- | ------ | --------------------------------------------------- |
| `/backups/servers`                    | `root:root`     | `0755` | Shared chroot dir for all backup users. Must not be group/other writable. |
| `/backups/servers/myserver`           | `myserver:bdwn` | `0700` | Restic repository, private to its user. Seen as `/myserver` inside the jail. |
| `/etc/ssh/authorized_keys/myserver`   | `root:root`     | `0644` | Client public key, kept outside the chroot.         |
| `/etc/ssh/sshd_config.d/bdwn-backup.conf` | `root:root` | `0644` | sshd `Match Group` drop-in, shared by all users.    |

## Client-side settings

Because of the shared chroot, the repository root the client sees is
`/<server-name>`, not the absolute path on the server. In
`/etc/bdwn/bdwn.conf` on the client:

```sh
BDWN_REPO="sftp:backup-remote:/myserver"
```

And the matching `Host` entry in `/var/lib/bdwn/.ssh/config` (`User` is the
server name):

```sshconfig
Host backup-remote
    HostName backup.example.com
    Port 22
    User myserver
    IdentityFile /var/lib/bdwn/.ssh/id_ed25519
    IdentitiesOnly yes
```

Record the server host key so restic can connect non-interactively (run as root
on the client):

```sh
ssh-keyscan -p 22 backup.example.com >> /var/lib/bdwn/.ssh/known_hosts
chown bdwn:bdwn /var/lib/bdwn/.ssh/known_hosts
chmod 0600 /var/lib/bdwn/.ssh/known_hosts
```

## Tunables

Override via environment variables when running `bdwn-setup-backup-user`:

| Variable       | Default            | Meaning                                        |
| -------------- | ------------------ | ---------------------------------------------- |
| `BACKUP_GROUP` | `bdwn`             | Shared system group for all backup users.      |
| `CHROOT_BASE`  | `/backups/servers` | Shared chroot jail holding the per-server dirs.|

Example:

```sh
CHROOT_BASE=/data/backups bdwn-setup-backup-user myserver id_ed25519.pub
```

## Export to an external device

`bdwn-export [--target DIR] [SERVER...]` mirrors the per-client restic
repositories held in the chroot jail onto an external device, with
`rsync -a --delete --stats`, one subdirectory per client. Run it as root **on
the backup server** (it may mount a device and reads every repository).

No passphrase is involved: rsync copies bytes. The backup server never holds
the client passphrases and does not need them to make the offline copy.

With no `SERVER` argument, every repository under the jail is mirrored. Name
one or more servers to mirror only those. `bdwn-export` refuses unknown names,
names outside the charset `a-z 0-9 _ -`, and directories holding no restic
`config` file.

After the copy it runs `sync` and prints the restore command.

### With a device you mount yourself

`--target DIR` uses a directory you mounted. The directory must **already** be
a mount point, otherwise `bdwn-export` refuses to run (guard against mirroring
onto the root filesystem). You unmount it yourself too:

```sh
mount /dev/sdb1 /mnt/usb
bdwn-export --target /mnt/usb                  # every repository
bdwn-export --target /mnt/usb myserver other   # only these two
umount /mnt/usb
```

### With a device bdwn-export mounts itself

Without `--target`, `BDWN_EXPORT_UUID` must be set in
`/etc/bdwn/bdwn-export.conf`. `bdwn-export` mounts
`/dev/disk/by-uuid/<UUID>` on `BDWN_EXPORT_MOUNT`, mirrors, then unmounts,
including when the export fails. A mount point already mounted is left
mounted:

```sh
lsblk -o NAME,SIZE,FSTYPE,UUID          # find the UUID of the external device

cat >> /etc/bdwn/bdwn-export.conf <<'EOF'
BDWN_EXPORT_UUID="1234abcd-56ef-78ab-90cd-1234567890ab"
EOF

bdwn-export                             # mount, mirror, unmount
```

### Configuration

`/etc/bdwn/bdwn-export.conf` is plain shell syntax (sourced by
`bdwn-export`), so use no spaces around `=`. Every key is optional, and so is
the file itself. It holds no passphrase.

| Variable             | Default             | Meaning                                                                                       |
| -------------------- | ------------------- | --------------------------------------------------------------------------------------------- |
| `BDWN_SERVER_ROOT`   | `/backups/servers`  | Chroot jail holding one repository per client. Must match `CHROOT_BASE` if you changed it.     |
| `BDWN_EXPORT_UUID`   | unset               | Filesystem UUID of the external device. Required only when `bdwn-export` runs without `--target`. |
| `BDWN_EXPORT_MOUNT`  | `/mnt/bdwn-export`  | Mount point used for `BDWN_EXPORT_UUID`.                                                       |
| `BDWN_EXPORT_SUBDIR` | `bdwn-repos`        | Directory created on the device to hold the repositories.                                     |

The environment variable `BDWN_EXPORT_CONFIG` overrides the config file path:

```sh
BDWN_EXPORT_CONFIG=/root/export-usb.conf bdwn-export
```

### Destination layout

On the device, for `BDWN_EXPORT_MOUNT=/mnt/bdwn-export`, the default subdir
and a client named `myserver`:

| Path                                    | Mode   | Purpose                                             |
| --------------------------------------- | ------ | --------------------------------------------------- |
| `/mnt/bdwn-export/bdwn-repos`           | `0700` | Root of the mirror on the device.                   |
| `/mnt/bdwn-export/bdwn-repos/myserver`  | `0700` | Byte-for-byte copy of that client's restic repository. |

Each copy is a standalone restic repository. Restore from it later, on any
machine with restic, using that client's own passphrase (restic prompts for
it):

```sh
restic -r /mnt/bdwn-export/bdwn-repos/myserver restore latest --target /tmp/restore
```

### Timer

`bdwn-export.service` is a oneshot unit calling `/usr/bin/bdwn-export` with no
argument, so it mirrors every repository and **requires** `BDWN_EXPORT_UUID`
to be set. `bdwn-export.timer` is weekly (`Persistent=true`,
`RandomizedDelaySec=900`) and disabled by default. Enable it only if the
external device stays permanently plugged in:

```sh
systemctl enable --now bdwn-export.timer
journalctl -u bdwn-export.service
```

Mounting needs the `@mount` syscalls and a writable mount point, so
`bdwn-export.service` is deliberately less sandboxed than `bdwn.service`: no
`ProtectSystem`, no `SystemCallFilter`. It still keeps `ProtectHome=yes`
(which also gives it a private mount namespace, so the mount is torn down with
the unit even if `bdwn-export` is killed), `PrivateNetwork=yes`,
`RestrictAddressFamilies=AF_UNIX`, `NoNewPrivileges=yes`,
`ProtectKernelModules`/`ProtectKernelLogs`/`ProtectControlGroups`,
`ProtectClock`, `ProtectHostname`, `ProtectProc=invisible`, `ProcSubset=pid`,
`RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`,
`MemoryDenyWriteExecute`, `RemoveIPC`, `UMask=0077` and
`KeyringMode=private`.

### Caveats

- `--delete` makes the destination a mirror, not an archive: a repository
  pruned or lost on the server loses the same data on the device at the next
  export.
- Server names come from directory names under `BDWN_SERVER_ROOT`, so a
  directory that is not a restic repository (no `config` file) aborts the run
  when named explicitly.
- The mirror is unencrypted only in the sense that `bdwn-export` adds nothing:
  the restic repositories themselves stay encrypted with each client's
  passphrase, which is not stored anywhere on the backup server. Lose a client
  passphrase and its copy on the device is unreadable.

## Hardening notes

The `Match Group bdwn` block disables the shell, PTY, and all forwarding
(`ForceCommand internal-sftp`, `PermitTTY no`, `X11Forwarding no`,
`AllowTcpForwarding no`, `AllowAgentForwarding no`, `PermitTunnel no`), so the
users can only speak SFTP inside the jail.

The block also sets `AllowGroups bdwn`. On servers hardened with a global
`AllowGroups` directive, the backup users would otherwise be rejected at login
("not allowed because none of user's groups are listed in AllowGroups"). The
drop-in is included at the top of Debian's `sshd_config` and the first obtained
value wins, so this overrides the global list for the `bdwn` group only; all
other connections keep the global policy. Caveat: if the server uses a global
`AllowUsers` directive instead, the backup users must be added to it manually,
as the drop-in cannot list them.

All server users share the same jail: they can list the top-level directory
names of the other servers, but the `0700` mode on each per-server dir stops
them from entering or reading any repository other than their own.

Optional: to protect existing snapshots against deletion (append-only style),
you would need to prevent the SFTP user from removing files in its dir, in
which case retention/prune must be disabled on the client (unset the
`BDWN_KEEP_*` values), since pruning requires delete access.
