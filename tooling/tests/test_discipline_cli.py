"""Tests for discipline.cli."""

from __future__ import annotations

from pathlib import Path

import pytest

from discipline.cli import (
    DEFAULT_CAP_BYTES,
    PrintOptions,
    main,
    parse_args,
    render_context,
)
from discipline.relevance import BlockSpec, Domain


@pytest.fixture(name="repo")
def _repo(tmp_path: Path) -> Path:
    """Build a tiny synthetic repo with one schema, DECISIONS, REQUIREMENTS."""
    (tmp_path / "arch" / "aarch64" / "memory").mkdir(parents=True)
    (tmp_path / "arch" / "x86_64" / "memory").mkdir(parents=True)
    inc = tmp_path / "arch" / "aarch64" / "memory" / "memreq.inc"
    inc.write_text(
        "noise\n"
        "// DISCIPLINE-PRINT-START: memreq-record-fields\n"
        "  offset 0  name_hash\n"
        "// DISCIPLINE-PRINT-END: memreq-record-fields\n"
        "// DISCIPLINE-PRINT-START: memreq-macro-shape\n"
        ".macro memreq region\n"
        ".endm\n"
        "// DISCIPLINE-PRINT-END: memreq-macro-shape\n",
        encoding="utf-8",
    )
    inc_x = tmp_path / "arch" / "x86_64" / "memory" / "memreq.inc"
    inc_x.write_text(
        "; DISCIPLINE-PRINT-START: memreq-record-fields\n"
        "; offset 0 name_hash\n"
        "; DISCIPLINE-PRINT-END: memreq-record-fields\n"
        "; DISCIPLINE-PRINT-START: memreq-macro-shape\n"
        "%macro memreq 7\n"
        "%endmacro\n"
        "; DISCIPLINE-PRINT-END: memreq-macro-shape\n",
        encoding="utf-8",
    )
    (tmp_path / "tooling" / "src" / "memlayout").mkdir(parents=True)
    models = tmp_path / "tooling" / "src" / "memlayout" / "models.py"
    models.write_text(
        "# DISCIPLINE-PRINT-START: memreq-pydantic-model\n"
        "class MemoryRegion: ...\n"
        "# DISCIPLINE-PRINT-END: memreq-pydantic-model\n",
        encoding="utf-8",
    )
    (tmp_path / "DECISIONS.md").write_text(
        "preface\n"
        "### D058: Actor model\n"
        "body of D058\n"
        "### D060: Bump allocator\n"
        "body of D060\n"
        "### D065: Old\n"
        "**DEPRECATED 2026-05-01 — superseded by D066.** rationale\n"
        "### D999: Unrelated\n"
        "noise\n",
        encoding="utf-8",
    )
    (tmp_path / "REQUIREMENTS.md").write_text(
        "### MR-001: Owner identification\n"
        "shall clause for MR-001\n"
        "### MR-007: Layout\n"
        "shall clause for MR-007\n"
        "### AL-001: Init-time alloc\n"
        "shall clause for AL-001\n"
        "### BC-001: Unrelated\n"
        "should not appear\n",
        encoding="utf-8",
    )
    return tmp_path


def _opts(repo: Path, *, cap: int = DEFAULT_CAP_BYTES) -> PrintOptions:
    return PrintOptions(
        repo_root=repo,
        show_schemas=True,
        show_decisions=True,
        show_requirements=True,
        cap_bytes=cap,
    )


class TestParseArgs:
    """argparse wiring."""

    def test_path_required(self) -> None:
        with pytest.raises(SystemExit):
            parse_args([])

    def test_defaults(self) -> None:
        ns = parse_args(["arch/aarch64/memory/memreq.inc"])
        assert ns.path == "arch/aarch64/memory/memreq.inc"
        assert ns.schemas is False
        assert ns.decisions is False
        assert ns.requirements is False
        assert ns.cap_bytes == DEFAULT_CAP_BYTES

    def test_section_flags(self) -> None:
        ns = parse_args(["p", "--schemas", "--decisions", "--requirements"])
        assert ns.schemas
        assert ns.decisions
        assert ns.requirements

    def test_cap_bytes_override(self) -> None:
        ns = parse_args(["p", "--cap-bytes", "100"])
        assert ns.cap_bytes == 100


