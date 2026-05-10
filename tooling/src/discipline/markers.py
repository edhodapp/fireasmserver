"""Extract DISCIPLINE-PRINT-START/-END marker blocks from source files.

Markers are author-placed comments that name the canonical block of
the file. Examples:

    // DISCIPLINE-PRINT-START: memreq-record-fields
    // ... canonical content ...
    // DISCIPLINE-PRINT-END: memreq-record-fields

Marker comment syntax (//, ;, #, etc.) is irrelevant — the scanner
matches the literal `DISCIPLINE-PRINT-(START|END): <name>` token
inside any line. Markers themselves are excluded from the extracted
block.

The block-name grammar is constrained: an ASCII letter followed by
letters, digits, underscores, or hyphens, optionally trailed by
whitespace to end-of-line. A typo like `: foo.` (trailing period)
or `: foo bar` (embedded space) does NOT match — the marker will
appear absent and `extract_block` raises a MarkerError, which is
preferable to silently capturing the typo as the block name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MARKER = re.compile(
    r"DISCIPLINE-PRINT-(START|END):\s*([A-Za-z][A-Za-z0-9_-]*)\s*$",
)


@dataclass(frozen=True)
class MarkerError:
    """Failure to extract a named block from a file."""

    block_name: str
    reason: str


def extract_block(
    text: str,
    block_name: str,
) -> list[str] | MarkerError:
    """Return lines between START/END markers for `block_name`.

    Markers are excluded. Returns MarkerError on missing markers,
    duplicates, mis-ordering, or end-before-start.
    """
    lines = text.splitlines()
    pair = _find_marker_pair(lines, block_name)
    if isinstance(pair, MarkerError):
        return pair
    start_idx, end_idx = pair
    return lines[start_idx + 1:end_idx]


def _find_marker_pair(
    lines: list[str],
    block_name: str,
) -> tuple[int, int] | MarkerError:
    """Locate the (start, end) line indices for one named block."""
    starts: list[int] = []
    ends: list[int] = []
    for i, line in enumerate(lines):
        m = _MARKER.search(line)
        if m is None or m.group(2) != block_name:
            continue
        target = starts if m.group(1) == "START" else ends
        target.append(i)
    return _validate_pair(block_name, starts, ends)


def _validate_pair(
    block_name: str,
    starts: list[int],
    ends: list[int],
) -> tuple[int, int] | MarkerError:
    """Reject 0/many starts, 0/many ends, or end-before-start."""
    if len(starts) != 1:
        return MarkerError(
            block_name=block_name,
            reason=f"expected 1 START marker, found {len(starts)}",
        )
    if len(ends) != 1:
        return MarkerError(
            block_name=block_name,
            reason=f"expected 1 END marker, found {len(ends)}",
        )
    if starts[0] >= ends[0]:
        return MarkerError(
            block_name=block_name,
            reason="END marker is at or before START marker",
        )
    return (starts[0], ends[0])
