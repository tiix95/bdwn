# bdwn backup server setup

Locks down SSH users on the remote repository server so they can only receive
bdwn (restic) backups over SFTP, chrooted to a shared jail with one private
directory per client server.

The users get no interactive shell, no command execution and no forwarding of
any kind. Access is confined to a chroot jail. Note this is not literally
"upload-only": restic needs read, write and delete inside the repository to
create snapshots and prune old ones. The lockdown restricts *where* the client
can act (its own dir in the jail), not the SFTP operations it can perform
there.

## What it does

All backup clients share a single chroot jail at `/backups/servers`. Each
client server gets its own system user, named after the server, member of the
shared `bdwn` group, with a private repository dir inside the jail.

`setup-backup-user.sh` performs, in order (run once per client server):

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
   One block covers every server user.
7. Runs `sshd -t` and reloads ssh.

## Requirements

- Run as `root` on the backup (repository) server.
- OpenSSH server installed and running.
- The main `sshd_config` must keep the Debian default line:

  ```
  Include /etc/ssh/sshd_config.d/*.conf
  ```

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

3. On the SERVER, run the setup script as root, once per client server. The
   first argument is the server name (it becomes the SFTP user name, charset
   `a-z 0-9 _ -`, must not start with `-`). The second argument is the client
   public key, either as a path to the key file:

   ```sh
   ./setup-backup-user.sh myserver /root/id_ed25519.pub
   ```

   or as the key itself, inline (quote it):

   ```sh
   ./setup-backup-user.sh myserver "ssh-ed25519 AAAA... root@myserver"
   ```

   In both cases the key is validated with `ssh-keygen` before any change is
   made to the system; an invalid key aborts the script.

The script prints the exact `BDWN_REPO` value to set on the client when done.
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

Override via environment variables when running the script:

| Variable       | Default            | Meaning                                        |
| -------------- | ------------------ | ---------------------------------------------- |
| `BACKUP_GROUP` | `bdwn`             | Shared system group for all backup users.      |
| `CHROOT_BASE`  | `/backups/servers` | Shared chroot jail holding the per-server dirs.|

Example:

```sh
CHROOT_BASE=/data/backups ./setup-backup-user.sh myserver id_ed25519.pub
```

## Hardening notes

The `Match Group bdwn` block disables the shell, PTY, and all forwarding
(`ForceCommand internal-sftp`, `PermitTTY no`, `X11Forwarding no`,
`AllowTcpForwarding no`, `AllowAgentForwarding no`, `PermitTunnel no`), so the
users can only speak SFTP inside the jail.

All server users share the same jail: they can list the top-level directory
names of the other servers, but the `0700` mode on each per-server dir stops
them from entering or reading any repository other than their own.

Optional: to protect existing snapshots against deletion (append-only style),
you would need to prevent the SFTP user from removing files in its dir, in
which case retention/prune must be disabled on the client (unset the
`BDWN_KEEP_*` values), since pruning requires delete access.
