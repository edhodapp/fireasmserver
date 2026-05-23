"""Ethernet MAC filter — `ETH-006`, `ETH-007`, `MAC-001`.

Per `docs/l2/REQUIREMENTS.md` and `TEST_PLAN.md` §1.4: the L2
receiver must accept frames whose Ethernet destination is the
guest MAC (`02:00:00:00:00:01`), the broadcast address
(`ff:ff:ff:ff:ff:ff`), or any multicast address (the bit-0 of
the first byte set), and drop unicast frames addressed to any
other MAC. Without this filter the guest leaks higher-layer
processing into frames that physically reached its tap but
were destined for someone else — a real attack surface once
multiple guests share a bridge.

Covered here:
  - MAC-001: unicast frame to GUEST_MAC accepted.
  - ETH-006: wrong-MAC unicast frame dropped.
  - ETH-007: multicast destination accepted.

NOT covered here (broader MAC filter rows that need their own
files or shapes):
  - ETH-008: broadcast accept (currently implicit via the ARP
    tests which broadcast their request; explicit guard
    queued).
  - MAC-002..005: specific multicast group filtering, joined-
    group tracking, etc. — out of scope until we implement
    IGMP / MLD subscription state.
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
POST_MARKER_QUIESCE_SECONDS = 0.3

MULTICAST_DST_MAC = "33:33:00:00:00:01"
"""IPv6 all-nodes link-local multicast MAC.

Byte 0 = 0x33 = 0b00110011 — bit 0 set → multicast. Real
networks regularly deliver this address (it's the IPv6 NDP
all-nodes group); a guest that wants to support any IPv6
must accept it. Even without IPv6 in scope today, the L2
filter's multicast-accept rule is the same.
"""

WRONG_UNICAST_MAC = "02:00:00:00:00:99"
"""A locally-administered MAC that is NOT the guest MAC.

Locally-administered (bit 1 of byte 0 = 1) AND unicast
(bit 0 of byte 0 = 0). The host-side kernel won't ARP for
it, and the guest must drop it as not-for-us.
"""


def _no_arp_reply_assert(cap_packets: list[Packet],
                         captured_pcap: Path,
                         serial_text: str,
                         case_id: str) -> None:
    """Helper: assert no ARP reply landed on tap0.

    The capturing() context uses `bpf_filter="arp"` so only ARP
    frames reach `cap_packets` in the first place — non-ARP
    guest TX (today: the canary tx_test_pkt) is invisible to
    this assertion. That's intentional: this helper exists to
    catch the "guest replied to our stimulus" case, which only
    happens via the ARP responder. Broader "no unexpected
    guest TX" coverage would need a different filter + parser
    and is tracked separately.
    """
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
    # Capture window outlasts the serial wait + the absent-
    # window so a late guest TX (the case _no_arp_reply_assert
    # below is meant to catch) can't slip through after the
    # sniffer ends but before the test exits the with block.
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS + 0.5,
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

    _no_arp_reply_assert(
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
    # See test_unicast_to_guest_mac_accepted for the capture-
    # timeout rationale — sniffer must outlast the serial wait.
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS + 0.5,
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

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(),
        "ETH-006",
    )


def test_multicast_destination_accepted(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-007: multicast destination frame → accept, no drop.

    The iter-1 kernel NDP frame on the laptop hits this same
    code path incidentally, but we don't have an explicit test
    that controls the multicast address bits and asserts on
    acceptance. This is the regression guard against a future
    bug that flips the multicast-bit check (the `tbnz w14, #0`
    on aarch64 / `test r10d, 1` on x86_64) to its opposite
    sense — every multicast / broadcast frame would then drop.

    Asserts on the frame-specific `used_len` marker so we
    distinguish OUR test frame from any iter-1 kernel traffic
    (NDP at used_len=0x7A) that might also fire RX:FRAME.
    """
    payload = b"\x66" * 46
    frame = raw_eth_frame(
        dst_mac=MULTICAST_DST_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,
        payload=payload,
    )
    assert len(frame) == 60, (
        f"ETH-007 test frame must be 60 wire bytes, got {len(frame)}"
    )
    expected_used_len = f"used_len={(len(frame) + 12):08X}"

    captured_pcap = artifact_dir / "captured-eth007.pcap"
    # Capture timeout MUST outlast the serial wait window — if
    # capture ends before serial wait, a late reply (e.g. one
    # that arrives 500 ms into a 1.5 s serial wait) would never
    # land in cap.packets and _no_arp_reply_assert would silently
    # pass. Set capture to the serial wait + the quiescence
    # window so the capture window is a strict superset of the
    # interval during which a guest reply could land.
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS + POST_MARKER_QUIESCE_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        serial_log.assert_marker_observed(
            expected_used_len, timeout=CAPTURE_WINDOW_SECONDS,
        )
        # No drop for this frame — the MAC filter accepted it
        # via the multicast bit branch, and the size + src MAC
        # gates are satisfied too. A regression in any of those
        # would surface here.
        serial_log.assert_marker_absent(
            "RX:DROP", window=POST_MARKER_QUIESCE_SECONDS,
        )

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(),
        "ETH-007",
    )
