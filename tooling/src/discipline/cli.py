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
import sys
from dataclasses import dataclass
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


@dataclass(frozen=True)
class PrintOptions:
    """Per-invocation options resolved from argparse."""

    repo_root: Path
    show_schemas: bool
    show_decisions: bool
    show_requirements: bool
    cap_bytes: int


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
        default=Path.cwd(),
        help="Repo root (defaults to cwd).",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Always returns 0."""
    ns = parse_args(argv)
    opts = _options_from_namespace(ns)
    output = render_context(ns.path, opts)
    sys.stdout.write(output)
    return 0


def render_context(rel_path: str, opts: PrintOptions) -> str:
    """Assemble the printed context for one touched path."""
    domains = matching_domains(rel_path)
    if not domains:
        return (
            f"# discipline-print: no canonical context for {rel_path}\n"
        )
    header = f"# discipline-print: canonical context for {rel_path}\n"
    sections: list[str] = [header]
    for domain in domains:
        sections.append(_render_domain(domain, rel_path, opts))
    return "\n".join(sections)


def _options_from_namespace(ns: argparse.Namespace) -> PrintOptions:
    """Resolve --schemas/--decisions/--requirements (no flags = all)."""
    any_flag = ns.schemas or ns.decisions or ns.requirements
    return PrintOptions(
        repo_root=ns.repo_root,
        show_schemas=ns.schemas or not any_flag,
        show_decisions=ns.decisions or not any_flag,
        show_requirements=ns.requirements or not any_flag,
        cap_bytes=ns.cap_bytes,
    )


def _render_domain(
    domain: Domain,
    rel_path: str,
    opts: PrintOptions,
) -> str:
    """Render one domain's context (schemas + decisions + requirements)."""
    parts: list[str] = [f"\n## domain: {domain.name}\n"]
    if opts.show_schemas:
        parts.append(_render_schemas(domain, rel_path, opts))
    if opts.show_decisions:
        parts.append(_render_decisions(domain, opts))
    if opts.show_requirements:
        parts.append(_render_requirements(domain, opts))
    return "".join(parts)


def _render_schemas(
    domain: Domain,
    rel_path: str,
    opts: PrintOptions,
) -> str:
    """Render every resolved schema block for the domain."""
    blocks = resolve_blocks(domain, rel_path)
    if not blocks:
        return ""
    parts: list[str] = ["\n### schemas\n"]
    for block in blocks:
        parts.append(_render_one_block(block, opts))
    return "".join(parts)


def _render_one_block(block: ResolvedBlock, opts: PrintOptions) -> str:
    """Render one resolved schema block (or an inline error note)."""
    full = opts.repo_root / block.file
    header = f"\n#### {block.file} :: {block.block_name}\n"
    text = _read_text(full)
    if text is None:
        return header + f"(file not found: {block.file})\n"
    extracted = mk_mod.extract_block(text, block.block_name)
    if isinstance(extracted, mk_mod.MarkerError):
        return header + f"(marker error: {extracted.reason})\n"
    body = "\n".join(extracted) + "\n"
    return header + _cap_text(body, opts.cap_bytes, block.file)


def _render_decisions(domain: Domain, opts: PrintOptions) -> str:
    """Render the requested decision entries from DECISIONS.md."""
    if not domain.decisions:
        return ""
    text = _read_text(opts.repo_root / _DECISIONS_FILE)
    if text is None:
        return f"\n### decisions\n(file not found: {_DECISIONS_FILE})\n"
    entries = dec_mod.parse_entries(text)
    parts: list[str] = ["\n### decisions\n"]
    for did in domain.decisions:
        parts.append(_render_one_decision(entries, did, opts))
    return "".join(parts)


def _render_one_decision(
    entries: list[dec_mod.Entry],
    decision_id: str,
    opts: PrintOptions,
) -> str:
    """Render one decision entry, or a not-found note."""
    entry = dec_mod.find_entry(entries, decision_id)
    if entry is None:
        return f"\n(decision {decision_id} not found in DECISIONS.md)\n"
    if entry.deprecated:
        return f"\n(decision {decision_id} is deprecated; skipped)\n"
    return "\n" + _cap_text(entry.render(), opts.cap_bytes, _DECISIONS_FILE)


def _render_requirements(domain: Domain, opts: PrintOptions) -> str:
    """Render every requirement matching the domain's prefix list."""
    if not domain.requirements_prefixes:
        return ""
    text = _read_text(opts.repo_root / _REQUIREMENTS_FILE)
    if text is None:
        return (
            "\n### requirements\n"
            f"(file not found: {_REQUIREMENTS_FILE})\n"
        )
    entries = dec_mod.parse_entries(text)
    parts: list[str] = ["\n### requirements\n"]
    for prefix in domain.requirements_prefixes:
        matches = dec_mod.find_by_prefix(entries, prefix)
        for entry in matches:
            parts.append(
                "\n" + _cap_text(
                    entry.render(),
                    opts.cap_bytes,
                    _REQUIREMENTS_FILE,
                )
            )
    return "".join(parts)


def _read_text(path: Path) -> str | None:
    """Read a file, returning None if it doesn't exist."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _cap_text(text: str, cap_bytes: int, source_label: str) -> str:
    """Truncate `text` to `cap_bytes`; append a pointer if truncated."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return text
    truncated = encoded[:cap_bytes].decode("utf-8", errors="ignore")
    pointer = f"\n... [truncated; see {source_label} for full text]\n"
    return truncated + pointer


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
