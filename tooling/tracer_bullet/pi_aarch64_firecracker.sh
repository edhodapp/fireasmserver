#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# AArch64 Firecracker tracer bullet.
#
# Builds arch/aarch64/platform/firecracker/guest.bin on the laptop,
# stages it on the Pi via scp, runs Firecracker with a minimal JSON
# config, captures serial output, and verifies the expected "READY"
# marker appears within the timeout.
#
# This is the first end-to-end proof that:
#   laptop ↔ Pi 5 bridge (D024)
#   SSH + passwordless sudo (D023)
#   apt-cacher-ng package flow (D035)
#   Pi 5 kernel with CONFIG_KVM=y (D023/D033)
#   Firecracker installed on Pi (D037)
#   aarch64 boot stub (arch/aarch64)
# are all wired up correctly. If this script exits 0, the pipeline is live.
#
# Pi stays up throughout — no SD handling, no image rebuild.

set -euo pipefail

PI_HOST="${PI_HOST:-10.0.2.2}"
PI_USER="${PI_USER:-ed}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fireasm_pi5_ed}"
TIMEOUT="${TIMEOUT:-5}"   # seconds to wait for READY before giving up
READY_MARKER="${READY_MARKER:-READY}"

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user." >&2
    exit 1
fi
[[ -f "$SSH_KEY" ]] || { echo "ERROR: SSH key missing at $SSH_KEY" >&2; exit 1; }

SSH_OPTS=(
    -i "$SSH_KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
)

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

echo "=== AArch64 Firecracker tracer bullet ==="
echo "  Pi:            $PI_USER@$PI_HOST"
echo "  ready-marker:  $READY_MARKER"
echo "  timeout:       ${TIMEOUT}s"
echo

### 1. Build guest.bin on the laptop ################################
echo "--- building arch/aarch64 PLATFORM=firecracker ---"
make -C arch/aarch64 PLATFORM=firecracker >/dev/null

GUEST_BIN="arch/aarch64/build/firecracker/guest.bin"
[[ -f "$GUEST_BIN" ]] || {
    echo "ERROR: $GUEST_BIN not produced by the build." >&2
    exit 1
}
echo "    $(stat -c '%n (%s bytes)' "$GUEST_BIN")"

### 2. Stage a fresh workdir on the Pi ##############################
PI_TMP="$(ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" 'mktemp -d /tmp/fireasm-tracer.XXXXXXXX')"
[[ "$PI_TMP" =~ ^/tmp/fireasm-tracer\.[A-Za-z0-9]+$ ]] || {
    echo "ERROR: Pi returned unexpected mktemp -d path: '$PI_TMP'" >&2
    exit 1
}

cleanup() {
    # Best-effort: if SSH is down or the Pi rebooted mid-run, we still
    # want the script to exit; a stale /tmp/fireasm-tracer.*/ on the Pi
    # is a minor leak, not a crisis. Next run of this script or any
    # boot-time /tmp sweep handles it.
    if ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" \
            "rm -rf '$PI_TMP'" 2>/dev/null; then
        echo "--- cleanup: removed $PI_USER@$PI_HOST:$PI_TMP ---"
    else
        echo "--- cleanup: could not reach Pi to remove $PI_TMP (Pi may be down) ---"
    fi
}
trap cleanup EXIT INT TERM

echo "--- staging on Pi at $PI_TMP ---"
scp "${SSH_OPTS[@]}" "$GUEST_BIN" "$PI_USER@$PI_HOST:$PI_TMP/guest.bin" >/dev/null

### 3. Write the Firecracker config on the Pi #######################
# Inline heredoc sent over SSH so the kernel_image_path points at the
# Pi-side copy. vcpu_count=1 matches the aarch64 stub's MPIDR gate
# (secondary vCPUs would just park in WFE anyway, but no need to spawn them).
#
# boot_args must contain "console=" — without that substring,
# Firecracker v1.15.1's aarch64 device_manager.attach_legacy_devices_aarch64
# skips register_mmio_serial and the UART is never mapped. We pass
# "console=ttyS0" so the serial device registers at SERIAL_MEM_START
# (0x4000_2000). The stub doesn't parse cmdline, so the token value is
# purely a trigger.
ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "cat > '$PI_TMP/config.json'" <<EOF
{
  "boot-source": {
    "kernel_image_path": "$PI_TMP/guest.bin",
    "boot_args": "console=ttyS0"
  },
  "machine-config": {
    "vcpu_count": 1,
    "mem_size_mib": 128
  },
  "drives": []
}
EOF

### 4. Run Firecracker with a time budget; grep for READY ###########
# Firecracker's aarch64 serial (PL011) output lands on its own stdout
# with --no-api. We redirect that into serial.log, then grep.
#
# The stub WFE-halts forever, so Firecracker never exits on its own —
# `timeout` sends SIGTERM after $TIMEOUT seconds. timeout exits 124
# on the timeout path, which we discard (the READY check is authoritative).
echo "--- launching Firecracker (SIGTERM after ${TIMEOUT}s) ---"
if ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "
    set -u
    cd '$PI_TMP'
    timeout ${TIMEOUT}s firecracker \
        --no-api \
        --config-file config.json \
        --id tracer \
        > serial.log 2>&1 || true
    grep -q '${READY_MARKER}' serial.log
"; then
    echo
    echo "=== tracer bullet PASSED ==="
    echo "  '$READY_MARKER' observed in guest serial output within ${TIMEOUT}s."
    exit 0
else
    echo
    echo "=== tracer bullet FAILED ==="
    echo "  '$READY_MARKER' not observed. Dumping Pi-side serial.log:"
    ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "sed 's/^/    /' '$PI_TMP/serial.log'" || true
    exit 1
fi
