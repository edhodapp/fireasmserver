#!/bin/bash -e
# SPDX-License-Identifier: AGPL-3.0-or-later
# Bake the apt-cacher-ng proxy config into the image (D035).
# Pi boots with the proxy already configured; no first-boot action needed.

: "${ROOTFS_DIR:?ROOTFS_DIR must be set by pi-gen}"

# Defaults match D024's laptop-side address and D035's default port.
LAPTOP_IP="${FIREASMSERVER_LAPTOP_IP:-10.0.0.1}"
PROXY_PORT="${FIREASMSERVER_PROXY_PORT:-3142}"

install -d -m 0755 "${ROOTFS_DIR}/etc/apt/apt.conf.d"
cat > "${ROOTFS_DIR}/etc/apt/apt.conf.d/00proxy" <<EOF
# Baked by pi_gen_stage/06-apt-proxy (D035).
# Routes Pi APT traffic via laptop-hosted apt-cacher-ng.
Acquire::http::Proxy "http://${LAPTOP_IP}:${PROXY_PORT}";
EOF
chmod 0644 "${ROOTFS_DIR}/etc/apt/apt.conf.d/00proxy"

echo "Installed apt proxy config: http://${LAPTOP_IP}:${PROXY_PORT}"
