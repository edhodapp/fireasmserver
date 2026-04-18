#!/bin/bash -e
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install the custom KVM-enabled kernel built by build_pi_kernel.sh.
# Overwrites stage2's stock kernel files with our Pi 5 kernel.

: "${FIREASMSERVER_KERNEL_DIR:?FIREASMSERVER_KERNEL_DIR must point to the kernel build output}"
: "${ROOTFS_DIR:?ROOTFS_DIR must be set by pi-gen}"

KDIR="${FIREASMSERVER_KERNEL_DIR}"

# Pi 5 boot artifacts live under /boot/firmware/ on Trixie.
install -d "${ROOTFS_DIR}/boot/firmware"
install -m 0644 "${KDIR}/kernel_2712.img" "${ROOTFS_DIR}/boot/firmware/kernel_2712.img"
install -m 0644 "${KDIR}/dtb/bcm2712-rpi-5-b.dtb" "${ROOTFS_DIR}/boot/firmware/bcm2712-rpi-5-b.dtb"

# Overlays
install -d "${ROOTFS_DIR}/boot/firmware/overlays"
if compgen -G "${KDIR}/overlays/*.dtbo" >/dev/null; then
    install -m 0644 "${KDIR}"/overlays/*.dtbo "${ROOTFS_DIR}/boot/firmware/overlays/"
fi

# Modules: rsync the depmod'd tree preserving perms/times.
rsync -a "${KDIR}/modules_staging/lib/modules/" "${ROOTFS_DIR}/lib/modules/"

# Report what we installed.
# sort -V + tail -1 picks the highest kernel version if multiple staging
# dirs ever coexist (find's default order is filesystem-dependent and would
# silently report the wrong KVER).
KVER=$(find "${KDIR}/modules_staging/lib/modules" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -1)
echo "Installed custom kernel ${KVER}"
echo "  Image:     /boot/firmware/kernel_2712.img"
echo "  DTB:       /boot/firmware/bcm2712-rpi-5-b.dtb"
echo "  Modules:   /lib/modules/${KVER}/"
