"""Ethernet MAC filter — `ETH-006`, `ETH-008`, `MAC-001..005`.

Per `docs/l2/REQUIREMENTS.md` and `TEST_PLAN.md` §1.4: the L2
receiver must accept frames whose Ethernet destination is the
guest MAC (`02:00:00:00:00:01`), the broadcast address
(`ff:ff:ff:ff:ff:ff`), or any multicast address (the bit-0 of
the first byte set), and drop unicast frames addressed to any
other MAC. Without this filter the guest leaks higher-layer
processing into frames that physically reached its tap but
were destined for someone else — a real attack surface once
multiple guests share a bridge.

These tests exist BEFORE the dispatcher implements the check —
test-first per CLAUDE.md's "Repro before fix" discipline. The
negative test will fail until the MAC filter lands in
arch/<isa>/l2/dispatcher.S; the positive test passes today
(any unicast frame is accepted) but is included as a
regression guard so a future bug that turns the filter into a
"drop everything not broadcast" policy gets caught.
"""

from __future__ import annotations

from pathlib import Path

from scapy.packet import Packet

from l2_harness import frames
from l2_harness.capture import FrameSender, capturing
from l2_harness.firecracker import FirecrackerGuest
from l2_harness.frames import parse_arp_reply, raw_eth_frame
from l2_harness.serial import SerialLog


CAPTURE_WINDOW_SECONDS = 1.5

WRONG_UNICAST_MAC = "02:00:00:00:00:99"
"""A locally-administered MAC that is NOT the guest MAC.

Locally-administered (bit 1 of byte 0 = 1) AND unicast
(bit 0 of byte 0 = 0). The host-side kernel won't ARP for
it, and the guest must drop it as not-for-us.
"""


def _no_reply_assert(cap_packets: list[Packet],
                     captured_pcap: Path,
                     serial_text: str,
                     case_id: str) -> None:
    """Helper: assert no ARP reply (or any other guest TX) on tap0."""
    parsed = [parse_arp_reply(bytes(p)) for p in cap_packets]
    replies = [r for r in parsed if r is not None]
    if replies:
        raise AssertionError(
            f"{case_id}: frame should not elicit any reply but "
            f"{len(replies)} reply observed. "
            f"Serial log:\n{serial_text}\n"
            f"See {captured_pcap}"
        )


def test_unicast_to_guest_mac_accepted(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """MAC-001: unicast frame to GUEST_MAC → accept, RX:FRAME, no drop.

    Regression guard against a future MAC filter bug that turns
    "accept our MAC + broadcast + multicast" into "accept
    broadcast only" or similar. Sends a non-ARP frame so we
    distinguish "frame accepted by L2" from "frame triggered
    higher-layer ARP processing."
    """
    payload = b"\x55" * 60  # 14 + 60 = 74 wire bytes, valid size
    frame = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,           # local-experimental EtherType
        payload=payload,
    )

    captured_pcap = artifact_dir / "captured-mac-accept.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        # The guest must accept this frame. RX:FRAME should be
        # observed (it gets processed normally past the MAC
        # filter and emits the standard marker).
        serial_log.assert_marker_observed(
            "RX:FRAME", timeout=CAPTURE_WINDOW_SECONDS,
        )
        # And RX:DROP must NOT appear — the frame is to us, valid
        # size, no reason to drop. Detect regressions that flip
        # the filter polarity.
        serial_log.assert_marker_absent("RX:DROP", window=0.5)

    _no_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(),
        "MAC-001",
    )


def test_unicast_to_wrong_mac_dropped(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-006: unicast frame to wrong MAC → drop, no further processing.

    The frame's size is valid and its EtherType is well-formed,
    so any drop the dispatcher emits is attributable to the
    MAC filter (not the size-bounds path). We assert on the
    specific MAC-filter marker `RX:DROP mac` to differentiate
    drop reasons in the serial log.
    """
    payload = b"\xAA" * 60
    frame = raw_eth_frame(
        dst_mac=WRONG_UNICAST_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,
        payload=payload,
    )

    captured_pcap = artifact_dir / "captured-mac-drop.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        serial_log.assert_marker_observed(
            "RX:DROP mac", timeout=CAPTURE_WINDOW_SECONDS,
        )
        # MAC filter runs BEFORE ARP recognition, so an ARP
        # request to a wrong MAC (had we sent one) would not
        # reach the recognition block. Sending a non-ARP frame
        # here, but assert ARP:REQUEST absent as a belt-and-
        # braces check that the drop happened early.
        serial_log.assert_marker_absent(
            "ARP:REQUEST", window=0.0,
        )

    _no_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(),
        "ETH-006",
    )
