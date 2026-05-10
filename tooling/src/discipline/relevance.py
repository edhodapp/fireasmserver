"""Path → canonical-context domain mapping.

The map is a declarative list of `Domain` records. Each domain
declares the file globs that activate it (the touched-path patterns),
the schema blocks worth printing (referenced by file + marker name),
the decision IDs to print, and the requirement ID prefixes to expand.

Adding a new domain = appending a `Domain(...)` to `DOMAINS`. No
runtime configuration; the map is mypy-checked source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath

_ARCH_PATTERN = re.compile(r"^arch/(aarch64|x86_64)/")
_SUPPORTED_ARCHES = ("aarch64", "x86_64")


@dataclass(frozen=True)
class BlockSpec:
    """One canonical-schema block worth printing.

    `file` may contain a `{arch}` placeholder; if `arch_aware` is
    True, the placeholder resolves to the touched path's arch (or
    expands across all arches when no arch is present in the path).
    """

    file: str
    block_name: str
    arch_aware: bool = False


@dataclass(frozen=True)
class ResolvedBlock:
    """A `BlockSpec` after `{arch}` resolution against a touched path."""

    file: str
    block_name: str


@dataclass(frozen=True)
class Domain:
    """One canonical-context domain (e.g., `memreq`)."""

    name: str
    path_globs: tuple[str, ...]
    schema_blocks: tuple[BlockSpec, ...] = field(default_factory=tuple)
    decisions: tuple[str, ...] = field(default_factory=tuple)
    requirements_prefixes: tuple[str, ...] = field(default_factory=tuple)


DOMAINS: tuple[Domain, ...] = (
    Domain(
        name="memreq",
        path_globs=(
            "arch/*/memory/memreq.inc",
            "arch/*/memory/allocator.S",
            "tooling/src/memlayout/models.py",
            "tooling/src/memlayout/reference.py",
            "tooling/src/memlayout/bytecode.py",
            "tooling/src/memlayout/types.py",
        ),
        schema_blocks=(
            BlockSpec(
                file="arch/{arch}/memory/memreq.inc",
                block_name="memreq-record-fields",
                arch_aware=True,
            ),
            BlockSpec(
                file="arch/{arch}/memory/memreq.inc",
                block_name="memreq-macro-shape",
                arch_aware=True,
            ),
            BlockSpec(
                file="tooling/src/memlayout/models.py",
                block_name="memreq-pydantic-model",
            ),
        ),
        decisions=("D058", "D059", "D060", "D063", "D064", "D065"),
        requirements_prefixes=("MR-", "AL-"),
    ),
)


def detect_arch(rel_path: str) -> str | None:
    """Return the arch slug embedded in `rel_path`, or None."""
    m = _ARCH_PATTERN.match(rel_path)
    return m.group(1) if m else None


def matching_domains(
    rel_path: str,
    domains: tuple[Domain, ...] = DOMAINS,
) -> list[Domain]:
    """Return every domain whose globs match the path."""
    p = PurePath(rel_path)
    return [d for d in domains if _path_matches_any(p, d.path_globs)]


def resolve_blocks(
    domain: Domain,
    rel_path: str,
) -> list[ResolvedBlock]:
    """Resolve `{arch}` placeholders in the domain's schema blocks."""
    arch = detect_arch(rel_path)
    out: list[ResolvedBlock] = []
    for spec in domain.schema_blocks:
        out.extend(_resolve_one(spec, arch))
    return out


def _resolve_one(
    spec: BlockSpec,
    arch: str | None,
) -> list[ResolvedBlock]:
    """Expand one BlockSpec — single-arch, all-arches, or arch-agnostic.

    Uses `str.replace("{arch}", ...)` rather than `str.format(arch=...)`
    so a path that happens to contain other curly-brace literals (e.g.
    `path/v{version}/file.inc`) does not raise KeyError; only the
    `{arch}` placeholder is honored by design.
    """
    if not spec.arch_aware:
        return [ResolvedBlock(file=spec.file, block_name=spec.block_name)]
    if arch is not None:
        return [ResolvedBlock(
            file=spec.file.replace("{arch}", arch),
            block_name=spec.block_name,
        )]
    return [
        ResolvedBlock(
            file=spec.file.replace("{arch}", a),
            block_name=spec.block_name,
        )
        for a in _SUPPORTED_ARCHES
    ]


def _path_matches_any(path: PurePath, globs: tuple[str, ...]) -> bool:
    """True if `path` matches any of `globs` via PurePath.match."""
    return any(path.match(g) for g in globs)
