"""Serial log reader for L2 integration tests.

Tails Firecracker's serial-output file and exposes blocking
`wait_for` plus snapshot `text` operations. Tests use this to
assert on marker emission and to capture the full guest serial
log for the artifact directory on failure.

Per `docs/l2/HARNESS.md` §3.5.

Read model
----------
SerialLog maintains an internal cursor (`_cursor`) so that
`wait_for` / `assert_marker_observed` / `assert_marker_absent`
only see content appended SINCE the last `checkpoint()` call
(default: since construction). The full file is still available
via `text()` for diagnostics and AssertionError context. This
matters when a test needs to verify a REPEATED marker (e.g., a
second READY after a reboot) — without a cursor, the search
would match the first occurrence and never wait for the
second.

Backing storage is incremental: each refresh seeks to where the
last read left off and consumes only new bytes, so the cost of
`wait_for`'s ~50 ms polling is O(delta) rather than O(N) per
poll (the prior implementation re-read + re-stripped the whole
file each tick, which became O(N²) over the life of a long
test).
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
# line>\n00002\n", and a literal substring `RX:FAIL num_bufs=
# 00000002` fails to match. The fix: strip every Firecracker
# log line out of the captured text before substring checks.
# Pattern matches "2026-05-24T16:37:37.929247818 [l2-harness:
# <thread>] <rest of line>\n" anywhere in the buffer — the
# date/time prefix is sufficiently unique that no legitimate
# guest emit will collide. The trailing `\n?` consumes the FC
# line's terminator so an injected line splices out cleanly
# and the broken guest line is re-joined as a single line
# (Gemini MED, post-cursor-overhaul review).
_FIRECRACKER_LOG_LINE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+ "
    r"\[l2-harness:[^\]]+\] [^\n]*\n?",
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

    Cursor semantics: at construction, cursor = 0 (matches
    behavior before the cursor was introduced — wait_for sees
    the full file). Call `checkpoint()` after observing an
    event to advance the cursor; subsequent wait_for /
    assert_marker_observed only consider content after the
    checkpoint. `text()` is always full-history for diagnostics.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        # Incremental read state. `_cleaned_buf` holds the
        # accumulated content of complete lines after the
        # Firecracker-strip regex; `_raw_partial` is whatever
        # follows the last newline (we cannot safely line-strip
        # a partial line in case the FC log injection is still
        # arriving).
        self._cleaned_buf: str = ""
        self._raw_partial: str = ""
        self._bytes_consumed: int = 0
        # Cursor is a SNAPSHOT of the cleaned text at checkpoint
        # time — the literal "what wait_for would see at this
        # moment" string. Subsequent `_text_since_cursor` calls
        # compute the current cleaned text and return the suffix
        # after the snapshot. This handles all edge cases:
        #   - mid-emit guest line (snapshot includes partial;
        #     when partial completes, snapshot is still a prefix
        #     of current → delta is correct)
        #   - mid-emit FC line (snapshot includes partial-FC
        #     content that hasn't been stripped yet; when FC
        #     line completes and gets stripped, snapshot is NO
        #     LONGER a prefix → we fall back to current and
        #     correctly hide nothing rather than leak the FC
        #     tail an offset-based approach would expose)
        # Codex P2 + Gemini MED, post-byte-offset review.
        self._cursor_snapshot: str = ""

    @property
    def path(self) -> Path:
        """The on-disk path being tailed."""
        return self._path

    def _refresh(self) -> None:
        """Read any new bytes since last refresh; merge to buffer.

        Cheap when no new bytes are present (one stat + early
        return). When new bytes ARE present, only the delta is
        decoded + regex-stripped. Lines past the last newline
        are held in `_raw_partial` until a later refresh
        appends their terminator.
        """
        if not self._path.exists():
            return
        size = self._path.stat().st_size
        if size <= self._bytes_consumed:
            return
        with self._path.open("rb") as fh:
            fh.seek(self._bytes_consumed)
            chunk = fh.read(size - self._bytes_consumed)
        self._bytes_consumed = size
        self._raw_partial += chunk.decode("utf-8", errors="replace")
        last_nl = self._raw_partial.rfind("\n")
        if last_nl == -1:
            return
        consumable = self._raw_partial[:last_nl + 1]
        self._raw_partial = self._raw_partial[last_nl + 1:]
        self._cleaned_buf += _FIRECRACKER_LOG_LINE_RE.sub("", consumable)

    def text(self) -> str:
        """Snapshot the FULL cleaned log as a text string.

        Used for diagnostics and AssertionError context — see
        `_tail_for_assert`. Cursor is ignored; this is always
        the complete history.
        """
        self._refresh()
        return self._compute_full_cleaned()

    def checkpoint(self) -> None:
        """Mark current end-of-log as the cursor.

        Subsequent `wait_for` / `assert_marker_observed` /
        `assert_marker_absent` only consider content appended
        after this point. Use to verify repeated events
        (e.g., second READY after a reboot, or a second
        TX:RECLAIMED after stimulating another frame).

        Snapshots the cleaned text — see `_cursor_snapshot`'s
        comment in `__init__` for why this handles both the
        mid-emit-guest-line and mid-emit-FC-line edge cases
        without the byte-offset approach's FC-tail leak.
        """
        self._refresh()
        self._cursor_snapshot = self._compute_full_cleaned()

    def _compute_full_cleaned(self) -> str:
        """Cleaned text view of accumulated content (no cursor).

        `_cleaned_buf` holds lines through the last seen \\n,
        already FC-stripped. `_raw_partial` is best-effort
        FC-stripped at query time; a partial FC line that
        hasn't completed yet won't match the regex (because of
        the trailing `\\n?` anchor's preference for a complete
        line; partials still flow through to the result).
        """
        return self._cleaned_buf + _FIRECRACKER_LOG_LINE_RE.sub(
            "", self._raw_partial,
        )

    def _text_since_cursor(self) -> str:
        """Cleaned content appended after the last checkpoint.

        Returns the suffix of the current cleaned text after
        the snapshot taken at checkpoint. Three cases:

          1. No checkpoint set (snapshot empty) → return full
             current cleaned text (matches behavior before
             cursor was introduced).

          2. Current text starts with snapshot → return the
             suffix (the normal case: snapshot stays a stable
             prefix as new content is appended).

          3. Snapshot is no longer a prefix → the partial
             content captured at checkpoint has since been
             stripped (e.g., a partial timestamp that grew
             into a complete FC line and got removed). Compute
             the longest common prefix between snapshot and
             current; return current's suffix past that point.
             This is the only correct way to recover the
             "appended" content when the snapshot ceases to
             be literally a prefix — returning the full
             current text would re-expose pre-checkpoint
             markers (Gemini MED, post-snapshot-cursor review).
        """
        self._refresh()
        current = self._compute_full_cleaned()
        if not self._cursor_snapshot:
            return current
        if current.startswith(self._cursor_snapshot):
            return current[len(self._cursor_snapshot):]
        common = 0
        limit = min(len(current), len(self._cursor_snapshot))
        while common < limit and (
            current[common] == self._cursor_snapshot[common]
        ):
            common += 1
        return current[common:]

    def wait_for(self, marker: str, timeout: float = 1.0) -> bool:
        """Block until `marker` appears AFTER the last checkpoint.

        Returns True on observation, False on timeout. Always
        performs at least one snapshot check before considering
        the timeout expired — `timeout=0.0` therefore means "look
        right now, don't wait" rather than "always return False."

        Default cursor is 0 (set at construction), so a fresh
        SerialLog sees the full log just like the pre-cursor
        implementation. Tests that need event-vs-event
        discrimination should call `checkpoint()` between
        stimuli.
        """
        deadline = time.monotonic() + timeout
        while True:
            if marker in self._text_since_cursor():
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
        """Wait `window` seconds; raise if `marker` appears between
        the cursor and the end of the window.

        Cursor-based, NOT window-only: covers the existing
        test pattern of `send frame → wait for positive →
        assert negative absent`, where a forbidden marker
        that fires between the stimulus and the positive-wait
        should still trip the absence assertion. Tests that
        truly want window-only semantics (ignore prior
        emissions) call `checkpoint()` before this call.

        Polls every `WAIT_POLL_INTERVAL_SECONDS` so a
        forbidden marker that lands early in the window
        fails the test immediately rather than after the
        full sleep — saves a few hundred ms per failed
        assertion at unbounded scale (Gemini LOW, post-
        cursor-overhaul review).
        """
        deadline = time.monotonic() + window
        while True:
            since = self._text_since_cursor()
            if marker in since:
                raise AssertionError(
                    f"marker {marker!r} unexpectedly observed "
                    f"within {window}s\n"
                    f"{_tail_for_assert(self.text(), self._path)}"
                )
            if time.monotonic() >= deadline:
                return
            time.sleep(WAIT_POLL_INTERVAL_SECONDS)
