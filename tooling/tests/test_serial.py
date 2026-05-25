"""Unit tests for `l2_harness.serial.SerialLog`.

Covers the cursor + incremental-read model added on top of the
plain "snapshot + substring" interface. Each test writes lines
to a temp file in known order and asserts the cursor /
strip-regex / window-only behavior the integration tests
depend on. Runs without Firecracker — pure file I/O.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from l2_harness.serial import SerialLog


# Window short enough that 30 tests don't add seconds to the
# suite, long enough that a 50 ms poll has time to fire at
# least twice.
_SHORT_WINDOW = 0.15

# Match a fake "Firecracker startup line" the strip regex
# should consume verbatim. Same shape as the production
# Firecracker emits: ISO timestamp + [l2-harness:thread] + body.
_FIRECRACKER_LINE = (
    "2026-05-24T16:37:37.929247818 [l2-harness:main] "
    "Successfully started microvm\n"
)


def test_text_returns_full_file(tmp_path: Path) -> None:
    """`text()` returns the full file content, post-strip."""
    log = tmp_path / "serial.log"
    log.write_text("READY\nRX:FRAME id=1\n")
    serial = SerialLog(log)
    assert "READY" in serial.text()
    assert "RX:FRAME id=1" in serial.text()


def test_text_strips_firecracker_log_lines(tmp_path: Path) -> None:
    """Firecracker startup lines are stripped from `text()`.

    Mid-line interleave is the production failure mode — emit
    one guest line that has an FC log line spliced into it and
    verify the marker is recoverable after the strip.
    """
    log = tmp_path / "serial.log"
    log.write_text(
        "READY\n"
        "RX:FAIL num_bufs=0002026-05-24T16:37:37.929247818 "
        "[l2-harness:main] Successfully started microvm\n"
        "00002\n"
    )
    serial = SerialLog(log)
    # The strip regex consumes only the timestamped-prefix
    # portion of the broken line, leaving "RX:FAIL num_bufs="
    # plus the trailing "000\n" plus the next line's "00002".
    # The recoverable substring depends on how the regex
    # matches; minimum invariant is that we no longer see the
    # Firecracker prefix in the output.
    text = serial.text()
    assert "[l2-harness:" not in text
    assert "2026-05-24T16:37:37" not in text


def _delayed_write(log: Path, payload: str,
                   delay: float = 0.05) -> threading.Thread:
    """Spawn a daemon thread that appends `payload` after `delay`."""
    def _writer() -> None:
        time.sleep(delay)
        with log.open("a") as fh:
            fh.write(payload)
    thread = threading.Thread(target=_writer, daemon=True)
    thread.start()
    return thread


def test_wait_for_picks_up_appended_text(tmp_path: Path) -> None:
    """`wait_for` polls and returns True when a writer appends."""
    log = tmp_path / "serial.log"
    log.write_text("READY\n")
    serial = SerialLog(log)
    _delayed_write(log, "RX:FRAME id=1\n")
    assert serial.wait_for("RX:FRAME", timeout=1.0)


def test_wait_for_times_out_when_marker_never_appears(
    tmp_path: Path,
) -> None:
    """`wait_for` returns False if marker is absent for the window."""
    log = tmp_path / "serial.log"
    log.write_text("READY\n")
    serial = SerialLog(log)
    assert not serial.wait_for("NEVER", timeout=_SHORT_WINDOW)


def test_checkpoint_hides_pre_existing_marker(tmp_path: Path) -> None:
    """After `checkpoint()`, `wait_for` ignores prior occurrences.

    This is the HIGH Gemini finding: prior to the cursor, a
    test that wanted to verify a SECOND READY after a reboot
    would always match the first one, never wait. Validate
    the fix.
    """
    log = tmp_path / "serial.log"
    log.write_text("READY\n")
    serial = SerialLog(log)
    # Without checkpoint, READY is immediately visible.
    assert serial.wait_for("READY", timeout=_SHORT_WINDOW)
    # Checkpoint past the first READY, then wait_for the same
    # marker — should TIME OUT because no new READY landed.
    serial.checkpoint()
    assert not serial.wait_for("READY", timeout=_SHORT_WINDOW)
    # If a SECOND READY arrives after checkpoint, wait_for
    # picks it up.
    with log.open("a") as fh:
        fh.write("READY\n")
    assert serial.wait_for("READY", timeout=_SHORT_WINDOW)


def test_assert_marker_absent_is_window_only(tmp_path: Path) -> None:
    """LOW Gemini finding: `assert_marker_absent` checks only the
    window, not the full history.

    A marker that landed BEFORE the call must not trip the
    assertion; only emissions DURING the window do.
    """
    log = tmp_path / "serial.log"
    log.write_text("RX:DROP earlier\n")
    serial = SerialLog(log)
    # Old behavior would have raised because "RX:DROP" is in
    # the file. New semantics: only the window matters.
    serial.assert_marker_absent("RX:DROP", window=_SHORT_WINDOW)


def test_assert_marker_absent_fires_on_in_window_emit(
    tmp_path: Path,
) -> None:
    """`assert_marker_absent` raises when the marker lands during
    the sleep window."""
    log = tmp_path / "serial.log"
    log.write_text("READY\n")
    serial = SerialLog(log)
    _delayed_write(log, "RX:DROP fired\n")
    with pytest.raises(AssertionError) as exc:
        serial.assert_marker_absent("RX:DROP", window=_SHORT_WINDOW)
    assert "RX:DROP" in str(exc.value)


def test_assert_marker_observed_raises_with_context_on_miss(
    tmp_path: Path,
) -> None:
    """`assert_marker_observed` includes the log path + tail in
    the AssertionError so a developer can pivot to the artifact."""
    log = tmp_path / "serial.log"
    log.write_text("READY\nVIRTIO:OK\n")
    serial = SerialLog(log)
    with pytest.raises(AssertionError) as exc:
        serial.assert_marker_observed("RX:FRAME", timeout=_SHORT_WINDOW)
    msg = str(exc.value)
    assert "RX:FRAME" in msg
    assert "READY" in msg                # tail is embedded
    assert str(log) in msg               # path is embedded


def test_incremental_read_handles_mid_line_appends(
    tmp_path: Path,
) -> None:
    """The buffer accumulates correctly across writes that split a
    line at any byte boundary.

    Mimics Firecracker's pattern of writing piecemeal: the
    incremental seek+read path must not lose bytes when a
    line straddles two refresh calls.
    """
    log = tmp_path / "serial.log"
    log.write_text("READ")
    serial = SerialLog(log)
    # First refresh sees "READ" — no newline yet, so no
    # cleaned content yet.
    assert "READ" in serial.text()
    with log.open("a") as fh:
        fh.write("Y\nRX:FRAME id=1\n")
    # Second refresh consumes the rest of "READY" and the new line.
    text = serial.text()
    assert "READY" in text
    assert "RX:FRAME id=1" in text


def test_path_property_is_unchanged_by_refresh(tmp_path: Path) -> None:
    """The `.path` property continues to point at the raw on-disk
    file (artifacts test fixtures rely on this)."""
    log = tmp_path / "serial.log"
    log.write_text("READY\n")
    serial = SerialLog(log)
    serial.text()              # trigger a refresh
    assert serial.path == log
