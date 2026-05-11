"""Command-line interface for `discipline-print`.

Usage:
    discipline-print <touched-path> [--schemas|--decisions|--requirements]
    discipline-print <touched-path> [--repo-root <path>] [--cap-bytes N]

Reads the touched path, looks up matching domain(s), and prints
the relevant canonical context to stdout. Always exits 0; missing
context emits a one-line note. Per-section truncation keeps the
output bounded; truncated sections emit a `... see <file>` pointer.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from discipline import decisions as dec_mod
from discipline import markers as mk_mod
from discipline.relevance import (
    Domain,
    ResolvedBlock,
    matching_domains,
    resolve_blocks,
)

DEFAULT_CAP_BYTES = 32_768
_DECISIONS_FILE = "DECISIONS.md"
_REQUIREMENTS_FILE = "REQUIREMENTS.md"

# 1 MiB hard cap on any single file read. The canonical files
# (DECISIONS.md ~70 KiB, REQUIREMENTS.md ~30 KiB, schema-bearing
# source files much smaller) are well under this — the cap is a
# guardrail against a domain glob accidentally matching a generated
# artifact or large log file and OOMing the tool.
_MAX_READ_BYTES = 1_048_576


class _FileTooLargeError(OSError):
    """Read aborted because the file exceeds `_MAX_READ_BYTES`."""

    def __init__(self, size: int, cap: int) -> None:
        super().__init__()
        self.size = size
        self.cap = cap

    def __str__(self) -> str:
        return f"{self.size} bytes exceeds {self.cap}-byte cap"


# Read failures: file-system errors (the OSError family), the
# size-cap subclass above, or a UnicodeDecodeError when a domain
# glob lands on a binary or non-UTF-8 file. Callers isinstance-check
# against this union.
_ReadFailure = OSError | UnicodeDecodeError


@dataclass(frozen=True)
class PrintOptions:
    """Per-invocation options resolved from argparse."""

    repo_root: Path
    show_schemas: bool
    show_decisions: bool
    show_requirements: bool
    cap_bytes: int


@dataclass
class RenderState:
    """Mutable per-render scratch: error sentinels and file/parse caches.

    Marker errors, missing files, and missing decision IDs all signal
    canonical-context drift and land in `errors`. Deprecated entries
    are expected and traced explicitly — they do NOT count as errors.

    `read()` and `parsed_entries()` memoize results so the same file
    is not re-opened or re-parsed when multiple domains or schema
    blocks touch it within one render pass.
    """

    errors: list[str] = field(default_factory=list)
    _text_cache: "dict[Path, str | _ReadFailure]" = field(
        default_factory=dict,
    )
    _entries_cache: dict[Path, "list[dec_mod.Entry]"] = field(
        default_factory=dict,
    )

    def read(self, path: Path) -> "str | _ReadFailure":
        """Read `path` once per render; subsequent calls hit the cache."""
        if path not in self._text_cache:
            self._text_cache[path] = _read_text(path)
        return self._text_cache[path]

    def parsed_entries(
        self, path: Path,
    ) -> "list[dec_mod.Entry] | _ReadFailure":
        """Parse entries from `path`; subsequent calls hit the cache."""
        text = self.read(path)
        if isinstance(text, _ReadFailure):
            return text
        if path not in self._entries_cache:
            self._entries_cache[path] = dec_mod.parse_entries(text)
        return self._entries_cache[path]


@dataclass(frozen=True)
class RenderResult:
    """Outcome of one render pass — text plus error sentinels."""

    text: str
    errors: tuple[str, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the discipline-print CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="discipline-print",
        description=(
            "Print canonical context for a touched path. "
            "Always exits 0."
        ),
    )
    parser.add_argument(
        "path",
        help="The touched path, relative to the repo root.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "Repo root. Defaults to walking up from cwd to find a "
            "`.git` marker; falls back to cwd if none is found."
        ),
    )
    parser.add_argument(
        "--schemas",
        action="store_true",
        help="Show only schema blocks.",
    )
    parser.add_argument(
        "--decisions",
        action="store_true",
        help="Show only decision entries.",
    )
    parser.add_argument(
        "--requirements",
        action="store_true",
        help="Show only requirement entries.",
    )
    parser.add_argument(
        "--cap-bytes",
        type=int,
        default=DEFAULT_CAP_BYTES,
        help=f"Per-section cap in bytes (default {DEFAULT_CAP_BYTES}).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit non-zero if any inline error note is emitted "
            "(missing file, marker drift, or unknown decision ID). "
            "Deprecated entries are not errors."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 by default; non-zero only with --strict."""
    ns = parse_args(argv)
    opts = _options_from_namespace(ns)
    rel_path = _normalize_path(ns.path, opts.repo_root)
    result = render_full(rel_path, opts)
    try:
        sys.stdout.write(result.text)
    except BrokenPipeError:
        _silence_broken_pipe()
    if ns.strict and result.errors:
        return 1
    return 0


