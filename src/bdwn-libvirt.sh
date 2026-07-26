#!/bin/sh
# bdwn-libvirt - cold backup of a libvirt VM into the bdwn restic repository.
# Workflow: clean shutdown, cold external snapshot (qcow2 overlays), restart
# the VM, then export the stable base images + domain XML through
# '/usr/bin/bdwn restic backup' (which owns the repo, passphrase and SSH
# privilege drop). Finally the overlays are merged back and removed, live
# (blockcommit) or cold (qemu-img commit), see BDWN_LIBVIRT_MERGE_MODE.
# A VM that is already shut off is exported directly, no snapshot.
# Meant to be run by the bdwn-libvirt@<vm>.timer systemd template as root.
set -eu

VM="${1:?usage: bdwn-libvirt <vm-name>}"

CONFIG_FILE="${BDWN_CONFIG:-/etc/bdwn/bdwn.conf}"

die() { echo "bdwn-libvirt: $*" >&2; exit 1; }

# Only BDWN_LIBVIRT_* settings are used here; the repo/passphrase part of the
# config is consumed by /usr/bin/bdwn itself.
[ -r "$CONFIG_FILE" ] || die "config file not found or not readable: $CONFIG_FILE"
# shellcheck disable=SC1090
. "$CONFIG_FILE"

SHUTDOWN_TIMEOUT="${BDWN_LIBVIRT_SHUTDOWN_TIMEOUT:-300}"
# How the overlays are merged back once the export is done:
#   auto (default) live merge, cold merge if the live one is refused
#   live           live merge only, fail otherwise
#   cold           cold merge only, no live attempt
MERGE_MODE="${BDWN_LIBVIRT_MERGE_MODE:-auto}"
STAGING_DIR="/var/backups/bdwn/libvirt"
SNAP_NAME="bdwn"

case "$MERGE_MODE" in
auto | live | cold) ;;
*) die "invalid BDWN_LIBVIRT_MERGE_MODE: $MERGE_MODE (auto, live or cold)" ;;
esac

virsh dominfo "$VM" >/dev/null 2>&1 || die "unknown libvirt domain: $VM"

# Restart the VM if the script dies between the clean shutdown and the
# restart: a failed backup must not leave the machine down.
RESTART_ON_FAIL=0
on_exit() {
    status=$?
    if [ "$status" -ne 0 ] && [ "$RESTART_ON_FAIL" = 1 ]; then
        echo "bdwn-libvirt: failure, restarting $VM" >&2
        virsh start "$VM" >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap on_exit EXIT

# Ask the guest to power off and wait for it. Callers must set
# RESTART_ON_FAIL=1 first: from here on a failure leaves the VM down.
shutdown_vm() {
    echo "bdwn-libvirt: shutting down $VM (timeout ${SHUTDOWN_TIMEOUT}s)"
    # Default mode goes through the guest agent when a channel is defined in
    # the domain XML; a broken agent then fails the whole call (e.g. 'failed
    # to parse JSON'), so fall back to a plain ACPI power button press.
    if ! virsh shutdown "$VM" >/dev/null 2>&1; then
        echo "bdwn-libvirt: default shutdown failed, falling back to ACPI"
        virsh shutdown --mode acpi "$VM" >/dev/null
    fi
    waited=0
    while [ "$(virsh domstate "$VM")" != "shut off" ]; do
        [ "$waited" -lt "$SHUTDOWN_TIMEOUT" ] || die "$VM did not shut down within ${SHUTDOWN_TIMEOUT}s"
        sleep 5
        waited=$((waited + 5))
    done
}

# Point the persistent definition back at the base images and drop the
# overlays. blockcommit only pivots the live config and the cold merge does
# not touch the definition at all, so both paths end here.
restore_definition() {
    virsh define "$DOMAIN_XML" >/dev/null
    for overlay in $OVERLAYS; do
        rm -f "$overlay"
    done
}

# Live merge: commit each overlay into its base image while the VM runs.
# Refused on hosts where AppArmor confines qemu: virt-aa-helper writes an
# explicit 'deny <base image> w,' rule for read-only backing files, and an
# AppArmor deny cannot be overridden by any allow rule.
merge_live() {
    for target in $DISK_TARGETS; do
        echo "bdwn-libvirt: merging overlay of $target"
        virsh blockcommit "$VM" "$target" --active --pivot --wait >/dev/null || return 1
    done
}

# Cold merge: stop the VM and commit the overlays with qemu-img, which runs as
# root outside the per-VM AppArmor profile. Costs a second downtime, short
# since an overlay only holds the writes made since the snapshot. A failed
# commit leaves a valid chain (qemu-img copies blocks down without touching
# the overlay), so the trap can safely restart the VM on its overlay.
merge_cold() {
    RESTART_ON_FAIL=1
    shutdown_vm
    for overlay in $OVERLAYS; do
        echo "bdwn-libvirt: committing overlay $overlay"
        qemu-img commit "$overlay" >/dev/null || die "qemu-img commit failed for $overlay"
    done
    restore_definition
    echo "bdwn-libvirt: restarting $VM"
    virsh start "$VM" >/dev/null
    RESTART_ON_FAIL=0
}

# Base disk images (device 'disk' only, cdroms skipped), captured before any
# snapshot so they point to the real files. Paths must not contain spaces.
BASE_DISKS=$(virsh domblklist "$VM" --details | awk 'NR>2 && $2=="disk" {print $4}')
DISK_TARGETS=$(virsh domblklist "$VM" --details | awk 'NR>2 && $2=="disk" {print $3}')
[ -n "$BASE_DISKS" ] || die "no disk found for domain $VM"

# A disk still pointing to a .bdwn overlay means a previous run failed after
# the snapshot: backing up (and snapshotting) on top of it would stack
# overlays, refuse and let the admin merge it back first.
for disk in $BASE_DISKS; do
    case "$disk" in
    *."$SNAP_NAME")
        die "leftover overlay from a failed run: $disk
