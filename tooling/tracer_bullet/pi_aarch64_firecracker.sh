#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# AArch64 Firecracker tracer bullet.
#
# Builds arch/aarch64/platform/firecracker/guest.bin on the laptop,
# ensures tap0 exists on the Pi (ephemeral or pre-persistent), stages
# the guest binary, runs Firecracker on the Pi with virtio-net wired
# to tap0, pulls the serial log back, and validates the L2-init
# marker chain through TX:RECLAIMED.
#
# This script asserts the same marker sequence x86_64/firecracker
# does in run_local.sh — READY → VIRTIO:OK → STATUS:DRIVER →
# FEATURES:OK → QUEUES:RX/TX → QUEUES:READY → DRIVER_OK →
# RX:POPULATED → RX:FRAME or RX:TIMEOUT (either is a PASS for
# VIO-R-003/004) → optionally RX:RETURNED + TX:SUBMITTED +
# TX:RECLAIMED on the RX:FRAME path.
#
# Pi-side prereqs: SSH key auth (D023), passwordless sudo for the
# tap0 setup (D023), Firecracker v1.15.1 (D037).

set -euo pipefail

PI_HOST="${PI_HOST:-10.0.2.2}"
PI_USER="${PI_USER:-ed}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fireasm_pi5_ed}"
TIMEOUT="${TIMEOUT:-60}"     # Boot-to-TX:RECLAIMED on the success
                             # path is ~100-200 ms on Pi 5. The
                             # no-traffic path (poll budget exhausts
                             # before RX:TIMEOUT fires) is the
                             # dominator and stays in the 35-50s
                             # band with high variance.
                             #
                             # P1 (post-H5) measurement: 8-run
                             # samples at TIMEOUT=45/50/55 showed
                             # 2/3 SIGTERM-mid-poll failures
                             # respectively; TIMEOUT=60 held 5/5 in
                             # H5 verification. The dsb sy → dsb
                             # ishld narrowing in H5 was correct on
                             # its own terms, but the wall-clock
                             # impact under KVM is dominated by
                             # `yield` + VMEXIT round-trips and
                             # cross-cluster memory-system traffic,
                             # not the dsb instruction class. So:
                             # 60s stays as the conservative ceiling.
                             #
                             # The follow-up that would actually
                             # move the needle is reducing
                             # POLL_BUDGET (boot.S, per-arch) or
                             # removing `yield` from the poll loop —
                             # tracked separately, not folded into
                             # tracer-bullet TIMEOUT.
READY_MARKER="${READY_MARKER:-READY}"

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user." >&2
    exit 1
fi
[[ -f "$SSH_KEY" ]] || { echo "ERROR: SSH key missing at $SSH_KEY" >&2; exit 1; }

# PI_USER and PI_HOST get interpolated into a heredoc that lands on
# the Pi as a sudo invocation. Validate against conservative patterns
# before any SSH/sudo call so a malicious env override can't inject
# shell metacharacters into a privileged command.
[[ "$PI_USER" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || {
    echo "ERROR: PI_USER '$PI_USER' fails [a-z_][a-z0-9_-]* validation" >&2
    exit 1
}
[[ "$PI_HOST" =~ ^[a-zA-Z0-9.-]+$ ]] || {
    echo "ERROR: PI_HOST '$PI_HOST' fails host-format validation" >&2
    exit 1
}

SSH_OPTS=(
    -i "$SSH_KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
)

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

TMPDIR="$(mktemp -d)"
PI_TMP=""
PI_TAP_CREATED=0

cleanup() {
    if [[ -n "$PI_TMP" ]]; then
        if ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" \
                "rm -rf '$PI_TMP'" 2>/dev/null; then
            echo "--- cleanup: removed $PI_USER@$PI_HOST:$PI_TMP ---"
        else
            echo "--- cleanup: could not reach Pi to remove $PI_TMP ---"
        fi
    fi
    if [[ "$PI_TAP_CREATED" == "1" ]]; then
        if ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" \
                "sudo ip link del tap0" 2>/dev/null; then
            echo "--- cleanup: removed ephemeral tap0 on Pi ---"
        fi
    fi
    if [[ -d "$TMPDIR" ]]; then
        rm -rf "$TMPDIR"
    fi
}
trap cleanup EXIT INT TERM

SERIAL="$TMPDIR/serial.log"

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

### 2. Ensure tap0 on the Pi ########################################
# Mirror laptop run_local.sh's pre-persistent / ephemeral pattern.
# Pre-persistent: user (or a boot helper) created tap0 once with
# `sudo ip tuntap add dev tap0 mode tap user $USER && sudo ip link
# set tap0 up`; subsequent runs reuse it without touching sudo.
# Ephemeral: tap0 missing → we create it via sudo (D023 grants
# passwordless sudo) and tear it down in cleanup.
#
# Split into two SSH calls so cleanup state is set BEFORE the create.
# If a partial-success failure mode (SSH disconnect mid-stream after
# the create succeeded) prevents the second SSH from returning,
# PI_TAP_CREATED is already 1 and the trap will tear down on exit.
echo "--- ensuring tap0 on Pi ---"
PI_TAP_PROBE=$(ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" \
    '[[ -d /sys/class/net/tap0 ]] && echo present || echo missing')
case "$PI_TAP_PROBE" in
    present)
        echo "    tap0 on Pi exists — reusing"
        ;;
    missing)
        echo "    tap0 on Pi missing — creating ephemeral"
        # Optimistically claim ownership BEFORE the create succeeds
        # so the trap's cleanup runs even if the SSH that creates
        # tap0 partially fails after the `ip tuntap add` lands.
        PI_TAP_CREATED=1
        ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" \
            "sudo ip tuntap add dev tap0 mode tap user '$PI_USER' \
             && sudo ip link set tap0 up" \
            || { echo "ERROR: failed to create tap0 on Pi" >&2; exit 1; }
        ;;
    *)
        echo "ERROR: unexpected tap0 probe result: '$PI_TAP_PROBE'" >&2
        exit 1
        ;;
