"""Resolve ``ParsedRef`` objects against a repo working tree.

Each ref resolves to one of:

* ``resolved`` — file exists (plus symbol or line found when the
  ref demands it)
* ``file_missing`` — file does not exist under the repo root
* ``symbol_missing`` — file exists, symbol not found
* ``line_out_of_range`` — file exists but has fewer lines than
  requested
* ``invalid`` — the parser already flagged the ref as malformed;
  no filesystem access attempted

Symbol-lookup strategy per suffix:

* ``.py`` — AST parse, collect every top-level and nested
  ``FunctionDef`` / ``AsyncFunctionDef`` / ``ClassDef`` name.
  Briefing allows a ``def`` / ``class`` grep fallback; AST is the
  rigorous path (per CLAUDE.md "at decision points, take the
  rigorous path") and the cost difference is noise.
* ``.S`` / ``.s`` — line-anchored label regex ``^\\s*<sym>\\s*:``.
  Handles NASM (x86_64 per D048) and GAS (aarch64); both use
  ``label:`` syntax at line start.
* ``.c`` / ``.h`` — ``\\b<sym>\\s*\\(`` captures function
  definitions and declarations alike.
* everything else (``.md``, ``.sh``, ``.yml``, plain text) —
  literal substring match. Honest signal; no fuzzy-match magic.

Callers supply a ``repo_root`` explicitly rather than discovering
it via ``Path(__file__)``: the test fixture builds a synthetic
tree in ``tmp_path`` and points the resolver at it, and the CLI
wires the real repo root through. Keeps the resolver pure (no
implicit filesystem roots) and testable.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from audit_ontology.parser import ParsedRef

Resolution = Literal[
    "resolved",
    "file_missing",
    "symbol_missing",
    "line_out_of_range",
    "invalid",
]


class ResolvedRef(BaseModel):
    """A parsed ref paired with its resolution against the repo.

    ``resolution`` is the outcome code (see module docstring).
    ``detail`` carries a short human-readable note when the ref
    failed to resolve (e.g., the path that didn't exist), empty
    on success.
    """

    parsed: ParsedRef
    resolution: Resolution
    detail: str = ""


def resolve_ref(parsed: ParsedRef, repo_root: Path) -> ResolvedRef:
    """Resolve one parsed ref against ``repo_root``.

    Dispatches on ``parsed.kind``. File existence is checked once
    at the top so the three file-dependent branches can assume a
    readable path.
    """
    if parsed.kind == "invalid":
        return ResolvedRef(
            parsed=parsed, resolution="invalid",
            detail=parsed.error,
        )
    target = repo_root / parsed.path
    if not target.is_file():
        return ResolvedRef(
            parsed=parsed, resolution="file_missing",
            detail=f"no such file: {parsed.path}",
        )
    if parsed.kind == "whole_file":
        return ResolvedRef(parsed=parsed, resolution="resolved")
    if parsed.kind == "line":
        return _resolve_line(parsed, target)
    return _resolve_symbol(parsed, target)


def _resolve_line(parsed: ParsedRef, target: Path) -> ResolvedRef:
    """Resolve a ``path:<int>`` ref by counting the file's lines.

    Empty-file edge case: a 0-byte file has zero lines, not one.
    The trailing-newline branch only adds a line when there is
    content that isn't terminated with ``\\n``.
    """
    line = parsed.line or 0
    text = target.read_text(encoding="utf-8", errors="replace")
    total = text.count("\n") + (
        1 if text and not text.endswith("\n") else 0
    )
    if line < 1 or line > total:
        return ResolvedRef(
            parsed=parsed, resolution="line_out_of_range",
            detail=f"file has {total} lines; requested {line}",
        )
    return ResolvedRef(parsed=parsed, resolution="resolved")


def _resolve_symbol(parsed: ParsedRef, target: Path) -> ResolvedRef:
    """Resolve a ``path:<symbol>`` ref via suffix-appropriate lookup."""
    symbol = parsed.symbol or ""
    text = target.read_text(encoding="utf-8", errors="replace")
    suffix = target.suffix.lower()
    if suffix == ".py":
        found = _symbol_in_py(text, symbol)
    elif suffix in {".s"}:
        found = _symbol_in_asm(text, symbol)
    elif suffix in {".c", ".h"}:
        found = _symbol_in_c(text, symbol)
    else:
        found = symbol in text
    if not found:
        return ResolvedRef(
            parsed=parsed, resolution="symbol_missing",
            detail=f"no symbol '{symbol}' in {parsed.path}",
        )
    return ResolvedRef(parsed=parsed, resolution="resolved")


def _symbol_in_py(text: str, symbol: str) -> bool:
    """True iff any FunctionDef / AsyncFunctionDef / ClassDef in
    ``text`` has name ``symbol``. Unparseable source falls back to
    a ``def <symbol>`` / ``class <symbol>`` regex — better than
    returning False on a file the AST briefly trips over (e.g.,
    during partial edits)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _py_regex_fallback(text, symbol)
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ) and node.name == symbol:
            return True
    return False


def _py_regex_fallback(text: str, symbol: str) -> bool:
    """Regex fallback for un-AST-parseable Python. Matches
    ``def <symbol>`` / ``async def <symbol>`` / ``class <symbol>``
    at word boundary."""
    pattern = rf"(?:\bdef\s+|\bclass\s+){re.escape(symbol)}\b"
    return re.search(pattern, text) is not None


def _symbol_in_asm(text: str, symbol: str) -> bool:
    """True iff ``text`` contains a ``<symbol>:`` label at the
    start of a line (optionally after whitespace). Covers NASM
    (x86_64, D048) and GAS (aarch64) — both use the colon-suffix
    label syntax."""
    pattern = rf"(?m)^\s*{re.escape(symbol)}\s*:"
    return re.search(pattern, text) is not None


def _symbol_in_c(text: str, symbol: str) -> bool:
    """True iff ``text`` contains ``<symbol>(`` at a word
    boundary — function definition or declaration. Matches the
    common case; misses function-pointer typedefs, which the
    briefing accepts."""
    pattern = rf"\b{re.escape(symbol)}\s*\("
    return re.search(pattern, text) is not None
