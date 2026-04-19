"""Parse PC execution traces into ordered lists of addresses.

The canonical input format is plain text with one hex PC per line.
Blank lines and '#' comments are ignored. A small wrapper can filter
QEMU `-d exec` output into this format.

Scale note: the in-memory representation is list[int], which at ~28
bytes per int handles traces up to roughly 1M PCs on a reasonable
laptop. Beyond that, switching parse_trace's return type to
array.array('Q') (8 bytes per PC) or a streaming generator is the
natural evolution. Not done here because current tracer-bullet
traces are a handful of PCs.
"""

from __future__ import annotations

import bisect
from pathlib import Path


def _strip_line(line: str) -> str | None:
    """Return the hex PC substring on a line, or None if not a PC line.

    The initial check captures empty lines and lines whose first non-
    whitespace char is '#'. Past that, any inline '#' tail is stripped;
    the pre-'#' part is guaranteed non-empty at that point (otherwise the
    stripped line would have started with '#' and been caught above).
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "#" in stripped:
        stripped = stripped.split("#", 1)[0].strip()
    return stripped


def _parse_pc_line(line: str) -> int | None:
    """Parse one line; return the PC integer or None to skip."""
    raw = _strip_line(line)
    if raw is None:
        return None
    return int(raw, 16)


def parse_trace(trace_path: Path) -> list[int]:
    """Return PCs from the trace file in execution order.

    Raises ValueError with a filename:lineno context on malformed hex,
    so the CLI can surface a useful error rather than a bare
    "invalid literal for int()".
    """
    result: list[int] = []
    with open(trace_path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            try:
                pc = _parse_pc_line(raw)
            except ValueError as exc:
                msg = (
                    f"{trace_path}:{lineno}: malformed PC line "
                    f"{raw.rstrip()!r}: {exc}"
                )
                raise ValueError(msg) from exc
            if pc is not None:
                result.append(pc)
    return result


def _in_skip(pc: int, sorted_los: list[int], sorted_his: list[int]) -> bool:
    """True iff pc falls in the half-open range at the bisected index."""
    idx = bisect.bisect_right(sorted_los, pc) - 1
    if idx < 0:
        return False
    return sorted_los[idx] <= pc < sorted_his[idx]


def filter_trace(
    trace: list[int],
    skip_ranges: list[tuple[int, int]],
) -> list[int]:
    """Remove PCs that fall inside any half-open [start, end) range.

    Useful before compute_coverage to strip interrupt-handler PCs so
    the strict-adjacency classifier doesn't see a branch followed by a
    handler-entry PC and silently miss the branch's real outcome.

    O(N log M) for N trace PCs and M skip ranges — ranges are sorted
    once and bisected per PC. Assumes ranges are non-overlapping; an
    overlap would merely mean some PCs get multi-counted as "skipped,"
    which is benign but not asserted.

    No-op if skip_ranges is empty — returns a fresh copy regardless
    so callers can mutate safely.
    """
    if not skip_ranges:
        return list(trace)
    sorted_ranges = sorted(skip_ranges)
    sorted_los = [lo for lo, _ in sorted_ranges]
    sorted_his = [hi for _, hi in sorted_ranges]
    return [
        pc for pc in trace
        if not _in_skip(pc, sorted_los, sorted_his)
    ]