esac

### 3. Stage a fresh workdir on the Pi ##############################
PI_TMP="$(ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" 'mktemp -d /tmp/fireasm-tracer.XXXXXXXX')"
[[ "$PI_TMP" =~ ^/tmp/fireasm-tracer\.[A-Za-z0-9]+$ ]] || {
    echo "ERROR: Pi returned unexpected mktemp -d path: '$PI_TMP'" >&2
    exit 1
}
echo "--- staging on Pi at $PI_TMP ---"
scp "${SSH_OPTS[@]}" "$GUEST_BIN" "$PI_USER@$PI_HOST:$PI_TMP/guest.bin" >/dev/null

### 4. Write the Firecracker config on the Pi #######################
# boot_args must contain "console=" — without that, Firecracker
# v1.15.1's attach_legacy_devices_aarch64 skips register_mmio_serial
# and the UART is never mapped. "console=ttyS0" is purely a trigger
# token; the stub doesn't parse cmdline.
#
# network-interfaces wires virtio-net at the first post-SERIAL MMIO
# slot (0x4000_3000) so the VIO probe can find it. iface_id is
# opaque to the guest; host_dev_name binds to the tap on the Pi.
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
  "drives": [],
  "network-interfaces": [
    { "iface_id": "eth0", "host_dev_name": "tap0" }
  ]
}
EOF

### 5. Run Firecracker on the Pi, pull serial.log back ##############
# Firecracker's framework log lines and per-component errors share
# the YYYY-MM-DDTHH:MM:SS.NS [tracer:...] prefix and land on stdout
# mixed with the guest's serial stream. Same race observed laptop-
# side splitting marker lines mid-emit. Strip them post-hoc, on
# the laptop, after pulling the raw log back.
echo "--- launching Firecracker on Pi (SIGTERM after ${TIMEOUT}s) ---"
ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "
    set -u
    cd '$PI_TMP'
    timeout ${TIMEOUT}s firecracker \
        --no-api \
        --config-file config.json \
        --id tracer \
        > serial.log 2> firecracker-stderr.log || true
" >/dev/null

scp "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST:$PI_TMP/serial.log" "$SERIAL" >/dev/null

# Strip Firecracker's own log lines (timestamp prefix) so subsequent
# grep -qE assertions match the guest-emitted marker lines only.
STRIPPED="$TMPDIR/serial.stripped"
grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+' "$SERIAL" > "$STRIPPED" || true
mv "$STRIPPED" "$SERIAL"

### 6. Validate the marker chain ####################################
fail() {
    echo
    echo "=== tracer bullet FAILED ==="
    echo "  $1"
    echo "=== serial.log (stripped) ==="
    sed 's/^/    /' "$SERIAL"
    exit 1
}

# READY (boot reached the kernel image).
grep -qE "^${READY_MARKER}\$" "$SERIAL" \
    || fail "READY marker not observed"
echo "READY observed — kernel entry verified"

# LAYOUT-OK (D060 step 4.2 allocator pass).
grep -qE '^LAYOUT-OK$' "$SERIAL" \
    || fail "LAYOUT-OK not observed (allocator pass did not return cleanly)"
echo "LAYOUT-OK observed — D060 step 4.2 allocator pass verified"

# VIRTIO:OK (MagicValue == 0x74726976).
grep -qE '^VIRTIO:OK$' "$SERIAL" \
    || fail "VIRTIO:OK not observed (virtio-mmio magic mismatch?)"
echo "VIRTIO:OK observed — virtio-mmio magic verified"

# STATUS:DRIVER (VIO-001..003 init prefix).
grep -qE '^STATUS:DRIVER$' "$SERIAL" \
    || fail "STATUS:DRIVER not observed (init prefix VIO-001..003 failed?)"
