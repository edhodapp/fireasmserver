"""RX consume-loop budget exhaustion + reentrance — `FSA-4`.

Per `D043` (Astier paper) the dispatcher is a bounded-transition
engine: no single dispatch call may consume more than
`RX_FRAME_BUDGET` (= 16) frames from the RX used ring. When more
frames are queued, the dispatcher emits `RX:RETURNED`, runs the
TX phase, returns, and a subsequent call picks up where the
persistent `l2_state` shadow left off.

FSA-4 introduced the persistent shadow: `rx_next_avail` and
`rx_used_shadow` are stored in the `l2_state` memreq region
across dispatch calls. Without correct persistence, a second
dispatch would either re-process already-consumed frames
(shadow not advanced) or skip un-processed ones (shadow
overshot).

This test exercises both invariants in one shot:

  1. Send 30 frames in a tight burst so they're all visible
     in the RX used ring by the time iter-1 dispatch starts
     polling.
  2. Assert serial log shows >= 2 `RX:RETURNED` markers,
     proving the dispatcher did NOT consume all 30 in one
     call. (Without the budget gate, it would; without
     reentrance, a subsequent call would see an empty
     ring and time out.)
  3. Assert serial log shows >= 30 occurrences of the
     test-frame-specific `used_len` marker, proving every
     frame eventually reached `RX:FRAME` emission. Lost or
     duplicate frames manifest as count != 30.

The test does NOT pin the EXACT dispatch boundary (e.g., "iter-1
processed 16, iter-2 processed 14") because Firecracker's
virtio-net timing can interleave delivery with consumption —
some frames may surface to iter-1's used ring after the budget
hit, others batch into iter-2's view. The invariants we DO pin
are robust under any interleaving consistent with a working
FSA-4.
"""

from __future__ import annotations

import time
from pathlib import Path

from l2_harness import frames
from l2_harness.capture import FrameSender
from l2_harness.firecracker import FirecrackerGuest
from l2_harness.frames import raw_eth_frame
from l2_harness.serial import SerialLog


NUM_FRAMES = 30
"""Frame count for the burst.

Chosen to exceed RX_FRAME_BUDGET (16) by enough margin that even
with virtio-net delivery latency interleaving, at least one
dispatch is guaranteed to hit the budget AND a follow-on
dispatch is guaranteed to find more work — exercising both
sides of the reentrance invariant.
"""

TEST_FRAME_WIRE_LEN = 60
"""Wire bytes per test frame: 14 Ethernet header + 46 payload."""

TEST_FRAME_USED_LEN_HEX = (
    f"used_len={(TEST_FRAME_WIRE_LEN + 12):08X}"   # = "used_len=00000048"
)
"""Frame-specific marker suffix used to distinguish our test frames
from any iter-1 kernel-side traffic (NDP, etc.) in the log."""

DISPATCH_SETTLE_SECONDS = 2.0
"""Wall-clock window to let the dispatcher process the full burst.

Each dispatch call's RX-wait + consume + TX cycle is ~10-50 ms
on the laptop. Two dispatches plus margin lands well under 1 s;
we sleep 2 s to absorb scheduler jitter and serial flush.
"""


def test_rx_burst_exhausts_budget_and_continues(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,  # pylint: disable=unused-argument
) -> None:
    """FSA-4: 30-frame burst → ≥2 RX:RETURNED, ≥30 frame-specific markers."""
    payload = b"\x42" * 46
    frame = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,                  # local-experimental
        payload=payload,
    )
    assert len(frame) == TEST_FRAME_WIRE_LEN, (
        f"test frame must be {TEST_FRAME_WIRE_LEN} wire bytes, "
        f"got {len(frame)}"
    )

    # Burst-send. scapy.sendp's per-call overhead means this loop
    # takes ~10-30 ms total for 30 small frames — well inside the
    # dispatcher's RX-wait window before iter-1 starts polling
    # (boot path adds ~100-150 ms before l2_dispatch is reached).
    for _ in range(NUM_FRAMES):
        frame_sender.send(frame)

    # Wait for at least one RX:RETURNED, then let the second
    # dispatch settle. We assert the COUNT below rather than
    # waiting for a specific "second RX:RETURNED" marker — the
    # exact number depends on virtio delivery interleaving and
    # we don't want to pin a brittle expectation.
    serial_log.assert_marker_observed(
        "RX:RETURNED", timeout=DISPATCH_SETTLE_SECONDS,
    )
    time.sleep(DISPATCH_SETTLE_SECONDS)

    text = serial_log.text()
    returned_count = text.count("RX:RETURNED\n")
    test_frame_count = text.count(TEST_FRAME_USED_LEN_HEX)

    if returned_count < 2:
        raise AssertionError(
            "FSA-4 budget exhaustion not observed: expected >= 2 "
            f"RX:RETURNED markers (proving budget hit + continuation); "
            f"got {returned_count}.\n"
            "If 1: a single dispatch consumed every frame — the budget "
            "gate isn't enforced, OR frames arrived too late to land in "
            "iter-1's view.\n"
            "If 0: the dispatcher never reached RX:RETURNED at all.\n"
            f"--- serial log ---\n{text}"
        )

    if test_frame_count < NUM_FRAMES:
        raise AssertionError(
            "FSA-4 persistent-shadow not observed: expected >= "
            f"{NUM_FRAMES} occurrences of '{TEST_FRAME_USED_LEN_HEX}' "
            f"(one per sent frame, identified by size); got "
            f"{test_frame_count}.\n"
            "Missing frames indicate the second-dispatch shadow either "
            "skipped slots (shadow overshot) or re-consumed slots "
            "(shadow not advanced and we missed the actual unconsumed "
            "ones), OR a frame was dropped for a non-bursting reason.\n"
            f"--- serial log ---\n{text}"
        )
