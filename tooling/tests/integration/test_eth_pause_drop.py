"""Ethernet PAUSE frame drop — `ETH-018`.

Per `docs/l2/REQUIREMENTS.md` ETH-018 (IEEE 802.3x-1997 §31B.1):
receivers that do not implement the optional link-level flow-
control feature MUST silently discard PAUSE frames. PAUSE
frames are MAC Control frames identified by:

  - destination MAC = `01:80:C2:00:00:01` (the IEEE MAC Control
    multicast address — passes our MAC filter via the multicast
    bit, which is the point — they're meant to be heard by
    every station on the segment)
  - EtherType = `0x8808` (MAC Control / Slow Protocols)
  - first 2 payload bytes = `0x0001` (PAUSE opcode)
  - next 2 payload bytes = `pause_time` (quanta the sender
    should pause; we don't inspect this)

"Silently discard" in IEEE-spec language means no wire response
(no NACK, no congestion-control protocol bounce-back). It does
NOT preclude internal observability — the marker `RX:DROP pause`
is the test's hook.

Firecracker's tap-based virtio-net device doesn't generate
PAUSE frames, so this gate's real value is defensive spec
compliance: if a misbehaving or malicious peer ever injected
one (in a future bridged setup), the dispatcher would correctly
ignore it rather than emit RX:FRAME and treat it as ordinary
data.

D045 explicitly defers the inverse direction (acting on
received PAUSE frames to throttle our own TX) as a separate
flow-control module — see "PAUSE rate response stays deferred"
in the L2 design notes.
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

MAC_CONTROL_DST_MAC = "01:80:c2:00:00:01"
"""IEEE 802.3 MAC Control multicast destination address.

Reserved for PAUSE and related MAC Control frames. Passes
our MAC filter (multicast bit set) and our ETH-015 source
check is independent of destination.
"""

MAC_CONTROL_ETHERTYPE = 0x8808
"""EtherType for MAC Control frames (IEEE 802.3x §31A.2)."""

PAUSE_OPCODE = 0x0001
"""MAC Control opcode for PAUSE (IEEE 802.3x §31B.2)."""


def _no_arp_reply_assert(cap_packets: list[Packet],
                         captured_pcap: Path,
                         serial_text: str,
                         case_id: str) -> None:
    """Helper: assert no ARP reply landed on tap0."""
    parsed = [parse_arp_reply(bytes(p)) for p in cap_packets]
    replies = [r for r in parsed if r is not None]
    if replies:
        raise AssertionError(
            f"{case_id}: PAUSE frame should not elicit any reply "
            f"but {len(replies)} reply observed. "
            f"Serial log:\n{serial_text}\n"
            f"See {captured_pcap}"
        )


def test_pause_frame_dropped(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ETH-018: PAUSE frame → drop, no further processing, no wire response."""
    # PAUSE payload layout (IEEE 802.3x §31B.2):
    #   bytes 0-1: opcode (0x0001 BE for PAUSE)
    #   bytes 2-3: pause_time in quanta (BE; arbitrary here)
    #   bytes 4+ : reserved (zero-fill to min frame size)
    pause_payload = (
        PAUSE_OPCODE.to_bytes(2, "big")
        + (0x00FF).to_bytes(2, "big")           # pause_time = 255 quanta
        + b"\x00" * 42                          # zero-fill to 46
    )
    assert len(pause_payload) == 46
    frame = raw_eth_frame(
        dst_mac=MAC_CONTROL_DST_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=MAC_CONTROL_ETHERTYPE,
        payload=pause_payload,
    )
    assert len(frame) == 60, (
        f"PAUSE frame must be exactly 60 wire bytes, got {len(frame)}"
    )

    captured_pcap = artifact_dir / "captured-eth018.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp",
        timeout=POST_MARKER_QUIESCE_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(frame)
        serial_log.assert_marker_observed(
            "RX:DROP pause", timeout=CAPTURE_WINDOW_SECONDS,
        )
        # PAUSE drop runs BEFORE ARP recognition, so even if a
        # future bug somehow let a PAUSE frame through the
        # EtherType gate, ARP:REQUEST wouldn't fire (wrong
        # EtherType anyway). Belt-and-braces.
        serial_log.assert_marker_absent("ARP:REQUEST", window=0.0)
        # And the per-frame RX:FRAME emit must NOT happen — the
        # virtio used_len for this frame is 60 + 12 = 72 = 0x48.
        # If a future bug fell through to the RX:FRAME path,
        # this assertion would catch the regression.
        serial_log.assert_marker_absent("used_len=00000048", window=0.0)

    _no_arp_reply_assert(
        list(cap.packets), captured_pcap, serial_log.text(), "ETH-018",
    )
