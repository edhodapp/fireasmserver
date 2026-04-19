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
    """Return PCs from the trace file in execution order."""
    result: list[int] = []
    with open(trace_path, encoding="utf-8") as f:
        for raw in f:
            pc = _parse_pc_line(raw)
            if pc is not None:
                result.append(pc)
    return result


def filter_trace(
    trace: list[int],
    skip_ranges: list[tuple[int, int]],
) -> list[int]:
    """Remove PCs that fall inside any half-open [start, end) range.

    Useful before compute_coverage to strip interrupt-handler PCs so
    the strict-adjacency classifier doesn't see a branch followed by a
    handler-entry PC and silently miss the branch's real outcome.

    No-op if skip_ranges is empty — returns a fresh copy regardless
    so callers can mutate safely.
    """
    if not skip_ranges:
        return list(trace)
    return [
        pc for pc in trace
        if not any(lo <= pc < hi for lo, hi in skip_ranges)
    ]
