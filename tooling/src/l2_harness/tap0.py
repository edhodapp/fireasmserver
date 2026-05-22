"""tap0 interface verification for L2 integration tests.

The harness assumes a pre-persistent tap0 created by the operator
(matching the existing `tooling/tracer_bullet/run_local.sh`
"pre-persistent mode" path). On first invocation in a session it
verifies tap0 exists and is configured at the expected IP, then
flushes any host-side ARP cache entries for the guest IP so each
test starts from a known neighbor-table state.

Per `docs/l2/HARNESS.md` §3.1.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path


SYSFS_NET_DIR = Path("/sys/class/net")


class Tap0NotFoundError(RuntimeError):
    """tap0 is not present in /sys/class/net.

    The operator must create it explicitly (e.g.,
    `sudo ip tuntap add dev tap0 mode tap user $USER &&
     sudo ip link set tap0 up &&
     sudo ip addr add 192.168.42.1/24 dev tap0`)
    before the harness can run. The harness intentionally does
    NOT auto-create tap0 — that would require sudo elevation
    we want to keep out of test code.
    """


def has_tap0() -> bool:
    """True if the tap0 interface exists in the kernel."""
    return (SYSFS_NET_DIR / "tap0").exists()


def host_ipv4_of(iface: str) -> str | None:
    """Return the first IPv4 address bound to `iface`, or None.

    Used to verify the host has 192.168.42.1/24 on tap0 before
    running ARP tests. Reads via `ip -4 addr show` to avoid a
    netlink dependency.
    """
    result = subprocess.run(
        ["ip", "-4", "addr", "show", "dev", iface],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return None
    return _first_valid_inet(result.stdout)


def _first_valid_inet(ip_addr_output: str) -> str | None:
    """Pick the first `inet X.X.X.X/N` line and validate the address."""
    for line in ip_addr_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("inet "):
            continue
        addr = stripped.split()[1].split("/")[0]
        try:
            socket.inet_aton(addr)
            return addr
        except OSError:
            continue
    return None


def flush_arp_cache(guest_ip: str, iface: str = "tap0") -> None:
    """Drop the host's ARP/neighbor entry for `guest_ip`.

    Called between tests so each test starts with a cold cache
    and the guest must actually respond to ARP for the host's
    next request to succeed. Tolerates "no such entry" (the
    expected state on first call) without raising.

    `ip neigh del` needs CAP_NET_ADMIN. The harness's venv Python
    only has CAP_NET_RAW (per docs/l2/HARNESS.md §3.3), so the
    delete fails with `Operation not permitted`. That failure is
    currently benign — the integration tests use AF_PACKET sniff,
    which doesn't consult the kernel's ARP cache — but the
    "no silent suppressions" rule (CLAUDE.md feedback) says we
    must NOT hide it. Surface it as a debug-visible warning so
    a future test that DOES depend on a clean kernel ARP cache
    fails loudly rather than mysteriously.
    """
    result = subprocess.run(
        ["ip", "neigh", "del", guest_ip, "dev", iface],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        return
    stderr = result.stderr.strip()
    # "RTNETLINK answers: No such file or directory" is the
    # expected first-call state. Anything else (permission
    # denied, no route, etc.) gets surfaced.
    if "No such file or directory" in stderr:
        return
    print(
        f"WARNING: `ip neigh del {guest_ip} dev {iface}` "
        f"returned {result.returncode}: {stderr!r}",
        flush=True,
    )


def require_tap0(expected_host_ip: str = "192.168.42.1") -> None:
    """Raise if tap0 is missing or not at the expected IP.

    Tests call this from a session-scoped fixture to fail fast
    with an actionable error if the operator hasn't set up tap0
    correctly.
    """
    if not has_tap0():
        raise Tap0NotFoundError(
            "tap0 not present in /sys/class/net. Create it with:\n"
            "  sudo ip tuntap add dev tap0 mode tap user $USER\n"
            "  sudo ip link set tap0 up\n"
            f"  sudo ip addr add {expected_host_ip}/24 dev tap0\n"
            "and re-run the integration tests."
        )
    actual = host_ipv4_of("tap0")
    if actual != expected_host_ip:
        raise Tap0NotFoundError(
            f"tap0 exists but has IPv4 {actual!r}; expected "
            f"{expected_host_ip!r}. Re-configure with:\n"
            f"  sudo ip addr flush dev tap0\n"
            f"  sudo ip addr add {expected_host_ip}/24 dev tap0\n"
        )
