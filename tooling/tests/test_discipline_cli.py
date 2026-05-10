"""Tests for discipline.cli."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from discipline import cli as cli_mod
from discipline.cli import (
    DEFAULT_CAP_BYTES,
    PrintOptions,
    main,
    parse_args,
    render_context,
    render_full,
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
        "### D059: Memreq sections\n"
        "body of D059\n"
        "### D060: Bump allocator\n"
        "body of D060\n"
        "### D063: Stage-1 mode switch\n"
        "body of D063\n"
        "### D064: NXE\n"
        "body of D064\n"
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

    def test_directory_at_schema_path_yields_unreadable_note(
        self, repo: Path,
    ) -> None:
        inc = repo / "arch" / "aarch64" / "memory" / "memreq.inc"
        inc.unlink()
        inc.mkdir()
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "file unreadable" in out
        assert "arch/aarch64/memory/memreq.inc" in out

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


class TestRequirementsDeprecation:
    """Symmetry with decisions: deprecated requirements get an annotation."""

    def test_deprecated_requirement_gets_annotation(
        self, repo: Path,
    ) -> None:
        (repo / "REQUIREMENTS.md").write_text(
            "### MR-001: Active\n"
            "shall clause for MR-001\n"
            "### MR-002: Old\n"
            "**DEPRECATED 2026-04-01 — superseded.** rationale\n"
            "### MR-003: New\n"
            "shall clause for MR-003\n",
            encoding="utf-8",
        )
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert "shall clause for MR-001" in out
        assert "shall clause for MR-003" in out
        assert "requirement MR-002 is deprecated; skipped" in out
        assert "rationale" not in out


class TestMain:
    """Process-level entry point — exit codes, stdout content."""

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


class TestStrictMode:
    """--strict makes inline error notes fail the run."""

    def test_strict_zero_when_clean(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main([
            "arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
            "--strict",
        ])
        assert rc == 0
        capsys.readouterr()

    def test_strict_zero_when_only_deprecated_skipped(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main([
            "arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
            "--strict",
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "decision D065 is deprecated; skipped" in captured.out

    def test_strict_nonzero_on_missing_decisions_file(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        (repo / "DECISIONS.md").unlink()
        rc = main([
            "arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
            "--strict",
        ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "file not found: DECISIONS.md" in captured.out

    def test_strict_nonzero_on_marker_drift(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        bad = repo / "arch" / "aarch64" / "memory" / "memreq.inc"
        bad.write_text("no markers here\n", encoding="utf-8")
        rc = main([
            "arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
            "--strict",
        ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "marker error" in captured.out

    def test_strict_nonzero_on_missing_decision_id(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
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
            lambda _: list(memreq_only),
        )
        rc = main([
            "arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
            "--strict",
        ])
        assert rc == 1
        captured = capsys.readouterr()
        assert "D777 not found" in captured.out

    def test_no_strict_returns_zero_even_with_errors(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        (repo / "DECISIONS.md").unlink()
        rc = main([
            "arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "file not found: DECISIONS.md" in captured.out


class TestRenderFull:
    """render_full exposes errors alongside text for programmatic use."""

    def test_clean_run_has_no_errors(self, repo: Path) -> None:
        result = render_full(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert not result.errors
        assert "memreq" in result.text

    def test_marker_error_recorded(self, repo: Path) -> None:
        bad = repo / "arch" / "aarch64" / "memory" / "memreq.inc"
        bad.write_text("no markers here\n", encoding="utf-8")
        result = render_full(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert any("marker error" in e for e in result.errors)

    def test_deprecated_does_not_record_error(self, repo: Path) -> None:
        result = render_full(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert all("deprecated" not in e for e in result.errors)


class TestBrokenPipe:
    """`discipline-print … | head` must not crash with BrokenPipeError."""

    def test_main_survives_broken_pipe(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def raise_broken_pipe(*_args: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        monkeypatch.setattr(sys.stdout, "write", raise_broken_pipe)
        rc = main([
            "arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
        ])
        assert rc == 0
        capsys.readouterr()


class TestPrefixDedup:
    """Overlapping requirement prefixes must not duplicate output."""

    def test_overlapping_prefixes_dedupe_by_entry_id(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        overlap = (
            Domain(
                name="memreq",
                path_globs=("arch/*/memory/memreq.inc",),
                requirements_prefixes=("MR-", "MR-00"),
            ),
        )
        monkeypatch.setattr(
            "discipline.cli.matching_domains",
            lambda _: list(overlap),
        )
        out = render_context(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        assert out.count("### MR-001:") == 1
        assert out.count("### MR-007:") == 1


class TestPathNormalization:
    """CLI normalizes absolute and `./`-prefixed paths against repo root."""

    def test_main_handles_absolute_path(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        abs_path = str(repo / "arch" / "aarch64" / "memory" / "memreq.inc")
        rc = main([abs_path, "--repo-root", str(repo)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "arch/aarch64/memory/memreq.inc" in captured.out
        assert abs_path not in captured.out

    def test_main_handles_dot_slash_prefix(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main([
            "./arch/aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "for arch/aarch64/memory/memreq.inc" in captured.out
        assert "for ./arch" not in captured.out

    def test_main_path_outside_repo_falls_back_to_input(
        self, repo: Path, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("not in repo", encoding="utf-8")
        rc = main([str(outside), "--repo-root", str(repo)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "no canonical context" in captured.out

    def test_main_handles_absolute_path_via_symlink(
        self, repo: Path, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        link = tmp_path / "repo_link"
        link.symlink_to(repo)
        rc = main([
            str(link / "arch" / "aarch64" / "memory" / "memreq.inc"),
            "--repo-root", str(repo),
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "for arch/aarch64/memory/memreq.inc" in captured.out

    def test_main_handles_relative_dotdot_inside_repo(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main([
            "arch/aarch64/../aarch64/memory/memreq.inc",
            "--repo-root", str(repo),
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "for arch/aarch64/memory/memreq.inc" in captured.out

    def test_main_relative_dotdot_escapes_repo(
        self, repo: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main([
            "../outside/file.txt",
            "--repo-root", str(repo),
        ])
        assert rc == 0
        captured = capsys.readouterr()
        assert "no canonical context" in captured.out


class TestCapTextBoundary:
    """`_cap_text` truncates at the last newline ≤ cap."""

    # pylint: disable=protected-access

    def test_truncation_at_newline(self) -> None:
        text = "line one\nline two\nline three\nline four\n"
        # cap_bytes that lands in the middle of "line three"
        out = cli_mod._cap_text(text, 20, "test")
        assert out.startswith("line one\nline two\n")
        assert "line three" not in out.split("[truncated;")[0]
        assert "[truncated;" in out

    def test_no_newline_falls_back_to_byte_cut(self) -> None:
        text = "abcdefghij"
        out = cli_mod._cap_text(text, 5, "test")
        assert out.startswith("abcde")
        assert "[truncated;" in out

    def test_multibyte_not_split(self) -> None:
        # Two-byte char: é → 0xC3 0xA9 in UTF-8. Cap mid-byte must not
        # emit mojibake; the `errors="ignore"` plus newline-pref policy
        # keeps the output clean.
        text = "x\né"
        out = cli_mod._cap_text(text, 2, "test")
        assert "�" not in out  # no replacement chars
        # Truncated at "\n", so output starts with "x\n"
        assert out.startswith("x\n")


class TestRenderStateCaching:
    """Same file is read and parsed once per render."""

    # pylint: disable=protected-access

    def test_repeated_file_reads_collapse(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reads: list[str] = []
        real = cli_mod._read_text

        def counting(path: Path) -> "str | OSError":
            reads.append(str(path))
            return real(path)

        monkeypatch.setattr("discipline.cli._read_text", counting)
        render_full(
            "arch/aarch64/memory/memreq.inc", _opts(repo),
        )
        # The memreq domain has two schema blocks (record + macro)
        # in the same .inc file; without caching, that file would be
        # read twice. Caching collapses it to one.
        memreq_reads = [r for r in reads if r.endswith("memreq.inc")]
        assert len(memreq_reads) == 1


class TestBlockSpecDefaults:
    """Sanity check on the BlockSpec dataclass default."""

    def test_arch_aware_defaults_false(self) -> None:
        b = BlockSpec(file="x", block_name="y")
        assert b.arch_aware is False
