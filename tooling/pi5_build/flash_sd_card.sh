#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Flash the Pi 5 test-host image to an SD card.
#
# Uses udisksctl (not plain umount) so the desktop session knows the unmount
# is intentional — some cheap multi-slot USB readers park the card after a
# plain umount, causing /dev/sdX to report "No medium found" on the next write.
#
# Per D022-D034 design.

set -euo pipefail

### Usage ############################################################
usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") /dev/sdX

Flash the fireasmserver Pi 5 test image to an SD card.

Argument:
  /dev/sdX    Whole-device node for the target SD card (NOT a partition).
              Only /dev/sdX-style nodes from USB card readers are accepted.
              Built-in SD slots (/dev/mmcblk0 etc.) and NVMe devices are
              rejected by design — writing to an internal card slot on a
              laptop is almost never the intent, and the size check would
              not catch a small internal eMMC. If you *really* need a
              /dev/mmcblk* target, edit this script deliberately.

Environment:
  IMG_PATH    Path to the .img file. Defaults to the most recent
              *-fireasm-test-lite.img under build/pi-image/ at the repo root.

Safety:
  - Refuses anything that isn't USB + removable.
  - Refuses devices larger than 256 GiB (almost certainly the wrong target).
  - Refuses /dev/mmcblk* and NVMe (see above).
  - Prompts for 'yes' confirmation before writing.
EOF
    exit 1
}

[[ $# -eq 1 ]] || usage
DEV="$1"

# Whole-device only, USB-style only: /dev/sda, /dev/sdb, /dev/sdaa...
# Rejects partitions (/dev/sdb1), NVMe (/dev/nvme*), internal SD slots
# (/dev/mmcblk*), loop/zero/null/full, and anything else that isn't a
# USB card reader node. Matches the usage documentation above.
[[ "$DEV" =~ ^/dev/sd[a-z]+$ ]] || {
    echo "ERROR: device must be a /dev/sdX-style USB card-reader node." >&2
    echo "       Rejected patterns include /dev/sdb1 (partition), /dev/nvme*," >&2
    echo "       /dev/mmcblk*, /dev/loop*, /dev/zero, /dev/null, /dev/full." >&2
    exit 1
}
[[ -b "$DEV" ]] || { echo "ERROR: $DEV is not a block device" >&2; exit 1; }

### Image path #######################################################
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if [[ -z "${IMG_PATH:-}" ]]; then
    # shellcheck disable=SC2012  # ls -t is correct here; we want mtime ordering
    IMG_PATH="$(ls -t "$REPO_ROOT"/build/pi-image/*-fireasm-test-lite.img 2>/dev/null | head -n1 || true)"
fi
[[ -n "${IMG_PATH:-}" && -f "$IMG_PATH" ]] || {
    echo "ERROR: no image found. Run build_pi_image.sh first or set IMG_PATH." >&2
    exit 1
}

### Verify target is USB + removable + sane size ####################
TRAN="$(lsblk -no TRAN "$DEV" | head -n1 | tr -d ' ')"
RM="$(lsblk -no RM "$DEV" | head -n1 | tr -d ' ')"
SIZE_BYTES="$(lsblk -bno SIZE "$DEV" | head -n1 | tr -d ' ')"
SIZE_GIB=$(( SIZE_BYTES / 1024 / 1024 / 1024 ))

[[ "$TRAN" == "usb" ]] || {
    echo "ERROR: $DEV is not on USB (transport=$TRAN). Refusing." >&2; exit 1;
}
[[ "$RM" == "1" ]] || {
    echo "ERROR: $DEV is not removable (RM=$RM). Refusing." >&2; exit 1;
}
[[ "$SIZE_BYTES" -gt 0 ]] || {
    echo "ERROR: $DEV reports zero size — reseat the card and try again." >&2; exit 1;
}
[[ "$SIZE_GIB" -le 256 ]] || {
    echo "ERROR: $DEV is ${SIZE_GIB} GiB, larger than 256. Almost certainly wrong target. Refusing." >&2
    exit 1
}

### Summary + confirmation ##########################################
echo
echo "=== SD card flash ==="
echo "Image:  $IMG_PATH"
echo "  size: $(du -h "$IMG_PATH" | cut -f1)"
echo "Target: $DEV (${SIZE_GIB} GiB)"
echo "  layout:"
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,MODEL,TRAN,RM "$DEV" | sed 's/^/    /'
echo
echo "!!! THIS WILL DESTROY ALL DATA ON $DEV !!!"
read -r -p "Type 'yes' to proceed: " CONFIRM
[[ "$CONFIRM" == "yes" ]] || { echo "Aborted."; exit 1; }

### Unmount any auto-mounted partitions via udisks ##################
for part in $(lsblk -lno NAME,TYPE "$DEV" | awk '$2=="part"{print $1}'); do
    MP="$(lsblk -no MOUNTPOINT "/dev/$part" | head -n1 | tr -d ' ')"
    if [[ -n "$MP" ]]; then
        echo "--- unmounting /dev/$part ($MP) via udisksctl ---"
        udisksctl unmount -b "/dev/$part"
    fi
done

### Prime sudo now so the write doesn't pause mid-transfer ##########
sudo -v

### Flash ###########################################################
echo
echo "--- writing image (~5-10 min USB 2.0, 1-2 min USB 3.0) ---"
sudo dd if="$IMG_PATH" of="$DEV" bs=4M status=progress conv=fsync
echo
echo "--- syncing ---"
sudo sync

### Power off the card ##############################################
echo
echo "--- powering off $DEV ---"
sudo eject "$DEV" || echo "(eject returned non-zero; card is still safe to remove once LED stops)"

echo
echo "=== done ==="
echo "Insert into Pi 5 and power on."
