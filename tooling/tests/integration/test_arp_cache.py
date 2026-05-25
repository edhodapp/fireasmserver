"""ARP cache + initiator — D068 working order item 6.

Phase 6.a (this test): an inbound ARP REPLY targeting the
guest's IP causes the dispatcher to call arp_cache_insert,
which the test verifies via the ARP:CACHE_INSERT serial
marker.

Future phases extend coverage:
  6.b adds the recognition hook (covered here).
  6.c adds the outbound ARP initiator (separate test).
  6.d adds the arp_resolve API (separate test).
  6.e adds the timer + state machine.
  6.f adds gratuitous ARP at boot.
"""

from __future__ import annotations

from pathlib import Path

from scapy.layers.l2 import ARP, Ether

from l2_harness import frames
from l2_harness.capture import FrameSender, capturing
from l2_harness.firecracker import FirecrackerGuest
from l2_harness.serial import SerialLog


MARKER_TIMEOUT_SECONDS = 1.5
CAPTURE_WINDOW_SECONDS = MARKER_TIMEOUT_SECONDS + 0.3


def test_arp_reply_inserts_into_cache(
    firecracker_guest: FirecrackerGuest,    # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
    tap_iface: str,    # pylint: disable=redefined-outer-name
) -> None:
    """An inbound ARP reply for our IP triggers arp_cache_insert.

    Sends a unicast ARP reply with SPA=HOST_DEFAULT_IP and
    TPA=GUEST_DEFAULT_IP; asserts the ARP:CACHE_INSERT
    marker fires with the sender IP in hex.
    """
    # Scapy builds the wire bytes; pad to 60 to clear the
    # minimum-frame check. The full ARP-over-Ethernet wire is
    # 14 + 28 = 42, so we need 18 bytes of explicit pad.
    eth = Ether(
        dst=frames.GUEST_DEFAULT_MAC,
        src=frames.HOST_DEFAULT_MAC,
        type=0x0806,
    )
    arp = ARP(
        op=2,                                            # reply
        hwsrc=frames.HOST_DEFAULT_MAC,
        psrc=frames.HOST_DEFAULT_IP,
        hwdst=frames.GUEST_DEFAULT_MAC,
        pdst=frames.GUEST_DEFAULT_IP,
    )
    raw = bytes(eth / arp)
    if len(raw) < 60:
        raw += b"\x00" * (60 - len(raw))

    captured_pcap = artifact_dir / "captured.pcap"
    with capturing(
        iface=tap_iface,
        bpf_filter="arp",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ):
        frame_sender.send(raw)
        # SPA = 192.168.42.1 wire bytes = c0 a8 2a 01.
        # The dispatcher passes the SPA as a 4-byte BE wire
        # value to emit_hex32, which reads it as a u32 in
        # memory layout. The bytes [c0 a8 2a 01] read as a
        # LE u32 in memory = 0x012AA8C0; that's what emit_hex32
        # prints (most-significant nibble first).
        expected_marker = "ARP:CACHE_INSERT ip=012AA8C0"
        serial_log.assert_marker_observed(
            expected_marker,
            timeout=MARKER_TIMEOUT_SECONDS,
        )
