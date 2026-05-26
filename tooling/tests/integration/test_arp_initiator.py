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

CAPTURE_TIMEOUT_SECONDS = 12.0
# Long enough for three dispatcher iterations after boot —
# in TXAPI_PREBAKE builds the queue order is:
#   iter 1: 6.f gratuitous ARP (TPA=GUEST_IP, ip=022AA8C0)
#   iter 2: TXAPI test frame (Ethertype 0x88B5)
#   iter 3: prebake HOST_IP ARP probe (this test's target)
# Per-iter cost is bounded by POLL_BUDGET (~2–3 s when no
# RX is arriving) regardless of TX activity, so the iter-3
# worst case is ~6–9 s. 8 s gives ~1.5x headroom on the
# common case where iters 1–2 are RX-bound and finish in
# under 1 s each.
POST_MARKER_QUIESCE_SECONDS = 8.0


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
        # Target IP here is HOST_IP (192.168.42.1, LE u32
        # 0x012AA8C0) — the TXAPI_PREBAKE harness calls
        # arp_send_request(HOST_IP) as a probe. This is
        # intentionally distinct from D068 6.f's production
        # gratuitous ARP at boot, which targets GUEST_IP
        # (0x022AA8C0) for the SPA == TPA shape. Both fire
        # in this build (production 6.f on iter 0, prebake
        # probe right after), and the dispatcher submits
        # them on successive iterations.
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
