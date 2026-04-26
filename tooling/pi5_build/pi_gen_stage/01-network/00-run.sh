#!/bin/bash -e
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install the static-IP NetworkManager connection for eth0.
# Per D022 + D024: Pi 5 is 10.0.2.2/24 on direct Ethernet to laptop.

: "${ROOTFS_DIR:?ROOTFS_DIR must be set by pi-gen}"

# NetworkManager requires these files be owned root:root with mode 0600.
install -d -m 0755 -o root -g root "${ROOTFS_DIR}/etc/NetworkManager/system-connections"
install -m 0600 -o root -g root \
    "$(dirname "$0")/files/fireasm-eth0.nmconnection" \
    "${ROOTFS_DIR}/etc/NetworkManager/system-connections/fireasm-eth0.nmconnection"
