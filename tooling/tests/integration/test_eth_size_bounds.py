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

Each assertion narrows on the marker that uniquely identifies
THIS test's frame: `used_len=<hex>` for the specific virtio-side
size. The iter-1 kernel NDP frame (122 bytes / `used_len=0000007A`)
shares the dispatcher with the test's frame, so a naked
`assert_marker_observed("RX:FRAME")` would succeed on the NDP
alone and miss test-frame-specific bugs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scapy.packet import Packet

from l2_harness import frames
from l2_harness.capture import FrameSender, capturing
from l2_harness.firecracker import FirecrackerGuest
from l2_harness.frames import (
    VIRTIO_NET_HDR_LEN,
    parse_arp_reply,
    raw_eth_frame,
)
from l2_harness.serial import SerialLog
from l2_harness.tap0 import host_mtu_of


# Worst-case time we'll wait for a marker to land. The dispatcher
# processes a frame within a few ms once virtio delivers it, but
# the boot path takes ~150 ms before l2_dispatch is even running,
# and AF_PACKET → tap0 → Firecracker → guest RX has its own
# latency. 1.5 s covers all of that with comfortable slack.
MARKER_TIMEOUT_SECONDS = 1.5

# After the dispatcher has had time to process our frame (RX:DROP
# observed, or RX:FRAME observed via used_len match), this short
# additional window catches a delayed second emit — e.g. if a
# bogus duplicate landed and we want to know about it.
POST_MARKER_QUIESCE_SECONDS = 0.3

# Capture timeout MUST outlast (marker wait + quiescence) — if the
# sniffer stops while the test body is still waiting for the
# serial marker, any late guest TX in that gap (the case the
# _no_arp_reply_assert helper guards against) lands after the
# sniffer is gone and is silently missed. AsyncSniffer makes
# __exit__ return as soon as we call .stop() so this is no longer
# a runtime-cost issue, just a coverage one.
CAPTURE_TIMEOUT_SECONDS = MARKER_TIMEOUT_SECONDS + POST_MARKER_QUIESCE_SECONDS

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


def _virtio_used_len_hex(wire_bytes: int) -> str:
    """Format the marker suffix for a given wire frame size.

    Dispatcher emits `used_len=` followed by an 8-hex-digit
    representation of the virtio used_len, which is wire bytes
    plus the 12-byte virtio_net_hdr prepended by the device.
    """
    return f"used_len={(wire_bytes + VIRTIO_NET_HDR_LEN):08X}"


def _no_arp_reply_assert(cap_packets: list[Packet],
                         captured_pcap: Path,
                         serial_text: str,
                         case_id: str) -> None:
    """Helper: assert no ARP reply landed on tap0.

    Belt-and-braces guard — the test frames have a non-ARP
    EtherType so a reply shouldn't be possible regardless, but
    a dispatcher bug that mis-routes any frame to the ARP
    reply path (e.g., the TX side firing on stale state) would
    show up here.
    """
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
    tap_iface: str,  # pylint: disable=redefined-outer-name
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
    expected_used_len = _virtio_used_len_hex(60)   # "used_len=00000048"

    captured_pcap = artifact_dir / "captured-eth003.pcap"
    with capturing(
        iface=tap_iface,
        bpf_filter="arp",
        timeout=CAPTURE_TIMEOUT_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        # Specific marker — `used_len=00000048` is uniquely this
        # frame's. The iter-1 kernel NDP frame has used_len=0x7A
        # so its RX:FRAME marker doesn't satisfy this assertion.
        serial_log.assert_marker_observed(
            expected_used_len, timeout=MARKER_TIMEOUT_SECONDS,
        )
        # And no drop for any frame in this dispatch — this also
        # catches a regression that turns the iter-1 NDP into a
        # drop (would imply a different bug, but worth knowing).
        serial_log.assert_marker_absent(
            "RX:DROP", window=POST_MARKER_QUIESCE_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-003",
    )


def test_max_size_frame_accepted(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
    tap_iface: str,  # pylint: disable=redefined-outer-name
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
    expected_used_len = _virtio_used_len_hex(1518)  # "used_len=000005FA"

    captured_pcap = artifact_dir / "captured-eth004.pcap"
    with capturing(
        iface=tap_iface,
        bpf_filter="arp",
        timeout=CAPTURE_TIMEOUT_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        serial_log.assert_marker_observed(
            expected_used_len, timeout=MARKER_TIMEOUT_SECONDS,
        )
        serial_log.assert_marker_absent(
            "RX:DROP", window=POST_MARKER_QUIESCE_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-004",
    )


def test_oversize_frame_dropped(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
    tap_iface: str,  # pylint: disable=redefined-outer-name
) -> None:
    """ETH-011: frame > 1518 bytes (wire) → drop, no further processing."""
    mtu = host_mtu_of("tap0")
    if mtu is None or mtu < MAX_FRAME_REQUIRED_MTU:
        pytest.skip(
            f"tap0 MTU is {mtu}; ETH-011 needs >= "
            f"{MAX_FRAME_REQUIRED_MTU} to send an oversize frame "
            "without kernel EMSGSIZE. Bump with:\n"
            "    sudo ip link set tap0 mtu 2000"
        )
    # 1600-byte payload + 14-byte header = 1614 bytes wire = 1626
    # virtio bytes (0x65A). Comfortably above the 1530 ceiling.
    payload = b"\xAB" * 1600
    oversize = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,           # local-experimental EtherType
        payload=payload,
    )
    expected_drop = (
        f"RX:DROP used_len={(len(oversize) + VIRTIO_NET_HDR_LEN):08X}"
    )

    captured_pcap = artifact_dir / "captured-eth011.pcap"
    with capturing(
        iface=tap_iface,
        bpf_filter="arp",
        timeout=CAPTURE_TIMEOUT_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(oversize)
        # Wait for the SPECIFIC drop marker for our frame, with
        # a fast-resolution timeout — Gemini-review feedback on
        # the unconditional sleep-window pattern that the
        # original test used.
        serial_log.assert_marker_observed(
            expected_drop, timeout=MARKER_TIMEOUT_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-011",
    )


def test_runt_frame_dropped(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
    tap_iface: str,  # pylint: disable=redefined-outer-name
) -> None:
    """ETH-010: frame < 60 bytes (wire) → drop, no further processing.

    On Linux, AF_PACKET SOCK_RAW + tap0 does NOT pad runt frames
    on the way out — scapy.sendp delivers our bytes verbatim
    to the kernel and the tap driver hands them to Firecracker
    unchanged. (PF_PACKET SOCK_DGRAM would pad; we use SOCK_RAW.)
    If a future host kernel started padding here, the runt would
    arrive as 60+ bytes and the dispatcher would accept it; this
    test's `expected_drop` substring assertion would fail with
    a clear "marker not observed" message rather than passing
    silently — surfaces the regression rather than hiding it.
    """
    # 30-byte payload + 14-byte header = 44 bytes wire = 56 virtio
    # (0x38). Well below the 72-virtio runt threshold.
    payload = b"\xCD" * 30
    runt = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,
        payload=payload,
    )
    expected_drop = (
        f"RX:DROP used_len={(len(runt) + VIRTIO_NET_HDR_LEN):08X}"
    )

    captured_pcap = artifact_dir / "captured-eth010.pcap"
    with capturing(
        iface=tap_iface,
        bpf_filter="arp",
        timeout=CAPTURE_TIMEOUT_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(runt)
        serial_log.assert_marker_observed(
            expected_drop, timeout=MARKER_TIMEOUT_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-010",
    )
