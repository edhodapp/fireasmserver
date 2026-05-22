"""Ethernet frame size bounds — `ETH-003`, `ETH-013`.

Per `docs/l2/REQUIREMENTS.md` and `TEST_PLAN.md` §1.2: the L2
receiver must drop frames outside the Ethernet wire-size range
(60..1518 bytes, FCS-excluded per Virtio 1.2 §5.1.6.1). Frames
shorter than 60 bytes are runts (ETH-013); frames longer than
1518 bytes are oversize (ETH-003). Either way the receiver
emits an `RX:DROP` marker, does not invoke ARP recognition, and
does not place anything on TX in response.

These tests exist BEFORE the dispatcher implements the check —
test-first per CLAUDE.md's "Repro before fix" discipline. The
tests will fail until the size-bounds gate lands in
arch/<isa>/l2/dispatcher.S.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scapy.packet import Packet

from l2_harness import frames
from l2_harness.capture import FrameSender, capturing
from l2_harness.firecracker import FirecrackerGuest
from l2_harness.frames import parse_arp_reply, raw_eth_frame
from l2_harness.serial import SerialLog
from l2_harness.tap0 import host_mtu_of


CAPTURE_WINDOW_SECONDS = 1.5

OVERSIZE_REQUIRED_MTU = 1700
"""Minimum tap0 MTU for the oversize test to actually send the frame.

The kernel's AF_PACKET raw send refuses frames larger than the
device MTU (errno EMSGSIZE). Default tap0 MTU is 1500, which
caps wire frames at 1514 bytes — below the ETH-003 oversize
threshold (1518). Operators bump tap0 MTU at setup time with:

    sudo ip link set tap0 mtu 2000

The test skips with a clear message rather than failing
opaquely on EMSGSIZE.
"""


def _no_arp_reply_assert(cap_packets: list[Packet],
                         captured_pcap: Path,
                         serial_text: str,
                         case_id: str) -> None:
    """Helper: assert no ARP reply landed on tap0."""
    parsed = [parse_arp_reply(bytes(p)) for p in cap_packets]
    replies = [r for r in parsed if r is not None]
    if replies:
        raise AssertionError(
            f"{case_id}: malformed frame should not elicit a "
            f"reply but {len(replies)} reply observed. "
            f"Serial log:\n{serial_text}\n"
            f"See {captured_pcap}"
        )


def test_oversize_frame_dropped(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-003: frame > 1518 bytes (wire) → drop, no ARP, no reply."""
    mtu = host_mtu_of("tap0")
    if mtu is None or mtu < OVERSIZE_REQUIRED_MTU:
        pytest.skip(
            f"tap0 MTU is {mtu}; ETH-003 needs >= "
            f"{OVERSIZE_REQUIRED_MTU} to send an oversize frame "
            "without kernel EMSGSIZE. Bump with:\n"
            "    sudo ip link set tap0 mtu 2000"
        )
    # 1600-byte payload + 14-byte header = 1614 bytes wire,
    # comfortably above the 1518 ceiling.
    payload = b"\xAB" * 1600
    oversize = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,           # local-experimental EtherType
        payload=payload,
    )

    captured_pcap = artifact_dir / "captured-eth003.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(oversize)
        # Wait for the capture window; assert the guest never
        # processed it as a regular frame.
        serial_log.assert_marker_absent(
            "ARP:REQUEST", window=CAPTURE_WINDOW_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-003",
    )
    # The guest must explicitly emit RX:DROP for the dropped frame
    # — the production-bar requirement is observability of the
    # drop, not just absence of further processing.
    serial_log.assert_marker_observed("RX:DROP", timeout=0.0)


def test_runt_frame_dropped(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-013: frame < 60 bytes (wire) → drop, no ARP, no reply."""
    # 30-byte payload + 14-byte header = 44 bytes wire,
    # well below the 60-byte runt threshold.
    payload = b"\xCD" * 30
    runt = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,
        payload=payload,
    )

    captured_pcap = artifact_dir / "captured-eth013.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(runt)
        serial_log.assert_marker_absent(
            "ARP:REQUEST", window=CAPTURE_WINDOW_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-013",
    )
    serial_log.assert_marker_observed("RX:DROP", timeout=0.0)
