#!/bin/sh
# setup-backup-user.sh - backup server side setup for a bdwn repository.
#
# Creates a locked-down system user reachable ONLY over SFTP, chrooted to its
# own directory. The backup client can upload and manage its restic repository
# there and do nothing else: no interactive shell, no command execution, no
# port/agent forwarding, no access outside the jail.
#
# Run as root on the backup (repository) server.
#   ./setup-backup-user.sh <client-public-key-file>
set -eu

# Tunables (override via environment if needed)
BACKUP_USER="${BACKUP_USER:-backup}"
CHROOT_BASE="${CHROOT_BASE:-/srv/backups}"
AUTH_KEYS_DIR="/etc/ssh/authorized_keys"
SSHD_DROPIN="/etc/ssh/sshd_config.d/bdwn-backup.conf"

die() { echo "setup-backup-user: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "must run as root"

PUBKEY_FILE="${1:-}"
[ -n "$PUBKEY_FILE" ] && [ -r "$PUBKEY_FILE" ] \
    || die "usage: $0 <client-public-key-file>"

CHROOT_DIR="${CHROOT_BASE}/${BACKUP_USER}"
DATA_DIR="${CHROOT_DIR}/data"

# 1. System group and user: no shell, home is the chroot dir.
if ! getent group "$BACKUP_USER" > /dev/null 2>&1; then
    groupadd --system "$BACKUP_USER"
fi
if ! getent passwd "$BACKUP_USER" > /dev/null 2>&1; then
    useradd --system --gid "$BACKUP_USER" \
        --home-dir "$CHROOT_DIR" --no-create-home \
        --shell /usr/sbin/nologin "$BACKUP_USER"
fi

# 2. The chroot dir MUST be owned by root and not writable by group/others,
#    otherwise sshd refuses to chroot into it.
mkdir -p "$CHROOT_DIR"
chown root:root "$CHROOT_DIR"
chmod 0755 "$CHROOT_DIR"

# 3. Writable data dir inside the jail: this is where restic stores the repo.
#    Seen as /data from inside the chroot.
mkdir -p "$DATA_DIR"
chown "$BACKUP_USER":"$BACKUP_USER" "$DATA_DIR"
chmod 0700 "$DATA_DIR"

# 4. authorized_keys kept OUTSIDE the chroot, owned by root (read before chroot).
mkdir -p "$AUTH_KEYS_DIR"
chmod 0755 "$AUTH_KEYS_DIR"
install -m 0644 -o root -g root "$PUBKEY_FILE" "${AUTH_KEYS_DIR}/${BACKUP_USER}"

# 5. sshd drop-in: force SFTP-only, chrooted access for this user only.
cat > "$SSHD_DROPIN" <<EOF
# Managed by bdwn setup-backup-user.sh - do not edit by hand
Match User ${BACKUP_USER}
    AuthorizedKeysFile ${AUTH_KEYS_DIR}/%u
    ChrootDirectory ${CHROOT_BASE}/%u
    ForceCommand internal-sftp
    PermitTTY no
    X11Forwarding no
    AllowTcpForwarding no
    AllowAgentForwarding no
    PermitTunnel no
EOF
chmod 0644 "$SSHD_DROPIN"

# 6. Validate the config, then reload sshd.
sshd -t || die "sshd config test failed, not reloading"
if command -v systemctl > /dev/null 2>&1; then
    systemctl reload ssh 2> /dev/null \
        || systemctl reload sshd 2> /dev/null \
        || true
fi

echo ""
echo "Backup user '${BACKUP_USER}' ready (SFTP-only, chrooted to ${CHROOT_DIR})."
echo "On the client, set in /etc/bdwn/bdwn.conf:"
echo "    BDWN_REPO=\"sftp:backup-remote:/data\""
echo ""
