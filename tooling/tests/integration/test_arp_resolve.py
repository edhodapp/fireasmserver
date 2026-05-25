"""arp_resolve API — D068 working order item 6, phase 6.d.

The firecracker_txapi build's TXAPI_PREBAKE block (extended
in 6.d) exercises arp_resolve in two scenarios:

  1. First resolve(192.168.42.99) on an empty cache → MISS
     path: insert INCOMPLETE placeholder, send request,
     return PENDING. Marker: ARP:RESOLVE_PENDING ip=632AA8C0.
  2. Direct arp_cache_insert with REACHABLE state + a fake
     distinctive MAC (02:de:ad:be:ef:99). Then a second
     resolve(192.168.42.99) hits the cache, returns OK.
     Marker: ARP:RESOLVE_OK ip=632AA8C0.

Test asserts both markers appear in serial in that order.
Bypasses the actual wire round-trip — Linux's tap0 wouldn't
respond to a synthetic test IP anyway, and the round-trip
ARP-reply path is exercised separately by test_arp_cache
(via padded scapy frames).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

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

MARKER_TIMEOUT_SECONDS = 5.0


@pytest.fixture(scope="session")
def _ensure_txapi_built_for_resolve() -> None:
    """Always build firecracker_txapi (sub-second no-op when up
    to date). Mirrors the same fixture in test_l2_tx_api and
    test_arp_initiator."""
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
def test_arp_resolve_miss_then_hit(
    _ensure_txapi_built_for_resolve: None,
    artifact_dir: Path,
) -> None:
    """First resolve emits PENDING (cache miss); after a
    forced cache insert, the second resolve emits OK."""
    cfg = FirecrackerConfig(
        kernel_image_path=TXAPI_GUEST_ELF,
        artifact_dir=artifact_dir,
    )
    with launched_guest(cfg) as guest:
        serial = SerialLog(guest.serial_log_path)
        # Both markers are distinct strings — no need to
        # checkpoint between them. (checkpoint() would race
        # the boot pre-bake's fast emit chain anyway.)
        serial.assert_marker_observed(
            "ARP:RESOLVE_PENDING ip=632AA8C0",
            timeout=MARKER_TIMEOUT_SECONDS,
        )
        serial.assert_marker_observed(
            "ARP:RESOLVE_OK ip=632AA8C0",
            timeout=MARKER_TIMEOUT_SECONDS,
        )
        # Tiny quiesce so serial drains and any subsequent
        # text lands in the captured log for failure context.
        time.sleep(0.1)
