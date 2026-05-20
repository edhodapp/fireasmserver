"""Parsers for DECISIONS.md and the two REQUIREMENTS.md files.

Each parser is a pure function from input text to a typed model;
file I/O lives in the audit driver so test fixtures can feed
synthetic input directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# REQ-ID shape: starts with a letter, alphanumeric segments
# separated by `-`. Matches MR-001, AES128-001, VIO-MVP-001,
# VIO-F-001, BOUNDARY-001, etc.
_REQ_ID_PATTERN = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+"
_REQ_ID_RE = re.compile(_REQ_ID_PATTERN)

# D-class heading. Captures the 3-digit number and the rest of the
# heading line for context in error messages.
_D_HEADING_RE = re.compile(
    r"^### D(\d{3}):\s*(.+)$", re.MULTILINE,
)

# REQ heading in the root REQUIREMENTS.md.
_REQ_HEADING_RE = re.compile(
    r"^### (" + _REQ_ID_PATTERN + r"):", re.MULTILINE,
)

# REQ row in docs/l2/REQUIREMENTS.md table form. A row that opens
# with a backticked identifier matching the REQ pattern.
_L2_TABLE_ROW_RE = re.compile(
    r"^\|\s*`(" + _REQ_ID_PATTERN + r")`", re.MULTILINE,
)


@dataclass(frozen=True)
class Decision:
    """One D-class entry's coverage state."""

    id: str                              # e.g. "D058"
    title: str                           # heading text after `: `
    requirements_line: str | None        # raw value after `**Requirements:**`
    req_ids: tuple[str, ...] = field(default_factory=tuple)
    is_na: bool = False                  # "N/A — <reason>" form
    is_see_block: bool = False           # "see ... block below"


def parse_decisions(text: str) -> list[Decision]:
    """Extract all D-class entries from DECISIONS.md text."""
    headings = list(_D_HEADING_RE.finditer(text))
    decisions: list[Decision] = []
    for i, match in enumerate(headings):
        d_id = f"D{match.group(1)}"
        title = match.group(2).strip()
        body_start = match.end()
        body_end = (
            headings[i + 1].start() if i + 1 < len(headings)
            else len(text)
        )
        body = text[body_start:body_end]
        decisions.append(_classify(d_id, title, body))
    return decisions


def _classify(d_id: str, title: str, body: str) -> Decision:
    """Parse one D-entry's body for its `**Requirements:**` line."""
    req_line = _extract_requirements_line(body)
    if req_line is None:
        return Decision(id=d_id, title=title, requirements_line=None)
    is_na = req_line.startswith("N/A")
    is_see_block = req_line.lower().startswith("see ")
    req_ids: tuple[str, ...] = ()
    if not is_na and not is_see_block:
        req_ids = tuple(_REQ_ID_RE.findall(req_line))
    return Decision(
        id=d_id, title=title, requirements_line=req_line,
        req_ids=req_ids, is_na=is_na, is_see_block=is_see_block,
    )


def _extract_requirements_line(body: str) -> str | None:
    """Find `**Requirements:** <value>` and return <value>.

    Tolerates the value spilling across continuation lines by
    greedy-matching everything up to the next blank line or the
    next `**` annotation.
    """
    marker = "**Requirements:**"
    idx = body.find(marker)
    if idx < 0:
        return None
    rest = body[idx + len(marker):].lstrip()
    # Stop at the first blank line — Requirements is a single
    # logical line, possibly soft-wrapped by markdown but never
    # paragraph-broken.
    first_break = rest.find("\n\n")
    if first_break >= 0:
        rest = rest[:first_break]
    return " ".join(rest.split())


def parse_requirements_md(text: str) -> set[str]:
    """Extract REQ IDs from the root REQUIREMENTS.md."""
    return {match.group(1) for match in _REQ_HEADING_RE.finditer(text)}


def parse_l2_requirements_table(text: str) -> set[str]:
    """Extract REQ IDs from docs/l2/REQUIREMENTS.md table rows."""
    return {
        match.group(1) for match in _L2_TABLE_ROW_RE.finditer(text)
    }
