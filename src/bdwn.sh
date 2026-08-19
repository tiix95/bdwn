#!/bin/sh
# bdwn - BackDown: encrypted server backups via restic, to an SFTP
# repository or a local filesystem path.
# Sources /etc/bdwn/bdwn.conf and drives restic. Meant to be run by the
# systemd timer as the unprivileged 'bdwn' user, but usable by hand for
# maintenance (snapshots, list, restore, check, arbitrary restic commands).
#
# 'bdwn zfs-sync' is the one command that leaves restic aside: it replicates
# a local ZFS dataset onto a dataset of a remote server with 'zfs send' over
# SSH, driven by its own timer (see BDWN_ZFS_SYNC_* in the config).
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

SSH_DIR="/var/lib/bdwn/.ssh"
RESTIC_SFTP_OPT=""

# Check the repository settings and hand restic what it expects through the
# environment. Called for every command but 'zfs-sync', which replicates a
# ZFS dataset over SSH and never opens the repository.
init_restic() {
    : "${BDWN_REPO:?BDWN_REPO not set in $CONFIG_FILE}"
    : "${RESTIC_PASSWORD:?RESTIC_PASSWORD not set in $CONFIG_FILE}"
    : "${BACKUP_PATHS:?BACKUP_PATHS not set in $CONFIG_FILE}"

    export RESTIC_REPOSITORY="$BDWN_REPO"
    export RESTIC_PASSWORD
    export RESTIC_CACHE_DIR="${BDWN_CACHE_DIR:-/var/cache/bdwn}"

    # When run as root (the systemd service), restic reads BACKUP_PATHS with
    # full privileges but the SSH connection is dropped to the unprivileged
    # 'bdwn' user via runuser: root never talks to the network, and bdwn's ssh
    # config, key and known_hosts are used explicitly. The passphrase is
    # stripped from the environment so the network-facing ssh process never
    # sees it.
    if [ "$(id -u)" = "0" ]; then
        case "$BDWN_REPO" in
        sftp:*)
            SSH_HOST="${BDWN_REPO#sftp:}"
            SSH_HOST="${SSH_HOST%%:*}"
            RESTIC_SFTP_OPT="sftp.command=/usr/sbin/runuser -u bdwn -- env -u RESTIC_PASSWORD -u RESTIC_REPOSITORY ssh -F ${SSH_DIR}/config -o UserKnownHostsFile=${SSH_DIR}/known_hosts ${SSH_HOST} -s sftp"
            ;;
        esac
    fi
}

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

# Initialize the repository the first time it is used, and only then.
#
# The probe used to swallow its own error output, so every failure looked like
# a missing repository: an unwritable cache, a wrong passphrase, a read-only
# mount or an unreachable server all ended in an init attempt over an existing
# repository, and the real cause never reached the journal. Now anything but a
# genuinely absent repository is reported and aborts the run.
ensure_repo() {
    status=0
    probe=$(run_restic cat config 2>&1) || status=$?
    if [ "$status" = 0 ]; then
        return 0
    fi

    # restic exits 10 when the repository does not exist (since 0.17). Older
    # versions exit 1 on every failure, so their message is matched as well:
    # restic ends a failed lookup with this question.
    missing=no
    if [ "$status" = 10 ]; then
        missing=yes
    else
        case "$probe" in
        *"Is there a repository at the following location?"*) missing=yes ;;
        esac
    fi

    if [ "$missing" = no ]; then
        echo "$probe" >&2
        die "cannot open repository $RESTIC_REPOSITORY (restic exit $status)
The repository is there but unusable, so bdwn will not initialize over it.
Fix the cause reported above. Under the systemd unit, the usual ones are a
cache or repository directory root cannot write (the unit drops
CAP_DAC_OVERRIDE: 'chown -R root:root /var/cache/bdwn'), a local repository
missing its ReadWritePaths drop-in, or a passphrase that does not match."
    fi

    echo "bdwn: initializing repository $RESTIC_REPOSITORY"
    run_restic init
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

