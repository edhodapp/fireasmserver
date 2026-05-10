"""Parse DECISIONS.md and REQUIREMENTS.md into id-keyed entries.

Both files use `### <id>:` heading conventions, where `<id>` is an
all-caps token ending in a digit (e.g. `D058`, `MR-007`, `BS-001`).
An entry's body runs from one heading up to the next id-shaped
heading or end of file. Non-id `### ` sub-headings inside a body
(e.g. `### Memory model`, `### Examples:`) are intentionally NOT
recognized as new entries — only the constrained id shape matches,
so authors can use markdown sub-headings freely inside an entry.

An entry is "deprecated" when its body's first non-blank line begins
with `**DEPRECATED ` (the immutable bidirectional-annotation pattern
required by the immutable decision-log convention).

Sub-bullet `**DEPRECATED ...**` markers further down in a body are
unrelated to entry-level deprecation and are intentionally ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^### ([A-Z][A-Z0-9-]*\d):")
_DEPRECATED_PREFIX = "**DEPRECATED "


@dataclass(frozen=True)
class Entry:
    """One `### <id>:` markdown entry."""

    entry_id: str
    body_lines: tuple[str, ...]
    deprecated: bool

    def render(self) -> str:
        """Render the entry as it would appear in source form."""
        body = "\n".join(self.body_lines).rstrip()
        return f"### {self.entry_id}:\n{body}".rstrip() + "\n"


def parse_entries(text: str) -> list[Entry]:
    """Return all `### <id>:` entries in document order."""
    blocks = _split_blocks(text)
    return [_finalize(eid, body) for eid, body in blocks]


def find_entry(entries: list[Entry], entry_id: str) -> Entry | None:
    """Return the first entry whose id matches `entry_id`."""
    for entry in entries:
        if entry.entry_id == entry_id:
            return entry
    return None


def find_by_prefix(
    entries: list[Entry],
    prefix: str,
    *,
    include_deprecated: bool = False,
) -> list[Entry]:
    """Return entries whose id starts with prefix.

    By default, deprecated entries are filtered out. Pass
    `include_deprecated=True` when the caller wants to render its own
    "deprecated; skipped" annotation alongside the active entries
    (preserving traceability with the immutable decision-log).
    """
    return [
        e for e in entries
        if e.entry_id.startswith(prefix)
        and (include_deprecated or not e.deprecated)
    ]


def _split_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Walk lines, accumulating (entry_id, body_lines) pairs."""
    blocks: list[tuple[str, list[str]]] = []
    state = _SplitState()
    for line in text.splitlines():
        _process_line(line, state, blocks)
    state.flush(blocks)
    return blocks


@dataclass
class _SplitState:
    """Streaming state for `_split_blocks` — current id + body buffer."""

    current_id: str | None = None
    current_body: list[str] = field(default_factory=list)

    def flush(self, blocks: list[tuple[str, list[str]]]) -> None:
        """Emit the in-progress entry, if any."""
        if self.current_id is not None:
            blocks.append((self.current_id, self.current_body))


def _process_line(
    line: str,
    state: _SplitState,
    blocks: list[tuple[str, list[str]]],
) -> None:
    """Handle one input line — either continue body or open new entry."""
    m = _HEADING.match(line)
    if m is None:
        if state.current_id is not None:
            state.current_body.append(line)
        return
    state.flush(blocks)
    state.current_id = m.group(1)
    state.current_body = []


def _finalize(entry_id: str, body: list[str]) -> Entry:
    """Build an Entry, computing its deprecated flag."""
    return Entry(
        entry_id=entry_id,
        body_lines=tuple(body),
        deprecated=_is_deprecated(body),
    )


def _is_deprecated(body: list[str]) -> bool:
    """True if first non-blank body line starts with `**DEPRECATED `."""
    for line in body:
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith(_DEPRECATED_PREFIX)
    return False
