#!/bin/sh
# bdwn-libvirt - cold backup of a libvirt VM into the bdwn restic repository.
# Workflow: clean shutdown, cold external snapshot (qcow2 overlays), restart
# the VM, then export the stable base images + domain XML through
# '/usr/bin/bdwn restic backup' (which owns the repo, passphrase and SSH
# privilege drop). Finally the overlays are merged back (blockcommit) and
# removed. A VM that is already shut off is exported directly, no snapshot.
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
STAGING_DIR="/var/backups/bdwn/libvirt"
SNAP_NAME="bdwn"

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

# Base disk images (device 'disk' only, cdroms skipped), captured before any
# snapshot so they point to the real files. Paths must not contain spaces.
BASE_DISKS=$(virsh domblklist "$VM" --details | awk 'NR>2 && $2=="disk" {print $4}')
DISK_TARGETS=$(virsh domblklist "$VM" --details | awk 'NR>2 && $2=="disk" {print $3}')
[ -n "$BASE_DISKS" ] || die "no disk found for domain $VM"

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

    echo "bdwn-libvirt: shutting down $VM (timeout ${SHUTDOWN_TIMEOUT}s)"
    virsh shutdown "$VM" >/dev/null
    RESTART_ON_FAIL=1
    waited=0
    while [ "$(virsh domstate "$VM")" != "shut off" ]; do
        [ "$waited" -lt "$SHUTDOWN_TIMEOUT" ] || die "$VM did not shut down within ${SHUTDOWN_TIMEOUT}s"
        sleep 5
        waited=$((waited + 5))
    done

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
    # Merge the overlays back into the base images while the VM runs, then
    # restore the persistent definition (blockcommit only pivots the live
    # config) and drop the overlay files.
    for target in $DISK_TARGETS; do
        echo "bdwn-libvirt: merging overlay of $target"
        virsh blockcommit "$VM" "$target" --active --pivot --wait >/dev/null
    done
    virsh define "$DOMAIN_XML" >/dev/null
    for overlay in $OVERLAYS; do
        rm -f "$overlay"
    done
fi

echo "bdwn-libvirt: backup of $VM done"
