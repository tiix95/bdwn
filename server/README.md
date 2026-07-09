# bdwn backup server setup

Locks down an SSH user on the remote repository server so it can only receive
bdwn (restic) backups over SFTP, chrooted to its own directory.

The user gets no interactive shell, no command execution and no forwarding of
any kind. Access is confined to a chroot jail. Note this is not literally
"upload-only": restic needs read, write and delete inside the repository to
create snapshots and prune old ones. The lockdown restricts *where* the client
can act (the jail), not the SFTP operations it can perform there.

## What it does

`setup-backup-user.sh` performs, in order:

1. Creates a system group and user (default `backup`), shell `/usr/sbin/nologin`,
   home set to the chroot dir.
2. Creates the chroot dir owned `root:root` mode `0755` (sshd refuses to chroot
   into a dir writable by group/others).
3. Creates a writable `data` subdir owned by the user, mode `0700`. This holds
   the restic repository. Inside the chroot it appears as `/data`.
4. Installs the client public key OUTSIDE the chroot at
   `/etc/ssh/authorized_keys/<user>` (owned root, read by sshd before chroot).
5. Installs an sshd drop-in at `/etc/ssh/sshd_config.d/bdwn-backup.conf` with a
   `Match User` block forcing SFTP-only, chrooted, no-forwarding access.
6. Runs `sshd -t` and reloads ssh.

## Requirements

- Run as `root` on the backup (repository) server.
- OpenSSH server installed and running.
- The main `sshd_config` must keep the Debian default line:

  ```
  Include /etc/ssh/sshd_config.d/*.conf
  ```

- The client's SSH public key file, copied to this server.

## Usage

1. On the CLIENT (the machine being backed up), generate the key pair used by
   the `bdwn` user:

   ```sh
   ssh-keygen -t ed25519 -f /var/lib/bdwn/.ssh/id_ed25519
   ```

2. Copy the public key to the backup server:

   ```sh
   scp /var/lib/bdwn/.ssh/id_ed25519.pub server:/root/id_ed25519.pub
   ```

3. On the SERVER, run the setup script as root:

   ```sh
   ./setup-backup-user.sh /root/id_ed25519.pub
   ```

The script prints the exact `BDWN_REPO` value to set on the client when done.

## Resulting layout

Default values (`BACKUP_USER=backup`, `CHROOT_BASE=/srv/backups`):

| Path                                  | Owner           | Mode   | Purpose                                             |
| ------------------------------------- | --------------- | ------ | --------------------------------------------------- |
| `/srv/backups/backup`                 | `root:root`     | `0755` | Chroot dir. Must not be group/other writable.       |
| `/srv/backups/backup/data`            | `backup:backup` | `0700` | Restic repository. Seen as `/data` inside the jail. |
| `/etc/ssh/authorized_keys/backup`     | `root:root`     | `0644` | Client public key, kept outside the chroot.         |
| `/etc/ssh/sshd_config.d/bdwn-backup.conf` | `root:root` | `0644` | sshd `Match User` drop-in.                          |

## Client-side settings

Because of the chroot, the repository root the client sees is `/data`, not the
absolute path on the server. In `/etc/bdwn/bdwn.conf` on the client:

```sh
BDWN_REPO="sftp:backup-remote:/data"
```

And the matching `Host` entry in `/var/lib/bdwn/.ssh/config`:

```sshconfig
Host backup-remote
    HostName backup.example.com
    Port 22
    User backup
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

| Variable      | Default         | Meaning                                  |
| ------------- | --------------- | ---------------------------------------- |
| `BACKUP_USER` | `backup`        | Name of the system user and group.       |
| `CHROOT_BASE` | `/srv/backups`  | Parent dir of the per-user chroot.       |

Example:

```sh
BACKUP_USER=srv1-backup CHROOT_BASE=/data/backups ./setup-backup-user.sh id_ed25519.pub
```

## Hardening notes

The `Match User` block disables the shell, PTY, and all forwarding
(`ForceCommand internal-sftp`, `PermitTTY no`, `X11Forwarding no`,
`AllowTcpForwarding no`, `AllowAgentForwarding no`, `PermitTunnel no`), so the
user can only speak SFTP inside its jail.

Optional: to protect existing snapshots against deletion (append-only style),
you would need to prevent the SFTP user from removing files in `data`, in which
case retention/prune must be disabled on the client (unset the `BDWN_KEEP_*`
values), since pruning requires delete access.
