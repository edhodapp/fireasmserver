"""ARP cache state machine — D068 working order item 6.e.

`arp_cache_tick` (called from the dispatcher loop once per
iteration) scans the cache for entries whose
`last_event_time` has aged past `ARP_AGING_CYCLES`. The
first transition implemented is REACHABLE → STALE; further
transitions (INCOMPLETE retry → FAILED, STALE → PROBE,
PROBE → REACHABLE/FAILED) follow in 6.e sub-commits.

The transition emits ARP:STATE_CHANGE ip=XXXXXXXX
old=00000002 new=00000003 so tests can observe the
state-machine evolution without poking guest memory.

This test uses the TXAPI_PREBAKE binary (which inserts a
REACHABLE entry at boot for 192.168.42.99 = 0x632AA8C0
during the 6.d resolve test scaffolding). The dispatcher
loop runs 100 iterations; each iteration's tick checks
ages. With ARP_AGING_CYCLES ≈ 100 ms equivalent on x86,
the entry should transition within the first second of
the dispatch loop.
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
def _ensure_txapi_built_for_state() -> None:
    """Always build firecracker_txapi (sub-second no-op when
    up to date). Same as the fixture in test_arp_resolve and
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
def test_reachable_entry_ages_to_stale(
    _ensure_txapi_built_for_state: None,
    artifact_dir: Path,
) -> None:
    """A REACHABLE entry transitions to STALE after the aging
    threshold + a dispatcher tick.

    Pre-bake sequence (in boot.S, TXAPI_PREBAKE block):
      ... → ARP:RESOLVE_OK ip=632AA8C0 (entry REACHABLE)
    Dispatch loop starts; arp_cache_tick fires per iteration.
    Once enough cycles elapse, tick observes the entry is
    aged past ARP_AGING_CYCLES (~100 ms @ 3 GHz on x86) and
    emits ARP:STATE_CHANGE.
    """
    cfg = FirecrackerConfig(
        kernel_image_path=TXAPI_GUEST_ELF,
        artifact_dir=artifact_dir,
    )
    with launched_guest(cfg) as guest:
        serial = SerialLog(guest.serial_log_path)
        # Wait for the REACHABLE entry to be in place.
        serial.assert_marker_observed(
            "ARP:RESOLVE_OK ip=632AA8C0",
            timeout=MARKER_TIMEOUT_SECONDS,
        )
        # Then wait for the tick to age it.
        serial.assert_marker_observed(
            "ARP:STATE_CHANGE ip=632AA8C0 old=00000002 new=00000003",
            timeout=MARKER_TIMEOUT_SECONDS,
        )
        time.sleep(0.1)