echo "STATUS:DRIVER observed — VIO-001..003 init prefix verified"

# FEATURES:OK (VIO-004..006 feature negotiation).
if ! grep -qE '^FEATURES:OK$' "$SERIAL"; then
    fail_line=$(grep -E '^FEATURES:FAIL ' "$SERIAL" | head -1 || true)
    if [[ -n "$fail_line" ]]; then
        fail "feature negotiation rejected — $fail_line"
    else
        fail "FEATURES:OK not observed (guest did not reach VIO-004..006)"
    fi
fi
echo "FEATURES:OK observed — VIO-004..006 feature negotiation verified"

# QUEUES:RX/TX (VIO-007 part 1, discovery).
queues_line=$(grep -E '^QUEUES:RX=[0-9A-F]{8} TX=[0-9A-F]{8}$' \
    "$SERIAL" | head -1 || true)
if [[ -z "$queues_line" ]]; then
    fail_line=$(grep -E '^QUEUES:FAIL ' "$SERIAL" | head -1 || true)
    if [[ -n "$fail_line" ]]; then
        fail "virtqueue discovery — $fail_line"
    else
        fail "QUEUES:RX/TX not observed (guest did not reach VIO-007 discovery)"
    fi
fi
echo "${queues_line} observed — VIO-007 discovery verified"

# QUEUES:READY (VIO-007 part 2, init).
if ! grep -qE '^QUEUES:READY$' "$SERIAL"; then
    fail_line=$(grep -E '^QUEUES:FAIL queue-not-ready' "$SERIAL" | head -1 || true)
    if [[ -n "$fail_line" ]]; then
        fail "virtqueue init — $fail_line"
    else
        fail "QUEUES:READY not observed (guest did not reach VIO-007 init)"
    fi
fi
echo "QUEUES:READY observed — VIO-007 init verified"

# DRIVER_OK (VIO-008).
if ! grep -qE '^DRIVER_OK$' "$SERIAL"; then
    fail_line=$(grep -E '^DRIVER_OK:FAIL ' "$SERIAL" | head -1 || true)
    if [[ -n "$fail_line" ]]; then
        fail "DRIVER_OK transition — $fail_line"
    else
        fail "DRIVER_OK not observed (guest did not reach VIO-008)"
    fi
fi
echo "DRIVER_OK observed — VIO-008 live-device transition verified"

# RX:POPULATED (VIO-R-002).
grep -qE '^RX:POPULATED$' "$SERIAL" \
    || fail "RX:POPULATED not observed (guest did not reach VIO-R-002)"
echo "RX:POPULATED observed — VIO-R-002 descriptor fill + notify verified"

# VIO-R-003/004: either RX:FRAME or RX:TIMEOUT is a PASS. Active
# traffic injection isn't part of this MVP — the polling code path
# is what's being validated.
rx_line=$(grep -E '^RX:(FRAME |TIMEOUT$)' "$SERIAL" | head -1 || true)
[[ -n "$rx_line" ]] \
    || fail "neither RX:FRAME nor RX:TIMEOUT observed (guest did not reach VIO-R-003/004)"
echo "RX poll observed — VIO-R-003/004: $rx_line"

# On the RX:FRAME path, RX:RETURNED + TX:SUBMITTED + TX:RECLAIMED
# must follow. On the RX:TIMEOUT path nothing further runs.
if grep -qE '^RX:FRAME ' "$SERIAL"; then
    grep -qE '^RX:RETURNED$' "$SERIAL" \
        || fail "RX:FRAME observed but RX:RETURNED missing (VIO-R-006/007 cycle incomplete)"
    echo "RX:RETURNED observed — VIO-R-006/007 return + notify verified"

    grep -qE '^TX:SUBMITTED$' "$SERIAL" \
        || fail "TX:SUBMITTED not observed (VIO-T-002..005 submit did not run)"
    echo "TX:SUBMITTED observed — VIO-T-002..005 submit verified"

    # VIO-T-006: TX:RECLAIMED is required once TX:SUBMITTED has fired —
    # the device must process the submitted descriptor. TX:TIMEOUT and
    # TX:FAIL are both failures (Codex P2 finding on H1 push range).
    tx_line=$(grep -E '^TX:RECLAIMED ' "$SERIAL" | head -1 || true)
    if [[ -z "$tx_line" ]]; then
        fail_line=$(grep -E '^TX:(TIMEOUT$|FAIL )' "$SERIAL" | head -1 || true)
        if [[ -n "$fail_line" ]]; then
            fail "TX failed to reclaim — $fail_line"
        else
            fail "TX:RECLAIMED not observed (VIO-T-006 poll did not complete)"
        fi
    fi
    echo "TX completion observed — VIO-T-006: $tx_line"
fi

echo
echo "=== tracer bullet PASSED ==="
echo "  Full L2-init marker chain verified."
exit 0
