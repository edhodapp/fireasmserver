"""Tests for branch_cov.cli."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from branch_cov.cli import _print_report, main, parse_args
from branch_cov.coverage import (
    BranchOutcome,
    CoverageGap,
    CoverageReport,
)
from branch_cov.disasm import ConditionalBranch


class TestParseArgs:
    """argparse wiring."""

    def test_basic(self) -> None:
        ns = parse_args(
            ["--elf", "/tmp/g.elf", "--trace", "/tmp/t.log"],
        )
        assert ns.elf == Path("/tmp/g.elf")
        assert ns.trace == Path("/tmp/t.log")

    def test_missing_required_raises_systemexit(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--elf", "/tmp/g.elf"])

    def test_load_offset_defaults_zero(self) -> None:
        ns = parse_args(
            ["--elf", "/tmp/g.elf", "--trace", "/tmp/t.log"],
        )
        assert ns.load_offset == 0

    def test_load_offset_accepts_hex(self) -> None:
        ns = parse_args([
            "--elf", "/tmp/g.elf", "--trace", "/tmp/t.log",
            "--load-offset", "0x40080000",
        ])
        assert ns.load_offset == 0x40080000

    def test_load_offset_accepts_decimal(self) -> None:
        ns = parse_args([
            "--elf", "/tmp/g.elf", "--trace", "/tmp/t.log",
            "--load-offset", "1073741824",
        ])
        assert ns.load_offset == 1073741824


def _write_empty_elf(path: Path) -> None:
    # Minimal ELF64 header, little-endian, zero sections. Enough for
    # pyelftools to open; _code_sections will find nothing, so
    # enumerate_branches returns [].
    # Header reference: linux/elf.h e_ident + fields.
    path.write_bytes(
        b"\x7fELF"            # magic
        b"\x02"               # EI_CLASS = ELFCLASS64
        b"\x01"               # EI_DATA = little-endian
        b"\x01"               # EI_VERSION = 1
        b"\x00"               # EI_OSABI = System V
        b"\x00"               # EI_ABIVERSION
        + b"\x00" * 7         # EI_PAD
        + b"\x01\x00"         # e_type = ET_REL
        + b"\x3e\x00"         # e_machine = EM_X86_64
        + b"\x01\x00\x00\x00"  # e_version
        + b"\x00" * 8          # e_entry
        + b"\x00" * 8          # e_phoff
        + b"\x00" * 8          # e_shoff
        + b"\x00" * 4          # e_flags
        + b"\x40\x00"          # e_ehsize
        + b"\x00" * 2          # e_phentsize
        + b"\x00" * 2          # e_phnum
        + b"\x00" * 2          # e_shentsize
        + b"\x00" * 2          # e_shnum
        + b"\x00" * 2          # e_shstrndx
    )


class TestMain:
    """End-to-end CLI with temp files."""

    def test_exit_0_on_no_branches_no_trace(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        elf = tmp_path / "g.elf"
        trace = tmp_path / "t.log"
        _write_empty_elf(elf)
        trace.write_text("", encoding="utf-8")
        rc = main(["--elf", str(elf), "--trace", str(trace)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Branches: 0" in out
        assert "Gaps: 0" in out


class TestErrorPaths:
    """main() returns exit 2 with a clean message on I/O or parse errors."""

    def test_missing_elf_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        trace = tmp_path / "t.log"
        trace.write_text("", encoding="utf-8")
        rc = main([
            "--elf", str(tmp_path / "nonexistent.elf"),
            "--trace", str(trace),
        ])
        assert rc == 2
        assert "file not found" in capsys.readouterr().err

    def test_missing_trace_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        elf = tmp_path / "g.elf"
        _write_empty_elf(elf)
        rc = main([
            "--elf", str(elf),
            "--trace", str(tmp_path / "nonexistent.log"),
        ])
        assert rc == 2
        assert "file not found" in capsys.readouterr().err

    def test_malformed_trace_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        elf = tmp_path / "g.elf"
        trace = tmp_path / "t.log"
        _write_empty_elf(elf)
        trace.write_text("not-a-hex-value\n", encoding="utf-8")
        rc = main(["--elf", str(elf), "--trace", str(trace)])
        assert rc == 2
        assert "invalid input" in capsys.readouterr().err

    def test_permission_error_returns_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        elf = tmp_path / "g.elf"
        trace = tmp_path / "t.log"
        _write_empty_elf(elf)
        trace.write_text("", encoding="utf-8")
        elf.chmod(0o000)
        try:
            rc = main(["--elf", str(elf), "--trace", str(trace)])
        finally:
            elf.chmod(0o644)  # so tmp_path can clean up
        assert rc == 2
        assert "permission denied" in capsys.readouterr().err


class TestLoadOffsetApplied:
    """main() subtracts --load-offset from every trace PC before matching."""

    def test_nonzero_offset_does_not_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # An empty ELF has no branches; any trace including a huge offset
        # should still produce a fully-covered (0 branches) report.
        elf = tmp_path / "g.elf"
        trace = tmp_path / "t.log"
        _write_empty_elf(elf)
        # PCs that would be "runtime" addresses above our offset; after
        # subtraction they become low-range values. No branches to match,
        # so all we're testing is that the subtraction path runs cleanly.
        trace.write_text("0x40080040\n0x40080044\n", encoding="utf-8")
        rc = main([
            "--elf", str(elf),
            "--trace", str(trace),
            "--load-offset", "0x40080000",
        ])
        assert rc == 0
        assert "Branches: 0" in capsys.readouterr().out


class TestModuleInvocation:
    """`python -m branch_cov` enters through __main__.py."""

    def test_m_invocation_returns_zero_on_empty_elf(
        self, tmp_path: Path,
    ) -> None:
        elf = tmp_path / "g.elf"
        trace = tmp_path / "t.log"
        _write_empty_elf(elf)
        trace.write_text("", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable, "-m", "branch_cov",
                "--elf", str(elf),
                "--trace", str(trace),
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0


class TestPrintReport:
    """Report-printing format."""

    def test_no_gaps(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        report = CoverageReport(
            total_branches=0, observed_outcomes=0, gaps=[],
        )
        _print_report(report)
        out = capsys.readouterr().out
        assert "Branches: 0" in out
        assert "Gaps: 0" in out

    def test_with_gap(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        branch = ConditionalBranch(
            addr=0x1234,
            insn_size=2,
            target_taken=0x1250,
            target_not_taken=0x1236,
            mnemonic="jne",
        )
        gap = CoverageGap(
            branch=branch, missing=BranchOutcome.TAKEN,
        )
        report = CoverageReport(
            total_branches=1, observed_outcomes=1, gaps=[gap],
        )
        _print_report(report)
        out = capsys.readouterr().out
        assert "0x1234" in out
        assert "jne" in out
        assert "taken" in out
