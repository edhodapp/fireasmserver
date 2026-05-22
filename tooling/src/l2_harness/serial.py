"""Serial log reader for L2 integration tests.

Tails Firecracker's serial-output file and exposes blocking
`wait_for` plus snapshot `text` operations. Tests use this to
assert on marker emission and to capture the full guest serial
log for the artifact directory on failure.

Per `docs/l2/HARNESS.md` §3.5.
"""

from __future__ import annotations

import time
from pathlib import Path


WAIT_POLL_INTERVAL_SECONDS = 0.05


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
        """
        if not self._path.exists():
            return ""
        return self._path.read_bytes().decode("utf-8", errors="replace")

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
                f"--- serial log ({self._path}) ---\n"
                f"{self.text()}\n"
                "--- end serial log ---"
            )

    def assert_marker_absent(self, marker: str,
                             window: float = 1.0) -> None:
        """Sleep `window` seconds; raise if `marker` appears at all."""
        time.sleep(window)
        if marker in self.text():
            raise AssertionError(
                f"marker {marker!r} unexpectedly observed\n"
                f"--- serial log ({self._path}) ---\n"
                f"{self.text()}\n"
                "--- end serial log ---"
            )