Merge it back before retrying (see README, libvirt troubleshooting):
  virsh shutdown $VM   # wait for 'shut off'
  qemu-img commit $disk
  virsh define /var/backups/bdwn/libvirt/$VM.xml
  rm $disk
  virsh start $VM" ;;
    esac
done

# Persistent domain definition, dumped before the snapshot: it references the
# base images (consistent with what is exported) and is used to restore the
# persistent config after the blockcommit pivot.
mkdir -p "$STAGING_DIR"
chmod 0700 "$STAGING_DIR"
DOMAIN_XML="$STAGING_DIR/$VM.xml"
virsh dumpxml --inactive --security-info "$VM" > "$DOMAIN_XML"

WAS_RUNNING=0
if [ "$(virsh domstate "$VM")" = "running" ]; then
    WAS_RUNNING=1

    # An overlay file left behind by a previous run (merged back by hand but
    # never deleted) makes 'snapshot-create-as' fail. Check it here, before
    # shutting the VM down, instead of failing mid-backup. Same naming rule as
    # libvirt: base file name with its last extension replaced by the snapshot
    # name, in the same directory.
    for disk in $BASE_DISKS; do
        name=$(basename "$disk")
        case "$name" in
        *.*) name="${name%.*}" ;;
        esac
        overlay="$(dirname "$disk")/$name.$SNAP_NAME"
        [ -e "$overlay" ] || continue
        die "orphaned overlay file from a previous run: $overlay
$VM currently runs on its base image $disk, so this overlay is stale and
cannot be merged back. Check then remove it before retrying:
  qemu-img info $overlay
  rm $overlay"
    done

    RESTART_ON_FAIL=1
    shutdown_vm

    # Cold snapshot: qcow2 overlays created while the VM is off. The VM then
    # writes to the overlays, leaving the base images stable for the export.
    # --no-metadata: libvirt does not track the snapshot, cleanup is just
    # blockcommit + rm of the overlay files.
    echo "bdwn-libvirt: taking cold snapshot of $VM"
    virsh snapshot-create-as "$VM" "$SNAP_NAME" --disk-only --atomic --no-metadata >/dev/null
    OVERLAYS=$(virsh domblklist "$VM" --details | awk 'NR>2 && $2=="disk" {print $4}')

    echo "bdwn-libvirt: restarting $VM"
    virsh start "$VM" >/dev/null
    RESTART_ON_FAIL=0
fi

# Export domain XML and base images. restic reads the base files in place:
# no local copy is needed, the running VM only touches the overlays.
echo "bdwn-libvirt: exporting $VM to the bdwn repository"
bdwn init
# shellcheck disable=SC2086
bdwn restic backup --verbose --tag "libvirt-$VM" "$DOMAIN_XML" $BASE_DISKS

if [ "$WAS_RUNNING" = 1 ]; then
    if [ "$MERGE_MODE" = cold ]; then
        merge_cold
    elif merge_live; then
        restore_definition
    elif [ "$MERGE_MODE" = live ]; then
        die "blockcommit failed: the backup itself succeeded and $VM keeps running
safely on its overlay. Usual cause on Debian: AppArmor denies qemu the
read-write reopen of the base image. Set BDWN_LIBVIRT_MERGE_MODE=auto (or
cold) to merge with the VM stopped instead, or merge by hand (see README,
libvirt troubleshooting)."
    else
        echo "bdwn-libvirt: live merge refused, falling back to a cold merge" >&2
        merge_cold
    fi
fi

echo "bdwn-libvirt: backup of $VM done"