class TestRenderContext:
    """Composition of schemas + decisions + requirements."""

    def test_no_matching_domain_emits_one_line(self, repo: Path) -> None:
        out = render_context("README.md", _opts(repo))
        assert "no canonical context" in out

    def test_arch_path_prints_only_that_arch_schema(
        self, repo: Path,
    ) -> None:
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "arch/aarch64/memory/memreq.inc" in out
        assert "arch/x86_64/memory/memreq.inc" not in out
        assert ".macro memreq region" in out
        assert "name_hash" in out

    def test_python_path_expands_to_both_arches(self, repo: Path) -> None:
        out = render_context(
            "tooling/src/memlayout/models.py", _opts(repo),
        )
        assert "arch/aarch64/memory/memreq.inc" in out
        assert "arch/x86_64/memory/memreq.inc" in out
        assert "class MemoryRegion" in out

    def test_decisions_skip_deprecated(self, repo: Path) -> None:
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "body of D058" in out
        assert "body of D060" in out
        assert "deprecated; skipped" in out
        assert "rationale" not in out

    def test_decisions_missing_id_noted(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        memreq_only = (
            Domain(
                name="memreq",
                path_globs=("arch/*/memory/memreq.inc",),
                decisions=("D777",),
            ),
        )
        monkeypatch.setattr(
            "discipline.cli.matching_domains",
            lambda p: list(memreq_only)
            if "memreq" in p
            else [],
        )
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "D777 not found" in out

    def test_requirements_filter_by_prefix(self, repo: Path) -> None:
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "shall clause for MR-001" in out
        assert "shall clause for MR-007" in out
        assert "shall clause for AL-001" in out
        assert "should not appear" not in out

    def test_section_flags_filter_output(self, repo: Path) -> None:
        opts_schemas_only = PrintOptions(
            repo_root=repo,
            show_schemas=True,
            show_decisions=False,
            show_requirements=False,
            cap_bytes=DEFAULT_CAP_BYTES,
        )
        out = render_context(
            "arch/aarch64/memory/memreq.inc", opts_schemas_only,
        )
        assert "name_hash" in out
        assert "body of D058" not in out
        assert "shall clause" not in out

    def test_decisions_only_omits_schema_section(self, repo: Path) -> None:
        opts_decisions_only = PrintOptions(
            repo_root=repo,
            show_schemas=False,
            show_decisions=True,
            show_requirements=False,
            cap_bytes=DEFAULT_CAP_BYTES,
        )
        out = render_context(
            "arch/aarch64/memory/memreq.inc", opts_decisions_only,
        )
        assert "body of D058" in out
        assert "name_hash" not in out
        assert "shall clause" not in out

    def test_marker_error_inlined(self, repo: Path) -> None:
        bad = repo / "arch" / "aarch64" / "memory" / "memreq.inc"
        bad.write_text("no markers here\n", encoding="utf-8")
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "marker error" in out

    def test_missing_schema_file_inlined(self, repo: Path) -> None:
        (repo / "arch" / "aarch64" / "memory" / "memreq.inc").unlink()
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "file not found" in out

    def test_missing_decisions_file_noted(self, repo: Path) -> None:
        (repo / "DECISIONS.md").unlink()
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "file not found: DECISIONS.md" in out

    def test_missing_requirements_file_noted(self, repo: Path) -> None:
        (repo / "REQUIREMENTS.md").unlink()
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "file not found: REQUIREMENTS.md" in out

    def test_per_section_truncation_appends_pointer(
        self, repo: Path,
    ) -> None:
        out = render_context(
            "arch/aarch64/memory/memreq.inc",
            _opts(repo, cap=20),
        )
        assert "[truncated;" in out

    def test_domain_with_no_decisions_skips_section(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        empty_decisions = (
            Domain(
                name="memreq",
                path_globs=("arch/*/memory/memreq.inc",),
                requirements_prefixes=("MR-",),
            ),
        )
        monkeypatch.setattr(
            "discipline.cli.matching_domains",
            lambda _: list(empty_decisions),
        )
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "decisions" not in out.lower()

    def test_domain_with_no_requirements_skips_section(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        no_reqs = (
            Domain(
                name="memreq",
                path_globs=("arch/*/memory/memreq.inc",),
                decisions=("D058",),
            ),
        )
        monkeypatch.setattr(
            "discipline.cli.matching_domains",
            lambda _: list(no_reqs),
        )
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "shall clause" not in out

    def test_domain_with_no_schema_blocks_skips_section(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        no_schemas = (
            Domain(
                name="memreq",
                path_globs=("arch/*/memory/memreq.inc",),
                decisions=("D058",),
            ),
        )
        monkeypatch.setattr(
            "discipline.cli.matching_domains",
            lambda _: list(no_schemas),
        )
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "name_hash" not in out


class TestMain:
    """Process-level entry point — exit 0, stdout content."""

    def test_main_returns_zero(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main([
            "arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "memreq" in captured.out

    def test_main_no_match_returns_zero(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["README.md", "--repo-root", str(repo)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "no canonical context" in captured.out


class TestBlockSpecDefaults:
    """Sanity check on the BlockSpec dataclass default."""

    def test_arch_aware_defaults_false(self) -> None:
        b = BlockSpec(file="x", block_name="y")
        assert b.arch_aware is False
