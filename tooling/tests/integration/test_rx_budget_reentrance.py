"""RX consume-loop budget exhaustion + reentrance — `FSA-4`.

Per `D043` (Astier paper) the dispatcher is a bounded-transition
engine: no single dispatch call may consume more than
`RX_FRAME_BUDGET` (= 16) frames from the RX used ring. When more
frames are queued, the dispatcher emits `RX:RETURNED`, runs the
TX phase, returns, and a subsequent call picks up where the
persistent `l2_state` shadow left off.

FSA-4 introduced the persistent shadow: `rx_next_avail` and
`rx_used_shadow` are stored in the `l2_state` memreq region
across dispatch calls. Three failure modes the test must catch:

  - **lost frames**: shadow advances past unconsumed slots
    (overshoot) → some frames never reach `RX:FRAME` emit.
  - **duplicate frames**: shadow doesn't advance (no
    writeback, or writeback to wrong field) → next dispatch
    re-reads already-consumed slots, emits `RX:FRAME` twice
    for the same descriptor.
  - **no budget gate**: dispatcher consumes >16 frames in one
    call → only one `RX:RETURNED` after the burst, instead of
    the >= 2 that proper budget enforcement produces.

The test catches each by comparing a baseline snapshot (taken
AFTER the dispatcher's iter-1 outcome settles) against the
post-burst state, using EXACT equality on the test-frame-
specific marker count and a delta floor on `RX:RETURNED`.
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

Chosen to exceed RX_FRAME_BUDGET (16) by enough margin that
even with virtio-net delivery latency interleaving, at least
one dispatch is guaranteed to hit the budget AND a follow-on
dispatch is guaranteed to find more work — exercising both
sides of the reentrance invariant.
"""

TEST_FRAME_WIRE_LEN = 60
"""Wire bytes per test frame: 14 Ethernet header + 46 payload.

60 bytes maps to virtio used_len = 72 (0x48) — distinct from
the iter-1 kernel NDP frame at 0x7A, so our frame-specific
marker assertion isn't contaminated by unrelated kernel
traffic.
"""

TEST_FRAME_USED_LEN_HEX = (
    f"used_len={(TEST_FRAME_WIRE_LEN + 12):08X}"   # = "used_len=00000048"
)

BOOT_READY_TIMEOUT_SECONDS = 3.0
"""How long to wait for the boot marker (RX:POPULATED)."""

ITER1_SETTLE_TIMEOUT_SECONDS = 2.0
"""How long to wait for iter-1's dispatch to settle.

iter-1 either consumes the kernel-NDP frame (→ RX:RETURNED) or
times out (→ RX:TIMEOUT). Either outcome marks the dispatcher
as actively polling, which is when we baseline.
"""

BURST_SETTLE_TIMEOUT_SECONDS = 5.0
"""Max wait for the post-burst delta to reach the threshold.

Each post-burst dispatch (consume + TX) takes ~10-50 ms; 5 s
is ample for two dispatches and the L2 marker flush, even on a
loaded host. We poll rather than fixed-sleep so fast machines
exit in <1 s.
"""

POLL_INTERVAL_SECONDS = 0.05


def _count(text: str, marker: str) -> int:
    """Substring count without trailing-newline brittleness.

    The dispatcher emits each marker with a trailing `\\n`, but
    binding the count to that literal is fragile across the
    QEMU / Firecracker / UART stack. Substring-match the marker
    body and trust the per-emit `\\n` to keep them
    line-separated.
    """
    return text.count(marker)


def _wait_for_iter1_settled(serial_log: SerialLog) -> None:
    """Block until iter-1 dispatch produces an outcome marker.

    First wait for RX:POPULATED (boot complete). Then poll for
    either RX:RETURNED (frame received + processed) or RX:TIMEOUT
    (no frame within POLL_BUDGET). Either is acceptable —
    we just need the dispatcher to be in a known state before
    snapshotting the baseline.
    """
    if not serial_log.wait_for("RX:POPULATED",
                               timeout=BOOT_READY_TIMEOUT_SECONDS):
        raise AssertionError(
            "guest did not reach RX:POPULATED within "
            f"{BOOT_READY_TIMEOUT_SECONDS}s — boot stalled before "
            "the dispatcher loop started\n"
            f"--- serial log ---\n{serial_log.text()}"
        )
    deadline = time.monotonic() + ITER1_SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        text = serial_log.text()
        if "RX:RETURNED" in text or "RX:TIMEOUT" in text:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(
        "iter-1 dispatch did not settle (no RX:RETURNED nor "
        f"RX:TIMEOUT within {ITER1_SETTLE_TIMEOUT_SECONDS}s after "
        "RX:POPULATED)\n"
        f"--- serial log ---\n{serial_log.text()}"
    )


