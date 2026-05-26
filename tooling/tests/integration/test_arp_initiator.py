"""ARP initiator — D068 working order item 6, phase 6.c.

The firecracker_txapi build (TXAPI_PREBAKE=1) now also
calls `arp_send_request(HOST_DEFAULT_IP)` after the TX
API enqueue at boot. This test asserts:

  1. The ARP:TX_REQUEST marker fires with the right IP.
  2. A wire ARP request appears on tap0 with the right
     dst/src MACs, OPER=request, SHA=GUEST_MAC,
     SPA=GUEST_DEFAULT_IP, TPA=HOST_DEFAULT_IP.

Reuses the firecracker_txapi build infrastructure (the
_ensure_txapi_built fixture in test_l2_tx_api builds the
binary on demand). Bundled into the TXAPI build because
the marginal cost of one more boot-time enqueue is zero
and avoids a second build flag + dir.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from scapy.layers.l2 import ARP

from l2_harness import frames
from l2_harness.capture import capturing
from l2_harness.firecracker import (
    FirecrackerConfig,
    launched_guest,
)
from l2_harness.serial import SerialLog


REPO_ROOT = Path(__file__).resolve().parents[3]
TXAPI_GUEST_ELF = (
    REPO_ROOT / "arch" / "x86_64" / "build"
    / "firecracker_txapi" / "guest.elf"
)

CAPTURE_TIMEOUT_SECONDS = 5.0
# Long enough for two dispatcher iterations after boot — iter 1
# drains the TXAPI test request, iter 2 drains the ARP request
# (~1 s per iter on x86 with POLL_BUDGET=100M when no RX
# arrives). 2.5 s gives 2x headroom over the ~1.05 s the second
# TX needs.
POST_MARKER_QUIESCE_SECONDS = 2.5


@pytest.fixture(scope="session")
def _ensure_txapi_built_for_arp() -> None:
    """Always build firecracker_txapi (sub-second no-op when up
    to date). Mirrors the same fixture in test_l2_tx_api."""
    subprocess.run(
        ["make", "-C",
         str(REPO_ROOT / "arch" / "x86_64"),
         "PLATFORM=firecracker", "TXAPI_PREBAKE=1"],
        check=True,
    )
    if not TXAPI_GUEST_ELF.exists():
        pytest.fail(
            f"TXAPI build claimed success but {TXAPI_GUEST_ELF} "
            "is missing"
        )


# pylint: disable=unused-argument,invalid-name
def test_arp_initiator_sends_request_for_host_ip(
    _ensure_txapi_built_for_arp: None,
    tap_iface: str,
    artifact_dir: Path,
) -> None:
    """The pre-baked arp_send_request emits the marker + a
    wire ARP request to HOST_DEFAULT_IP."""
    cfg = FirecrackerConfig(
        kernel_image_path=TXAPI_GUEST_ELF,
        artifact_dir=artifact_dir,
    )
    capture_pcap = artifact_dir / "arp_capture.pcap"
    with capturing(
        iface=tap_iface,
        bpf_filter="arp",
        timeout=CAPTURE_TIMEOUT_SECONDS,
        pcap_path=capture_pcap,
    ) as cap, launched_guest(cfg) as guest:
        serial = SerialLog(guest.serial_log_path)
        # ARP:TX_REQUEST fires synchronously at boot when
        # the request is enqueued. The dispatcher then needs
        # one or two iterations to actually submit it on the
        # wire (iter 1 drains the TXAPI-TEST queue entry, iter
        # 2 drains this one). Wait for the marker, then sleep
        # long enough for the second iteration to complete.
        #
        # Round-trip verification (host replies, guest's RX
        # recognition fires ARP:CACHE_INSERT) is NOT reliable
        # here: Linux's tap0 ARP reply is 42 wire bytes (no
        # padding to the 60-byte minimum), which the
        # dispatcher correctly drops per ETH-003. So this
        # test verifies the SEND path only via the captured
        # pcap; the inbound-reply path is covered by
        # test_arp_cache (which sends a padded reply via
        # scapy).
        serial.assert_marker_observed(
            "ARP:TX_REQUEST ip=012AA8C0",
            timeout=CAPTURE_TIMEOUT_SECONDS,
        )
        time.sleep(POST_MARKER_QUIESCE_SECONDS)

    # Parse captured frames for an ARP request matching our
    # expected fields. The pre-bake fires once at boot but the
    # guest's RX side may receive other ARP traffic (host's
    # gratuitous ARPs, etc); filter for the specific TPA.
    arp_requests = []
    for pkt in cap.packets:
        if not pkt.haslayer(ARP):
            continue
        arp = pkt[ARP]
        if arp.op != 1:                             # request only
            continue
        if arp.pdst != frames.HOST_DEFAULT_IP:      # our target
            continue
        arp_requests.append(arp)

    assert arp_requests, (
        f"Expected at least one ARP request with TPA="
        f"{frames.HOST_DEFAULT_IP}; got {len(cap.packets)} ARP "
        f"frames total. Pcap: {capture_pcap}"
    )

    # Field-level sanity on the first match.
    arp = arp_requests[0]
    assert arp.hwsrc.lower() == frames.GUEST_DEFAULT_MAC.lower(), (
        f"ARP SHA mismatch: got {arp.hwsrc!r}, expected "
        f"{frames.GUEST_DEFAULT_MAC!r}"
    )
    assert arp.psrc == frames.GUEST_DEFAULT_IP, (
        f"ARP SPA mismatch: got {arp.psrc!r}, expected "
        f"{frames.GUEST_DEFAULT_IP!r}"
    )
    assert arp.hwdst == "00:00:00:00:00:00", (
        f"ARP THA must be zeros for a request; got {arp.hwdst!r}"
    )
