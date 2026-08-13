# bdwn-server (backup server side)

Server-side tooling for bdwn, shipped as the `bdwn-server` Debian package:

- `bdwn-setup-backup-user` locks down SSH users on the repository server so
  they can only receive bdwn (restic) backups over SFTP, chrooted to a shared
  jail with one private directory per client server.
- `bdwn-export` mirrors those repositories onto an external device with rsync,
  for an offline copy kept out of the building.
- `bdwn-webui` serves a read-only status page showing the state, the size and
  the last snapshot date of every repository, plus a JSON and a Prometheus
  endpoint.

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
dependencies are `openssh-server`, `openssh-client`, `rsync`, `adduser`,
`python3` and `python3-jinja2` (the last two for the web UI only).

What the package installs:

| Path                                            | Purpose                                             |
| ----------------------------------------------- | --------------------------------------------------- |
| `/usr/bin/bdwn-setup-backup-user`               | Creates one SFTP-only chrooted user per backup client |
| `/usr/bin/bdwn-export`                          | Mirrors the repositories onto an external device      |
| `/usr/bin/bdwn-webui`                           | Status web UI (wrapper around `python3 -m bdwn_webui`) |
| `/usr/lib/python3/dist-packages/bdwn_webui/`    | Python module of the web UI, with its HTML template   |
| `/etc/bdwn/bdwn-export.conf`                    | Export configuration (`0600` root:root)               |
| `/etc/bdwn/bdwn-webui.conf`                     | Web UI configuration (`0640` root:bdwn-webui)         |
| `/usr/share/bdwn-server/bdwn-export.conf.example` | Shipped example, source of the seeded config        |
| `/usr/share/bdwn-server/bdwn-webui.conf.example`  | Shipped example, source of the seeded web UI config |
| `/usr/share/bdwn-server/sshd_bdwn-backup.conf.example` | Reference copy of the sshd drop-in, for review only |
| `/usr/share/bdwn-server/webui-tcp-dropin.conf.example` | systemd drop-in to let the web UI listen on TCP |
| `/usr/share/bdwn-server/nginx-bdwn-webui.conf.example` | Reverse proxy for the web UI (TLS, auth, rate limit) |
| `/lib/systemd/system/bdwn-export.service`       | Oneshot export service (no argument)                  |
| `/lib/systemd/system/bdwn-export.timer`         | Weekly export timer, **not** enabled by the package   |
| `/lib/systemd/system/bdwn-webui.service`        | Status web UI service, **not** enabled by the package |

The `postinst` creates `/etc/bdwn` (`0700` root:root, shared with the `bdwn`
client package when both are installed) and seeds
`/etc/bdwn/bdwn-export.conf` and `/etc/bdwn/bdwn-webui.conf` from the examples
on first install only. It creates the `bdwn-webui` system user and group that
run the web UI. It reloads systemd but enables nothing: the export timer only
makes sense when the external device stays plugged in, and the web UI is
started once its config suits you. It then prints a numbered "Next steps" block
summarising the setup (sshd `Include` line, `bdwn-setup-backup-user`, nftables
rule, export, timer, web UI).

`apt purge bdwn-server` removes only `/etc/bdwn/bdwn-export.conf`,
`/etc/bdwn/bdwn-webui.conf` and the `bdwn-webui` user and group. The
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

## Status web UI

`bdwn-webui` serves one read-only page listing every repository under the
jail, or under each jail when there are several: state, size, number of
snapshots, date of the last one. It is meant to answer the only question a
backup server is really asked: did every client push last night, and how much
does it weigh.

It never opens a repository. The repositories are encrypted and this server
holds no passphrase, so everything on the page comes from the filesystem:

| Shown                | Read from                                              |
| -------------------- | ------------------------------------------------------ |
| Repository size      | Sum of the file sizes under `<jail>/<server>/`          |
| Last backup          | mtime of the newest file in `<jail>/<server>/snapshots/` |
| Number of snapshots  | Number of files in `<jail>/<server>/snapshots/`         |
| `[busy]` marker      | A file in `<jail>/<server>/locks/`, so restic is running (or a lock was left behind by a crash) |
| Volume usage         | `statvfs` on each jail, one tile per filesystem          |
| Free space           | Same                                                    |

What it can **not** show, by construction: the list of backed up files, the
snapshot tags or hosts, and whether the data is sound (`restic check`). All of
these need the client's passphrase, which never reaches this server. Restoring
still happens on the client, or on any machine holding the passphrase.

Each repository gets one of these states:

