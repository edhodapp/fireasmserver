"""Tests for branch_cov.trace."""

from __future__ import annotations

from pathlib import Path

import pytest

from branch_cov.trace import (
    _parse_pc_line,
    _strip_line,
    filter_trace,
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

    def test_malformed_line_error_includes_lineno_and_path(
        self, tmp_path: Path,
    ) -> None:
        """ValueError message should cite filename + line for quick debug."""
        path = tmp_path / "trace.log"
        path.write_text("0x40\nnot-a-hex\n0x50\n", encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            parse_trace(path)
        msg = str(exc.value)
        assert str(path) in msg
        assert ":2:" in msg  # line 2 is the malformed one
        assert "not-a-hex" in msg


class TestFilterTrace:
    """Skip PCs that fall inside any half-open [start, end) range."""

    def test_empty_skip_ranges_returns_copy(self) -> None:
        original = [0x100, 0x200, 0x300]
        result = filter_trace(original, [])
        assert result == original
        assert result is not original  # fresh copy so callers can mutate

    def test_single_range_filters_matching_pcs(self) -> None:
        trace = [0x100, 0x500, 0x200, 0x600, 0x300]
        result = filter_trace(trace, [(0x500, 0x700)])
        assert result == [0x100, 0x200, 0x300]

    def test_half_open_excludes_end(self) -> None:
        # 0x700 is the upper bound and should be included (half-open).
        assert filter_trace([0x500, 0x700], [(0x500, 0x700)]) == [0x700]

    def test_multiple_ranges(self) -> None:
        trace = [0x100, 0x200, 0x300, 0x400, 0x500]
        ranges = [(0x100, 0x200), (0x400, 0x500)]
        assert filter_trace(trace, ranges) == [0x200, 0x300, 0x500]

    def test_pc_outside_any_range_preserved(self) -> None:
        assert filter_trace([0x42], [(0x100, 0x200)]) == [0x42]
