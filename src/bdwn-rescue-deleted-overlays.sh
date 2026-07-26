#!/usr/bin/env bash
#
# rescue-deleted-overlays.sh
#
# Deal with a libvirt qcow2 backing chain that has grown out of control, with
# or without its files still present on disk.
#
# Two families of problems, one tool:
#   * files deleted while the domain runs -> the data is still reachable
#     through /proc/<pid>/fd/ as long as QEMU holds the descriptors open
#   * chain too deep -> libvirt fails with "failed to parse JSON: nesting too
#     deep" on blockcommit or even on start; qemu-img has no such limit
#
# Usage:
#   ./rescue-deleted-overlays.sh -d <domain> [options]
#
#   -d <domain>   libvirt domain name (required)
#   -o <dir>      backup directory (default: /var/lib/libvirt/images/rescue)
#   -t <target>   block device target, e.g. vda (default: first disk)
#   -y            do not ask for confirmation
#
# Domain RUNNING:
#   (no mode)     inspect the chain and copy every layer, change nothing
#   -r            restore the read-only backing layers to their original paths
#   -c            flatten the chain live, one layer at a time (implies -r)
#   -C            cold exit: quiesce, copy the active overlay back to its
#                 original path, restart the domain (implies -r)
#
# Domain SHUT OFF:
#   -F            flatten the whole chain into the base with qemu-img, repoint
#                 the domain XML at the base, restart
#
# WARNING: never stop / destroy a domain whose overlays are deleted before
#          running this script without a mode. Once the last file descriptor
#          is closed, the inodes are freed and the data is gone for good.

set -euo pipefail

DOMAIN=""
BACKUP_DIR="/var/lib/libvirt/images/rescue"
TARGET=""
DO_RESTORE=0
DO_COMMIT=0
DO_COLD=0
DO_FLATTEN=0
ASSUME_YES=0

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[*] $*"; }
warn() { echo "[!] $*" >&2; }

usage() { sed -n '4,36p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

confirm() {   # <prompt> -- honours -y
    [ "$ASSUME_YES" -eq 1 ] && return 0
    local answer
    read -r -p "$1 [y/N] " answer
    case "$answer" in [yY]) return 0 ;; *) return 1 ;; esac
}

while getopts ":d:o:t:rcCFyh" opt; do
    case "$opt" in
        d) DOMAIN="$OPTARG" ;;
        o) BACKUP_DIR="$OPTARG" ;;
        t) TARGET="$OPTARG" ;;
        r) DO_RESTORE=1 ;;
        c) DO_COMMIT=1; DO_RESTORE=1 ;;
        C) DO_COLD=1; DO_RESTORE=1 ;;
        F) DO_FLATTEN=1 ;;
        y) ASSUME_YES=1 ;;
        *) usage ;;
    esac
done

[ -n "$DOMAIN" ] || usage
[ "$(id -u)" -eq 0 ] || die "must run as root"
if [ $(( DO_COMMIT + DO_COLD + DO_FLATTEN )) -gt 1 ]; then
    die "-c, -C and -F are mutually exclusive"
fi
for bin in virsh qemu-img dd od; do
    command -v "$bin" >/dev/null || die "$bin not found"
done
if [ "$DO_FLATTEN" -eq 1 ]; then
    command -v python3 >/dev/null || die "python3 not found (needed to edit the domain XML)"
fi

# --- qcow2 header helpers -----------------------------------------------------
# qemu-img info refuses to open an image whose backing file is missing, so read
# the header fields directly:
#   magic(4) version(4) backing_file_offset(8) backing_file_size(4)
# All big-endian, and readable through /proc/<pid>/fd/<n>.
read_be() {   # <file> <offset> <length> -> decimal
    local hex
    hex="$(dd if="$1" bs=1 skip="$2" count="$3" status=none | od -An -tx1 | tr -d ' \n')"
    [ -n "$hex" ] || { echo 0; return; }
    printf '%d\n' "$((16#$hex))"
}

