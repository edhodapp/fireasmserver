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

PI_HOST="${PI_HOST:-10.0.2.2}"
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

run_memlayout_diff() {
    echo
    echo "=== pre-push: memlayout bytecode-VM differential ==="
    # Builds the per-arch C+asm driver and runs the differential
    # test harness — Python reference vs each arch's assembly
    # interpreter under qemu-<arch>-static. Both must agree on
    # every (bytecode, cpu_values, tuning_values) input.
    # Auto-skips if a build tool (gcc / aarch64-linux-gnu-gcc /
    # nasm) or qemu-aarch64-static is missing.
    if ! make -C tooling/memlayout_diffharness -s all \
            >/dev/null 2>&1; then
        echo "SKIP: diff harness build failed (toolchain?)"
        return 0
    fi
    if [[ ! -x "$REPO_ROOT/.venv/bin/pytest" ]]; then
        echo "SKIP: no .venv/bin/pytest"
        return 0
    fi
    "$REPO_ROOT/.venv/bin/pytest" \
        tooling/tests/test_memlayout_diff.py \
        -q --no-header
}

run_crypto_tests() {
    echo
    echo "=== pre-push: crypto primitive tests (both arches) ==="
    # Exercises every crypto primitive (CRC-32/FCS, SHA-256, AES-128,
    # AES-128-GCM) against NIST / IEEE / RFC / zlib test vectors via
    # the host C drivers. Fast (~seconds total); catches regressions
    # in the crypto primitives independent of the boot stubs that
    # the tracer-bullet cells cover. Each primitive runs in multiple
    # CPU-feature cells under the QEMU fork (native + feature-off
    # + feature-on) so we verify the CPUID-probe path as well.
    make -C tooling/crypto_tests -s test
}

run_c_gates() {
    echo
    echo "=== pre-push: C linter stack ==="
    # Four-layer static analysis over the host-side crypto test
    # drivers: gcc + clang compile-as-lint, clang-tidy, cppcheck,
    # scan-build. Each catches a different class of bug and the
    # combined runtime is seconds — per the 2026-04-22 automation-
    # cost-is-bounded discipline, all four run every push.
    #
    # FAIL on missing tools, don't SKIP. An un-linted push is
    # exactly the "unwired gate" the complete-pipeline-before-
    # shipping discipline forbids. Install via:
    #   sudo apt install clang clang-tidy clang-tools cppcheck
    local missing=()
    local tool
    for tool in clang clang-tidy cppcheck scan-build; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            missing+=("$tool")
        fi
    done
    if [[ ${#missing[@]} -ne 0 ]]; then
        echo "FAIL: C lint toolchain incomplete — missing:" \
            "${missing[*]}"
        echo "  install: sudo apt install clang clang-tidy" \
            "clang-tools cppcheck"
        return 1
    fi
    make -C tooling/crypto_tests -s lint
}

run_pytest_suite() {
    echo
    echo "=== pre-push: full pytest suite ==="
    # Covers every pytest file under tooling/tests/ — ontology,
    # branch-cov, CRC-32 wrapper, CLI, QEMU harness, side-session
    # derive_fold_constants, and concurrent-safety tests. The
    # per-commit quality gate runs pytest too, but only when .py
    # files are staged; this gate makes sure no commit (e.g., a
    # pure .S or .md one) can slip past without the suite running.
    if [[ ! -x "$REPO_ROOT/.venv/bin/pytest" ]]; then
        echo "SKIP: no .venv/bin/pytest (run: pip install -e .[dev])"
        return 0
    fi
    "$REPO_ROOT/.venv/bin/pytest" -q --no-header
}

run_ontology_audit() {
    echo
    echo "=== pre-push: ontology audit (D051) ==="
    # audit-ontology verifies that every implementation_refs /
    # verification_refs entry in tooling/qemu-harness.json resolves
    # against the working tree and that status ↔ refs fields are
    # internally consistent. D051 makes this a closing gate so the
    # ontology cannot drift from the code it claims to describe.
    if [[ ! -x "$REPO_ROOT/.venv/bin/audit-ontology" ]]; then
        echo "SKIP: no .venv/bin/audit-ontology" \
            "(run: pip install -e .[dev] to install console scripts)"
        return 0
    fi
    "$REPO_ROOT/.venv/bin/audit-ontology" --exit-nonzero-on-gap
}

echo "=== fireasmserver pre-push integration tests ==="

run_local_cell x86_64  firecracker   || fail=1
# TRACE=1 on aarch64/qemu: captures the QEMU instruction stream and
# runs branch-cov — lets developers see the same coverage numbers
# locally that CI reports on push. Advisory (no gate) until we have
# tests that exercise every branch.
run_local_cell aarch64 qemu        1 || fail=1
run_pi_cell                          || fail=1
run_c_gates                          || fail=1
run_memlayout_diff                   || fail=1
run_crypto_tests                     || fail=1
run_pytest_suite                     || fail=1
run_ontology_audit                   || fail=1

echo
if [[ $fail -ne 0 ]]; then
    echo "!!! PRE-PUSH BLOCKED: integration tests failed !!!"
    echo "    Fix the failures above or use 'git push --no-verify' to"
    echo "    override deliberately (you will own the broken CI)."
    exit 1
fi
echo "=== pre-push: all integration cells green ==="
