#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Configure a running Pi 5 to use the laptop-hosted apt-cacher-ng proxy.
# Per D035. Assumes the laptop-side setup (laptop_setup.sh) has already
# run and the Pi is reachable via SSH at 10.0.2.2 with the fireasm_pi5_ed
# key.
#
# Idempotent — re-running overwrites /etc/apt/apt.conf.d/00proxy on the Pi
# with the current values. The next pi-gen rebuild bakes this file in via
# tooling/pi5_build/pi_gen_stage/06-apt-proxy/, so this script is only
# needed for a Pi already booted from an image that predates the stage.

set -euo pipefail

PI_HOST="${PI_HOST:-10.0.2.2}"
PI_USER="${PI_USER:-ed}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/fireasm_pi5_ed}"

# These env vars match the names used by pi_gen_stage/06-apt-proxy so that
# a user exporting FIREASMSERVER_LAPTOP_IP=<x> affects BOTH the baked-into-
# image proxy config AND this running-Pi override script. Avoids silent
# drift where a custom build gets a different proxy address than
# pi_setup.sh writes on its next run.
LAPTOP_IP="${FIREASMSERVER_LAPTOP_IP:-10.0.2.1}"
PORT="${FIREASMSERVER_PROXY_PORT:-3142}"

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user." >&2
    exit 1
fi

if [[ ! -f "$SSH_KEY" ]]; then
    echo "ERROR: SSH key missing at $SSH_KEY" >&2
    echo "       (expected from build_pi_image.sh; re-run that if lost)" >&2
    exit 1
fi

# Preserve -i + IdentitiesOnly so the agent doesn't offer other keys.
# StrictHostKeyChecking=accept-new lets first-contact succeed cleanly under
# BatchMode (which disables prompts) — without it, the first run fails
# unpredictably on a host-key decision it cannot ask about.
SSH_OPTS=(
    -i "$SSH_KEY"
    -o IdentitiesOnly=yes
    -o BatchMode=yes
    -o StrictHostKeyChecking=accept-new
)

echo "=== Pi-side apt-cacher-ng config (D035) ==="
echo "  Pi:     $PI_USER@$PI_HOST"
echo "  Proxy:  http://$LAPTOP_IP:$PORT"
echo

### 1. Sanity: Pi reachable? ########################################
echo "--- verifying SSH reachable ---"
if ! ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" true; then
    echo "ERROR: cannot SSH to $PI_USER@$PI_HOST with key $SSH_KEY" >&2
    exit 1
fi

### 2. Verify Pi can reach the proxy ################################
echo "--- verifying Pi can reach $LAPTOP_IP:$PORT ---"
if ! ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" \
    "nc -zw 2 $LAPTOP_IP $PORT"; then
    echo "ERROR: Pi cannot reach TCP $LAPTOP_IP:$PORT" >&2
    echo "       Check that apt-cacher-ng is running on the laptop" >&2
    echo "       (./tooling/apt_cache/laptop_setup.sh) and that any" >&2
    echo "       firewall permits 10.0.2.0/24." >&2
    exit 1
fi

### 3. Write /etc/apt/apt.conf.d/00proxy on the Pi ##################
echo "--- writing /etc/apt/apt.conf.d/00proxy ---"
TMPFILE="$(mktemp)"
trap 'rm -f "$TMPFILE"' EXIT
cat > "$TMPFILE" <<EOF
# Managed by tooling/apt_cache/pi_setup.sh (D035).
# Routes Pi APT traffic via laptop-hosted apt-cacher-ng.
Acquire::http::Proxy "http://$LAPTOP_IP:$PORT";
EOF

scp "${SSH_OPTS[@]}" "$TMPFILE" \
    "$PI_USER@$PI_HOST:/tmp/fireasm-apt-proxy.conf" >/dev/null
ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "
    set -e
    sudo install -m 0644 -o root -g root \
        /tmp/fireasm-apt-proxy.conf /etc/apt/apt.conf.d/00proxy
    rm -f /tmp/fireasm-apt-proxy.conf
"

### 4. Proof-of-life: apt update via the proxy ######################
echo "--- apt-get update (proof of life through the proxy) ---"
ssh "${SSH_OPTS[@]}" "$PI_USER@$PI_HOST" "sudo apt-get update"

echo
echo "=== Pi setup complete ==="
echo "Pi now fetches packages via the laptop cache."
echo "'sudo apt install <pkg>' on the Pi requires no pi-gen rebuild."
