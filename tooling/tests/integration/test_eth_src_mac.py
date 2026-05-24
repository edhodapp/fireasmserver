"""Source MAC unicast-bit check — `ETH-015`.

Per `docs/l2/REQUIREMENTS.md` §1 row ETH-015: the source MAC
field of an incoming Ethernet frame MUST have its unicast bit
clear (bit 0 of byte 0 = 0). A frame with a multicast or
broadcast source address is malformed per IEEE 802.3-2018
§4.1.2.1 — the source field identifies the originating
station, which is by definition a unicast endpoint. The
dispatcher drops such frames with marker `RX:DROP src` and
does not invoke ARP recognition or RX:FRAME emission.

Positive coverage (unicast source MAC accepted) is implicit
in every other integration test in the suite — they all use
src MACs with bit 0 of byte 0 = 0. This file is the negative-
side guard, plus a sanity-check that does send a known-good
source and confirms the filter doesn't false-positive.
"""

from __future__ import annotations

from pathlib import Path

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


MARKER_TIMEOUT_SECONDS = 1.5
POST_MARKER_QUIESCE_SECONDS = 0.3
# Capture timeout MUST outlast (marker wait + quiescence) so a
# late guest TX in that gap can't slip past the sniffer. See
# test_eth_size_bounds.py for the longer rationale.
CAPTURE_TIMEOUT_SECONDS = MARKER_TIMEOUT_SECONDS + POST_MARKER_QUIESCE_SECONDS

MULTICAST_SRC_MAC = "03:00:00:00:00:42"
"""Multicast source MAC for the negative test.

Byte 0 = 0x03 = 0b00000011. Bit 0 set → multicast. Bit 1 set
→ locally-administered (informational only per ETH-017; the
test uses it just to keep the MAC clearly synthetic). The
dispatcher's ETH-015 check fires on bit 0 alone; the L/A bit
is irrelevant.
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
            f"{case_id}: frame should not elicit a reply but "
            f"{len(replies)} reply observed. "
            f"Serial log:\n{serial_text}\n"
            f"See {captured_pcap}"
        )


def test_multicast_source_mac_dropped(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-015: frame with multicast source MAC → drop, no further processing.

    The frame's destination is GUEST_MAC (passes MAC filter)
    and its size is valid (passes size bounds), so any drop
    must come from the ETH-015 source-MAC check. Asserts on
    the specific marker `RX:DROP src` to differentiate from
    other drop reasons in the serial log.
    """
    payload = b"\x77" * 46     # 14 header + 46 payload = 60 wire
    frame = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=MULTICAST_SRC_MAC,
        ethertype=0x88B5,           # local-experimental EtherType
        payload=payload,
    )
    assert len(frame) == 60, (
        f"ETH-015 test frame must be 60 wire bytes, got {len(frame)}"
    )

    captured_pcap = artifact_dir / "captured-eth015.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_TIMEOUT_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        serial_log.assert_marker_observed(
            "RX:DROP src", timeout=MARKER_TIMEOUT_SECONDS,
        )
        # The source-MAC check runs AFTER the dst MAC filter
        # (which passed: dst was GUEST_MAC) but BEFORE ARP
        # recognition — assert no ARP marker fired even though
        # the EtherType could in principle be ARP.
        serial_log.assert_marker_absent(
            "ARP:REQUEST", window=POST_MARKER_QUIESCE_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-015",
    )


def test_unicast_source_mac_accepted(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-015 companion: frame with unicast source MAC → no src drop.

    Belt-and-braces against a future polarity flip that would
    turn the `tbnz w14, #0 / test byte, 1` into the opposite
    sense and start dropping every well-formed frame.
    """
    payload = b"\x88" * 46
    frame = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,    # 02:00:00:00:00:42 — bit 0 clear
        ethertype=0x88B5,
        payload=payload,
    )
    expected_used_len = f"used_len={(len(frame) + VIRTIO_NET_HDR_LEN):08X}"

    captured_pcap = artifact_dir / "captured-eth015-ok.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_TIMEOUT_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        serial_log.assert_marker_observed(
            expected_used_len, timeout=MARKER_TIMEOUT_SECONDS,
        )
        serial_log.assert_marker_absent(
            "RX:DROP src", window=POST_MARKER_QUIESCE_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-015-OK",
    )
