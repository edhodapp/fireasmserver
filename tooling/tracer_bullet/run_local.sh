#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Launch the pre-built guest for <arch>/<platform> on the local host,
# capture serial output to a temp file, and verify the READY marker
# appears within the configured timeout.
#
# Usage: run_local.sh <arch> <platform>
#
# Arch/platform dispatch:
#   x86_64/qemu        — qemu-system-x86_64 -machine pc (Multiboot1)
#   x86_64/firecracker — firecracker --no-api --config-file (PVH ELF64)
#   aarch64/qemu       — qemu-system-aarch64 -M virt (Linux Image, PL011)
#   aarch64/firecracker — NOT viable from an x86_64 host; delegates to
#                         tooling/tracer_bullet/pi_aarch64_firecracker.sh
#                         for Pi-side execution and returns 0 (skip)
#                         so CD matrix cells can call this uniformly.
#
# Environment:
#   TIMEOUT   seconds to wait for READY before killing the VM (default 5)

set -euo pipefail

ARCH="${1:?usage: $0 <arch> <platform>}"
PLATFORM="${2:?usage: $0 <arch> <platform>}"
TIMEOUT="${TIMEOUT:-5}"

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT INT TERM
SERIAL="$TMPDIR/serial.log"

echo "=== tracer bullet: $ARCH/$PLATFORM (timeout ${TIMEOUT}s) ==="

launch_qemu_x86_64() {
    local guest="arch/x86_64/build/qemu/guest.elf"
    [[ -f "$guest" ]] || { echo "missing $guest — run make first" >&2; exit 1; }
    timeout "${TIMEOUT}s" qemu-system-x86_64 \
        -machine pc -cpu qemu64 -m 128 \
        -display none -no-reboot \
        -serial "file:$SERIAL" \
        -kernel "$guest" || true
}

launch_firecracker_x86_64() {
    local guest="$REPO_ROOT/arch/x86_64/build/firecracker/guest.elf"
    [[ -f "$guest" ]] || { echo "missing $guest — run make first" >&2; exit 1; }
    cat > "$TMPDIR/fc.json" <<EOF
{
  "boot-source": { "kernel_image_path": "$guest" },
  "machine-config": { "vcpu_count": 1, "mem_size_mib": 128 },
  "drives": []
}
EOF
    (cd "$TMPDIR" && \
        timeout "${TIMEOUT}s" firecracker \
            --no-api --config-file fc.json --id tracer \
            > "$SERIAL" 2>&1 || true)
}

launch_qemu_aarch64() {
    local guest="arch/aarch64/build/qemu/guest.bin"
    [[ -f "$guest" ]] || { echo "missing $guest — run make first" >&2; exit 1; }
    timeout "${TIMEOUT}s" qemu-system-aarch64 \
        -M virt -cpu cortex-a72 -m 128 \
        -display none -no-reboot \
        -serial "file:$SERIAL" \
        -kernel "$guest" || true
}

case "$ARCH/$PLATFORM" in
    x86_64/qemu)        launch_qemu_x86_64 ;;
    x86_64/firecracker) launch_firecracker_x86_64 ;;
    aarch64/qemu)       launch_qemu_aarch64 ;;
    aarch64/firecracker)
        echo "SKIP: aarch64/firecracker is not viable from an x86_64 host"
        echo "      (needs aarch64 KVM; no /dev/kvm on free arm64 hosted CI)."
        echo "      Use tooling/tracer_bullet/pi_aarch64_firecracker.sh"
        echo "      for Pi-side execution. Returning 0."
        exit 0
        ;;
    *)
        echo "ERROR: unknown cell '$ARCH/$PLATFORM'" >&2
        exit 1
        ;;
esac

if grep -q 'READY' "$SERIAL"; then
    echo "READY observed — PASS"
    exit 0
fi

echo "FAIL: READY not observed in ${TIMEOUT}s"
echo "=== serial.log ==="
sed 's/^/    /' "$SERIAL"
exit 1