# List the files held by a snapshot ('latest' when none is given). Anything
# else is handed to 'restic ls': paths to descend into, --host, --tag...
cmd_list() {
    # A first argument that is a path or an option is not a snapshot ID, so
    # the default one is inserted before it.
    case "${1:-}" in
    '' | /* | -*) set -- latest "$@" ;;
    esac
    run_restic ls --long "$@"
}

# Restore files out of a snapshot. Paths are restic --include patterns, so a
# directory brings back everything below it; with no path the whole snapshot
# is restored. Paths must not contain spaces.
cmd_restore() {
    snapshot="latest"
    target="${BDWN_RESTORE_DIR:-/var/backups/bdwn/restore}"
    includes=""
    while [ $# -gt 0 ]; do
        case "$1" in
        --snapshot) shift; [ $# -gt 0 ] || die "--snapshot needs a snapshot ID"; snapshot="$1" ;;
        --target)   shift; [ $# -gt 0 ] || die "--target needs a directory"; target="$1" ;;
        -*)         die "unknown restore option: $1 (use: --snapshot ID, --target DIR)" ;;
        *)          includes="$includes --include=$1" ;;
        esac
        shift
    done

    # Restored files keep their original content, so the target is created
    # root-only. An existing directory is left alone: '--target /' restores
    # in place and must not see its mode changed.
    if [ ! -d "$target" ]; then
        mkdir -p "$target"
        chmod 0700 "$target"
    fi

    echo "bdwn: restoring snapshot $snapshot into $target"
    # shellcheck disable=SC2086
    run_restic restore "$snapshot" --target "$target" $includes
}

# Mirror a local repository onto an external device with rsync: the copy is a
# byte-for-byte repository, readable with the same passphrase and usable as-is
# with 'restic -r <device>/<subdir>'. Only local repositories can be mirrored.
# The device is either an already mounted directory given as argument, or the
# BDWN_EXPORT_UUID filesystem, which is then mounted and unmounted by bdwn.
cmd_export() {
    [ "$(id -u)" = "0" ] || die "export needs root (mount and full read of the repository)"
    case "$BDWN_REPO" in
    sftp:*) die "export mirrors a local repository, but BDWN_REPO is remote: $BDWN_REPO" ;;
    esac
    [ -d "$BDWN_REPO" ] || die "local repository not found: $BDWN_REPO"

    target="${1:-}"
    if [ -z "$target" ]; then
        : "${BDWN_EXPORT_UUID:?no target directory given and BDWN_EXPORT_UUID not set in $CONFIG_FILE}"
        device="/dev/disk/by-uuid/$BDWN_EXPORT_UUID"
        [ -b "$device" ] || die "external device not found: $device (is it plugged in?)"
        target="${BDWN_EXPORT_MOUNT:-/mnt/bdwn-export}"
        mkdir -p "$target"
        if ! mountpoint -q "$target"; then
            echo "bdwn: mounting $device on $target"
            mount "$device" "$target"
            # Unmount whatever the outcome, including when die() aborts below.
            trap 'echo "bdwn: unmounting $target"; umount "$target"' EXIT INT TERM
        fi
    fi

    # Guards against mirroring onto the root filesystem when the device failed
    # to mount or the given path is a plain directory.
    mountpoint -q "$target" || die "$target is not a mount point, refusing to export outside an external device"

    dest="$target/${BDWN_EXPORT_SUBDIR:-bdwn-repo}"
    mkdir -p "$dest"
    chmod 0700 "$dest"

    echo "bdwn: mirroring $BDWN_REPO to $dest"
    rsync -a --delete --stats "$BDWN_REPO/" "$dest/"
    sync

    # Cheap sanity check: an unreadable config means the copy is unusable.
    restic -r "$dest" cat config >/dev/null || die "exported repository is unreadable: $dest"
    echo "bdwn: export complete, restore with: restic -r $dest restore latest --target DIR"
}

# Replicate a local ZFS dataset onto a dataset of a remote server, with
# 'zfs send' piped into 'zfs receive' over SSH. Nothing goes through restic:
# the remote side holds real ZFS data, not an encrypted repository, so the
# remote pool is the place to encrypt it if needed.
#
# Every run snapshots the dataset under <prefix>-<UTC timestamp> and sends it
# incrementally from the previous sync snapshot, or in full when the two sides
# share none (first run, remote dataset recreated).

# Run a command on the remote server. As root the SSH client is dropped to the
# unprivileged BDWN_ZFS_SYNC_LOCAL_USER, like the SFTP repository does: the
# process talking to the network never runs as root. RESTIC_PASSWORD is only
# exported by init_restic, which zfs-sync does not call, so it stays out of
# the environment of that ssh process.
zfs_sync_ssh() {
    if [ "$(id -u)" = "0" ]; then
        # shellcheck disable=SC2086
        /usr/sbin/runuser -u "$ZFS_SYNC_LOCAL_USER" -- \
            ssh $ZFS_SYNC_SSH_ARGS "$ZFS_SYNC_TARGET" "$@"
    else
        # SC2029: the remote command is built here on purpose, from the
        # configured dataset and zfs path, and must expand locally.
        # shellcheck disable=SC2086,SC2029
        ssh $ZFS_SYNC_SSH_ARGS "$ZFS_SYNC_TARGET" "$@"
    fi
}

# Sync snapshots of the dataset, oldest first. The first run has none, which
# is not an error, hence the '|| true' on the empty grep.
zfs_sync_snapshots() {
    zfs list -H -o name -t snapshot -s creation -d 1 "$ZFS_SYNC_LOCAL" \
        | grep -F "@${ZFS_SYNC_PREFIX}-" || true
}

cmd_zfs_sync() {
    command -v zfs >/dev/null 2>&1 || die "zfs is not installed (zfsutils-linux)"

    ZFS_SYNC_LOCAL="${BDWN_ZFS_SYNC_LOCAL_DATASET:?BDWN_ZFS_SYNC_LOCAL_DATASET not set in $CONFIG_FILE}"
    ZFS_SYNC_REMOTE="${BDWN_ZFS_SYNC_REMOTE_DATASET:?BDWN_ZFS_SYNC_REMOTE_DATASET not set in $CONFIG_FILE}"
    ZFS_SYNC_HOST="${BDWN_ZFS_SYNC_HOST:?BDWN_ZFS_SYNC_HOST not set in $CONFIG_FILE}"
    ZFS_SYNC_LOCAL_USER="${BDWN_ZFS_SYNC_LOCAL_USER:-bdwn}"
    ZFS_SYNC_PREFIX="${BDWN_ZFS_SYNC_SNAP_PREFIX:-bdwn-sync}"
    ZFS_SYNC_KEEP="${BDWN_ZFS_SYNC_KEEP:-3}"
    ZFS_SYNC_REMOTE_ZFS="${BDWN_ZFS_SYNC_REMOTE_ZFS:-zfs}"
    # '-x mountpoint' is not optional in spirit: 'zfs send -R' carries the
    # properties of the source, so without it the copy inherits the mountpoint
    # of the dataset it mirrors and mounts over it on the remote server,
    # shadowing whatever lives there.
    ZFS_SYNC_RECV_OPTS="${BDWN_ZFS_SYNC_RECV_OPTS:--u -F -x mountpoint}"
    ZFS_SYNC_SSH_DIR="${BDWN_ZFS_SYNC_SSH_DIR:-/var/lib/bdwn/.ssh}"

    # At least one snapshot must survive the pruning: it is the base of the
    # next incremental stream.
    case "$ZFS_SYNC_KEEP" in
    '' | *[!0-9]*) die "invalid BDWN_ZFS_SYNC_KEEP: $ZFS_SYNC_KEEP (positive integer)" ;;
    esac
    [ "$ZFS_SYNC_KEEP" -ge 1 ] || die "BDWN_ZFS_SYNC_KEEP must be at least 1"

    # Recursive by default: children of the dataset are snapshotted and sent
    # along, and the remote copy mirrors the whole tree.
    case "${BDWN_ZFS_SYNC_RECURSIVE:-yes}" in
    yes) ZFS_SNAP_OPT="-r"; ZFS_SEND_OPT="-R"; ZFS_DESTROY_OPT="-r" ;;
    no)  ZFS_SNAP_OPT=""; ZFS_SEND_OPT=""; ZFS_DESTROY_OPT="" ;;
    *)   die "invalid BDWN_ZFS_SYNC_RECURSIVE: ${BDWN_ZFS_SYNC_RECURSIVE:-} (yes or no)" ;;
    esac

    [ -r "$ZFS_SYNC_SSH_DIR/config" ] || die "SSH config not found or not readable: $ZFS_SYNC_SSH_DIR/config"
    ZFS_SYNC_SSH_ARGS="-F $ZFS_SYNC_SSH_DIR/config -o UserKnownHostsFile=$ZFS_SYNC_SSH_DIR/known_hosts -o BatchMode=yes"
    ZFS_SYNC_SSH_ARGS="$ZFS_SYNC_SSH_ARGS${BDWN_ZFS_SYNC_PORT:+ -p $BDWN_ZFS_SYNC_PORT}"
    ZFS_SYNC_SSH_ARGS="$ZFS_SYNC_SSH_ARGS${BDWN_ZFS_SYNC_SSH_KEY:+ -i $BDWN_ZFS_SYNC_SSH_KEY -o IdentitiesOnly=yes}"
    ZFS_SYNC_SSH_ARGS="$ZFS_SYNC_SSH_ARGS ${BDWN_ZFS_SYNC_SSH_OPTS:-}"
    ZFS_SYNC_TARGET="${BDWN_ZFS_SYNC_SSH_USER:+${BDWN_ZFS_SYNC_SSH_USER}@}${ZFS_SYNC_HOST}"

    zfs list -H -o name "$ZFS_SYNC_LOCAL" >/dev/null 2>&1 || die "unknown ZFS dataset: $ZFS_SYNC_LOCAL"

    # Base of the incremental stream: the last sync snapshot, provided the
    # remote still holds it. Anything else means the two sides share no common
    # point and only a full stream can be received.
    base=""
    last=$(zfs_sync_snapshots | tail -n 1)
    if [ -n "$last" ]; then
        remote_snap="${ZFS_SYNC_REMOTE}@${last#*@}"
        if zfs_sync_ssh "$ZFS_SYNC_REMOTE_ZFS list -H -o name -t snapshot $remote_snap" >/dev/null 2>&1; then
            base="$last"
        else
            echo "bdwn: $remote_snap missing on $ZFS_SYNC_HOST, falling back to a full stream"
        fi
    fi

    snap="${ZFS_SYNC_LOCAL}@${ZFS_SYNC_PREFIX}-$(date -u +%Y%m%dT%H%M%SZ)"
    echo "bdwn: snapshotting $snap"
    # shellcheck disable=SC2086
    zfs snapshot $ZFS_SNAP_OPT "$snap"

    if [ -n "$base" ]; then
        echo "bdwn: sending $snap incrementally from ${base#*@} to ${ZFS_SYNC_TARGET}:${ZFS_SYNC_REMOTE}"
    else
        echo "bdwn: sending $snap in full to ${ZFS_SYNC_TARGET}:${ZFS_SYNC_REMOTE}"
    fi

    # 'set -o pipefail' is not POSIX, so the exit status of 'zfs send' is
    # written to a file: a truncated stream fails the receive too, but the
    # status tells which side actually broke.
    send_status_file=$(mktemp)
    recv_status=0
    # shellcheck disable=SC2086
    { zfs send $ZFS_SEND_OPT ${BDWN_ZFS_SYNC_SEND_OPTS:-} ${base:+-I $base} "$snap"
      echo $? > "$send_status_file"
    } | zfs_sync_ssh "$ZFS_SYNC_REMOTE_ZFS receive $ZFS_SYNC_RECV_OPTS $ZFS_SYNC_REMOTE" || recv_status=$?
    send_status=$(cat "$send_status_file")
    rm -f "$send_status_file"

    if [ "$send_status" != 0 ] || [ "$recv_status" != 0 ]; then
        # The remote never got this snapshot. Keeping it would make the next
        # run pick it as an incremental base the remote cannot match, and fall
        # back to a full stream; dropping it keeps the next run incremental.
        # shellcheck disable=SC2086
        zfs destroy $ZFS_DESTROY_OPT "$snap"
        die "replication failed (zfs send exit $send_status, zfs receive exit $recv_status)"
    fi

    # Older sync snapshots are only useful as incremental bases, and the remote
    # keeps its own copies of them.
    for old in $(zfs_sync_snapshots | head -n "-$ZFS_SYNC_KEEP"); do
        echo "bdwn: destroying old sync snapshot $old"
        # shellcheck disable=SC2086
        zfs destroy $ZFS_DESTROY_OPT "$old"
    done

    echo "bdwn: replication of $ZFS_SYNC_LOCAL done"
}

COMMAND="${1:-backup}"
if [ $# -gt 0 ]; then
    shift
fi

# zfs-sync is the only command that does not talk to the repository, so it is
# also the only one that runs without the restic settings being configured.
if [ "$COMMAND" != "zfs-sync" ]; then
    init_restic
fi

case "$COMMAND" in
    backup)    cmd_backup ;;
    init)      ensure_repo ;;
    snapshots) run_restic snapshots "$@" ;;
    list)      cmd_list "$@" ;;
    restore)   cmd_restore "$@" ;;
    export)    cmd_export "$@" ;;
    zfs-sync)  cmd_zfs_sync ;;
    check)     run_restic check "$@" ;;
    restic)    run_restic "$@" ;;
    *)         die "unknown command: $COMMAND (use: backup|init|snapshots|list|restore|export|zfs-sync|check|restic)" ;;
esac