def _normalize_path(raw_path: str, repo_root: Path) -> str:
    """Normalize a touched-path argument to a repo-relative posix string.

    Resolves both the touched path and `repo_root` so symlinks and
    `..` segments are handled consistently. For a relative input,
    considers both cwd-relative (user intent when invoking from a
    subdir with a discovered repo-root) and repo-root-relative
    (tests and scripted invocations that pass a repo-rooted path)
    candidates. Prefers a candidate that actually exists; otherwise
    falls back to the last inside-repo candidate (the repo-rooted
    one for relative input) — so a "file about to be created" path
    still produces a sensible relative form. Paths that resolve
    outside `repo_root` fall back to the input posix form with
    `./` stripped — relevance matching then returns no domains and
    the caller emits a "no canonical context" note.
    """
    p = Path(raw_path)
    repo_resolved = repo_root.resolve()
    if p.is_absolute():
        candidates: list[Path] = [p]
    else:
        candidates = [Path.cwd() / p, repo_root / p]
    rel = _pick_inside_repo(candidates, repo_resolved)
    if rel is not None:
        return rel.as_posix()
    parts = [s for s in p.parts if s != "."]
    if not parts:
        return p.as_posix()
    return Path(*parts).as_posix()


def _pick_inside_repo(
    candidates: list[Path],
    repo_resolved: Path,
) -> "Path | None":
    """Pick the best inside-repo candidate (existing preferred) or None."""
    fallback: Path | None = None
    for c in candidates:
        try:
            resolved = c.resolve()
            rel = resolved.relative_to(repo_resolved)
        except ValueError:
            continue
        if resolved.exists():
            return rel
        fallback = rel
    return fallback


def _silence_broken_pipe() -> None:
    """Redirect stdout to /dev/null after a BrokenPipeError.

    This is the canonical recipe from the Python signal docs
    (https://docs.python.org/3/library/signal.html#note-on-sigpipe):
    after BrokenPipeError, dup /dev/null onto stdout's fd so the
    interpreter-shutdown stdout flush has somewhere benign to write.

    Trade-off: the redirect is process-global. If `main()` is called
    from a long-lived host (e.g., a test runner that captures stdout
    via fd manipulation rather than sys.stdout swap), subsequent
    stdout writes by the host will land in /dev/null. We accept this
    because (a) at the point of BrokenPipe the original consumer is
    gone, so there is nothing useful to flush; (b) `main()` is a
    leaf CLI entry point — the documented embedding pattern is
    subprocess, not in-process call; and (c) under pytest's stdout
    capture the fd path raises `io.UnsupportedOperation` and we fall
    back to swapping `sys.stdout` for an in-memory sink, which is
    test-safe.
    """
    try:
        fd = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, fd)
        finally:
            os.close(devnull)
    except (AttributeError, io.UnsupportedOperation, OSError):
        sys.stdout = io.StringIO()


def render_context(rel_path: str, opts: PrintOptions) -> str:
    """Assemble the printed context for one touched path (text only)."""
    return render_full(rel_path, opts).text


def render_full(rel_path: str, opts: PrintOptions) -> RenderResult:
    """Assemble printed context plus the error sentinels emitted."""
    domains = matching_domains(rel_path)
    if not domains:
        return RenderResult(
            text=(
                f"# discipline-print: no canonical context for "
                f"{rel_path}\n"
            ),
            errors=(),
        )
    state = RenderState()
    header = f"# discipline-print: canonical context for {rel_path}\n"
    sections: list[str] = [header]
    for domain in domains:
        sections.append(_render_domain(domain, rel_path, opts, state))
    return RenderResult(text="\n".join(sections), errors=tuple(state.errors))


def _options_from_namespace(ns: argparse.Namespace) -> PrintOptions:
    """Resolve --schemas/--decisions/--requirements (no flags = all)."""
    any_flag = ns.schemas or ns.decisions or ns.requirements
    repo_root = (
        ns.repo_root if ns.repo_root is not None else _find_repo_root()
    )
    return PrintOptions(
        repo_root=repo_root,
        show_schemas=ns.schemas or not any_flag,
        show_decisions=ns.decisions or not any_flag,
        show_requirements=ns.requirements or not any_flag,
        cap_bytes=ns.cap_bytes,
    )


