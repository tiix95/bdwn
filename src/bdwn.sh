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

# Build --exclude arguments from the optional BDWN_EXCLUDES list.
build_excludes() {
    EXCLUDE_ARGS=""
    for pattern in ${BDWN_EXCLUDES:-}; do
        EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude=$pattern"
    done
}

# Initialize the repository the first time it is used.
ensure_repo() {
    if ! restic cat config >/dev/null 2>&1; then
        echo "bdwn: initializing repository $RESTIC_REPOSITORY"
        restic init
    fi
}

cmd_backup() {
    ensure_repo
    build_excludes
    # shellcheck disable=SC2086
    restic backup --verbose $EXCLUDE_ARGS $BACKUP_PATHS

    # Apply retention only when at least one keep policy is configured.
    if [ -n "${BDWN_KEEP_DAILY:-}${BDWN_KEEP_WEEKLY:-}${BDWN_KEEP_MONTHLY:-}" ]; then
        # shellcheck disable=SC2086
        restic forget --prune \
            ${BDWN_KEEP_DAILY:+--keep-daily $BDWN_KEEP_DAILY} \
            ${BDWN_KEEP_WEEKLY:+--keep-weekly $BDWN_KEEP_WEEKLY} \
            ${BDWN_KEEP_MONTHLY:+--keep-monthly $BDWN_KEEP_MONTHLY}
    fi
}

case "${1:-backup}" in
    backup)    cmd_backup ;;
    init)      ensure_repo ;;
    snapshots) shift; exec restic snapshots "$@" ;;
    check)     shift; exec restic check "$@" ;;
    restic)    shift; exec restic "$@" ;;
    *)         die "unknown command: $1 (use: backup|init|snapshots|check|restic)" ;;
esac
