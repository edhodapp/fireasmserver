#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# AArch64 fail-path tracer for L2 dispatcher defensive checks.
#
# Mirrors arch/x86_64/platform/failpath/boot.S's three scenarios
# (BAD_ID, NUM_BUFS, TX_BAD_ID) on aarch64. For each scenario:
# build the failpath stub locally with the cross-compiler, scp
# to the Pi, run Firecracker over SSH, pull the serial log
# back, assert the expected marker chain.
#
# The Pi setup is the same as pi_aarch64_firecracker.sh —
# passwordless sudo for tap0 management is NOT needed here
# because the failpath stub doesn't use the network (the
# dispatcher's TX/RX never reaches a real virtio backend; we
# pre-populate ring memory ourselves). Firecracker still
# requires the network-interfaces config block (it complains
# without one), so we wire it to tap0 like the production
# script does and rely on the stub never actually using it.
#
# Wire into pre_push.sh once stable; today it's a manual
# command per scenario. Exit 0 if every scenario PASSes; 1 on
# any failure or build error.

set -euo pipefail

PI_HOST="${PI_HOST:-10.0.2.2}"
PI_USER="${PI_USER:-ed}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fireasm_pi5_ed}"
TIMEOUT="${TIMEOUT:-15}"     # Same justification as
                             # pi_aarch64_firecracker.sh.

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user." >&2
    exit 1
fi

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

SSH_OPTS=(
    -i "$SSH_KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=5
)

### 1. Sanity-check Pi reachable ####################################
if ! ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" true 2>/dev/null; then
    echo "SKIP: $PI_USER@$PI_HOST not reachable; failpath aarch64 tests skipped."
    exit 0
fi

SCENARIOS=(BAD_ID NUM_BUFS TX_BAD_ID)

# Expected fail markers per scenario. These mirror the dispatcher's
# .l2_*_fail / .Lrx_bad_id_fail / .Lrx_num_bufs_fail /
# .Ltx_bad_id_fail emit strings.
declare -A FAIL_MARKER=(
    [BAD_ID]="RX:FAIL bad_id=00000100"
    [NUM_BUFS]="RX:FAIL num_bufs=00000002"
    [TX_BAD_ID]="TX:FAIL bad_id=00000100"
)

# Additional markers that MUST appear (the chain leading up to
# the fail point). Each entry is a space-separated list of
# substrings to grep for.
declare -A REQUIRED_CHAIN=(
    [BAD_ID]="READY FAILPATH:BOOT"
    [NUM_BUFS]="READY FAILPATH:BOOT"
    # TX_BAD_ID needs the valid RX phase to complete before the
    # TX fail can fire — so RX:FRAME + RX:RETURNED + TX:SUBMITTED
    # all need to be in the log too.
    [TX_BAD_ID]="READY FAILPATH:BOOT RX:FRAME RX:RETURNED TX:SUBMITTED"
)

# Markers that MUST NOT appear (gate short-circuit invariants).
declare -A FORBIDDEN=(
    # bad_id / num_bufs gates fire BEFORE the dispatcher reaches
    # RX:FRAME emit. If RX:FRAME shows up, the gate didn't actually
    # short-circuit.
    [BAD_ID]="RX:FRAME TX:SUBMITTED"
    [NUM_BUFS]="RX:FRAME TX:SUBMITTED"
    # TX_BAD_ID: the TX completion (TX:RECLAIMED) must NOT fire —
    # the dispatcher caught the bad id and bailed before the
    # success-side reclaim emit.
    [TX_BAD_ID]="TX:RECLAIMED"
)

OVERALL_RC=0

