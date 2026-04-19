"""Tests for branch_cov.trace."""

from __future__ import annotations

from pathlib import Path

import pytest

from branch_cov.trace import (
    _parse_pc_line,
    _strip_line,
    parse_trace,
)


class TestStripLine:
    """Whitespace / comment filtering on a single line."""

    def test_blank_line_returns_none(self) -> None:
        assert _strip_line("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _strip_line("   \t  \n") is None

    def test_full_line_comment_returns_none(self) -> None:
        assert _strip_line("# just a comment") is None

    def test_inline_comment_strips_tail(self) -> None:
        assert _strip_line("0x1000  # entry") == "0x1000"

    def test_inline_comment_only_returns_none(self) -> None:
        assert _strip_line("   # nothing before") is None

    def test_plain_pc_passes_through(self) -> None:
        assert _strip_line("0xdeadbeef") == "0xdeadbeef"


class TestParsePcLine:
    """End-to-end line parsing to int."""

    def test_hex_with_prefix(self) -> None:
        assert _parse_pc_line("0x40") == 0x40

    def test_hex_without_prefix(self) -> None:
        assert _parse_pc_line("40") == 0x40

    def test_blank_returns_none(self) -> None:
        assert _parse_pc_line("\n") is None

    def test_comment_returns_none(self) -> None:
        assert _parse_pc_line("#nope") is None

    def test_malformed_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_pc_line("not-a-hex-value")


class TestParseTrace:
    """File-level parse over multiple lines."""

    def test_mixed_content(self, tmp_path: Path) -> None:
        path = tmp_path / "trace.log"
        path.write_text(
            "# header\n"
            "0x40\n"
            "\n"
            "  0x44  # jump target\n"
            "0x48\n",
            encoding="utf-8",
        )
        assert parse_trace(path) == [0x40, 0x44, 0x48]

    def test_empty_file_returns_empty_list(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "trace.log"
        path.write_text("", encoding="utf-8")
        assert not parse_trace(path)

    def test_only_comments_returns_empty_list(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "trace.log"
        path.write_text("# a\n# b\n", encoding="utf-8")
        assert not parse_trace(path)