def _wait_for_delta(serial_log: SerialLog,
                    marker: str,
                    baseline: int,
                    delta: int,
                    timeout: float) -> None:
    """Block until `_count(text, marker)` - baseline >= delta.

    Adaptive polling — returns as soon as the threshold is met
    rather than sleeping a fixed window. The post-burst RX +
    TX cycles complete in well under a second on a healthy
    machine; on a loaded one this may take longer. Caller
    handles the timeout case via assertion on the final count.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _count(serial_log.text(), marker) - baseline >= delta:
            return
        time.sleep(POLL_INTERVAL_SECONDS)


def test_rx_burst_exhausts_budget_and_continues(
    firecracker_guest: FirecrackerGuest,  # pylint: disable=unused-argument
    frame_sender: FrameSender,
    serial_log: SerialLog,
    artifact_dir: Path,  # pylint: disable=unused-argument
) -> None:
    """FSA-4: 30-frame burst → exact frame count + ≥2 RX:RETURNED."""
    payload = b"\x42" * 46
    frame = raw_eth_frame(
        dst_mac=frames.GUEST_DEFAULT_MAC,
        src_mac=frames.HOST_DEFAULT_MAC,
        ethertype=0x88B5,
        payload=payload,
    )
    assert len(frame) == TEST_FRAME_WIRE_LEN, (
        f"test frame must be {TEST_FRAME_WIRE_LEN} wire bytes, "
        f"got {len(frame)}"
    )

    # Boot-readiness gate: wait until iter-1 dispatch outcome
    # is observable. Sending frames before the dispatcher is
    # polling can cause Firecracker to buffer them in the
    # host-side tap queue and deliver them in an unpredictable
    # cadence — losing the burst-density property the test
    # depends on. Per Gemini pre-push review.
    _wait_for_iter1_settled(serial_log)

    # Baseline snapshot AFTER iter-1 settles. Subsequent counts
    # are measured as deltas from these — the absolute counts
    # include iter-1's RX:RETURNED (if NDP arrived), which would
    # mask a single-dispatch regression. Per Codex pre-push
    # review.
    baseline_text = serial_log.text()
    baseline_returned = _count(baseline_text, "RX:RETURNED")
    baseline_frame_markers = _count(baseline_text, TEST_FRAME_USED_LEN_HEX)
    # NOTE: baseline_frame_markers is typically 0 — NDP is
    # used_len=0x7A, not 0x48 — but the delta form below is
    # correct even if a future kernel happens to send a
    # 60-byte iter-1 frame.

    # Burst-send. List-form sendp() amortises AF_PACKET socket
    # overhead so the 30 frames hit the virtio backend in tight
    # succession — maximising the chance ≥16 land in any one
    # dispatch's used-ring view. Per Gemini pre-push review.
    frame_sender.send_burst([frame] * NUM_FRAMES)

    # Adaptive poll: wait until at least 2 RX:RETURNED markers
    # have arrived past baseline (proves budget hit +
    # continuation), then a short grace for the second TX
    # chain to finish flushing. Per Gemini pre-push review
    # (replaces the prior fixed time.sleep).
    _wait_for_delta(
        serial_log, "RX:RETURNED",
        baseline=baseline_returned, delta=2,
        timeout=BURST_SETTLE_TIMEOUT_SECONDS,
    )
    # Grace window for the trailing TX:RECLAIMED + any straggler
    # RX:FRAME emit to flush — the prior assertion only waits
    # for the second RX:RETURNED, but the frame-count assertion
    # below needs every RX:FRAME line in the log too.
    time.sleep(0.3)

    text = serial_log.text()
    delta_returned = _count(text, "RX:RETURNED") - baseline_returned
    delta_frames = (
        _count(text, TEST_FRAME_USED_LEN_HEX) - baseline_frame_markers
    )

    if delta_returned < 2:
        raise AssertionError(
            "FSA-4 budget exhaustion not observed: expected >= 2 "
            "post-baseline RX:RETURNED markers (proving budget hit + "
            f"continuation); got delta={delta_returned} "
            f"(baseline={baseline_returned}, "
            f"final={baseline_returned + delta_returned}).\n"
            "If 1: a single dispatch consumed every burst frame — "
            "the budget gate isn't enforced, OR frames arrived too "
            "late / too slowly to land in any one dispatch's view.\n"
            "If 0: the dispatcher never reached RX:RETURNED after "
            "the burst — possible hang or device-side drop.\n"
            f"--- serial log ---\n{text}"
        )

    # EXACT equality: catches BOTH FSA-4 failure modes.
    #   delta < NUM_FRAMES: shadow overshot (skipped unconsumed
    #     slots) OR host-side frame loss. Either way the
    #     dispatcher "missed" some.
    #   delta > NUM_FRAMES: shadow undershot (didn't advance
    #     past consumed slots, so next dispatch re-processed
    #     them). Per Codex review — the prior `< NUM_FRAMES`
    #     check missed this whole class.
    if delta_frames != NUM_FRAMES:
        raise AssertionError(
            "FSA-4 frame-count mismatch: expected exactly "
            f"{NUM_FRAMES} '{TEST_FRAME_USED_LEN_HEX}' markers past "
            f"baseline; got delta={delta_frames} "
            f"(baseline={baseline_frame_markers}, "
            f"final={baseline_frame_markers + delta_frames}).\n"
            f"  delta < {NUM_FRAMES}: shadow overshot (skipped "
            "slots) OR host-side frame loss.\n"
            f"  delta > {NUM_FRAMES}: shadow undershot — "
            "dispatcher re-processed already-consumed descriptors "
            "(persistent shadow not advancing or not writing back).\n"
            f"--- serial log ---\n{text}"
        )
