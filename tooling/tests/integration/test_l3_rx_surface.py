"""L3-callable RX surface — D068 working order item 5.

Verifies that the L2 dispatcher hands off accepted non-ARP
frames to the L3 stub (`arch/<arch>/l3/stub.S`'s
`l3_rx_dispatch` symbol). The stub emits
`L3:RX_FRAME len=<hex>\\n`; this test sends one well-formed
non-ARP Ethernet frame addressed to the guest's MAC and
asserts:

  1. L3:RX_FRAME marker appears in the serial log.
  2. The reported length matches the frame's wire-bytes
     (the dispatcher subtracts the 12-byte virtio_net_hdr
     before passing wire_len to l3_rx_dispatch).

When real L3 replaces stub.S, the marker emission may move
or change shape; that's deliberately a soft contract — the
HARD contract is the function call. This test verifies the
call fires through the stub's marker; future test updates
will tighten what they assert as the surface matures.

ARP-for-us frames are NOT passed to L3 (the dispatcher's
ARP responder consumes them); test_arp_request_reply.py
already covers the ARP path. ARP-not-for-us frames ARE
passed to L3 today (the stub is a no-op so they're
harmless); a future commit may refine that.
"""

from __future__ import annotations

from pathlib import Path

from l2_harness import frames
from l2_harness.capture import FrameSender, capturing
from l2_harness.firecracker import FirecrackerGuest
from l2_harness.frames import raw_eth_frame
from l2_harness.serial import SerialLog


# Local Experimental EtherType 1 (IEEE Std 802 §9.2.3.1).
# Same value used by the canary and TX API test — a
# convenient non-ARP, non-IPv4 ethertype for L2 negative-test
# stimuli.
TEST_ETHERTYPE = 0x88B5

# Payload bytes — value doesn't matter; the L3 stub doesn't
# look at the body. 50 bytes pushes the wire frame to
# 14 (Eth header) + 50 = 64 bytes, comfortably above the
# 60-byte minimum so we don't drag the size-bounds test
# into the L3 question.
TEST_PAYLOAD = b"L3RX" * 12 + b"AB"

# Wire length the L3 stub should report. The dispatcher
# passes wire_len = used_len - 12 (skipping virtio_net_hdr).
# The on-tap0 frame is `raw_eth_frame` output = 14 + 50 = 64.
# virtio prepends 12 bytes for virtio_net_hdr, used_len = 76;
# wire_len passed to L3 = 76 - 12 = 64.
EXPECTED_WIRE_LEN = 14 + len(TEST_PAYLOAD)

MARKER_TIMEOUT_SECONDS = 1.5
CAPTURE_WINDOW_SECONDS = MARKER_TIMEOUT_SECONDS + 0.3


def test_l3_dispatch_fires_on_non_arp_unicast_frame(
    firecracker_guest: FirecrackerGuest,    # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,
    tap_iface: str,    # pylint: disable=redefined-outer-name
) -> None:
    """A non-ARP unicast frame addressed to GUEST_MAC reaches L3.

    Sends one raw Ethernet frame with ethertype 0x88B5 to
    GUEST_DEFAULT_MAC, then asserts the L3 stub's marker
    appears in the serial log with the expected wire length.
    """
    frame = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=TEST_ETHERTYPE,
        payload=TEST_PAYLOAD,
    )
    captured_pcap = artifact_dir / "captured.pcap"
    # Capture context is opened mainly to bound where the
    # test interferes with tap0 — we don't actually need the
    # capture itself for the assertion (the marker IS the
    # signal). bpf is a no-op-ish "ether broadcast" so we
    # don't grab the guest's canary every iteration.
    with capturing(
        iface=tap_iface,
        bpf_filter="ether broadcast",
        timeout=CAPTURE_WINDOW_SECONDS,
        pcap_path=captured_pcap,
    ):
        frame_sender.send(frame)
        # Expected marker shape: "L3:RX_FRAME len=00000040\n"
        # where 0x40 = 64 = EXPECTED_WIRE_LEN.
        expected_marker = (
            f"L3:RX_FRAME len={EXPECTED_WIRE_LEN:08X}"
        )
        serial_log.assert_marker_observed(
            expected_marker,
            timeout=MARKER_TIMEOUT_SECONDS,
        )