| State         | Meaning                                                        |
| ------------- | -------------------------------------------------------------- |
| `ok`          | Last snapshot newer than `BDWN_WEBUI_STALE_HOURS` (default 26h) |
| `late`        | Older than that, but newer than `BDWN_WEBUI_CRITICAL_HOURS` (default 72h) |
| `stale`       | Older than `BDWN_WEBUI_CRITICAL_HOURS`: this client stopped backing up |
| `no snapshot` | Repository initialised, no snapshot in it yet                   |
| `no repo`     | User declared by `bdwn-setup-backup-user`, restic never ran     |
| `error`       | The directory could not be read                                 |

### Several jails

Repositories spread over more than one filesystem are listed on the same
page: give `BDWN_SERVER_ROOT` every jail, space separated, exactly as the
client config lists `BACKUP_PATHS`.

```sh
BDWN_SERVER_ROOT="/backups/servers /mnt/tank/servers"
```

What changes with more than one jail:

- The table gains a `jail` column, and the header lists every configured jail.
- Two jails may hold a repository of the **same server name**: they are two
  distinct lines, told apart by their jail. In `/metrics` they are two
  distinct series, since every repository metric carries both a `server` and
  a `root` label.
- One volume tile per **filesystem**, not per jail: two jails on the same
  filesystem are reported once.
- A jail that cannot be read (unmounted disk, typo) shows as a red banner at
  the top of the page and does not stop the others from being scanned.

Note that `bdwn-export` still mirrors a single jail per run, the one in
`BDWN_SERVER_ROOT` from `/etc/bdwn/bdwn-export.conf`. With several jails, run
it once per jail with `BDWN_EXPORT_CONFIG` pointing at a config file per jail.

### Start it and reach it

By default it listens on the unix socket `/run/bdwn-webui/bdwn-webui.sock`,
which exposes nothing on the network:

```sh
systemctl enable --now bdwn-webui
journalctl -u bdwn-webui
```

Read it from a workstation over SSH, with no proxy and no open port (OpenSSH
forwards a local port to a remote unix socket):

```sh
ssh -L 8080:/run/bdwn-webui/bdwn-webui.sock root@backup-server
```

then open `http://localhost:8080/`. Straight from the server:

```sh
curl --unix-socket /run/bdwn-webui/bdwn-webui.sock http://localhost/api/status
```

### Behind a reverse proxy

The page has **no authentication of its own**. Anything beyond the unix socket
(TCP, reverse proxy) must be protected by the proxy or the network. A complete
nginx server block is shipped with the package: TLS, network allowlist, basic
auth (or client certificates), rate limiting, `/metrics` restricted to the
scraper, everything else answering 404.

```sh
cp /usr/share/bdwn-server/nginx-bdwn-webui.conf.example \
   /etc/nginx/sites-available/bdwn-webui
# adapt listen address, server_name, certificates and allowed networks, then
adduser www-data bdwn-webui                # the socket is 0660 bdwn-webui:bdwn-webui
apt install apache2-utils
htpasswd -B -c /etc/nginx/bdwn-webui.htpasswd admin
chown root:www-data /etc/nginx/bdwn-webui.htpasswd
chmod 0640 /etc/nginx/bdwn-webui.htpasswd
ln -s ../sites-available/bdwn-webui /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

Matching nftables rule:

```nft
iifname $main_if ip daddr $main_ip ip saddr 192.168.7.0/24 tcp dport 443 \
    ct state new counter accept comment "bdwn-webui"
```

Two things not to miss:

- `adduser www-data bdwn-webui` is **required**, otherwise nginx answers 502:
  the socket is `0660`, not `0666`.
- Do not add `X-Frame-Options` or a `Content-Security-Policy` in the proxy.
  `add_header` does not replace the header sent by bdwn-webui, it duplicates
  it, and a browser given two `Content-Security-Policy` headers applies the
  intersection of both, which breaks the page. The shipped example only adds
  HSTS, which the application cannot know about.

### Listening on TCP instead

Set `BDWN_WEBUI_LISTEN` to a `host:port` and install the shipped drop-in, which
gives the unit the network stack its default sandbox denies:

```sh
install -d -m 0755 /etc/systemd/system/bdwn-webui.service.d
cp /usr/share/bdwn-server/webui-tcp-dropin.conf.example \
   /etc/systemd/system/bdwn-webui.service.d/tcp.conf
sed -i 's|^# BDWN_WEBUI_LISTEN="127.0.0.1:8080"|BDWN_WEBUI_LISTEN="127.0.0.1:8080"|' \
   /etc/bdwn/bdwn-webui.conf
