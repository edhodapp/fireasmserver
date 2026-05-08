"""Tests for discipline.markers."""

from __future__ import annotations

from discipline.markers import MarkerError, extract_block


class TestExtractBlock:
    """Marker-pair extraction."""

    def test_happy_path_single_block(self) -> None:
        text = (
            "noise\n"
            "// DISCIPLINE-PRINT-START: foo\n"
            "alpha\n"
            "beta\n"
            "// DISCIPLINE-PRINT-END: foo\n"
            "more noise\n"
        )
        assert extract_block(text, "foo") == ["alpha", "beta"]

    def test_comment_syntax_irrelevant(self) -> None:
        text = (
            "; DISCIPLINE-PRINT-START: bar\n"
            "x\n"
            "# DISCIPLINE-PRINT-END: bar\n"
        )
        assert extract_block(text, "bar") == ["x"]

    def test_only_named_block_extracted(self) -> None:
        text = (
            "// DISCIPLINE-PRINT-START: foo\n"
            "in-foo\n"
            "// DISCIPLINE-PRINT-END: foo\n"
            "// DISCIPLINE-PRINT-START: bar\n"
            "in-bar\n"
            "// DISCIPLINE-PRINT-END: bar\n"
        )
        assert extract_block(text, "foo") == ["in-foo"]
        assert extract_block(text, "bar") == ["in-bar"]

    def test_missing_block_returns_marker_error(self) -> None:
        result = extract_block("nothing\n", "foo")
        assert isinstance(result, MarkerError)
        assert result.block_name == "foo"
        assert "0" in result.reason

    def test_duplicate_start_returns_marker_error(self) -> None:
        text = (
            "// DISCIPLINE-PRINT-START: foo\n"
            "// DISCIPLINE-PRINT-START: foo\n"
            "// DISCIPLINE-PRINT-END: foo\n"
        )
        result = extract_block(text, "foo")
        assert isinstance(result, MarkerError)
        assert "2" in result.reason

    def test_missing_end_returns_marker_error(self) -> None:
        text = "// DISCIPLINE-PRINT-START: foo\n"
        result = extract_block(text, "foo")
        assert isinstance(result, MarkerError)
        assert "END" in result.reason or "0" in result.reason

    def test_duplicate_end_returns_marker_error(self) -> None:
        text = (
            "// DISCIPLINE-PRINT-START: foo\n"
            "// DISCIPLINE-PRINT-END: foo\n"
            "// DISCIPLINE-PRINT-END: foo\n"
        )
        result = extract_block(text, "foo")
        assert isinstance(result, MarkerError)

    def test_end_before_start_returns_marker_error(self) -> None:
        text = (
            "// DISCIPLINE-PRINT-END: foo\n"
            "// DISCIPLINE-PRINT-START: foo\n"
        )
        result = extract_block(text, "foo")
        assert isinstance(result, MarkerError)
        assert "before" in result.reason.lower()

    def test_empty_block_returns_empty_list(self) -> None:
        text = (
            "// DISCIPLINE-PRINT-START: foo\n"
            "// DISCIPLINE-PRINT-END: foo\n"
        )
        assert extract_block(text, "foo") == []
