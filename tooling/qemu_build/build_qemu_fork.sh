#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build edhodapp/qemu fork into a sandbox prefix, separate from
# any system-installed qemu. Reproducibly produces the fork-qemu
# the crypto test harness uses to exercise ISA extensions
# (SHA-NI, AES-NI, PCLMULQDQ, etc.) that stock qemu shipped with
# Ubuntu Noble (qemu 8.2.2) doesn't cleanly emulate.
#
# Rationale captured in DECISIONS.md D054 ("QEMU fork as the
# crypto-runtime sandbox"). Under D038's L2 methodology the
# SHA-NI path of sha256.S MUST be exercised end-to-end before
# shipping — and the fork-qemu is what makes that possible on a
# laptop that doesn't have SHA-NI silicon.
#
# Usage:
#   ./tooling/qemu_build/build_qemu_fork.sh
#
# Idempotent. Clones on first run, fetches + fast-forwards on
# subsequent runs. Build output is kept in ``$QEMU_SRC/build/``
# (outside the fireasmserver tree) and installed to
# ``$QEMU_PREFIX`` (default ``$HOME/opt/qemu-fork``).
#
# After install, source ``tooling/qemu_build/env.sh`` to put
# the sandbox qemu on PATH for the current shell:
#   source tooling/qemu_build/env.sh
#
# Host dependencies (Ubuntu Noble):
#   sudo apt install meson libglib2.0-dev libpixman-1-dev \
#                    libfdt-dev zlib1g-dev libslirp-dev \
#                    libcap-ng-dev libattr1-dev

set -euo pipefail

QEMU_REMOTE="${QEMU_REMOTE:-https://github.com/edhodapp/qemu}"
QEMU_SRC="${QEMU_SRC:-$HOME/src/qemu-fork}"
QEMU_PREFIX="${QEMU_PREFIX:-$HOME/opt/qemu-fork}"
QEMU_BRANCH="${QEMU_BRANCH:-master}"

# Targets built. x86_64-linux-user is the one the crypto test
# harness uses (ISA-extension runtime exercise under different
# -cpu models). The softmmu targets let the same sandbox serve
# future system-emulation tests without a second build pass.
QEMU_TARGETS="${QEMU_TARGETS:-x86_64-softmmu,aarch64-softmmu,x86_64-linux-user,aarch64-linux-user}"

echo "=== QEMU fork sandbox build ==="
echo "  remote: $QEMU_REMOTE"
echo "  branch: $QEMU_BRANCH"
echo "  src:    $QEMU_SRC"
echo "  prefix: $QEMU_PREFIX"
echo "  targets: $QEMU_TARGETS"
echo

# -- Step 1: clone or update the fork --
if [[ ! -d "$QEMU_SRC/.git" ]]; then
    echo "=== cloning $QEMU_REMOTE into $QEMU_SRC ==="
    mkdir -p "$(dirname "$QEMU_SRC")"
    git clone "$QEMU_REMOTE" "$QEMU_SRC"
fi
echo "=== fetching + fast-forwarding $QEMU_BRANCH ==="
git -C "$QEMU_SRC" fetch origin
git -C "$QEMU_SRC" checkout "$QEMU_BRANCH"
git -C "$QEMU_SRC" merge --ff-only "origin/$QEMU_BRANCH"

# -- Step 2: configure --
# Always re-run configure. It's cheap relative to make, and
# unconditional invocation means a change to $QEMU_PREFIX or
# $QEMU_TARGETS between runs takes effect instead of silently
# building against the previous config. QEMU's configure is
# itself idempotent — identical args produce identical output.
mkdir -p "$QEMU_SRC/build"
echo "=== configuring ==="
cd "$QEMU_SRC/build"
../configure \
    --prefix="$QEMU_PREFIX" \
    --target-list="$QEMU_TARGETS" \
    --disable-werror \
    --disable-docs

# -- Step 3: build + install --
echo "=== building ==="
make -C "$QEMU_SRC/build" -j"$(nproc)"
echo "=== installing to $QEMU_PREFIX ==="
mkdir -p "$QEMU_PREFIX"
make -C "$QEMU_SRC/build" install

# -- Step 4: self-check --
# Prove that SHA-NI decodes under -cpu Denverton and -cpu max.
# Not proving the Intel SHA Extensions semantics are correct —
# the crypto test harness does that end-to-end with vectors.
# This is the smoke test that the sandbox itself is wired up.
QEMU_USER="$QEMU_PREFIX/bin/qemu-x86_64"
if [[ ! -x "$QEMU_USER" ]]; then
    echo "ERROR: qemu-x86_64 not installed at $QEMU_USER"
    exit 1
fi
VER=$("$QEMU_USER" --version | head -1)
echo "=== installed: $VER ==="
echo
echo "QEMU CPU models advertising Intel SHA Extensions "
echo "(CPUID.(EAX=7,ECX=0).EBX[bit 29]):"
echo "  Denverton      -> use this for SHA-NI tests"
echo "  max            -> all features the build supports"
echo "  Icelake-Server -> DOES NOT advertise SHA-NI in QEMU's "
echo "                    CPU model even though real Ice "
echo "                    Lake-SP silicon has it; don't use."
echo
echo "To exercise the sandbox in the current shell:"
echo "  source tooling/qemu_build/env.sh"
echo "Then invoke qemu-x86_64 -cpu Denverton <binary>."
