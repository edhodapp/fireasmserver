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

* ``.py`` — AST parse, collect every ``FunctionDef`` /
  ``AsyncFunctionDef`` / ``ClassDef`` name AND every
  ``Assign``/``AnnAssign`` target, so module-level variables
  like ``vm_launcher._proc_registry`` / ``_proc_lock`` resolve
  as symbols. Briefing allows a ``def`` / ``class`` grep
  fallback; AST is the rigorous path (per CLAUDE.md "at decision
  points, take the rigorous path").
* ``.S`` / ``.s`` — line-anchored regex for three forms, tried
  in order: plain label ``^\\s*<sym>\\s*:`` (NASM x86_64 per
  D048, GAS aarch64), NASM multi-line macro definition
  ``^\\s*%macro\\s+<sym>\\b``, and NASM single-token macro
  ``^\\s*%define\\s+<sym>\\b``. Ed flagged upcoming crypto work
  will define CRC fold constants and related helpers as NASM
  macros, so the auditor needs to recognize them as first-class
  symbol definitions.
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
    """True iff ``symbol`` names a function, class, or top-level
    variable in ``text``.

    Real ontology refs point at module-level variables (e.g.
    ``vm_launcher.py:_proc_registry``) as well as defs and
    classes, so all three AST node classes are collected. Source
    the AST can't parse falls back to a regex that matches
    ``def``/``class``/``<sym> =``/``<sym>:``.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _py_regex_fallback(text, symbol)
    return symbol in _collect_py_names(tree)


def _collect_py_names(tree: ast.AST) -> set[str]:
    """Names defined anywhere in ``tree``: FunctionDef /
    AsyncFunctionDef / ClassDef plus Assign / AnnAssign targets.

    Uses ``ast.walk`` so nested definitions (methods, closures,
    class-body assignments) also count — the ontology's refs are
    typically top-level, but the auditor shouldn't false-negative
    on a perfectly valid nested reference. Per-node dispatch
    lives in ``_names_from_node`` to keep this pass under the
    project's McCabe cap.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        names.update(_names_from_node(node))
    return names


def _names_from_node(node: ast.AST) -> set[str]:
    """Extract any name(s) this AST node defines at its own
    position. Returns empty when the node is neither a def-class
    nor a simple assignment."""
    if isinstance(
        node,
        (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
    ):
        return {node.name}
    if isinstance(node, ast.Assign):
        return _names_from_assign_targets(node)
    if isinstance(node, ast.AnnAssign) and isinstance(
        node.target, ast.Name,
    ):
        return {node.target.id}
    return set()


def _names_from_assign_targets(node: ast.Assign) -> set[str]:
    """``ast.Assign`` can have multiple ``targets`` (``a = b = 1``);
    only plain ``Name`` targets contribute resolvable symbols —
    tuple-unpacking, subscripting, and attribute assignment aren't
    plausible refs from the ontology."""
    names: set[str] = set()
    for target in node.targets:
        if isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _py_regex_fallback(text: str, symbol: str) -> bool:
    """Regex fallback for un-AST-parseable Python. Matches
    ``def <sym>`` / ``class <sym>`` / line-start ``<sym> =`` /
    line-start ``<sym>: ``."""
    esc = re.escape(symbol)
    def_or_class = rf"(?:\bdef\s+|\bclass\s+){esc}\b"
    top_assign = rf"(?m)^{esc}\s*[:=]"
    return bool(
        re.search(def_or_class, text) or re.search(top_assign, text),
    )


def _symbol_in_asm(text: str, symbol: str) -> bool:
    """True iff ``text`` defines ``symbol`` as a label, NASM
    ``%macro``, or NASM ``%define``. GAS (aarch64) only uses the
    label form; NASM (x86_64, D048) uses all three and the
    upcoming CRC / crypto side session will lean on macros."""
    esc = re.escape(symbol)
    patterns = [
        rf"(?m)^\s*{esc}\s*:",
        rf"(?m)^\s*%macro\s+{esc}\b",
        rf"(?m)^\s*%define\s+{esc}\b",
    ]
    return any(re.search(p, text) is not None for p in patterns)


def _symbol_in_c(text: str, symbol: str) -> bool:
    """True iff ``text`` contains ``<symbol>(`` at a word
    boundary — function definition or declaration. Matches the
    common case; misses function-pointer typedefs, which the
    briefing accepts."""
    pattern = rf"\b{re.escape(symbol)}\s*\("
    return re.search(pattern, text) is not None
