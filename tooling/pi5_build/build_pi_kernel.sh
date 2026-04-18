#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Cross-compile the Raspberry Pi custom kernel for Pi 5 with KVM enabled.
# Per D023/D033: rpi-6.12.y branch, Pi 5 defconfig (bcm2712), KVM explicit.
#
# Safe to re-run, but NOT idempotent in the mathematical sense: Phase 1 runs
# `git reset --hard origin/$KERNEL_BRANCH` which discards any local edits
# inside $SRC_DIR. This is deliberate — we want the build to start from a
# known upstream state, not from whatever experimental changes a developer
# may have left behind. If you need to hand-modify the kernel source, do it
# on a branch outside $SRC_DIR or arrange for this script not to touch it.

set -euo pipefail

KERNEL_BRANCH="${KERNEL_BRANCH:-rpi-6.12.y}"
KERNEL_REPO="${KERNEL_REPO:-https://github.com/raspberrypi/linux.git}"
SRC_DIR="${SRC_DIR:-build/pi-kernel-src}"
OUT_DIR="${OUT_DIR:-build/pi-kernel}"
JOBS="${JOBS:-$(nproc)}"
CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}"

# Canonicalize for consistency with build_pi_image.sh (which needs the
# canonicalization for its realpath-based work-dir containment guard).
REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

mkdir -p "$OUT_DIR"

if ! command -v "${CROSS_COMPILE}gcc" >/dev/null 2>&1; then
    echo "ERROR: cross-compiler '${CROSS_COMPILE}gcc' not found on PATH." >&2
    echo "       On Ubuntu/Debian: sudo apt-get install gcc-aarch64-linux-gnu" >&2
    exit 1
fi

echo "=== Pi 5 kernel build ==="
echo "branch:    $KERNEL_BRANCH"
echo "source:    $SRC_DIR"
echo "output:    $OUT_DIR"
echo "jobs:      $JOBS"
echo "toolchain: ${CROSS_COMPILE}gcc $("${CROSS_COMPILE}gcc" -dumpversion)"
echo

### Phase 1: Fetch / update source #################################
if [[ -d "$SRC_DIR/.git" ]]; then
    echo "--- updating existing clone ---"
    git -C "$SRC_DIR" fetch --depth=1 origin "$KERNEL_BRANCH"
    git -C "$SRC_DIR" checkout "$KERNEL_BRANCH"
    git -C "$SRC_DIR" reset --hard "origin/$KERNEL_BRANCH"
else
    echo "--- shallow clone of $KERNEL_REPO@$KERNEL_BRANCH ---"
    git clone --depth=1 --branch "$KERNEL_BRANCH" "$KERNEL_REPO" "$SRC_DIR"
fi
echo

### Phase 2: Configure #############################################
cd "$SRC_DIR"

echo "--- bcm2712_defconfig (Pi 5) ---"
make ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" bcm2712_defconfig

echo "--- enforce KVM config ---"
# VHE is auto-selected on ARMv8.1+ cores; KVM + VIRTUALIZATION are the
# user-settable knobs we care about. olddefconfig then resolves dependencies.
scripts/config -e KVM -e VIRTUALIZATION
make ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" olddefconfig >/dev/null

echo "--- built config (KVM-related) ---"
# If none match, surface that explicitly — the Phase 5 hard-check below
# will still catch a missing CONFIG_KVM=y, but silently showing nothing
# here was misleading when scrolling the build log.
grep -E "^CONFIG_(KVM|VIRTUALIZATION|ARM64_VHE|HAVE_KVM)" .config \
    || echo "(no KVM-related CONFIG_* lines matched — Phase 5 hard-check will fail)"
echo

### Phase 3: Build #################################################
echo "--- build Image + modules + dtbs (this takes a while) ---"
make ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" -j"$JOBS" Image modules dtbs

### Phase 4: Stage outputs #########################################
cd "$REPO_ROOT"

echo
echo "--- staging outputs to $OUT_DIR ---"
rm -rf "$OUT_DIR/modules_staging"
mkdir -p "$OUT_DIR/modules_staging" "$OUT_DIR/dtb" "$OUT_DIR/overlays"

make -C "$SRC_DIR" ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" \
    INSTALL_MOD_PATH="$REPO_ROOT/$OUT_DIR/modules_staging" \
    modules_install >/dev/null

cp "$SRC_DIR/arch/arm64/boot/Image" "$OUT_DIR/kernel_2712.img"
cp "$SRC_DIR/arch/arm64/boot/dts/broadcom/bcm2712-rpi-5-b.dtb" "$OUT_DIR/dtb/"

if compgen -G "$SRC_DIR/arch/arm64/boot/dts/overlays/*.dtbo" >/dev/null; then
    cp "$SRC_DIR"/arch/arm64/boot/dts/overlays/*.dtbo "$OUT_DIR/overlays/"
fi

cp "$SRC_DIR/.config" "$OUT_DIR/kernel.config"

### Phase 5: Summary & verification ################################
KVER=$(make -C "$SRC_DIR" -s kernelrelease)

echo
echo "=== kernel build complete ==="
echo "Version:   $KVER"
echo "Image:     $OUT_DIR/kernel_2712.img ($(du -h "$OUT_DIR/kernel_2712.img" | cut -f1))"
echo "DTB:       $OUT_DIR/dtb/bcm2712-rpi-5-b.dtb"
echo "Modules:   $OUT_DIR/modules_staging/lib/modules/$KVER/ ($(du -sh "$OUT_DIR/modules_staging/lib/modules/$KVER" | cut -f1))"
echo "Overlays:  $(find "$OUT_DIR/overlays" -name '*.dtbo' | wc -l) files"
echo

# Hard fail if KVM isn't in the built config — the whole point of this
# build is to guarantee KVM availability on the Pi.
if grep -q "^CONFIG_KVM=y" "$OUT_DIR/kernel.config"; then
    echo "KVM verification: CONFIG_KVM=y OK"
else
    echo "ERROR: CONFIG_KVM=y not present in built kernel config" >&2
    echo "       KVM will not be available on the resulting image." >&2
    exit 1
fi
