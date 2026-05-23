"""Ethernet frame size bounds — `ETH-003`, `ETH-004`, `ETH-010`, `ETH-011`.

Per `docs/l2/REQUIREMENTS.md` §1 and `TEST_PLAN.md` §1.2:

  - `ETH-003`: minimum frame is 64 bytes including FCS. Virtio
    strips the 4-byte FCS (Virtio 1.2 §5.1.6.1), so the wire-as-
    seen-by-virtio minimum is 60 bytes. A 60-byte wire frame
    (translating to 72 virtio bytes once the 12-byte
    `virtio_net_hdr` is prepended) MUST be accepted.
  - `ETH-004`: maximum untagged frame is 1518 bytes. A 1518-byte
    wire frame (1530 virtio bytes) MUST be accepted.
  - `ETH-010`: frames shorter than the minimum MUST be discarded
    (runt).
  - `ETH-011`: frames longer than the maximum MUST be discarded
    (oversize).

The boundary OK cases (ETH-003, ETH-004) are regression guards
on the dispatcher's `cmp …, 72 / jb` and `cmp …, 1530 / ja`
checks — both use strict comparison so the boundary value
itself passes. A future bug that switches either to non-strict
(`jbe` / `jae`) would silently flip the boundary frame into a
drop; these tests catch that.

The discard cases (ETH-010, ETH-011) were the original size-
bounds work that landed with the dispatcher's drop path.
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

MAX_FRAME_REQUIRED_MTU = 1700
"""Minimum tap0 MTU for ETH-004 / ETH-011 to actually send their frames.

The kernel's AF_PACKET raw send refuses frames larger than the
device MTU (errno EMSGSIZE). Default tap0 MTU is 1500, which
caps wire frames at 1514 bytes — below the ETH-004 boundary
(1518) and well below the ETH-011 oversize threshold. Operators
bump tap0 MTU at setup time with:

    sudo ip link set tap0 mtu 2000

The tests skip with a clear message rather than failing
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
            f"{case_id}: frame should not elicit a "
            f"reply but {len(replies)} reply observed. "
            f"Serial log:\n{serial_text}\n"
            f"See {captured_pcap}"
        )


def test_min_size_frame_accepted(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-003: 60-byte wire frame (min) → accept, no drop.

    Regression guard against a future bug that flips the
    `cmp ebx, 72 / jb .l2_rx_drop` strict comparison to
    `jbe` and starts dropping minimum-size frames.
    """
    # 14-byte Ethernet header + 46-byte payload = 60 wire bytes
    # = 72 virtio bytes (with the 12-byte virtio_net_hdr).
    # Right at the dispatcher's lower bound.
    payload = b"\x55" * 46
    frame = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,
        payload=payload,
    )
    assert len(frame) == 60, (
        f"ETH-003 test frame must be 60 wire bytes, got {len(frame)}"
    )

    captured_pcap = artifact_dir / "captured-eth003.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        serial_log.assert_marker_observed(
            "RX:FRAME", timeout=CAPTURE_WINDOW_SECONDS,
        )
        serial_log.assert_marker_absent("RX:DROP", window=0.5)

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-003",
    )


def test_max_size_frame_accepted(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-004: 1518-byte wire frame (max untagged) → accept, no drop.

    Regression guard against a future bug that flips the
    `cmp ebx, 1530 / ja .l2_rx_drop` strict comparison to
    `jae` and starts dropping the largest valid frames.
    """
    mtu = host_mtu_of("tap0")
    if mtu is None or mtu < MAX_FRAME_REQUIRED_MTU:
        pytest.skip(
            f"tap0 MTU is {mtu}; ETH-004 needs >= "
            f"{MAX_FRAME_REQUIRED_MTU} to send a 1518-byte frame "
            "without kernel EMSGSIZE. Bump with:\n"
            "    sudo ip link set tap0 mtu 2000"
        )
    # 14-byte Ethernet header + 1504-byte payload = 1518 wire bytes
    # = 1530 virtio bytes. Right at the dispatcher's upper bound.
    payload = b"\xAA" * 1504
    frame = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,
        payload=payload,
    )
    assert len(frame) == 1518, (
        f"ETH-004 test frame must be 1518 wire bytes, got {len(frame)}"
    )

    captured_pcap = artifact_dir / "captured-eth004.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        serial_log.assert_marker_observed(
            "RX:FRAME", timeout=CAPTURE_WINDOW_SECONDS,
        )
        serial_log.assert_marker_absent("RX:DROP", window=0.5)

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-004",
    )


def test_oversize_frame_dropped(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-011: frame > 1518 bytes (wire) → drop, no ARP, no reply."""
    mtu = host_mtu_of("tap0")
    if mtu is None or mtu < MAX_FRAME_REQUIRED_MTU:
        pytest.skip(
            f"tap0 MTU is {mtu}; ETH-011 needs >= "
            f"{MAX_FRAME_REQUIRED_MTU} to send an oversize frame "
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

    captured_pcap = artifact_dir / "captured-eth011.pcap"
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
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-011",
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
    """ETH-010: frame < 60 bytes (wire) → drop, no ARP, no reply."""
    # 30-byte payload + 14-byte header = 44 bytes wire,
    # well below the 60-byte runt threshold.
    payload = b"\xCD" * 30
    runt = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,
        payload=payload,
    )

    captured_pcap = artifact_dir / "captured-eth010.pcap"
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
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-010",
    )
    serial_log.assert_marker_observed("RX:DROP", timeout=0.0)
