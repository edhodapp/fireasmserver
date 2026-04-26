#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Block-level hot backup of the running Pi 5 SD card via SSH + dd.
# Per D036. Pi stays up; no SD card removal for the backup.
#
# Output: a sparse .img under build/pi-backup/images/ that
# flash_sd_card.sh can restore to any USB-readable SD card. (The restore
# side does need the card in a reader, but that only happens on card
# replacement — not routine.)
#
# Consistency model: crash-consistent (equivalent to pulling power
# during the dd read). The journal is whatever state it was in; normal
# fsck replay on restore boot handles it.
#
# Zero-fill (opt-in via --zerofill): writes zeros into free rootfs space
# before dd so the conv=sparse receive produces a compact image. Writes
# ~120 GB to the SD card and consumes some write endurance — run this
# periodically (monthly), not per-backup.

set -euo pipefail

PI_HOST="${PI_HOST:-10.0.2.2}"
PI_USER="${PI_USER:-ed}"
PI_DEV="${PI_DEV:-/dev/mmcblk0}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fireasm_pi5_ed}"
ZEROFILL="no"

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [--zerofill]

Options:
  --zerofill   Zero-fill Pi rootfs free space before dd so the output
               image is compact. Run periodically (monthly), not
               per-backup — it writes the full card.

Environment:
  PI_HOST  (default 10.0.2.2)
  PI_USER  (default ed)
  PI_DEV   (default /dev/mmcblk0)
  SSH_KEY  (default ~/.ssh/fireasm_pi5_ed)
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --zerofill) ZEROFILL="yes"; shift ;;
        -h|--help)  usage ;;
        *)          usage ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user." >&2
    exit 1
fi
[[ -f "$SSH_KEY" ]] || { echo "ERROR: SSH key missing at $SSH_KEY" >&2; exit 1; }

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
BACKUP_DIR="$REPO_ROOT/build/pi-backup/images"
mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
OUT_IMG="$BACKUP_DIR/pi5-${TIMESTAMP}-${GIT_SHA}.img"

SSH_OPTS=(
    -i "$SSH_KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
)

echo "=== Pi 5 block-level backup (D036) ==="
echo "Pi:          $PI_USER@$PI_HOST ($PI_DEV)"
echo "Output:      $OUT_IMG"
echo "Zero-fill:   $ZEROFILL"
echo

# Sudo precheck: every subsequent ssh call needs passwordless sudo on the
# Pi (D023 default). Fail loud here instead of hanging inside a later call
# on a silent password prompt that BatchMode won't handle.
if ! ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "sudo -n true" 2>/dev/null; then
    echo "ERROR: 'sudo -n true' failed on the Pi. Passwordless sudo is required" >&2
    echo "       (D023 default via PASSWORDLESS_SUDO=1 in the pi-gen config)." >&2
    exit 1
fi

# Sanity: Pi reachable, device present, size known.
DEV_SIZE="$(ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "sudo blockdev --getsize64 $PI_DEV")"
[[ "$DEV_SIZE" =~ ^[0-9]+$ ]] || {
    echo "ERROR: could not read device size from Pi ('$DEV_SIZE')" >&2
    exit 1
}
echo "Device size: $DEV_SIZE bytes ($((DEV_SIZE / 1024 / 1024 / 1024)) GiB)"
echo

### Optional zero-fill phase ########################################
if [[ "$ZEROFILL" == "yes" ]]; then
    echo "--- zero-filling Pi rootfs free space (slow; writes full card) ---"
    # dd will return ENOSPC when the partition fills — expected.
    # The trap ensures /fireasm-zerofill is removed even if SSH drops mid-
    # run or the user interrupts; otherwise a 100+ GB orphan could sit on
    # the rootfs until manually cleaned.
    ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "sudo bash -s" <<'REMOTE' || true
set -u
rm -f /fireasm-zerofill
trap 'rm -f /fireasm-zerofill; sync' EXIT INT TERM HUP
dd if=/dev/zero of=/fireasm-zerofill bs=4M status=progress 2>&1 | tail -n 20
sync
REMOTE
    echo "--- zero-fill complete ---"
    echo
fi

### Hot-dd via SSH, receive with conv=sparse ########################
echo "--- streaming $PI_DEV from Pi over SSH (this takes a while) ---"
ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" \
    "sudo dd if=$PI_DEV bs=4M status=none" \
    | dd of="$OUT_IMG" bs=4M conv=sparse status=progress

### Short-image check ###############################################
# pipefail + set -e don't reliably catch a remote dd that dies mid-stream
# while keeping the SSH channel open — the local dd then completes on a
# truncated input and we'd silently accept a short .img. Verify length.
LOCAL_SIZE="$(stat -c%s "$OUT_IMG")"
if [[ "$LOCAL_SIZE" -ne "$DEV_SIZE" ]]; then
    echo "ERROR: backup is short — got $LOCAL_SIZE bytes, expected $DEV_SIZE." >&2
    echo "       Likely SSH drop or remote dd failure mid-stream." >&2
    echo "       Partial image left at $OUT_IMG for inspection." >&2
    exit 1
fi

### Report ##########################################################
APPARENT="$(du -h --apparent-size "$OUT_IMG" | cut -f1)"
ACTUAL="$(du -h "$OUT_IMG" | cut -f1)"
echo
echo "=== backup complete ==="
echo "Apparent size: $APPARENT  (= card size; sparse holes count here)"
echo "On-disk size:  $ACTUAL    (= actual laptop storage used)"
echo
echo "To restore to a new SD card in a USB reader:"
echo "  IMG_PATH=$OUT_IMG ./tooling/pi5_build/flash_sd_card.sh /dev/sdX"
