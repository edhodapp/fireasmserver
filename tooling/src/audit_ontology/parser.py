"""Parse raw ``implementation_refs`` / ``verification_refs`` strings.

The ontology's traceability ref strings take three conventional
forms:

* ``path/to/file`` — whole-file reference, no colon
* ``path/to/file:<int>`` — specific line number
* ``path/to/file:<symbol>`` — named entity (function / label /
  class / macro) inside the file

The split is on the LAST ``:`` because repo paths never contain a
colon in practice; splitting on the first would mis-handle the
(hypothetical) case of a colon inside a path segment. The ``<int>``
vs ``<symbol>`` disambiguation is: the tail parses as a plain
decimal integer → ``line``; otherwise → ``symbol``.

An empty ref or an empty-tail ref (``"path:"``) is a parse error —
surfaced as its own structural gap rather than silently treated as
whole-file.

**Path-traversal guard.** Absolute paths and any segment equal to
``..`` are rejected at parse time (``kind="invalid"``). Rationale:
the audit tool reads repo-relative paths only, and ``Path(root) /
parsed_path`` silently discards ``root`` when ``parsed_path`` is
absolute — a fat-fingered or malicious ref to ``/etc/passwd``
would otherwise be dutifully read. Mirrors the defensive posture
of ``tooling/src/qemu_harness/vm_launcher.py:_reject_traversal``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

RefKind = Literal["whole_file", "line", "symbol", "invalid"]


class ParsedRef(BaseModel):
    """Result of parsing a single ref string.

    ``kind="invalid"`` carries the original ``raw`` and an ``error``
    message; ``path`` is empty and ``line``/``symbol`` are None.
    All other kinds have ``path`` set; ``line`` is populated iff
    ``kind="line"`` and ``symbol`` iff ``kind="symbol"``.
    """

    raw: str
    kind: RefKind
    path: str = ""
    line: int | None = None
    symbol: str | None = None
    error: str = ""


def parse_ref(raw: str) -> ParsedRef:
    """Parse one ref string into a ``ParsedRef``.

    See module docstring for the grammar. Never raises; structural
    problems are reported via ``kind="invalid"`` so the audit
    accumulates a list of problems rather than short-circuiting.
    Delegates the path/tail split and tail classification to
    helpers to keep McCabe complexity inside the project cap.
    """
    if not raw:
        return ParsedRef(raw=raw, kind="invalid", error="empty ref")
    path, tail = _split_path_tail(raw)
    if path is None:
        return ParsedRef(
            raw=raw, kind="invalid",
            error="empty path or empty tail around ':'",
        )
    path_error = _reject_unsafe_path(path)
    if path_error:
        return ParsedRef(raw=raw, kind="invalid", error=path_error)
    return _classify_tail(raw, path, tail)


def _split_path_tail(raw: str) -> tuple[str | None, str]:
    """Split ``raw`` into ``(path, tail)``.

    ``tail`` is empty when ``raw`` has no colon (whole-file form).
    Returns ``(None, "")`` when either side of the colon is empty
    so the caller can report a structural error — keeps the error
    check as a single ``is None`` test upstairs.
    """
    if ":" not in raw:
        return raw, ""
    path, tail = raw.rsplit(":", 1)
    if not path or not tail:
        return None, ""
    return path, tail


def _classify_tail(raw: str, path: str, tail: str) -> ParsedRef:
    """Classify a safe ``(path, tail)`` into whole_file / line /
    symbol. ``tail`` empty → whole_file; all-digits → line;
    otherwise → symbol."""
    if not tail:
        return ParsedRef(raw=raw, kind="whole_file", path=path)
    if tail.isdigit():
        return ParsedRef(
            raw=raw, kind="line", path=path, line=int(tail),
        )
    return ParsedRef(raw=raw, kind="symbol", path=path, symbol=tail)


def _reject_unsafe_path(path: str) -> str:
    """Return an error message if ``path`` is absolute or contains
    a ``..`` segment; empty string means safe.

    Why at parse time: ``Path(root) / absolute`` silently drops
    ``root``, and ``..`` segments escape the repo root. The audit
    tool accepts repo-relative paths only; reject anything else at
    the earliest point so resolver code can assume containment.
    """
    if path.startswith("/"):
        return f"absolute path not allowed: {path}"
    if ".." in path.split("/"):
        return f"'..' segment not allowed: {path}"
    return ""