for SCENARIO in "${SCENARIOS[@]}"; do
    echo
    echo "=== pi failpath aarch64: SCENARIO=$SCENARIO ==="

    ### 2. Build the stub locally with the cross-compiler ##########
    rm -rf arch/aarch64/build/failpath
    if ! make -C arch/aarch64 PLATFORM=failpath \
            SCENARIO="$SCENARIO" >/dev/null 2>&1; then
        echo "FAIL: aarch64 failpath build (SCENARIO=$SCENARIO) failed"
        OVERALL_RC=1
        continue
    fi
    GUEST_BIN="arch/aarch64/build/failpath/guest.bin"
    [[ -f "$GUEST_BIN" ]] || {
        echo "FAIL: $GUEST_BIN missing after build"
        OVERALL_RC=1
        continue
    }

    ### 3. Stage on Pi, run Firecracker, pull serial.log ##########
    TMPDIR="$(mktemp -d /tmp/fireasm-failpath.XXXXXXXX)"
    SERIAL="$TMPDIR/serial.log"
    trap 'rm -rf "$TMPDIR"' RETURN

    PI_TMP="$(ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" \
        'mktemp -d /tmp/fireasm-failpath.XXXXXXXX')"
    [[ "$PI_TMP" =~ ^/tmp/fireasm-failpath\.[A-Za-z0-9]+$ ]] || {
        echo "FAIL: Pi returned unexpected mktemp -d path: '$PI_TMP'"
        OVERALL_RC=1
        rm -rf "$TMPDIR"
        continue
    }
    scp "${SSH_OPTS[@]}" "$GUEST_BIN" \
        "$PI_USER@$PI_HOST:$PI_TMP/guest.bin" >/dev/null

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

    # Firecracker rejects '_' in instance IDs ("Invalid instance
    # ID: InvalidChar('_', N)") — use lowercase + replace _ → -.
    FC_ID="failpath-${SCENARIO,,}"
    FC_ID="${FC_ID//_/-}"
    ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "
        set -u
        cd '$PI_TMP'
        timeout ${TIMEOUT}s firecracker \
            --no-api \
            --config-file config.json \
            --id ${FC_ID} \
            > serial.log 2> firecracker-stderr.log || true
    " >/dev/null

    scp "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST:$PI_TMP/serial.log" \
        "$SERIAL" >/dev/null
    ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "rm -rf '$PI_TMP'" \
        2>/dev/null || true

    # Strip Firecracker's own log lines (timestamp prefix).
    STRIPPED="$TMPDIR/serial.stripped"
    grep -vE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+' \
        "$SERIAL" > "$STRIPPED" || true

    ### 4. Assert marker chain ###################################
    PASS=1
    EXPECTED_FAIL="${FAIL_MARKER[$SCENARIO]}"
    if ! grep -qF "$EXPECTED_FAIL" "$STRIPPED"; then
        echo "FAIL: expected '$EXPECTED_FAIL' missing from serial log"
        PASS=0
    fi
    # Terminal marker: FAILPATH:DONE rc=00000001 (l2_dispatch
    # returns 1 on every fail path).
    if ! grep -qF "FAILPATH:DONE rc=00000001" "$STRIPPED"; then
        echo "FAIL: terminal marker 'FAILPATH:DONE rc=00000001' missing"
        PASS=0
    fi
    for marker in ${REQUIRED_CHAIN[$SCENARIO]}; do
        if ! grep -qF "$marker" "$STRIPPED"; then
            echo "FAIL: required marker '$marker' missing"
            PASS=0
        fi
    done
    for marker in ${FORBIDDEN[$SCENARIO]}; do
        if grep -qF "$marker" "$STRIPPED"; then
            echo "FAIL: forbidden marker '$marker' leaked through gate"
            PASS=0
        fi
    done

    if [[ $PASS -eq 1 ]]; then
        echo "PASS: scenario $SCENARIO — '$EXPECTED_FAIL' + chain observed"
    else
        OVERALL_RC=1
        echo "--- stripped serial log ($STRIPPED) ---"
        cat "$STRIPPED"
        echo "--- end serial log ---"
    fi

    rm -rf "$TMPDIR"
done

echo
if [[ $OVERALL_RC -ne 0 ]]; then
    echo "=== pi failpath aarch64: FAILED ==="
else
    echo "=== pi failpath aarch64: all scenarios PASS ==="
fi
exit $OVERALL_RC