def _find_repo_root() -> Path:
    """Walk up from cwd looking for a `.git` marker (dir or file).

    A `.git` file (rather than directory) is what `git worktree`
    creates, so we accept either. Falls back to cwd if no marker is
    found in the ancestor chain — relevance matching then fails on
    the touched path and the caller emits "no canonical context."
    """
    cwd = Path.cwd().resolve()
    for ancestor in (cwd, *cwd.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return cwd


def _render_domain(
    domain: Domain,
    rel_path: str,
    opts: PrintOptions,
    state: RenderState,
) -> str:
    """Render one domain's context (schemas + decisions + requirements)."""
    parts: list[str] = [f"\n## domain: {domain.name}\n"]
    if opts.show_schemas:
        parts.append(_render_schemas(domain, rel_path, opts, state))
    if opts.show_decisions:
        parts.append(_render_decisions(domain, opts, state))
    if opts.show_requirements:
        parts.append(_render_requirements(domain, opts, state))
    return "".join(parts)


def _render_schemas(
    domain: Domain,
    rel_path: str,
    opts: PrintOptions,
    state: RenderState,
) -> str:
    """Render every resolved schema block for the domain."""
    blocks = resolve_blocks(domain, rel_path)
    if not blocks:
        return ""
    parts: list[str] = ["\n### schemas\n"]
    for block in blocks:
        parts.append(_render_one_block(block, opts, state))
    return "".join(parts)


def _render_one_block(
    block: ResolvedBlock,
    opts: PrintOptions,
    state: RenderState,
) -> str:
    """Render one resolved schema block (or an inline error note)."""
    full = opts.repo_root / block.file
    header = f"\n#### {block.file} :: {block.block_name}\n"
    text = state.read(full)
    if isinstance(text, _ReadFailure):
        msg = _io_msg(block.file, text)
        state.errors.append(msg)
        return header + f"({msg})\n"
    extracted = mk_mod.extract_block(text, block.block_name)
    if isinstance(extracted, mk_mod.MarkerError):
        state.errors.append(
            f"marker error in {block.file}::{block.block_name}: "
            f"{extracted.reason}"
        )
        return header + f"(marker error: {extracted.reason})\n"
    if not extracted:
        return header + "(empty block)\n"
    body = "\n".join(extracted) + "\n"
    return header + _cap_text(body, opts.cap_bytes, block.file)


def _render_decisions(
    domain: Domain,
    opts: PrintOptions,
    state: RenderState,
) -> str:
    """Render the requested decision entries from DECISIONS.md.

    The `### decisions` header is always emitted when the domain
    declares at least one decision id, even when every id resolves to
    a "(not found)" or "(deprecated; skipped)" note. This asymmetry
    vs. `_render_requirement_entries` (which suppresses an empty
    header) is intentional: a domain's explicit decision-id list is
    a contract — a missing id is a signal, and the user benefits
    from seeing the lookup ran and what was missing. Prefix-based
    requirements, by contrast, may legitimately match zero entries
    in a domain whose work has not landed yet, where the header
    would be noise.
    """
    if not domain.decisions:
        return ""
    entries = state.parsed_entries(opts.repo_root / _DECISIONS_FILE)
    if isinstance(entries, _ReadFailure):
        msg = _io_msg(_DECISIONS_FILE, entries)
        state.errors.append(msg)
        return f"\n### decisions\n({msg})\n"
    parts: list[str] = ["\n### decisions\n"]
    for did in domain.decisions:
        parts.append(_render_one_decision(entries, did, opts, state))
    return "".join(parts)


def _render_one_decision(
    entries: list[dec_mod.Entry],
    decision_id: str,
    opts: PrintOptions,
    state: RenderState,
) -> str:
    """Render one decision entry, or an inline note."""
    entry = dec_mod.find_entry(entries, decision_id)
    if entry is None:
        state.errors.append(
            f"decision {decision_id} not found in {_DECISIONS_FILE}"
        )
        return f"\n(decision {decision_id} not found in DECISIONS.md)\n"
    if entry.deprecated:
        return f"\n(decision {decision_id} is deprecated; skipped)\n"
    return "\n" + _cap_text(entry.render(), opts.cap_bytes, _DECISIONS_FILE)


def _render_requirements(
    domain: Domain,
    opts: PrintOptions,
    state: RenderState,
) -> str:
    """Render every requirement matching the domain's prefix list.

    When two prefixes overlap (e.g. `MR-` and `MR-00`), each matched
    requirement is rendered exactly once; the first prefix that
    matches a given `entry_id` wins, and later prefix passes skip
    that id.
    """
    if not domain.requirements_prefixes:
        return ""
    entries = state.parsed_entries(opts.repo_root / _REQUIREMENTS_FILE)
    if isinstance(entries, _ReadFailure):
        msg = _io_msg(_REQUIREMENTS_FILE, entries)
        state.errors.append(msg)
        return f"\n### requirements\n({msg})\n"
    return _render_requirement_entries(entries, domain, opts)


def _render_requirement_entries(
    entries: list[dec_mod.Entry],
    domain: Domain,
    opts: PrintOptions,
) -> str:
    """Walk prefix list, render each unique matched entry once.

    The `### requirements` header is only emitted when at least one
    entry matches; an empty section header (which would clutter the
    output) is suppressed.
    """
    rendered: list[str] = []
    seen: set[str] = set()
    for prefix in domain.requirements_prefixes:
        matches = dec_mod.find_by_prefix(
            entries, prefix, include_deprecated=True,
        )
        for entry in matches:
            if entry.entry_id in seen:
                continue
            seen.add(entry.entry_id)
            rendered.append(_render_one_requirement(entry, opts))
    if not rendered:
        return ""
    return "\n### requirements\n" + "".join(rendered)


def _render_one_requirement(
    entry: dec_mod.Entry,
    opts: PrintOptions,
) -> str:
    """Render one requirement entry; deprecated entries get an annotation."""
    if entry.deprecated:
        return (
            f"\n(requirement {entry.entry_id} is deprecated; skipped)\n"
        )
    return "\n" + _cap_text(
        entry.render(), opts.cap_bytes, _REQUIREMENTS_FILE,
    )


def _read_text(path: Path) -> "str | _ReadFailure":
    """Read a file; return its text, or the failure if I/O/decode failed.

    Catches the full OSError family — FileNotFoundError (the typical
    "missing canonical file" case), IsADirectoryError (a directory at
    a path that expected a file), PermissionError, etc. — and also
    UnicodeDecodeError, which fires if a domain glob accidentally
    matches a binary or non-UTF-8 file.

    Defense-in-depth against OOM: the `stat`-based fast path refuses
    files known to exceed `_MAX_READ_BYTES` without opening them at
    all, AND the actual read is bounded to `_MAX_READ_BYTES + 1`
    bytes so a TOCTOU race (file grew after stat) still cannot
    breach the cap. Callers isinstance-check the result.
    """
    guard = _read_size_guard(path)
    if guard is not None:
        return guard
    return _read_bounded(path)


def _read_size_guard(path: Path) -> "_ReadFailure | None":
    """Stat `path`; return a failure if oversized or if stat itself failed.

    Returns None when the file is within `_MAX_READ_BYTES` per its
    current stat (a fast path) — the bounded read in `_read_bounded`
    is what actually enforces the cap.
    """
    try:
        size = path.stat().st_size
    except OSError as err:
        return err
    if size > _MAX_READ_BYTES:
        return _FileTooLargeError(size, _MAX_READ_BYTES)
    return None


def _read_bounded(path: Path) -> "str | _ReadFailure":
    """Open `path` and read up to `_MAX_READ_BYTES + 1` bytes; decode UTF-8.

    The +1 lets us detect the case where the file grew past the cap
    between stat and read (TOCTOU): if we got more than `_MAX_READ_BYTES`,
    the file is at least that long and we refuse it.
    """
    try:
        with path.open("rb") as f:
            raw = f.read(_MAX_READ_BYTES + 1)
    except OSError as err:
        return err
    if len(raw) > _MAX_READ_BYTES:
        return _FileTooLargeError(len(raw), _MAX_READ_BYTES)
    return _decode_utf8(raw)


def _decode_utf8(raw: bytes) -> "str | UnicodeDecodeError":
    """Decode `raw` as UTF-8; return the UnicodeDecodeError on failure."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as err:
        return err


def _io_msg(label: str, err: "_ReadFailure") -> str:
    """Format a read failure into a one-line inline note."""
    if isinstance(err, FileNotFoundError):
        return f"file not found: {label}"
    if isinstance(err, _FileTooLargeError):
        return f"file too large: {label} — {err}"
    if isinstance(err, UnicodeDecodeError):
        return f"file unreadable: {label} — not valid UTF-8"
    reason = err.strerror or type(err).__name__
    return f"file unreadable: {label} — {reason}"


def _cap_text(text: str, cap_bytes: int, source_label: str) -> str:
    """Truncate `text` to `cap_bytes`; append a pointer if truncated.

    The cut point is the last newline at or before `cap_bytes` so the
    truncated chunk ends on a clean line boundary; this also avoids
    splitting a multi-byte UTF-8 character at the cap. If no newline
    sits before the cap, fall back to a raw byte cut with
    `errors="ignore"` so a partial multi-byte sequence is dropped
    rather than rendered as mojibake.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return text
    cut = encoded.rfind(b"\n", 0, cap_bytes)
    if cut < 0:
        truncated = encoded[:cap_bytes].decode("utf-8", errors="ignore")
    else:
        truncated = encoded[: cut + 1].decode("utf-8", errors="ignore")
    pointer = f"\n... [truncated; see {source_label} for full text]\n"
    return truncated + pointer


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
