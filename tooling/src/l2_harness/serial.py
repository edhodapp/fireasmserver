"""Serial log reader for L2 integration tests.

Tails Firecracker's serial-output file and exposes blocking
`wait_for` plus snapshot `text` operations. Tests use this to
assert on marker emission and to capture the full guest serial
log for the artifact directory on failure.

Per `docs/l2/HARNESS.md` §3.5.
"""

from __future__ import annotations

import re
import time
from pathlib import Path


WAIT_POLL_INTERVAL_SECONDS = 0.05

# Firecracker writes its own startup log lines to the same file
# Python opens for the guest's serial output. When the guest's
# emit_bytes is in flight at the same moment Firecracker writes
# (e.g., its "Successfully started microvm" line), the two
# streams interleave mid-line — the guest's "RX:FAIL num_bufs=
# 00000002\n" becomes "RX:FAIL num_bufs=000<firecracker log
# line>00002\n", and a literal substring `RX:FAIL num_bufs=
# 00000002` fails to match. The fix: strip every Firecracker
# log line out of the captured text before substring checks.
# Pattern matches "2026-05-24T16:37:37.929247818 [l2-harness:
# <thread>] <rest of line>" anywhere in the buffer — the
# date/time prefix is sufficiently unique that no legitimate
# guest emit will collide.
_FIRECRACKER_LOG_LINE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+ "
    r"\[l2-harness:[^\]]+\] [^\n]*",
)

MAX_ASSERT_LOG_LINES = 40
"""Cap on the line count of the serial log embedded in
AssertionError messages from assert_marker_* helpers.

The full log can be many KB once a test runs through the boot
chain and a few dispatch iterations — embedding it in an
exception message swamps the failure signal and may be
truncated by CI log aggregators in ways that drop the actually-
relevant tail (the markers near the failure). Showing the LAST
N lines + a pointer to the on-disk path keeps the per-error
output bounded while leaving the full log available for deeper
diagnostics. Per Gemini LOW finding on the L2 cleanup pass.
"""


def _tail_for_assert(text: str, path: object) -> str:
    """Format the trailing lines of `text` for an assertion error.

    Returns the last `MAX_ASSERT_LOG_LINES` lines (or fewer if
    the log is short) bracketed by the path of the on-disk
    file so a developer reading the failure knows where the
    full log lives.
    """
    lines = text.splitlines()
    if len(lines) <= MAX_ASSERT_LOG_LINES:
        return (
            f"--- serial log ({path}) ---\n"
            f"{text}\n"
            "--- end serial log ---"
        )
    omitted = len(lines) - MAX_ASSERT_LOG_LINES
    tail = "\n".join(lines[-MAX_ASSERT_LOG_LINES:])
    return (
        f"--- serial log (last {MAX_ASSERT_LOG_LINES} of "
        f"{len(lines)} lines; full log at {path}) ---\n"
        f"... [{omitted} earlier lines omitted]\n"
        f"{tail}\n"
        "--- end serial log ---"
    )


class SerialLog:
    """Read-only view of the guest's serial output.

    The file is owned by the FirecrackerGuest's subprocess —
    this class only reads. Safe to use across multiple tests
    against the same guest (e.g., assert markers from earlier
    interactions).
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """The on-disk path being tailed."""
        return self._path

    def text(self) -> str:
        """Snapshot the full log as a text string.

        Decoded as utf-8 with errors=replace so any partial-byte
        boundary on a midstream read doesn't raise; the harness
        cares about marker substrings, not byte-exact integrity.
        Firecracker startup log lines are stripped to avoid the
        mid-line interleave failure mode described in the module
        docstring's `_FIRECRACKER_LOG_LINE_RE` comment.
        """
        if not self._path.exists():
            return ""
        raw = self._path.read_bytes().decode("utf-8", errors="replace")
        return _FIRECRACKER_LOG_LINE_RE.sub("", raw)

    def wait_for(self, marker: str, timeout: float = 1.0) -> bool:
        """Block until `marker` appears in the log or timeout.

        Returns True on observation, False on timeout. Always
        performs at least one snapshot check before considering
        the timeout expired — `timeout=0.0` therefore means "look
        right now, don't wait" rather than "always return False."
        """
        deadline = time.monotonic() + timeout
        while True:
            if marker in self.text():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(WAIT_POLL_INTERVAL_SECONDS)

    def assert_marker_observed(self, marker: str,
                               timeout: float = 1.0) -> None:
        """Wait for `marker`; raise AssertionError with context on miss."""
        if not self.wait_for(marker, timeout):
            raise AssertionError(
                f"marker {marker!r} not observed within {timeout}s\n"
                f"{_tail_for_assert(self.text(), self._path)}"
            )

    def assert_marker_absent(self, marker: str,
                             window: float = 1.0) -> None:
        """Sleep `window` seconds; raise if `marker` appears at all."""
        time.sleep(window)
        if marker in self.text():
            raise AssertionError(
                f"marker {marker!r} unexpectedly observed\n"
                f"{_tail_for_assert(self.text(), self._path)}"
            )
