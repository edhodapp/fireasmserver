#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Set up apt-cacher-ng on the laptop as the package source for the Pi 5.
# Per D035.
#
# Idempotent — safe to re-run. Installs the package if absent, ensures the
# service is enabled and running, and adds a UFW rule allowing only the
# laptop↔Pi network (10.0.2.0/24 by default) to reach the cache port.
#
# After this, run ./tooling/apt_cache/pi_setup.sh to configure the running
# Pi. For future pi-gen builds, the Pi-side config is baked in via
# tooling/pi5_build/pi_gen_stage/06-apt-proxy/.

set -euo pipefail

PI_NET="${PI_NET:-10.0.2.0/24}"
PORT="${PORT:-3142}"

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user; the script uses sudo internally." >&2
    exit 1
fi

# Validate PORT is a bare port number — used unquoted in regex comparisons
# below, so an exotic override like PORT=.* must not silently match things.
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] || {
    echo "ERROR: PORT='$PORT' is not a valid TCP port number." >&2
    exit 1
}

echo "=== apt-cacher-ng laptop setup (D035) ==="
echo "  package source for Pi 5 on isolated bridge"
echo "  cache will listen on port $PORT"
echo "  firewall will accept only $PI_NET"
echo

### 1. Install apt-cacher-ng ########################################
if ! command -v apt-cacher-ng >/dev/null 2>&1 \
   && ! dpkg -s apt-cacher-ng >/dev/null 2>&1; then
    echo "--- installing apt-cacher-ng ---"
    sudo apt-get update
    # Noninteractive — the installer otherwise asks a debconf question
    # about tunneling behavior.
    sudo DEBIAN_FRONTEND=noninteractive \
         DEBCONF_NONINTERACTIVE_SEEN=true \
         apt-get install -y apt-cacher-ng
else
    echo "--- apt-cacher-ng already installed ---"
fi

### 2. Ensure service is running ####################################
if systemctl is-active --quiet apt-cacher-ng; then
    echo "--- service already active ---"
else
    echo "--- enabling and starting apt-cacher-ng.service ---"
    sudo systemctl enable --now apt-cacher-ng
fi

### 3. Verify listening on $PORT ####################################
if ss -tln 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$"; then
    echo "--- listening on :$PORT ---"
else
    echo "ERROR: apt-cacher-ng did not bind to port $PORT" >&2
    echo "       Check systemctl status apt-cacher-ng" >&2
    exit 1
fi

### 4. Firewall: allow only $PI_NET to reach $PORT ##################
if command -v ufw >/dev/null 2>&1; then
    UFW_STATUS="$(sudo ufw status | head -1 | awk '{print $2}')"
    if [[ "$UFW_STATUS" == "active" ]]; then
        # ufw status prints rules as: "<port>/tcp  ALLOW IN  <source>"
        # Match that order; anchor the port with \b so 3142 doesn't match
        # 31420 or 13142. The PI_NET dots are regex-escaped explicitly.
        _PI_NET_RE="${PI_NET//./\\.}"
        if sudo ufw status \
            | grep -qE "^${PORT}/tcp[[:space:]]+ALLOW IN[[:space:]]+${_PI_NET_RE}([[:space:]]|\$)"; then
            echo "--- UFW rule already in place ---"
        else
            echo "--- adding UFW rule: $PI_NET → port $PORT/tcp ---"
            sudo ufw allow from "$PI_NET" to any port "$PORT" proto tcp
        fi
    else
        echo "--- UFW present but inactive; no firewall rule added ---"
        echo "    If you enable UFW later, run:"
        echo "      sudo ufw allow from $PI_NET to any port $PORT proto tcp"
    fi
else
    echo "--- UFW not installed; skipping firewall step ---"
    echo "    If you run iptables directly, the equivalent is:"
    echo "      sudo iptables -I INPUT -p tcp --dport $PORT -s $PI_NET -j ACCEPT"
    echo "      sudo iptables -A INPUT -p tcp --dport $PORT -j DROP"
fi

echo
echo "=== laptop setup complete ==="
echo
echo "Next step:"
echo "  ./tooling/apt_cache/pi_setup.sh      # configure the running Pi"
echo "  (new pi-gen builds pick up the config automatically via"
echo "   tooling/pi5_build/pi_gen_stage/06-apt-proxy/)"
