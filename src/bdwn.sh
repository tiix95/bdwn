#!/bin/sh
# bdwn - BackDown: encrypted server backups to an SFTP repository via restic.
# Sources /etc/bdwn/bdwn.conf and drives restic. Meant to be run by the
# systemd timer as the unprivileged 'bdwn' user, but usable by hand for
# maintenance (snapshots, check, arbitrary restic commands).
#
# 'set -f' disables pathname expansion: BDWN_EXCLUDES holds restic glob
# patterns (e.g. /home/*/.cache) that must reach restic literally, and we
# rely on word-splitting only, never on shell globbing.
set -euf

CONFIG_FILE="${BDWN_CONFIG:-/etc/bdwn/bdwn.conf}"

die() { echo "bdwn: $*" >&2; exit 1; }

[ -r "$CONFIG_FILE" ] || die "config file not found or not readable: $CONFIG_FILE"
# shellcheck disable=SC1090
. "$CONFIG_FILE"

: "${BDWN_REPO:?BDWN_REPO not set in $CONFIG_FILE}"
: "${RESTIC_PASSWORD:?RESTIC_PASSWORD not set in $CONFIG_FILE}"
: "${BACKUP_PATHS:?BACKUP_PATHS not set in $CONFIG_FILE}"

# Hand restic what it expects through the environment.
export RESTIC_REPOSITORY="$BDWN_REPO"
export RESTIC_PASSWORD
export RESTIC_CACHE_DIR="${BDWN_CACHE_DIR:-/var/cache/bdwn}"

# When run as root (the systemd service), restic reads BACKUP_PATHS with full
# privileges but the SSH connection is dropped to the unprivileged 'bdwn'
# user via runuser: root never talks to the network, and bdwn's ssh config,
# key and known_hosts are used explicitly. The passphrase is stripped from
# the environment so the network-facing ssh process never sees it.
SSH_DIR="/var/lib/bdwn/.ssh"
RESTIC_SFTP_OPT=""
if [ "$(id -u)" = "0" ]; then
    case "$BDWN_REPO" in
    sftp:*)
        SSH_HOST="${BDWN_REPO#sftp:}"
        SSH_HOST="${SSH_HOST%%:*}"
        RESTIC_SFTP_OPT="sftp.command=/usr/sbin/runuser -u bdwn -- env -u RESTIC_PASSWORD -u RESTIC_REPOSITORY ssh -F ${SSH_DIR}/config -o UserKnownHostsFile=${SSH_DIR}/known_hosts ${SSH_HOST} -s sftp"
        ;;
    esac
fi

# All restic invocations go through this wrapper so the privilege-drop
# option is applied consistently.
run_restic() {
    if [ -n "$RESTIC_SFTP_OPT" ]; then
        restic -o "$RESTIC_SFTP_OPT" "$@"
    else
        restic "$@"
    fi
}

# Build --exclude arguments from the optional BDWN_EXCLUDES list.
build_excludes() {
    EXCLUDE_ARGS=""
    for pattern in ${BDWN_EXCLUDES:-}; do
        EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude=$pattern"
    done
}

# Export the ZFS datasets listed in BDWN_ZFS_DATASETS to dump files so they
# can be picked up by the restic backup. Each entry is snapshotted recursively
# under a fixed name (@bdwn, destroyed after the send, and cleaned up first if
# a previous run died), then every dataset/volume below it is sent to its own
# file in BDWN_ZFS_DUMP_DIR ('/' in names becomes '_').
ZFS_SNAP="bdwn"
export_zfs() {
    command -v zfs >/dev/null 2>&1 || die "BDWN_ZFS_DATASETS is set but zfs is not installed (zfsutils-linux)"
    ZFS_DUMP_DIR="${BDWN_ZFS_DUMP_DIR:-/var/backups/bdwn/zfs}"
    mkdir -p "$ZFS_DUMP_DIR"
    chmod 0700 "$ZFS_DUMP_DIR"

    for entry in $BDWN_ZFS_DATASETS; do
        zfs list "$entry" >/dev/null 2>&1 || die "unknown ZFS dataset: $entry"
        zfs destroy -r "${entry}@${ZFS_SNAP}" 2>/dev/null || true
        zfs snapshot -r "${entry}@${ZFS_SNAP}"
        for dataset in $(zfs list -rH -o name -t filesystem,volume "$entry"); do
            dump_file="$ZFS_DUMP_DIR/$(echo "$dataset" | tr '/' '_').zfs"
            echo "bdwn: exporting ${dataset}@${ZFS_SNAP} to $dump_file"
            zfs send "${dataset}@${ZFS_SNAP}" > "$dump_file"
        done
        zfs destroy -r "${entry}@${ZFS_SNAP}"
    done
}

# Remove the dump files once they are safely in the repository: restic
# deduplicates by content, so keeping them locally buys nothing.
cleanup_zfs() {
    find "$ZFS_DUMP_DIR" -name '*.zfs' -type f -delete
}

# Initialize the repository the first time it is used.
ensure_repo() {
    if ! run_restic cat config >/dev/null 2>&1; then
        echo "bdwn: initializing repository $RESTIC_REPOSITORY"
        run_restic init
    fi
}

cmd_backup() {
    ensure_repo
    build_excludes

    if [ -n "${BDWN_ZFS_DATASETS:-}" ]; then
        export_zfs
        BACKUP_PATHS="$BACKUP_PATHS $ZFS_DUMP_DIR"
    fi

    # shellcheck disable=SC2086
    run_restic backup --verbose $EXCLUDE_ARGS $BACKUP_PATHS

    if [ -n "${BDWN_ZFS_DATASETS:-}" ]; then
        cleanup_zfs
    fi

    # Apply retention only when at least one keep policy is configured.
    if [ -n "${BDWN_KEEP_DAILY:-}${BDWN_KEEP_WEEKLY:-}${BDWN_KEEP_MONTHLY:-}" ]; then
        # shellcheck disable=SC2086
        run_restic forget --prune \
            ${BDWN_KEEP_DAILY:+--keep-daily $BDWN_KEEP_DAILY} \
            ${BDWN_KEEP_WEEKLY:+--keep-weekly $BDWN_KEEP_WEEKLY} \
            ${BDWN_KEEP_MONTHLY:+--keep-monthly $BDWN_KEEP_MONTHLY}
    fi
}

case "${1:-backup}" in
    backup)    cmd_backup ;;
    init)      ensure_repo ;;
    snapshots) shift; run_restic snapshots "$@" ;;
    check)     shift; run_restic check "$@" ;;
    restic)    shift; run_restic "$@" ;;
    *)         die "unknown command: $1 (use: backup|init|snapshots|check|restic)" ;;
esac
