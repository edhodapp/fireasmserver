"""ARP request/reply behavior — `ARP-001`, `ARP-004`, `ARP-011`.

First integration test under the new harness per
`docs/l2/HARNESS.md` §7. Drives diagnosis of the FSA-4(A) ARP
responder failure observed 2026-05-22 — the previous "verified
by tracer-bullet" claim was hollow because the tracer-bullet
never sent an ARP request.

Coverage:
- ARP-001 well-formed request to GUEST_IP → expect a REPLY with
  the correct OPER (2), sender HW = GUEST_MAC, sender IP =
  GUEST_IP.
- ARP-004 well-formed request to a different IP on the same
  subnet → expect no reply (guest is not responsible for that IP).
- ARP-011 well-formed request to a non-local IP → expect no reply.

All three cases reuse the same Firecracker fixture pattern: boot
a clean guest, send the stimulus on tap0, capture frames coming
back, assert on the captured set and on the serial log markers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from l2_harness import frames
from l2_harness.capture import FrameSender, capturing
from l2_harness.firecracker import FirecrackerGuest
from l2_harness.frames import parse_arp_reply
from l2_harness.serial import SerialLog


CAPTURE_WINDOW_SECONDS = 1.5
"""How long to listen for the ARP reply after sending the request.

The guest's dispatcher loops at most 100 times (per FSA-4(A)
boot.S loop); each iteration consumes any pending RX frames
within POLL_BUDGET ticks. 1.5s is comfortably above the
guest's per-iteration latency.
"""


def test_arp_request_for_guest_ip_gets_reply(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
) -> None:
    """ARP-001: GUEST_IP request → guest replies with GUEST_MAC."""
    request = frames.arp_request(
        target_ip=frames.GUEST_DEFAULT_IP,
        sender_ip=frames.HOST_DEFAULT_IP,
        sender_mac=frames.HOST_DEFAULT_MAC,
    )

    captured_pcap = artifact_dir / "captured.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp and arp[6:2] = 2",  # filter: ARP reply (op=2)
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(request)

    parsed = [parse_arp_reply(bytes(p)) for p in cap.packets]
    replies = [r for r in parsed if r is not None]

    if not replies:
        raise AssertionError(
            "No ARP reply observed from the guest. "
            f"Serial log:\n{serial_log.text()}\n"
            f"Captured: {len(cap.packets)} frames (see {captured_pcap})"
        )
    # Exactly one reply expected. A second reply would indicate the
    # dispatcher loop double-processed the same request — a real
    # regression we want to catch.
    assert len(replies) == 1, (
        f"expected exactly 1 ARP reply, got {len(replies)}; "
        f"see {captured_pcap}"
    )
    reply = replies[0]
    # Ethernet-layer destination should be the requester's MAC.
    # parse_arp_reply returns only the ARP layer; pull the
    # Ethernet header out of the raw bytes for completeness.
    raw = bytes(cap.packets[0])
    eth_dst = ":".join(f"{b:02x}" for b in raw[0:6])
    assert eth_dst == frames.HOST_DEFAULT_MAC.lower(), (
        f"Ethernet dst={eth_dst!r}, expected "
        f"{frames.HOST_DEFAULT_MAC.lower()!r}"
    )
    assert reply.psrc == frames.GUEST_DEFAULT_IP, (
        f"reply psrc={reply.psrc!r}, expected "
        f"{frames.GUEST_DEFAULT_IP!r}"
    )
    assert reply.hwsrc.lower() == frames.GUEST_DEFAULT_MAC.lower(), (
        f"reply hwsrc={reply.hwsrc!r}, expected "
        f"{frames.GUEST_DEFAULT_MAC!r}"
    )
    assert reply.pdst == frames.HOST_DEFAULT_IP
    assert reply.hwdst.lower() == frames.HOST_DEFAULT_MAC.lower()

    # Marker chain: the guest should have logged both ARP:REQUEST
    # (matched recognition) and ARP:REPLY (submitted on TX).
    serial_log.assert_marker_observed("ARP:REQUEST", timeout=0.0)
    serial_log.assert_marker_observed("ARP:REPLY", timeout=0.0)


@pytest.mark.parametrize(
    "wrong_target,case_id",
    [
        ("192.168.42.99", "ARP-004"),   # same subnet, different IP
        ("10.0.0.1",      "ARP-011"),   # non-local
    ],
)
def test_arp_request_for_wrong_ip_gets_no_reply(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
    wrong_target: str,
    case_id: str,
) -> None:
    """ARP-004 / ARP-011: request for a non-guest IP → no reply."""
    request = frames.arp_request(
        target_ip=wrong_target,
        sender_ip=frames.HOST_DEFAULT_IP,
        sender_mac=frames.HOST_DEFAULT_MAC,
    )

    captured_pcap = artifact_dir / f"captured-{case_id}.pcap"
    with capturing(
        iface="tap0",
        bpf_filter="arp and arp[6:2] = 2",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ) as cap:
        frame_sender.send(request)
        # Marker-absence check INSIDE the capture context, with
        # a window matching the sniff timeout. This synchronizes
        # the absence assertion with the live capture so a
        # delayed reply (e.g., from a future race) lands inside
        # the same window we're verifying didn't fire markers.
        # Per the clean-Claude 2026-05-22 review: doing this
        # AFTER the with block would only check the log instant
        # the call runs and could miss a delayed emit.
        serial_log.assert_marker_absent(
            "ARP:REQUEST", window=CAPTURE_WINDOW_SECONDS,
        )

    parsed = [parse_arp_reply(bytes(p)) for p in cap.packets]
    replies = [r for r in parsed if r is not None]

    if replies:
        raise AssertionError(
            f"{case_id}: guest should not reply to ARP for "
            f"{wrong_target!r} but {len(replies)} reply observed. "
            f"Serial log:\n{serial_log.text()}\n"
            f"See {captured_pcap}"
        )
    # Snapshot check on ARP:REPLY for completeness; the absence
    # check above already covered the same window via the
    # window= parameter.
    serial_log.assert_marker_absent("ARP:REPLY", window=0.0)