qcow2_backing() {   # <file> -> backing file name, empty when there is none
    local f="$1" magic off size
    magic="$(dd if="$f" bs=1 count=4 status=none | od -An -tx1 | tr -d ' \n')"
    [ "$magic" = "514649fb" ] || return 0        # not a qcow2 image
    off="$(read_be "$f" 8 8)"
    size="$(read_be "$f" 16 4)"
    { [ "$off" -gt 0 ] && [ "$size" -gt 0 ]; } || return 0
    dd if="$f" bs=1 skip="$off" count="$size" status=none
}

# Backing names may be relative to the directory of the referencing image.
resolve_backing() {
    local parent="$1" backing="$2"
    case "$backing" in
        /*) printf '%s\n' "$backing" ;;
        *)  printf '%s\n' "$(dirname "$parent")/$backing" ;;
    esac
}

human() { numfmt --to=iec --suffix=B "$1" 2>/dev/null || echo "${1}B"; }

# --- 1. Domain state and active layer ----------------------------------------
STATE="$(virsh domstate "$DOMAIN" 2>/dev/null || true)"
[ -n "$STATE" ] || die "unknown domain '$DOMAIN'"
info "domain '$DOMAIN' is $STATE"

if [ "$DO_FLATTEN" -eq 1 ]; then
    echo "$STATE" | grep -q "shut off" || die "-F needs the domain shut off (virsh shutdown $DOMAIN)"
else
    echo "$STATE" | grep -q running || die "this mode needs the domain running; use -F when it is off"
fi

# domblklist is authoritative: it reports the path the domain XML points at,
# even when that file has been deleted.
BLKLINE="$(virsh domblklist "$DOMAIN" --details \
           | awk -v t="$TARGET" '$2=="disk" && $4!="-" && (t=="" || $3==t) {print $3"|"$4; exit}')"
[ -n "$BLKLINE" ] || die "no disk found for domain '$DOMAIN'${TARGET:+ with target $TARGET}"
TARGET="${BLKLINE%%|*}"
ACTIVE_PATH="${BLKLINE#*|}"
info "active layer: $TARGET -> $ACTIVE_PATH"

# --- 2. File descriptors held by QEMU (running domain only) -------------------
# No extension filter: libvirt names external snapshots <disk>.<snapshot-name>,
# with no suffix at all.
declare -A FD_OF=()        # absolute path -> /proc/<pid>/fd/<n>
declare -A DELETED_OF=()   # absolute path -> 0|1
PID=""

if [ "$DO_FLATTEN" -eq 0 ]; then
    PIDFILE="/run/libvirt/qemu/${DOMAIN}.pid"
    if [ -r "$PIDFILE" ]; then
        PID="$(cat "$PIDFILE")"
    else
        PID="$(pgrep -f "qemu.*guest=${DOMAIN}," || true)"
    fi
    [ -n "$PID" ] || die "no running QEMU process found for domain '$DOMAIN'"
    [ -d "/proc/$PID" ] || die "pid $PID is not alive; overlay data is already lost"
    info "qemu pid $PID"

    for fd in /proc/"$PID"/fd/*; do
        [ -f "$fd" ] || continue          # regular files only; follows the link
        link="$(readlink "$fd" 2>/dev/null || true)"
        case "$link" in
            /dev/*|/proc/*|/sys/*|/run/*|/tmp/*) continue ;;
            *.log|*.log\ \(deleted\)) continue ;;
            /*) ;;
            *) continue ;;
        esac

        orig="$link"
        deleted=0
        if [[ "$link" == *" (deleted)" ]]; then
            deleted=1
            orig="${link% (deleted)}"
        fi
        FD_OF["$orig"]="$fd"
        DELETED_OF["$orig"]="$deleted"
    done
    [ "${#FD_OF[@]}" -gt 0 ] || die "no regular file descriptor found for pid $PID"
fi

# Readable handle for a chain node: the open fd when the file is gone.
source_for() {
    local path="$1"
    if [ -n "${FD_OF[$path]:-}" ]; then
        printf '%s\n' "${FD_OF[$path]}"
    else
        printf '%s\n' "$path"
    fi
}

# --- 3. Rebuild the backing chain --------------------------------------------
declare -a CHAIN=()
node="$ACTIVE_PATH"
while [ -n "$node" ]; do
    CHAIN=("$node" "${CHAIN[@]}")
    src="$(source_for "$node")"
    if [ ! -e "$src" ]; then
        warn "cannot read $node -- no open fd and no file on disk"
        if [ -n "$PID" ]; then
            warn "regular files held open by pid $PID:"
            for p in "${!FD_OF[@]}"; do warn "    ${FD_OF[$p]} -> $p"; done
        fi
        die "chain is broken at $node"
    fi

    backing="$(qcow2_backing "$src")"
    if [ -n "$backing" ]; then
        node="$(resolve_backing "$node" "$backing")"
        [ "$node" = "${CHAIN[0]}" ] && die "self-referencing backing file on ${CHAIN[0]}"
    else
        node=""
    fi
done
BASE="${CHAIN[0]}"

echo
info "backing chain (base first), ${#CHAIN[@]} layer(s):"
for i in "${!CHAIN[@]}"; do
    path="${CHAIN[$i]}"
    if [ -n "${FD_OF[$path]:-}" ]; then
        tag="$([ "${DELETED_OF[$path]}" = 1 ] && echo DELETED || echo on-disk)"
    else
        tag="$([ -e "$path" ] && echo on-disk || echo MISSING)"
    fi
    role="intermediate"
    [ "$i" -eq 0 ] && role="base"
    [ "$path" = "$ACTIVE_PATH" ] && role="ACTIVE"
    printf '    %2d. %-12s %-8s %s\n' "$i" "$role" "$tag" "$path"
done

# --- 4. Offline flatten -------------------------------------------------------
# qemu-img walks the chain itself and has no JSON nesting limit, so it copes
# with a chain libvirt can no longer even parse.
if [ "$DO_FLATTEN" -eq 1 ]; then
    [ "${#CHAIN[@]}" -ge 2 ] || die "chain has a single layer, nothing to flatten"

    for path in "${CHAIN[@]}"; do
        [ -e "$path" ] || die "$path is missing; cannot flatten offline"
    done

    # Worst case, the base grows by the allocated size of every layer above it.
    NEEDED=0
    for (( i = 1; i < ${#CHAIN[@]}; i++ )); do
        layer_size="$(du -B1 "${CHAIN[$i]}" | cut -f1)"
        NEEDED=$(( NEEDED + layer_size ))
    done
    AVAIL="$(df -B1 --output=avail "$(dirname "$BASE")" | tail -n1 | tr -d ' ')"
    info "worst-case growth of the base: $(human "$NEEDED"), available: $(human "$AVAIL")"
    if [ "$NEEDED" -gt "$AVAIL" ]; then
        warn "not enough free space for the worst case; qemu-img may still fit"
        warn "thanks to overwritten clusters, but a failure here leaves the base"
        warn "partially written -- back it up first"
        confirm "Continue anyway?" || die "aborted"
    fi

    echo
    warn "the base $BASE will be modified in place"
    warn "make sure $BACKUP_DIR holds a copy before continuing"
    confirm "Flatten ${#CHAIN[@]} layers into $(basename "$BASE")?" || { info "aborted"; exit 0; }

    XML_BACKUP="${BACKUP_DIR}/${DOMAIN}-$(date +%Y%m%d-%H%M%S).xml"
    mkdir -p "$BACKUP_DIR"
    virsh dumpxml --inactive "$DOMAIN" > "$XML_BACKUP"
    info "domain XML saved to $XML_BACKUP"

    echo
    info "committing $(basename "$ACTIVE_PATH") -> $(basename "$BASE") (this takes a while)"
    # -d keeps the top image intact: nothing is destroyed until the domain is
    # verified to boot on the flattened base.
    qemu-img commit -p -d -b "$BASE" "$ACTIVE_PATH" \
        || die "commit failed; restore the base from $BACKUP_DIR before retrying"

    info "checking $BASE"
    qemu-img check "$BASE" || die "base is inconsistent; restore it from $BACKUP_DIR"

    # Repoint the disk at the base and drop the stale backingStore elements.
    XML_NEW="${BACKUP_DIR}/${DOMAIN}-flattened.xml"
    python3 - "$XML_BACKUP" "$TARGET" "$BASE" "$XML_NEW" <<'PY'
import sys, xml.etree.ElementTree as ET

src, target, newpath, out = sys.argv[1:5]
tree = ET.parse(src)
found = False
for disk in tree.getroot().findall('./devices/disk'):
    tgt = disk.find('target')
    if tgt is None or tgt.get('dev') != target:
        continue
    found = True
    source = disk.find('source')
    if source is None:
        sys.exit(f"disk {target} has no <source> element")
    source.set('file', newpath)
    # libvirt regenerates <backingStore> from the image itself on start.
    for bs in disk.findall('backingStore'):
        disk.remove(bs)
if not found:
    sys.exit(f"no disk with target {target} in the domain XML")
tree.write(out, encoding='unicode')
PY

    virsh define "$XML_NEW" >/dev/null
    info "domain XML repointed at $BASE"

    virsh start "$DOMAIN" >/dev/null
    info "domain started"

    echo
    info "verify the guest, then reclaim the space:"
    printf '    rm -f'
    for (( i = 1; i < ${#CHAIN[@]}; i++ )); do printf ' \\\n        %q' "${CHAIN[$i]}"; done
    printf '\n'
    info "keep $BACKUP_DIR until you are sure"
    exit 0
fi

# --- 5. Copy every layer we can reach to the backup directory -----------------
# Copies are crash-consistent at best (the guest keeps writing): a safety net,
# not a clean backup.
mkdir -p "$BACKUP_DIR"
echo
info "copying to $BACKUP_DIR"
declare -A BACKUP_OF=()

for path in "${CHAIN[@]}"; do
    src="$(source_for "$path")"
    [ -e "$src" ] || { warn "$path unreachable, skipped"; continue; }
    dest="${BACKUP_DIR}/$(basename "$path")"
    cp --sparse=always "$src" "$dest"
    BACKUP_OF["$path"]="$dest"
    echo "    $(basename "$path") -> $dest ($(du -h "$dest" | cut -f1))"
done

echo
info "verifying the copies"
for path in "${CHAIN[@]}"; do
    dest="${BACKUP_OF[$path]:-}"
    [ -n "$dest" ] || continue
    magic="$(dd if="$dest" bs=1 count=4 status=none | od -An -tx1 | tr -d ' \n')"
    [ "$magic" = "514649fb" ] || die "invalid qcow2 header in $dest -- stop, do NOT touch the domain"
    echo "    OK  $(basename "$dest")"
done

# --- 6. Put the read-only layers back at their original paths -----------------
# Only the layers below the active one: they are read-only, so the copy is
# byte-identical to what QEMU holds open. The active overlay is NEVER restored
# here, QEMU keeps writing to its deleted inode.
if [ "$DO_RESTORE" -eq 1 ]; then
    echo
    info "restoring the read-only layers"
    for path in "${CHAIN[@]}"; do
        [ "$path" = "$ACTIVE_PATH" ] && continue
        if [ -e "$path" ]; then
            echo "    already present: $path"
            continue
        fi
        cp --sparse=always "${BACKUP_OF[$path]}" "$path"
        chown --reference="$(dirname "$path")" "$path" 2>/dev/null || true
        echo "    restored: $path"
    done
fi

# --- 7. Flatten the chain live, one layer at a time ---------------------------
if [ "$DO_COMMIT" -eq 1 ]; then
    [ "${#CHAIN[@]}" -ge 2 ] || die "chain has a single layer, nothing to commit"
    if [ "${#CHAIN[@]}" -gt 8 ]; then
        warn "${#CHAIN[@]} layers: libvirt is likely to fail on the JSON nesting"
        warn "limit whatever we do. -F on a stopped domain is the reliable route."
    fi
    confirm "Flatten ${#CHAIN[@]} layers into $(basename "$BASE")?" || { info "commit skipped"; exit 0; }

    for (( i = 1; i < ${#CHAIN[@]} - 1; i++ )); do
        top="${CHAIN[$i]}"
        echo
        info "commit $(basename "$top") -> $(basename "$BASE")"
        virsh blockcommit "$DOMAIN" "$TARGET" \
              --base "$BASE" --top "$top" --wait --verbose \
            || die "commit failed on $top -- copies are in $BACKUP_DIR, try -F instead"
    done

    echo
    info "commit of the active layer $(basename "$ACTIVE_PATH")"
    virsh blockcommit "$DOMAIN" "$TARGET" --active --pivot --wait --verbose \
        || die "active commit failed -- copies are in $BACKUP_DIR, try -F instead"

    echo
    info "done, the domain now writes to $BASE"
fi

# --- 8. Cold exit -------------------------------------------------------------
# Put a fresh copy of the active overlay back at the path the domain XML already
# points at, then restart. No XML edit needed.
if [ "$DO_COLD" -eq 1 ]; then
    [ "${DELETED_OF[$ACTIVE_PATH]:-0}" = 1 ] \
        || die "$ACTIVE_PATH still exists on disk; cold exit is pointless"

    echo
    warn "the domain will be shut down and restarted"
    confirm "Proceed with the cold exit on '$DOMAIN'?" || { info "cold exit skipped"; exit 0; }

    # Quiesce the guest so the copy is filesystem-consistent. domfsfreeze needs
    # qemu-guest-agent; without it, suspend gives a crash-consistent copy that
    # the guest journal replays at boot.
    QUIESCE=""
    if virsh domfsfreeze "$DOMAIN" >/dev/null 2>&1; then
        QUIESCE="freeze"
        info "guest filesystems frozen (guest agent)"
    else
        virsh suspend "$DOMAIN" >/dev/null
        QUIESCE="suspend"
        warn "no guest agent, domain suspended (crash-consistent copy)"
    fi

    unquiesce() {
        case "$QUIESCE" in
            freeze)  virsh domfsthaw "$DOMAIN" >/dev/null 2>&1 || true ;;
            suspend) virsh resume "$DOMAIN" >/dev/null 2>&1 || true ;;
        esac
        QUIESCE=""
    }
    trap unquiesce EXIT

    info "fresh copy of the active overlay -> $ACTIVE_PATH"
    cp --sparse=always "${FD_OF[$ACTIVE_PATH]}" "$ACTIVE_PATH"
    chown --reference="$(dirname "$ACTIVE_PATH")" "$ACTIVE_PATH" 2>/dev/null || true

    unquiesce
    trap - EXIT

    info "checking $ACTIVE_PATH"
    qemu-img check "$ACTIVE_PATH" || die "check failed; domain left running, nothing lost"

    info "stopping the domain"
    virsh destroy "$DOMAIN" >/dev/null

    # A deep chain trips the same JSON limit on start, so flatten offline rather
    # than hand back a domain that cannot boot.
    if [ "${#CHAIN[@]}" -gt 8 ]; then
        echo
        warn "${#CHAIN[@]} layers: libvirt will likely refuse to start the domain"
        warn "run the offline flatten now:"
        echo "    $0 -d $DOMAIN -F"
        exit 0
    fi

    virsh start "$DOMAIN" >/dev/null
    info "domain restarted on the restored chain"
fi

if [ $(( DO_COMMIT + DO_COLD )) -eq 0 ]; then
    echo
    info "backup done. Live flatten: -c. Cold exit: -C. Offline flatten: -F"
fi
