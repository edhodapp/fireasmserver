#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Fast incremental backup of a running Pi 5 rootfs via rsync over SSH,
# with hardlink-snapshotted timestamped dirs on the laptop. Per D036.
#
# Pi stays up; no SD card removal. Seconds-to-minutes after first sync.
# Covers the "I broke something, put me back" case via targeted
# rsync-restore. Pairs with backup_pi_dd.sh for bit-perfect SD death
# recovery.
#
# Output layout:
#   build/pi-backup/snapshots/YYYYMMDDTHHMMSSZ/   (newest)
#   build/pi-backup/snapshots/...older dirs...
#   build/pi-backup/snapshots/latest              (symlink to newest)
#
# Storage: rsync --link-dest shares unchanged files across snapshots via
# hardlinks, so keeping 30+ snapshots costs almost nothing.

set -euo pipefail

PI_HOST="${PI_HOST:-10.0.0.2}"
PI_USER="${PI_USER:-ed}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fireasm_pi5_ed}"

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user." >&2
    exit 1
fi
[[ -f "$SSH_KEY" ]] || { echo "ERROR: SSH key missing at $SSH_KEY" >&2; exit 1; }

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
BACKUP_ROOT="$REPO_ROOT/build/pi-backup/snapshots"
mkdir -p "$BACKUP_ROOT"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SNAPSHOT_DIR="$BACKUP_ROOT/$TIMESTAMP"
LATEST_LINK="$BACKUP_ROOT/latest"

# Refuse to --delete into a pre-existing directory. This guards against
# same-second timestamp collisions and against anyone manually pre-populating
# the path under $BACKUP_ROOT — rsync --delete would otherwise prune their
# files.
if [[ -e "$SNAPSHOT_DIR" ]]; then
    echo "ERROR: $SNAPSHOT_DIR already exists. Refusing to run rsync --delete into it." >&2
    echo "       (Re-run in a new UTC second, or remove the directory deliberately.)" >&2
    exit 1
fi

# Single-string SSH command for rsync -e. SSH_KEY assumed to have no
# whitespace (standard ~/.ssh paths).
SSH_CMD="ssh -i $SSH_KEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

echo "=== Pi 5 rsync backup (D036) ==="
echo "Pi:       $PI_USER@$PI_HOST"
echo "Snapshot: $SNAPSHOT_DIR"

# Incremental against previous latest snapshot (hardlinks for unchanged
# files). --link-dest takes an absolute path.
LINK_DEST=()
if [[ -L "$LATEST_LINK" ]]; then
    PREV="$(readlink -f "$LATEST_LINK")"
    if [[ -d "$PREV" ]]; then
        LINK_DEST=(--link-dest="$PREV")
        echo "Incremental against: $PREV"
    fi
fi
echo

# -a   archive (perms, owner, group, times, symlinks, recurse)
# -A   ACLs
# -X   extended attributes
# -H   hardlinks preserved within the transfer
# --numeric-ids  keep numeric uid/gid (restores cleanly as-is)
# --rsync-path='sudo rsync'  run rsync as root on Pi for full rootfs read
rsync -aAXH --numeric-ids --info=progress2 --delete "${LINK_DEST[@]}" \
    --exclude='/proc/*'        \
    --exclude='/sys/*'         \
    --exclude='/dev/*'         \
    --exclude='/run/*'         \
    --exclude='/tmp/*'         \
    --exclude='/var/tmp/*'     \
    --exclude='/var/cache/apt/archives/*.deb' \
    --exclude='/home/*/.cache/*' \
    --exclude='/root/.cache/*' \
    --exclude='/lost+found/*'  \
    --exclude='/swapfile'      \
    --rsync-path='sudo rsync'  \
    -e "$SSH_CMD"              \
    "$PI_USER@$PI_HOST:/" "$SNAPSHOT_DIR/"

# Atomic update of 'latest' symlink.
ln -snfT "$SNAPSHOT_DIR" "$LATEST_LINK"

echo
echo "=== backup complete ==="
# `du -sh` gives real on-disk usage (hardlinks with prior snapshots are
# deduped by the filesystem — this is the incremental cost of THIS run).
# `du -sh --apparent-size` gives the total apparent size regardless of
# hardlinking — what restore would "weigh" logically.
echo "On disk (incremental cost after hardlink-dedup): $(du -sh "$SNAPSHOT_DIR" 2>/dev/null | cut -f1)"
echo "Apparent (logical rootfs size):                  $(du -sh --apparent-size "$SNAPSHOT_DIR" 2>/dev/null | cut -f1)"
echo
echo "To restore selected files:"
echo "  rsync -aAX -e \"$SSH_CMD\" --rsync-path='sudo rsync' \\"
echo "      $SNAPSHOT_DIR/<path> $PI_USER@$PI_HOST:/<path>"
echo
echo "Full-rootfs restore is NOT recommended into a live Pi (rsync'ing over"
echo "/etc /var /run while services are active is unsafe). For SD death,"
echo "use backup_pi_dd.sh + flash_sd_card.sh instead."
