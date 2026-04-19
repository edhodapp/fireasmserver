"""Parse PC execution traces into ordered lists of addresses.

The canonical input format is plain text with one hex PC per line.
Blank lines and '#' comments are ignored. A small wrapper can filter
QEMU `-d exec` output into this format.
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
