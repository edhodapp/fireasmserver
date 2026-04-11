"""Tests for cli module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from qemu_harness.cli import (
    _load_suite,
    _print_result,
    main,
    parse_args,
)
from qemu_harness.test_runner import (
    SuiteResult,
    TestResult,
)


class TestParseArgs:
    """Tests for parse_args()."""

    def test_suite_required(self) -> None:
        with pytest.raises(SystemExit):
            parse_args([])

    def test_suite_path(self) -> None:
        args = parse_args(["--suite", "test.json"])
        assert args.suite == "test.json"

    def test_arch_filter(self) -> None:
        args = parse_args([
            "--suite", "t.json", "--arch", "x86_64",
        ])
        assert args.arch == "x86_64"

    def test_platform_filter(self) -> None:
        args = parse_args([
            "--suite", "t.json", "--platform", "qemu",
        ])
        assert args.platform == "qemu"

    def test_build_dir(self) -> None:
        args = parse_args([
            "--suite", "t.json", "--build-dir", "/out",
        ])
        assert args.build_dir == "/out"

    def test_defaults(self) -> None:
        args = parse_args(["--suite", "t.json"])
        assert args.arch is None
        assert args.platform is None
        assert args.build_dir is None


class TestLoadSuite:
    """Tests for _load_suite()."""

    def test_valid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "suite.json"
        p.write_text(
            '{"arch":"x86_64","platform":"qemu",'
            '"source_dir":"/src","cases":[]}'
        )
        suite = _load_suite(str(p))
        assert suite.arch == "x86_64"

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json")
        with pytest.raises(Exception):
            _load_suite(str(p))


class TestPrintResult:
    """Tests for _print_result()."""

    def test_all_pass(self, capsys: pytest.CaptureFixture[str]) -> None:
        sr = SuiteResult(
            arch="x86_64", platform="qemu",
            results=[TestResult(name="t1", passed=True)],
        )
        _print_result(sr)
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "ALL PASSED" in out

    def test_failure(self, capsys: pytest.CaptureFixture[str]) -> None:
        sr = SuiteResult(
            arch="x86_64", platform="qemu",
            results=[TestResult(
                name="t1", passed=False, message="bad",
            )],
        )
        _print_result(sr)
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "bad" in out


class TestMain:
    """Tests for main()."""

    @patch("qemu_harness.cli.run_suite")
    @patch("qemu_harness.cli._load_suite")
    def test_all_pass_returns_zero(
        self, mock_load: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        mock_load.return_value = MagicMock(
            arch="x86_64", platform="qemu",
        )
        mock_run.return_value = SuiteResult(
            arch="x86_64", platform="qemu",
            results=[TestResult(name="t", passed=True)],
        )
        result = main(["--suite", "t.json"])
        assert result == 0

    @patch("qemu_harness.cli.run_suite")
    @patch("qemu_harness.cli._load_suite")
    def test_failure_returns_one(
        self, mock_load: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        mock_load.return_value = MagicMock(
            arch="x86_64", platform="qemu",
        )
        mock_run.return_value = SuiteResult(
            arch="x86_64", platform="qemu",
            results=[TestResult(name="t", passed=False)],
        )
        result = main(["--suite", "t.json"])
        assert result == 1

    @patch("qemu_harness.cli.run_suite")
    @patch("qemu_harness.cli._load_suite")
    def test_arch_override(
        self, mock_load: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        suite = MagicMock(arch="x86_64", platform="qemu")
        mock_load.return_value = suite
        mock_run.return_value = SuiteResult(
            arch="aarch64", platform="qemu",
        )
        main(["--suite", "t.json", "--arch", "aarch64"])
        assert suite.arch == "aarch64"
