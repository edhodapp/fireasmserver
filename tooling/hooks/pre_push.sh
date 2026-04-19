#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Pre-push integration tests for fireasmserver.
#
# Rationale: per CLAUDE.md, "Integration tests must run before every push."
# Unit tests and quality gates fire at commit time. Boot-level integration
# (VM launch + READY marker) lives here — too slow for pre-commit at the
# per-commit cadence we use, but cheap enough per-push to guarantee no
# broken boot hits `origin/main` between local CI and GitHub CI.
#
# Cells executed:
#   x86_64/firecracker    laptop-local (PVH boot, no BIOS, ~1 s)
#   aarch64/qemu          laptop-local (qemu-system-aarch64 TCG, ~5 s)
#   aarch64/firecracker   Pi-side via SSH — only if Pi responds in 2 s;
#                         otherwise SKIP with a notice. This is the one
#                         path CI can't cover (no aarch64 KVM on hosted
#                         runners), so running it here when the Pi is up
#                         is the only regression signal.
#
# Cells intentionally omitted:
#   x86_64/qemu           BIOS path; we dropped it from CI too. Local
#                         stub still builds; we just don't exercise boot.

set -euo pipefail

REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

PI_HOST="${PI_HOST:-10.0.0.2}"
PI_USER="${PI_USER:-ed}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fireasm_pi5_ed}"

fail=0

run_local_cell() {
    local arch=$1 platform=$2
    local trace="${3:-0}"
    echo
    echo "=== pre-push: $arch/$platform (laptop, TRACE=$trace) ==="
    if ! make -C "arch/$arch" "PLATFORM=$platform" >/dev/null; then
        echo "FAIL: make failed for $arch/$platform"
        return 1
    fi
    TRACE="$trace" ./tooling/tracer_bullet/run_local.sh "$arch" "$platform"
}

pi_reachable() {
    ssh -i "$SSH_KEY" \
        -o IdentitiesOnly=yes \
        -o BatchMode=yes \
        -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=2 \
        "$PI_USER@$PI_HOST" true 2>/dev/null
}

run_pi_cell() {
    echo
    echo "=== pre-push: aarch64/firecracker (Pi) ==="
    if ! pi_reachable; then
        echo "SKIP: $PI_USER@$PI_HOST not reachable within 2s."
        echo "      (aarch64/firecracker cannot be tested in CI either;"
        echo "       regression may slip through until the Pi is back up.)"
        return 0
    fi
    ./tooling/tracer_bullet/pi_aarch64_firecracker.sh
}

echo "=== fireasmserver pre-push integration tests ==="

run_local_cell x86_64  firecracker   || fail=1
# TRACE=1 on aarch64/qemu: captures the QEMU instruction stream and
# runs branch-cov — lets developers see the same coverage numbers
# locally that CI reports on push. Advisory (no gate) until we have
# tests that exercise every branch.
run_local_cell aarch64 qemu        1 || fail=1
run_pi_cell                          || fail=1

echo
if [[ $fail -ne 0 ]]; then
    echo "!!! PRE-PUSH BLOCKED: integration tests failed !!!"
    echo "    Fix the failures above or use 'git push --no-verify' to"
    echo "    override deliberately (you will own the broken CI)."
    exit 1
fi
echo "=== pre-push: all integration cells green ==="
