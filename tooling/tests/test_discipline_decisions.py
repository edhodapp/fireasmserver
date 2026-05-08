"""Tests for discipline.decisions."""

from __future__ import annotations

from discipline.decisions import (
    Entry,
    find_by_prefix,
    find_entry,
    parse_entries,
)


class TestParseEntries:
    """`### <id>:` heading parsing."""

    def test_single_entry(self) -> None:
        text = (
            "preamble line\n"
            "### D001: First decision\n"
            "body line one\n"
            "body line two\n"
        )
        entries = parse_entries(text)
        assert len(entries) == 1
        assert entries[0].entry_id == "D001"
        assert entries[0].body_lines == (
            "body line one",
            "body line two",
        )
        assert entries[0].deprecated is False

    def test_multi_entry(self) -> None:
        text = (
            "### D001: One\n"
            "alpha\n"
            "### MR-007: Layout\n"
            "beta\n"
            "gamma\n"
        )
        entries = parse_entries(text)
        assert [e.entry_id for e in entries] == ["D001", "MR-007"]
        assert entries[0].body_lines == ("alpha",)
        assert entries[1].body_lines == ("beta", "gamma")

    def test_no_entries(self) -> None:
        assert parse_entries("just prose, no headings\n") == []

    def test_deprecated_first_nonblank_marks_entry(self) -> None:
        text = (
            "### D003: Old call\n"
            "\n"
            "**DEPRECATED 2026-04-01 — superseded by D004.** body...\n"
        )
        [entry] = parse_entries(text)
        assert entry.deprecated is True

    def test_deprecated_later_does_not_mark_entry(self) -> None:
        text = (
            "### D005: Active\n"
            "first body line\n"
            "**DEPRECATED note about a sub-bullet.**\n"
        )
        [entry] = parse_entries(text)
        assert entry.deprecated is False

    def test_blank_only_body_is_not_deprecated(self) -> None:
        text = "### D006: Empty\n\n\n"
        [entry] = parse_entries(text)
        assert entry.deprecated is False


class TestFindEntry:
    """Lookup helpers."""

    def _entries(self) -> list[Entry]:
        return parse_entries(
            "### D001: One\n"
            "alpha\n"
            "### MR-007: Layout\n"
            "beta\n"
            "### MR-008: Sibling\n"
            "gamma\n"
        )

    def test_find_entry_hit(self) -> None:
        e = find_entry(self._entries(), "MR-007")
        assert e is not None
        assert e.entry_id == "MR-007"

    def test_find_entry_miss(self) -> None:
        assert find_entry(self._entries(), "ZZ-999") is None

    def test_find_by_prefix_skips_deprecated(self) -> None:
        text = (
            "### MR-001: Active\n"
            "alpha\n"
            "### MR-002: Old\n"
            "**DEPRECATED 2026-04-01 — superseded.** body\n"
            "### MR-003: Active two\n"
            "beta\n"
        )
        ids = [e.entry_id for e in find_by_prefix(parse_entries(text), "MR-")]
        assert ids == ["MR-001", "MR-003"]

    def test_find_by_prefix_no_match(self) -> None:
        assert find_by_prefix(self._entries(), "ZZ-") == []


class TestEntryRender:
    """Render an entry back to source-form markdown."""

    def test_render_round_trip(self) -> None:
        text = "### D001: One\nalpha\nbeta\n"
        [entry] = parse_entries(text)
        rendered = entry.render()
        assert rendered.startswith("### D001:\n")
        assert "alpha" in rendered
        assert "beta" in rendered
        assert rendered.endswith("\n")
