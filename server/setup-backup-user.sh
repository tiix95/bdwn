#!/bin/sh
# setup-backup-user.sh - backup server side setup for a bdwn repository.
#
# All backup clients share a single chroot jail at CHROOT_BASE
# (/backups/servers). Each client server gets its own locked-down system
# user, reachable ONLY over SFTP, whose restic repository lives in
# /backups/servers/<SERVER_NAME> (mode 0700, so servers cannot read each
# other's backups). Inside the chroot the client sees it as /<SERVER_NAME>.
# No interactive shell, no command execution, no port/agent forwarding.
#
# Run as root on the backup (repository) server, once per client server.
# The public key can be given as a file path or inline:
#   ./setup-backup-user.sh <server-name> <client-public-key-file>
#   ./setup-backup-user.sh <server-name> "ssh-ed25519 AAAA... comment"
set -eu

# Tunables (override via environment if needed)
BACKUP_GROUP="${BACKUP_GROUP:-bdwn}"
CHROOT_BASE="${CHROOT_BASE:-/backups/servers}"
AUTH_KEYS_DIR="/etc/ssh/authorized_keys"
SSHD_DROPIN="/etc/ssh/sshd_config.d/bdwn-backup.conf"

die() { echo "setup-backup-user: $*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "must run as root"

SERVER_NAME="${1:-}"
PUBKEY="${2:-}"
[ -n "$SERVER_NAME" ] && [ -n "$PUBKEY" ] \
    || die "usage: $0 <server-name> <client-public-key-file | inline-public-key>"

# The server name becomes a system user name: keep it to a safe charset.
case "$SERVER_NAME" in
    *[!a-z0-9_-]* | -*) die "invalid server name: $SERVER_NAME (use a-z 0-9 _ -)" ;;
esac

# Accept the key as a readable file or as the key string itself, and
# validate it BEFORE touching the system.
if [ -r "$PUBKEY" ]; then
    PUBKEY="$(cat "$PUBKEY")"
fi
printf '%s\n' "$PUBKEY" | ssh-keygen -l -f /dev/stdin > /dev/null 2>&1 \
    || die "not a valid SSH public key: $PUBKEY"

SERVER_DIR="${CHROOT_BASE}/${SERVER_NAME}"

# 1. Shared system group for all backup users (used by the sshd Match block).
if ! getent group "$BACKUP_GROUP" > /dev/null 2>&1; then
    groupadd --system "$BACKUP_GROUP"
fi

# 2. Per-server system user: no shell, member of the shared group. Home is
#    /<SERVER_NAME> so that, once chrooted, SFTP lands in the server's dir.
if ! getent passwd "$SERVER_NAME" > /dev/null 2>&1; then
    useradd --system --gid "$BACKUP_GROUP" \
        --home-dir "/${SERVER_NAME}" --no-create-home \
        --shell /usr/sbin/nologin "$SERVER_NAME"
fi

# 3. The chroot dir MUST be owned by root and not writable by group/others,
#    otherwise sshd refuses to chroot into it.
mkdir -p "$CHROOT_BASE"
chown root:root "$CHROOT_BASE"
chmod 0755 "$CHROOT_BASE"

# 4. Per-server repository dir inside the jail, private to its user.
mkdir -p "$SERVER_DIR"
chown "$SERVER_NAME":"$BACKUP_GROUP" "$SERVER_DIR"
chmod 0700 "$SERVER_DIR"

# 5. authorized_keys kept OUTSIDE the chroot, owned by root (read before chroot).
mkdir -p "$AUTH_KEYS_DIR"
chmod 0755 "$AUTH_KEYS_DIR"
AUTH_KEYS_FILE="${AUTH_KEYS_DIR}/${SERVER_NAME}"
printf '%s\n' "$PUBKEY" > "$AUTH_KEYS_FILE"
chown root:root "$AUTH_KEYS_FILE"
chmod 0644 "$AUTH_KEYS_FILE"

# 6. sshd drop-in: force SFTP-only, chrooted access for the whole backup
#    group. One block covers every server user, so it is safe to rewrite.
cat > "$SSHD_DROPIN" <<EOF
# Managed by bdwn setup-backup-user.sh - do not edit by hand
Match Group ${BACKUP_GROUP}
    AuthorizedKeysFile ${AUTH_KEYS_DIR}/%u
    ChrootDirectory ${CHROOT_BASE}
    ForceCommand internal-sftp
    PermitTTY no
    X11Forwarding no
    AllowTcpForwarding no
    AllowAgentForwarding no
    PermitTunnel no
EOF
chmod 0644 "$SSHD_DROPIN"

# 7. Validate the config, then reload sshd.
sshd -t || die "sshd config test failed, not reloading"
if command -v systemctl > /dev/null 2>&1; then
    systemctl reload ssh 2> /dev/null \
        || systemctl reload sshd 2> /dev/null \
        || true
fi

echo ""
echo "Backup user '${SERVER_NAME}' ready (SFTP-only, chrooted to ${CHROOT_BASE})."
echo "Repository dir: ${SERVER_DIR} (seen as /${SERVER_NAME} from the client)."
echo "On the client, set in /etc/bdwn/bdwn.conf:"
echo "    BDWN_REPO=\"sftp:backup-remote:/${SERVER_NAME}\""
echo "And in /var/lib/bdwn/.ssh/config:  User ${SERVER_NAME}"
echo ""
