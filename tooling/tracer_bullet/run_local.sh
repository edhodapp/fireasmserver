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
#   TRACE     set to "1" to additionally capture a QEMU instruction trace
#             (-d exec -singlestep) and run branch-cov against it. Only
#             applies to qemu cells — firecracker has no built-in exec
#             tracing and is a no-op under TRACE=1. branch-cov output is
#             informational (does not fail this script).

set -euo pipefail

ARCH="${1:?usage: $0 <arch> <platform>}"
PLATFORM="${2:?usage: $0 <arch> <platform>}"
TIMEOUT="${TIMEOUT:-5}"
TRACE="${TRACE:-0}"

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

TMPDIR="$(mktemp -d)"
# Named cleanup so the output makes the "we did leave nothing behind"
# contract visible to anyone reading the log. trap covers abnormal
# exits (Ctrl-C, SIGTERM, errexit); the main flow also calls it
# directly at the end on the success path.
cleanup() {
    if [[ -d "$TMPDIR" ]]; then
        rm -rf "$TMPDIR"
        echo "--- cleanup: removed $TMPDIR ---"
    fi
}
trap cleanup EXIT INT TERM
SERIAL="$TMPDIR/serial.log"
TRACE_LOG="$TMPDIR/qemu-trace.log"
TRACE_PCS="$TMPDIR/trace-pcs.txt"

# Per-cell ELF path and (runtime load address - linker VMA). The linker
# places x86_64 .text at 0x100000 (matches the runtime load), so offset
# is 0 there. aarch64 links at 0x0 but the VMMs load the Linux Image at
# RAM_BASE + text_offset, so branch-cov --load-offset needs the RAM
# base + 0x80000 for those cells.
case "$ARCH/$PLATFORM" in
    x86_64/qemu)        ELF="arch/x86_64/build/qemu/guest.elf";        LOAD_OFFSET=0 ;;
    x86_64/firecracker) ELF="arch/x86_64/build/firecracker/guest.elf"; LOAD_OFFSET=0 ;;
    aarch64/qemu)       ELF="arch/aarch64/build/qemu/guest.elf";       LOAD_OFFSET=0x40080000 ;;
    aarch64/firecracker)
        ELF="arch/aarch64/build/firecracker/guest.elf";                LOAD_OFFSET=0x80080000 ;;
esac

echo "=== tracer bullet: $ARCH/$PLATFORM (timeout ${TIMEOUT}s) ==="

_qemu_trace_args() {
    # Emit additional args for TRACE=1 mode; empty otherwise.
    if [[ "$TRACE" == "1" ]]; then
        echo "-d exec -singlestep -D $TRACE_LOG"
    fi
}

launch_qemu_x86_64() {
    local guest="arch/x86_64/build/qemu/guest.elf"
    [[ -f "$guest" ]] || { echo "missing $guest — run make first" >&2; exit 1; }
    # shellcheck disable=SC2046
    timeout "${TIMEOUT}s" qemu-system-x86_64 \
        -machine pc -cpu qemu64 -m 128 \
        -display none -no-reboot \
        -serial "file:$SERIAL" \
        $(_qemu_trace_args) \
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
    # shellcheck disable=SC2046
    timeout "${TIMEOUT}s" qemu-system-aarch64 \
        -M virt -cpu cortex-a72 -m 128 \
        -display none -no-reboot \
        -serial "file:$SERIAL" \
        $(_qemu_trace_args) \
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

if ! grep -q 'READY' "$SERIAL"; then
    echo "FAIL: READY not observed in ${TIMEOUT}s"
    echo "=== serial.log ==="
    sed 's/^/    /' "$SERIAL"
    exit 1
fi
echo "READY observed — PASS"

# Optional: run branch-cov on the captured QEMU trace. Advisory only —
# current tracer stubs don't exercise every conditional path (e.g.,
# secondary-CPU entry, UART-FIFO-full) so a coverage gap is expected
# and must not fail the cell until richer tests exist.
if [[ "$TRACE" == "1" && -s "$TRACE_LOG" ]]; then
    echo
    echo "=== branch-cov report (advisory) ==="
    # QEMU -d exec -singlestep format:
    #   Trace 0: <host_ptr> [<cpu_idx>/<guest_pc>/<ccs>/<?>]
    # Extract the second /-separated hex field inside the [...] bracket.
    sed -nE 's|.*\[[0-9a-f]+/([0-9a-f]+)/.*|\1|p' "$TRACE_LOG" > "$TRACE_PCS"
    # Prefer the repo's venv Python if present so `branch_cov` resolves
    # without activating it; otherwise fall back to system python3.
    PY="${PYTHON:-python3}"
    if [[ -x "$REPO_ROOT/.venv/bin/python3" ]]; then
        PY="$REPO_ROOT/.venv/bin/python3"
    fi
    "$PY" -m branch_cov --elf "$ELF" --trace "$TRACE_PCS" \
        --load-offset "$LOAD_OFFSET" || true
fi
exit 0
