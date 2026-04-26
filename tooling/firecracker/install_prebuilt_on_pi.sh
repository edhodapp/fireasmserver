#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install Firecracker on the Pi 5 from the official upstream release
# tarball. Per D037 (amends D026). Downloads to the laptop, verifies
# SHA256, scp's to the Pi, installs to /usr/local/bin via sudo.
#
# Pi has no direct internet route (D022/D024); all network fetches
# happen on the laptop and the binary crosses the isolated bridge via
# scp.
#
# Idempotent — safe to re-run. Overwrites /usr/local/bin/firecracker
# on the Pi with the current pinned version.

set -euo pipefail

VERSION="${FIRECRACKER_VERSION:-v1.15.1}"
PI_HOST="${PI_HOST:-10.0.2.2}"
PI_USER="${PI_USER:-ed}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fireasm_pi5_ed}"

# Pi 5 is aarch64. If we ever run this against an x86_64 target, drive
# the arch via env.
ARCH="${FIRECRACKER_ARCH:-aarch64}"

# Validate env overrides. VERSION flows into the download URL; reject
# anything that isn't vNN.NN.NN so a typo or malicious override can't
# redirect the fetch to an unexpected GitHub path.
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "ERROR: FIRECRACKER_VERSION='$VERSION' must be vNN.NN.NN" >&2
    exit 1
}
[[ "$ARCH" =~ ^(aarch64|x86_64)$ ]] || {
    echo "ERROR: FIRECRACKER_ARCH='$ARCH' must be aarch64 or x86_64" >&2
    exit 1
}

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user; the script uses ssh/sudo internally." >&2
    exit 1
fi
[[ -f "$SSH_KEY" ]] || { echo "ERROR: SSH key missing at $SSH_KEY" >&2; exit 1; }

SSH_OPTS=(
    -i "$SSH_KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
)

echo "=== Firecracker $VERSION ($ARCH) install on Pi (D037) ==="
echo "  Pi:   $PI_USER@$PI_HOST"
echo

### Download + verify on the laptop ##################################
DOWNLOAD_DIR="$(mktemp -d)"
# Cover normal exit, Ctrl-C, and SIGTERM. Default `trap EXIT` alone does
# not always fire on SIGINT/SIGTERM under bash + set -e.
trap 'rm -rf "$DOWNLOAD_DIR"' EXIT INT TERM

BASE="https://github.com/firecracker-microvm/firecracker/releases/download/${VERSION}"
TARBALL_NAME="firecracker-${VERSION}-${ARCH}.tgz"
CHECKSUM_NAME="${TARBALL_NAME}.sha256.txt"

echo "--- downloading $TARBALL_NAME ---"
curl -fLsS -o "$DOWNLOAD_DIR/$TARBALL_NAME"  "$BASE/$TARBALL_NAME"
curl -fLsS -o "$DOWNLOAD_DIR/$CHECKSUM_NAME" "$BASE/$CHECKSUM_NAME"

echo "--- verifying SHA256 ---"
(cd "$DOWNLOAD_DIR" && sha256sum -c "$CHECKSUM_NAME")

# Tar-slip defense: SHA256 protects against tampering-in-transit, but a
# compromised release (same org keys) could ship a tarball with absolute
# paths or .. components. Refuse before extracting.
echo "--- validating tarball layout ---"
if tar -tzf "$DOWNLOAD_DIR/$TARBALL_NAME" | grep -qE '^(/|.*\.\./)'; then
    echo "ERROR: tarball contains unsafe paths (absolute or ..)" >&2
    exit 1
fi

echo "--- extracting ---"
tar --no-same-owner -xzf "$DOWNLOAD_DIR/$TARBALL_NAME" -C "$DOWNLOAD_DIR"

# Release tarball layout: release-<version>-<arch>/firecracker-<version>-<arch> (and jailer-...)
FC_BIN="$(find "$DOWNLOAD_DIR" -name "firecracker-${VERSION}-${ARCH}" -type f ! -name '*.tgz' | head -1)"
[[ -n "$FC_BIN" ]] || {
    echo "ERROR: firecracker binary not found in tarball layout" >&2
    find "$DOWNLOAD_DIR" -type f >&2
    exit 1
}

# Confirm it's actually the right arch (guards against a rehosted/bad tarball).
FILE_INFO="$(file "$FC_BIN")"
case "$ARCH" in
    aarch64)
        grep -q "ARM aarch64" <<< "$FILE_INFO" \
            || { echo "ERROR: binary is not aarch64 ($FILE_INFO)" >&2; exit 1; } ;;
    x86_64)
        grep -q "x86-64"      <<< "$FILE_INFO" \
            || { echo "ERROR: binary is not x86_64 ($FILE_INFO)" >&2; exit 1; } ;;
esac
echo "--- binary type confirmed: $FILE_INFO ---"

### Install on the Pi ################################################
echo
echo "--- verifying SSH reachable ---"
ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" true

echo "--- verifying passwordless sudo ---"
if ! ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "sudo -n true" 2>/dev/null; then
    echo "ERROR: sudo -n true failed on Pi — passwordless sudo required (D023)." >&2
    exit 1
fi

# Use a fresh mktemp'd path on the Pi rather than the predictable
# /tmp/firecracker.new, to close the symlink-race window on a multi-user
# host (a pre-created symlink to a sensitive path could otherwise trick
# sudo install into writing there via the source side).
echo "--- staging binary to a fresh path on Pi ---"
PI_TMP="$(ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" 'mktemp /tmp/firecracker.XXXXXXXX')"
[[ "$PI_TMP" =~ ^/tmp/firecracker\.[A-Za-z0-9]+$ ]] || {
    echo "ERROR: Pi returned unexpected mktemp path: '$PI_TMP'" >&2
    exit 1
}
scp "${SSH_OPTS[@]}" "$FC_BIN" "$PI_USER@$PI_HOST:$PI_TMP" >/dev/null

echo "--- installing to /usr/local/bin/firecracker ---"
ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "
    set -e
    sudo install -m 0755 -o root -g root '$PI_TMP' /usr/local/bin/firecracker
    rm -f '$PI_TMP'
"

### Sanity: check version ############################################
echo
echo "--- Pi-side verification ---"
ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "firecracker --version"

echo
echo "=== Firecracker $VERSION installed on $PI_HOST ==="
