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
# TAP_CREATED flips to 1 only when launch_firecracker_x86_64 successfully
# creates tap0 for the virtio-net probe. Scoped here so the trap can
# tear it down on any exit path before tap-creation has even run.
TAP_CREATED=0
# Named cleanup so the output makes the "we did leave nothing behind"
# contract visible to anyone reading the log. trap covers abnormal
# exits (Ctrl-C, SIGTERM, errexit); the main flow also calls it
# directly at the end on the success path.
cleanup() {
    if [[ -d "$TMPDIR" ]]; then
        rm -rf "$TMPDIR"
        echo "--- cleanup: removed $TMPDIR ---"
    fi
    if [[ "$TAP_CREATED" == "1" ]]; then
        sudo ip link del tap0 2>/dev/null || true
        echo "--- cleanup: removed tap0 ---"
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
    # virtio-net backing device. Two provisioning modes:
    #   (a) Pre-persistent tap0 (local dev ergonomic path): created once
    #       by a human via `sudo ip tuntap add dev tap0 mode tap user $USER
    #       && sudo ip link set tap0 up`. The script detects /sys/class/net/tap0
    #       and reuses it without touching sudo. TAP_CREATED stays 0 so
    #       the cleanup trap also leaves it alone.
    #   (b) Ephemeral tap0 (CI path / first-time local run): we create
    #       fresh here and tear down in cleanup(). Requires passwordless
    #       sudo — the contract on GHA runners.
    if [[ -d /sys/class/net/tap0 ]]; then
        echo "tap0 exists — reusing (pre-persistent mode)"
    else
        echo "tap0 missing — creating ephemeral (needs sudo)"
        sudo ip tuntap add dev tap0 mode tap user "$USER"
        sudo ip link set tap0 up
        TAP_CREATED=1
    fi
    cat > "$TMPDIR/fc.json" <<EOF
{
  "boot-source": { "kernel_image_path": "$guest" },
  "machine-config": { "vcpu_count": 1, "mem_size_mib": 128 },
  "drives": [],
  "network-interfaces": [
    { "iface_id": "eth0", "host_dev_name": "tap0" }
  ]
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

# Anchor markers to line start so a Firecracker log line containing the
# substring "READY" (or "VIRTIO:OK") can't masquerade as guest output.
# Our stubs emit each marker on its own line, so line-anchored grep is
# both sufficient and tighter than a loose substring match.
if ! grep -qE '^READY$' "$SERIAL"; then
    echo "FAIL: READY not observed in ${TIMEOUT}s"
    echo "=== serial.log ==="
    sed 's/^/    /' "$SERIAL"
    exit 1
fi
echo "READY observed — PASS"

# x86_64/firecracker additionally probes the virtio-MMIO MagicValue
# register at the first device slot (VIO-Q-001 per docs/l2/REQUIREMENTS.md,
# Virtio 1.2 §4.2.2). boot.S emits VIRTIO:OK on match, VIRTIO:FAIL on
# mismatch. Other cells don't have virtio-net wired yet — aarch64's
# tracer-bullet boot.S still stops at READY.
if [[ "$ARCH/$PLATFORM" == "x86_64/firecracker" ]]; then
    if ! grep -qE '^VIRTIO:OK$' "$SERIAL"; then
        echo "FAIL: VIRTIO:OK not observed (virtio-mmio magic mismatch?)"
        echo "=== serial.log ==="
        sed 's/^/    /' "$SERIAL"
        exit 1
    fi
    echo "VIRTIO:OK observed — virtio-mmio magic verified"

    # Device-status init prefix (VIO-001..003). boot.S walks the
    # required §2.1.2 step 1-3 sequence (reset, ACKNOWLEDGE, DRIVER),
    # reads the status register back, and emits STATUS:DRIVER on
    # match or STATUS:FAIL status=<hex> on mismatch. Virtqueue setup
    # (VIO-007) and DRIVER_OK (VIO-008) land in follow-up commits;
    # the marker here advances as each stage does.
    if ! grep -qE '^STATUS:DRIVER$' "$SERIAL"; then
        echo "FAIL: STATUS:DRIVER not observed (init prefix VIO-001..003 failed?)"
        echo "=== serial.log ==="
        sed 's/^/    /' "$SERIAL"
        exit 1
    fi
    echo "STATUS:DRIVER observed — VIO-001..003 init prefix verified"

    # Feature negotiation (VIO-004..006). boot.S verifies
    # VIRTIO_F_VERSION_1 is offered, writes driver-features = VERSION_1
    # only, sets FEATURES_OK, and re-reads Status to confirm the host
    # accepted the subset. Success → FEATURES:OK. Failure →
    # FEATURES:FAIL <reason> <hex-context>, then VIO-009 FAILED-bit
    # write, then halt.
    if ! grep -qE '^FEATURES:OK$' "$SERIAL"; then
        # Positive match on FEATURES:FAIL lets the reason line bubble
        # up as the top-line CI message rather than disappearing into
        # the serial-log dump. Absent both markers is its own case
        # (guest crashed before reaching the negotiation block).
        fail_line=$(grep -E '^FEATURES:FAIL ' "$SERIAL" | head -1 || true)
        if [[ -n "$fail_line" ]]; then
            echo "FAIL: feature negotiation rejected — $fail_line"
        else
            echo "FAIL: FEATURES:OK not observed (guest did not reach VIO-004..006)"
        fi
        echo "=== serial.log ==="
        sed 's/^/    /' "$SERIAL"
        exit 1
    fi
    echo "FEATURES:OK observed — VIO-004..006 feature negotiation verified"

    # Virtqueue discovery (VIO-007 part 1). boot.S reads QueueNumMax
    # for queue 0 (RX) and queue 1 (TX) after setting each via
    # QueueSel. Success → QUEUES:RX=<hex> TX=<hex>. Failure →
    # QUEUES:FAIL no-queue q=<0|1> (max=0) + VIO-009 halt.
    queues_line=$(grep -E '^QUEUES:RX=[0-9A-F]{8} TX=[0-9A-F]{8}$' \
        "$SERIAL" | head -1 || true)
    if [[ -z "$queues_line" ]]; then
        fail_line=$(grep -E '^QUEUES:FAIL ' "$SERIAL" | head -1 || true)
        if [[ -n "$fail_line" ]]; then
            echo "FAIL: virtqueue discovery — $fail_line"
        else
            echo "FAIL: QUEUES:RX/TX not observed (guest did not reach VIO-007 discovery)"
        fi
        echo "=== serial.log ==="
        sed 's/^/    /' "$SERIAL"
        exit 1
    fi
    echo "${queues_line} observed — VIO-007 discovery verified"

    # Virtqueue init (VIO-007 part 2). boot.S programs QueueNum,
    # QueueDesc / QueueDriver / QueueDevice physical addresses
    # for both queues, then toggles QueueReady and confirms it
    # stuck. Success → QUEUES:READY. Failure →
    # QUEUES:FAIL queue-not-ready + VIO-009 halt.
    if ! grep -qE '^QUEUES:READY$' "$SERIAL"; then
        fail_line=$(grep -E '^QUEUES:FAIL queue-not-ready' \
            "$SERIAL" | head -1 || true)
        if [[ -n "$fail_line" ]]; then
            echo "FAIL: virtqueue init — $fail_line"
        else
            echo "FAIL: QUEUES:READY not observed (guest did not reach VIO-007 init)"
        fi
        echo "=== serial.log ==="
        sed 's/^/    /' "$SERIAL"
        exit 1
    fi
    echo "QUEUES:READY observed — VIO-007 init verified"
fi

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
    # If a baseline file exists for this cell, promote branch-cov from
    # advisory to ratchet: exit 1 on any delta from the accepted gaps.
    BASELINE="tooling/branch_cov/baselines/${ARCH}-${PLATFORM}.txt"
    BASELINE_ARG=()
    if [[ -f "$BASELINE" ]]; then
        BASELINE_ARG=(--baseline "$BASELINE")
    fi
    if ! "$PY" -m branch_cov --elf "$ELF" --trace "$TRACE_PCS" \
            --load-offset "$LOAD_OFFSET" "${BASELINE_ARG[@]}"; then
        # Baseline mismatch is a real failure; propagate.
        [[ -f "$BASELINE" ]] && exit 1
        # No baseline → branch-cov's non-zero is just advisory gaps.
    fi
fi
exit 0
