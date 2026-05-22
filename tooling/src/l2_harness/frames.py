"""Frame construction helpers for L2 integration tests.

Wraps scapy's high-level layer construction for the common
protocol patterns the tests need (ARP, plain Ethernet). For
negative tests that need byte-exact corruption, drop down to
scapy's `Raw()` layer plus explicit byte construction directly
from the test — the helpers here intentionally don't try to
expose every corner.

Per `docs/l2/HARNESS.md` §3.4.
"""

from __future__ import annotations

from typing import cast

from scapy.layers.l2 import ARP, Ether


BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"
HOST_DEFAULT_MAC = "02:00:00:00:00:42"
HOST_DEFAULT_IP = "192.168.42.1"
GUEST_DEFAULT_MAC = "02:00:00:00:00:01"
GUEST_DEFAULT_IP = "192.168.42.2"


def arp_request(target_ip: str,
                sender_ip: str = HOST_DEFAULT_IP,
                sender_mac: str = HOST_DEFAULT_MAC) -> bytes:
    """Build an Ethernet-framed ARP request as raw bytes.

    Destination MAC is the Ethernet broadcast (RFC 826 §6); the
    target hardware address in the ARP payload is zero (the host
    doesn't yet know the target MAC — that's the whole point).
    The returned bytes are wire-ready: scapy's `sendp` accepts
    them via `Ether(bytes)` round-trip, but tests usually pass
    them through `FrameSender.send` which handles the conversion.
    """
    frame = (
        Ether(dst=BROADCAST_MAC, src=sender_mac, type=0x0806)
        / ARP(
            hwtype=1,
            ptype=0x0800,
            hwlen=6,
            plen=4,
            op=1,                  # request
            hwsrc=sender_mac,
            psrc=sender_ip,
            hwdst="00:00:00:00:00:00",
            pdst=target_ip,
        )
    )
    return bytes(frame)


def parse_arp_reply(raw: bytes) -> ARP | None:
    """Return the ARP layer if `raw` is an ARP reply, else None.

    Used by the capture side: tests filter for ARP frames coming
    out of the guest and extract the ARP fields to assert against
    expected MAC/IP values.
    """
    frame = Ether(raw)
    if ARP not in frame:
        return None
    arp_layer = cast(ARP, frame[ARP])
    if arp_layer.op != 2:  # ARP_REPLY
        return None
    return arp_layer
