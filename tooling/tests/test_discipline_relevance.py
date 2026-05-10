"""Tests for discipline.relevance."""

from __future__ import annotations

from discipline.relevance import (
    DOMAINS,
    BlockSpec,
    Domain,
    detect_arch,
    matching_domains,
    resolve_blocks,
)


class TestDetectArch:
    """Arch slug extraction from a relative path."""

    def test_aarch64(self) -> None:
        assert detect_arch("arch/aarch64/memory/memreq.inc") == "aarch64"

    def test_x86_64(self) -> None:
        assert detect_arch("arch/x86_64/memory/allocator.S") == "x86_64"

    def test_no_arch(self) -> None:
        assert detect_arch("tooling/src/memlayout/models.py") is None

    def test_unknown_arch(self) -> None:
        assert detect_arch("arch/riscv64/memory/memreq.inc") is None


class TestMatchingDomains:
    """`Domain.path_globs` match against `PurePath.match`."""

    def test_memreq_arch_path(self) -> None:
        domains = matching_domains("arch/aarch64/memory/memreq.inc")
        assert [d.name for d in domains] == ["memreq"]

    def test_memreq_python_path(self) -> None:
        domains = matching_domains("tooling/src/memlayout/models.py")
        assert [d.name for d in domains] == ["memreq"]

    def test_unrelated_path_returns_empty(self) -> None:
        assert matching_domains("README.md") == []

    def test_custom_domain_list(self) -> None:
        custom = (
            Domain(
                name="custom",
                path_globs=("foo/*.txt",),
            ),
        )
        assert matching_domains("foo/a.txt", custom)[0].name == "custom"
        assert matching_domains("bar/a.txt", custom) == []


class TestResolveBlocks:
    """`BlockSpec` `{arch}` placeholder expansion."""

    def test_arch_aware_with_arch_in_path(self) -> None:
        d = Domain(
            name="t",
            path_globs=("arch/*/memory/x.inc",),
            schema_blocks=(
                BlockSpec(
                    file="arch/{arch}/memory/x.inc",
                    block_name="b",
                    arch_aware=True,
                ),
            ),
        )
        blocks = resolve_blocks(d, "arch/aarch64/memory/x.inc")
        assert len(blocks) == 1
        assert blocks[0].file == "arch/aarch64/memory/x.inc"

    def test_arch_aware_without_arch_expands_all(self) -> None:
        d = Domain(
            name="t",
            path_globs=("p/x.py",),
            schema_blocks=(
                BlockSpec(
                    file="arch/{arch}/memory/x.inc",
                    block_name="b",
                    arch_aware=True,
                ),
            ),
        )
        blocks = resolve_blocks(d, "p/x.py")
        files = sorted(b.file for b in blocks)
        assert files == [
            "arch/aarch64/memory/x.inc",
            "arch/x86_64/memory/x.inc",
        ]

    def test_arch_agnostic_pass_through(self) -> None:
        d = Domain(
            name="t",
            path_globs=("p/x.py",),
            schema_blocks=(
                BlockSpec(file="lib/y.py", block_name="b"),
            ),
        )
        blocks = resolve_blocks(d, "arch/aarch64/memory/x.inc")
        assert len(blocks) == 1
        assert blocks[0].file == "lib/y.py"

    def test_unrelated_braces_in_file_pass_through(self) -> None:
        d = Domain(
            name="t",
            path_globs=("arch/*/memory/x.inc",),
            schema_blocks=(
                BlockSpec(
                    file="arch/{arch}/v{version}/x.inc",
                    block_name="b",
                    arch_aware=True,
                ),
            ),
        )
        blocks = resolve_blocks(d, "arch/aarch64/memory/x.inc")
        assert len(blocks) == 1
        assert blocks[0].file == "arch/aarch64/v{version}/x.inc"


class TestBundledMemreqDomain:
    """Sanity check on the shipped DOMAINS map."""

    def test_memreq_domain_present(self) -> None:
        names = [d.name for d in DOMAINS]
        assert "memreq" in names

    def test_memreq_domain_has_decisions_and_reqs(self) -> None:
        memreq = next(d for d in DOMAINS if d.name == "memreq")
        assert memreq.decisions
        assert memreq.requirements_prefixes
        assert memreq.schema_blocks