systemctl daemon-reload && systemctl restart bdwn-webui
```

Use a port above 1024: the service holds no `CAP_NET_BIND_SERVICE`.

### Endpoints

| Route            | Content                                                          |
| ---------------- | ----------------------------------------------------------------- |
| `/`              | The status page. Reloads itself every `BDWN_WEBUI_CACHE_TTL` seconds |
| `/?refresh=1`    | Same page, forcing a fresh scan instead of the cached report        |
| `/api/status`    | The same report as JSON (totals, volume, one object per repository) |
| `/metrics`       | The same report in the Prometheus text format                      |

The **refresh** button at the top right of the page points to `/?refresh=1`
and spins a wheel while the answer is being built. Two scans forced this way
are never closer than 10 seconds apart, whatever the button is subjected to;
the automatic reload of the page always uses the cached path. `?refresh=1`
works on `/api/status` too.

The button is the only reason the page carries a script: four lines, inlined,
which just set a class on click. The `Content-Security-Policy` pins their
sha256 hash, so the policy is as tight as it was with no script at all, and
the button still works (without the wheel) if scripts are blocked.

Only `GET` and `HEAD` are served; anything else is refused. Scraping
`/metrics` gives alerting on backups that stopped arriving, again without any
passphrase:

```yaml
# Backup missing for more than three days
- alert: BdwnRepositoryStale
  expr: time() - bdwn_repo_last_snapshot_timestamp_seconds > 3 * 86400
  labels: { severity: warning }
```

Exported metrics: `bdwn_repo_size_bytes`, `bdwn_repo_files`,
`bdwn_repo_snapshots`, `bdwn_repo_locks`,
`bdwn_repo_last_snapshot_timestamp_seconds`, `bdwn_repo_status`
(`1` on the current status of each server), `bdwn_volume_size_bytes`,
`bdwn_volume_free_bytes`, `bdwn_scan_duration_seconds` and
`bdwn_scan_timestamp_seconds`. All repository metrics carry a `server` and a
`root` label (the jail it was found in), the volume ones a `path` label, one
sample per filesystem.

The JSON of `/api/status` holds `server_roots`, `volumes` and `errors` as
lists, and each repository carries its `root`. A scraper written against the
single-jail shape (`server_root`, `volume`, `error`) needs updating.

### Configuration

`/etc/bdwn/bdwn-webui.conf`, same plain shell syntax as the other bdwn config
files (no spaces around `=`). Every key is optional, and so is the file. It
holds no passphrase.

| Variable                   | Default                              | Meaning                                                    |
| -------------------------- | ------------------------------------ | ---------------------------------------------------------- |
| `BDWN_SERVER_ROOT`         | `/backups/servers`                   | Chroot jail holding the repositories, or several of them separated by spaces. Must match `CHROOT_BASE` if you changed it. |
| `BDWN_WEBUI_LISTEN`        | `unix:/run/bdwn-webui/bdwn-webui.sock` | Unix socket (`unix:/path`) or TCP address (`host:port`).  |
| `BDWN_WEBUI_SOCKET_MODE`   | `0660`                               | Mode of the unix socket once bound.                        |
| `BDWN_WEBUI_CACHE_TTL`     | `300`                                | Seconds a scan is kept before being redone. Also the reload interval of the page. |
| `BDWN_WEBUI_STALE_HOURS`   | `26`                                 | Age of the last snapshot above which a repository is `late`. |
| `BDWN_WEBUI_CRITICAL_HOURS`| `72`                                 | Age above which it is `stale`.                              |

Command line, mostly for debugging (`--config` reads another file, `--listen`
overrides the address, `--dump` prints one report as JSON and exits):

```sh
bdwn-webui --dump | jq '.repositories[] | {name, status, size_bytes}'
bdwn-webui --listen 127.0.0.1:8080          # foreground, ignores the sandbox
```

### Cost of a scan

One scan walks every file of every repository (`stat` only, nothing is read),
so it is cached for `BDWN_WEBUI_CACHE_TTL` seconds and shared by all requests
and all endpoints. The page shows how long the last scan took. On a server
holding millions of pack files, raise the TTL rather than the hardware.

### Permissions

The repositories are `0700` and owned by each backup client, so being in the
`bdwn` group is not enough to read them. The service runs as the unprivileged
`bdwn-webui` user with the single capability `CAP_DAC_READ_SEARCH`, which
bypasses read and traverse checks but grants **no write access anywhere**. The
unit hides the two files that capability has no business reaching when the
host is also a backup client (`InaccessiblePaths`): `/etc/bdwn/bdwn.conf`
(client passphrase) and `/var/lib/bdwn/.ssh`.

The rest of the sandbox: `ProtectSystem=strict` (the whole filesystem is
read-only, only `/run/bdwn-webui` is writable), `PrivateNetwork=yes` with
`RestrictAddressFamilies=AF_UNIX` in the default unix-socket mode,
`NoNewPrivileges=yes`, `SystemCallFilter=@system-service` minus the dangerous
groups, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectProc=invisible`,
`ProcSubset=pid`, `RestrictNamespaces`, `MemoryDenyWriteExecute`, `UMask=0077`.

Symbolic links are never followed while scanning. A backup client owns its
directory over SFTP and could drop a link to `/` in it; following it would
report a nonsense size and read outside the jail.

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
