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

ETH_MIN_FRAME_LEN = 60
"""Minimum Ethernet frame size on the wire (excluding FCS).

Per IEEE 802.3, an Ethernet frame must be at least 64 bytes
including the 4-byte FCS. Virtio strips the FCS (Virtio 1.2
§5.1.6.1), so the on-the-wire-as-seen-by-virtio minimum is 60.
Senders must pad to this length; receivers may drop shorter
frames as runts (REQUIREMENTS.md ETH-013).

scapy's `Ether()/ARP()` round-trip emits 42 bytes (14 header +
28 ARP payload, no padding). The harness pads explicitly so
the production-bar L2 dispatcher's runt-drop policy doesn't
silently reject test stimuli.
"""

ETH_MAX_FRAME_LEN = 1518
"""Maximum Ethernet frame size on the wire (excluding FCS).

Standard untagged Ethernet: 1518 bytes including FCS, 1514
without. With one 802.1Q VLAN tag the limit grows to 1518
without FCS / 1522 with. The dispatcher's oversize check uses
the no-VLAN value as the conservative bound (ETH-003); VLAN
support is its own TEST_PLAN row (§2).
"""


def arp_request(target_ip: str,
                sender_ip: str = HOST_DEFAULT_IP,
                sender_mac: str = HOST_DEFAULT_MAC) -> bytes:
    """Build an Ethernet-framed ARP request as raw bytes.

    Destination MAC is the Ethernet broadcast (RFC 826 §6); the
    target hardware address in the ARP payload is zero (the host
    doesn't yet know the target MAC — that's the whole point).
    The frame is padded to ETH_MIN_FRAME_LEN (60 bytes) so the
    receiver's runt-drop policy (ETH-013) does not reject the
    test stimulus.
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
    raw = bytes(frame)
    if len(raw) < ETH_MIN_FRAME_LEN:
        raw += b"\x00" * (ETH_MIN_FRAME_LEN - len(raw))
    return raw


def raw_eth_frame(dst_mac: str, src_mac: str, ethertype: int,
                  payload: bytes) -> bytes:
    """Build a raw Ethernet frame with arbitrary payload.

    Used by negative tests that need to exercise specific size
    boundaries. The returned bytes are EXACTLY 14 (header) +
    len(payload) bytes — no padding, no validation. Tests
    construct oversize / runt frames by sizing the payload
    explicitly.
    """
    def _parse(mac: str) -> bytes:
        parts = mac.split(":")
        if len(parts) != 6:
            raise ValueError(f"bad MAC {mac!r}")
        return bytes(int(p, 16) for p in parts)

    return (
        _parse(dst_mac)
        + _parse(src_mac)
        + ethertype.to_bytes(2, "big")
        + payload
    )


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
