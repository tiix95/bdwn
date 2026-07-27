#!/bin/sh
# bdwn-export - mirror the restic repositories held by this backup server onto
# an external device, with rsync.
#
# Runs on the BACKUP SERVER, as root. The repositories live in the shared
# chroot jail created by setup-backup-user.sh (/backups/servers/<server-name>).
# rsync copies them byte for byte, so the passphrases are never needed here:
# the backup server does not hold them and does not need to.
#
# The external device is either a directory already mounted, given with
# --target, or the BDWN_EXPORT_UUID filesystem, which this script mounts and
# unmounts itself.
set -eu

CONFIG_FILE="${BDWN_EXPORT_CONFIG:-/etc/bdwn/bdwn-export.conf}"

die() { echo "bdwn-export: $*" >&2; exit 1; }

# The config only overrides defaults, so a missing one is not an error.
if [ -r "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
fi

SERVER_ROOT="${BDWN_SERVER_ROOT:-/backups/servers}"

target=""
servers=""
while [ $# -gt 0 ]; do
    case "$1" in
    --target) shift; [ $# -gt 0 ] || die "--target needs a directory"; target="$1" ;;
    -*)       die "unknown option: $1 (usage: bdwn-export [--target DIR] [SERVER...])" ;;
    *)        servers="$servers $1" ;;
    esac
    shift
done

[ "$(id -u)" = "0" ] || die "must run as root (mount and full read of the repositories)"
[ -d "$SERVER_ROOT" ] || die "repository root not found: $SERVER_ROOT"

# No --target: mount the configured device, and unmount it whatever the
# outcome, including when die() aborts below.
if [ -z "$target" ]; then
    : "${BDWN_EXPORT_UUID:?no --target given and BDWN_EXPORT_UUID not set in $CONFIG_FILE}"
    device="/dev/disk/by-uuid/$BDWN_EXPORT_UUID"
    [ -b "$device" ] || die "external device not found: $device (is it plugged in?)"
    target="${BDWN_EXPORT_MOUNT:-/mnt/bdwn-export}"
    mkdir -p "$target"
    if ! mountpoint -q "$target"; then
        echo "bdwn-export: mounting $device on $target"
        mount "$device" "$target"
        trap 'echo "bdwn-export: unmounting $target"; umount "$target"' EXIT INT TERM
    fi
fi

# Guards against mirroring onto the root filesystem when the device failed to
# mount or the given path is a plain directory.
mountpoint -q "$target" || die "$target is not a mount point, refusing to export outside an external device"

# No server named on the command line: every repository under the jail. A dir
# holding no repository yet (client declared but never backed up) is skipped,
# whereas naming it explicitly below is an error.
if [ -z "$servers" ]; then
    for dir in "$SERVER_ROOT"/*; do
        [ -d "$dir" ] || continue
        if [ ! -f "$dir/config" ]; then
            echo "bdwn-export: skipping $dir, no restic repository yet"
            continue
        fi
        servers="$servers $(basename "$dir")"
    done
    [ -n "$servers" ] || die "no repository found under $SERVER_ROOT"
fi

DEST_ROOT="$target/${BDWN_EXPORT_SUBDIR:-bdwn-repos}"
mkdir -p "$DEST_ROOT"
chmod 0700 "$DEST_ROOT"

for name in $servers; do
    # Same charset as the server user names created by setup-backup-user.sh,
    # which also keeps '..' and absolute paths out of the copy.
    case "$name" in
    *[!a-z0-9_-]* | -*) die "invalid server name: $name (use a-z 0-9 _ -)" ;;
    esac
    src="$SERVER_ROOT/$name"
    [ -d "$src" ] || die "unknown repository: $name (no such directory $src)"
    [ -f "$src/config" ] || die "$src holds no restic repository (no config file)"

    mkdir -p "$DEST_ROOT/$name"
    chmod 0700 "$DEST_ROOT/$name"
    echo "bdwn-export: mirroring $src to $DEST_ROOT/$name"
    rsync -a --delete --stats "$src/" "$DEST_ROOT/$name/"
done
sync

echo "bdwn-export: export complete in $DEST_ROOT"
echo "bdwn-export: restore a server with its own passphrase:"
echo "    restic -r $DEST_ROOT/<server-name> restore latest --target DIR"
