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
import functools
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from audit_ontology.parser import ParsedRef

Resolution = Literal[
    "resolved",
    "file_missing",
    "symbol_missing",
    "line_out_of_range",
    "outside_repo",
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

    Dispatches on ``parsed.kind``. Layered defense: parser rejects
    absolute and ``..`` paths, resolver rejects paths whose
    symlink-resolved target escapes ``repo_root`` — an in-repo
    symlink pointing at ``/etc/shadow`` would otherwise be read
    on every CI audit. File-existence + containment checks live
    in ``_check_file_and_containment`` to keep this dispatch under
    the project's McCabe cap.
    """
    if parsed.kind == "invalid":
        return ResolvedRef(
            parsed=parsed, resolution="invalid",
            detail=parsed.error,
        )
    target = repo_root / parsed.path
    early = _check_file_and_containment(target, repo_root, parsed)
    if early is not None:
        return early
    if parsed.kind == "whole_file":
        return ResolvedRef(parsed=parsed, resolution="resolved")
    if parsed.kind == "line":
        return _resolve_line(parsed, target)
    return _resolve_symbol(parsed, target)


def _check_file_and_containment(
    target: Path, repo_root: Path, parsed: ParsedRef,
) -> ResolvedRef | None:
    """Return a failure ResolvedRef when ``target`` is missing or
    escapes ``repo_root`` via symlink, else ``None`` so the caller
    proceeds to kind-specific resolution."""
    if not target.is_file():
        return ResolvedRef(
            parsed=parsed, resolution="file_missing",
            detail=f"no such file: {parsed.path}",
        )
    escape_detail = _check_contained(target, repo_root, parsed.path)
    if escape_detail:
        return ResolvedRef(
            parsed=parsed, resolution="outside_repo",
            detail=escape_detail,
        )
    return None


def _check_contained(
    target: Path, repo_root: Path, raw_path: str,
) -> str:
    """Empty when ``target`` resolves inside ``repo_root``; an
    error detail string otherwise.

    ``Path.resolve()`` raises ``RuntimeError`` on circular
    symlinks (ELOOP, Python 3.11+) and ``OSError`` for other
    resolve failures (permission denied, dangling symlink under
    ``strict=True``, etc.); either flavor is a containment
    failure from the auditor's view — honest signal rather than
    an unhandled exception that kills the audit. The repo-root
    resolve goes through ``_resolve_strict`` so it's cached
    across every ref in the same audit run.
    """
    try:
        real_target = target.resolve(strict=True)
        real_root = _resolve_strict(repo_root)
    except (OSError, RuntimeError) as exc:
        return f"symlink resolve failed for {raw_path}: {exc}"
    if not real_target.is_relative_to(real_root):
        return f"resolves outside repo: {raw_path} → {real_target}"
    return ""


@functools.lru_cache(maxsize=128)
def _resolve_strict(path: Path) -> Path:
    """Cached ``Path.resolve(strict=True)``.

    Every call to ``_check_contained`` resolves the repo root;
    during an audit the root never changes, so re-statting the
    filesystem per ref wastes cycles. ``lru_cache`` keyed on the
    hashable ``Path`` argument shares the resolved value across
    refs. Bounded at 128 so a long-running process doesn't
    accumulate resolved-path memory indefinitely — the audit
    tool itself is one-shot, but callers embedding it in a
    longer service benefit from the bound.

    Exceptions from ``resolve`` are NOT cached: lru_cache only
    stores return values, so a failing resolve re-raises on the
    next call — the caller wants fresh feedback if the
    filesystem state changes.
    """
    return path.resolve(strict=True)


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
    """Module-level names plus class-body names in ``tree``.

    Deliberately does NOT walk into function bodies: a function-
    local ``data = ...`` is not a module symbol, and matching it
    would false-positive on common loop-variable names (``x``,
    ``i``, ``data``) that reviewers never intend as refs. Matches
    the regex fallback's line-start-only scope, which was the
    previous inconsistency Gemini flagged.
    """
    names: set[str] = set()
    if not isinstance(tree, ast.Module):
        return names
    for stmt in tree.body:
        names.update(_names_from_stmt(stmt))
    return names


_CONTAINER_STMTS = (
    ast.If, ast.Try, ast.With, ast.AsyncWith,
    ast.For, ast.AsyncFor, ast.While,
)


def _names_from_stmt(stmt: ast.stmt) -> set[str]:
    """Names a statement defines in its enclosing scope.

    Function and class statements contribute their own name.
    Control-flow containers (``if``/``try``/``with``/``for``/
    ``while``) don't introduce a name themselves, but their
    child bodies still run at the enclosing scope — a
    version-gated import or a try/except fallback import is a
    real module-level symbol, so we recurse into container
    bodies rather than stopping at the container boundary.
    Function bodies remain opaque: local variables inside
    ``def foo(): ...`` don't leak out as module symbols.
    """
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {stmt.name}
    if isinstance(stmt, ast.ClassDef):
        return _names_from_classdef(stmt)
    if isinstance(stmt, _CONTAINER_STMTS):
        return _names_from_container(stmt)
    return _names_from_assign_like(stmt)


def _names_from_container(stmt: ast.stmt) -> set[str]:
    """Collect names from every child statement-list inside a
    control-flow block. Recurses via ``_names_from_stmt`` so
    nested containers and ``def`` / ``class`` definitions inside
    the block are captured at the same scope they'd have at
    runtime."""
    names: set[str] = set()
    for child in _container_children(stmt):
        names.update(_names_from_stmt(child))
    return names


def _container_children(stmt: ast.stmt) -> Iterator[ast.stmt]:
    """Yield every statement directly contained in ``stmt``'s
    body-like attributes.

    Handles the union of body-bearing fields across the
    container types: ``body`` (all), ``orelse`` (If/For/While/
    Try), ``finalbody`` (Try), plus the ``handlers`` list of
    ExceptHandlers on Try (each handler itself has a ``body``).
    ``getattr(..., ())`` lets the same function handle every
    container type without an isinstance ladder.
    """
    for attr in ("body", "orelse", "finalbody"):
        for child in getattr(stmt, attr, ()):
            yield child
    for handler in getattr(stmt, "handlers", ()):
        yield from handler.body


def _names_from_classdef(stmt: ast.ClassDef) -> set[str]:
    """Class name plus every statement directly inside its body
    (methods, class attributes). Recurses via ``_names_from_stmt``
    so a nested class still contributes its own name plus its
    methods — an ontology ref to a nested-class method is rare
    but valid."""
    names = {stmt.name}
    for inner in stmt.body:
        names.update(_names_from_stmt(inner))
    return names


def _names_from_assign_like(stmt: ast.stmt) -> set[str]:
    """Names introduced by a plain or annotated assignment at
    statement scope."""
    if isinstance(stmt, ast.Assign):
        return _names_from_assign_targets(stmt)
    if isinstance(stmt, ast.AnnAssign) and isinstance(
        stmt.target, ast.Name,
    ):
        return {stmt.target.id}
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
    ``def <sym>`` / ``async def <sym>`` / ``class <sym>`` /
    line-start ``<sym> =`` / line-start ``<sym>: ``. Async-def
    support mirrors the AST path's ``AsyncFunctionDef`` handling
    so the fallback doesn't regress coverage on syntax-broken
    files that still define async functions."""
    esc = re.escape(symbol)
    def_or_class = (
        rf"(?:\b(?:async\s+)?def\s+|\bclass\s+){esc}\b"
    )
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
