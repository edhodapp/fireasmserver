"""Tests for req_coverage.parser."""

from __future__ import annotations

from req_coverage.parser import (
    Decision,
    parse_decisions,
    parse_l2_requirements_table,
    parse_requirements_md,
)


class TestParseDecisions:
    """parse_decisions extracts D-entries + their Requirements."""

    def test_single_entry_with_req_list(self) -> None:
        text = (
            "# Log\n\n"
            "### D001: License\n\n"
            "**Requirements:** ENG-001\n\n"
            "Body text.\n"
        )
        decisions = parse_decisions(text)
        assert len(decisions) == 1
        d = decisions[0]
        assert d.id == "D001"
        assert d.title == "License"
        assert d.requirements_line == "ENG-001"
        assert d.req_ids == ("ENG-001",)
        assert not d.is_na
        assert not d.is_see_block

    def test_multi_req_list(self) -> None:
        text = (
            "### D058: Actor model\n\n"
            "**Requirements:** MR-001, MR-004, BC-005\n\n"
            "body\n"
        )
        decisions = parse_decisions(text)
        assert decisions[0].req_ids == ("MR-001", "MR-004", "BC-005")

    def test_na_form(self) -> None:
        text = (
            "### D001: License\n\n"
            "**Requirements:** N/A — governance (license terms)\n\n"
        )
        d = parse_decisions(text)[0]
        assert d.is_na
        assert d.req_ids == ()

    def test_see_block_form(self) -> None:
        text = (
            "### D066: memreq\n\n"
            "**Requirements:** see `REQ-IDs` block below for ...\n\n"
        )
        d = parse_decisions(text)[0]
        assert d.is_see_block
        assert d.req_ids == ()

    def test_missing_requirements_line(self) -> None:
        text = (
            "### D999: Untouched legacy entry\n\n"
            "Some body text without a Requirements annotation.\n"
        )
        d = parse_decisions(text)[0]
        assert d.requirements_line is None
        assert d.req_ids == ()

    def test_multiple_decisions_in_one_doc(self) -> None:
        text = (
            "### D001: First\n\n"
            "**Requirements:** ENG-001\n\n"
            "Body 1.\n"
            "### D002: Second\n\n"
            "**Requirements:** N/A — framing\n\n"
            "Body 2.\n"
        )
        decisions = parse_decisions(text)
        assert [d.id for d in decisions] == ["D001", "D002"]
        assert decisions[1].is_na

    def test_multi_segment_req_id(self) -> None:
        # AES128-GCM-002 has three hyphen-separated segments.
        text = (
            "### D050: Crypto\n\n"
            "**Requirements:** AES128-GCM-002, VIO-MVP-001\n\n"
        )
        d = parse_decisions(text)[0]
        assert d.req_ids == ("AES128-GCM-002", "VIO-MVP-001")

    def test_requirements_at_eof_without_blank_line(self) -> None:
        # File ends immediately after the Requirements line —
        # exercises the "no \n\n found in body" branch of the
        # parser's blank-line scan.
        text = "### D001: T1\n\n**Requirements:** MR-001"
        d = parse_decisions(text)[0]
        assert d.req_ids == ("MR-001",)


class TestParseRequirementsMd:
    """REQ heading extraction from root REQUIREMENTS.md."""

    def test_single_heading(self) -> None:
        text = "### MR-001: Owner\n\nbody\n"
        assert parse_requirements_md(text) == {"MR-001"}

    def test_multiple_headings(self) -> None:
        text = (
            "### MR-001: a\n\nx\n"
            "### MR-002: b\n\ny\n"
            "### ENG-001: c\n\nz\n"
        )
        assert parse_requirements_md(text) == {
            "MR-001", "MR-002", "ENG-001",
        }

    def test_ignores_non_req_headings(self) -> None:
        # Headings without the REQ-ID shape are not picked up.
        text = (
            "### Conventions\n\nbody\n"
            "### MR-001: a\n\nbody\n"
        )
        assert parse_requirements_md(text) == {"MR-001"}


class TestParseL2Table:
    """REQ extraction from docs/l2/REQUIREMENTS.md table rows."""

    def test_single_row(self) -> None:
        text = "| `ETH-001` | desc | 802.3 | spec | notes |\n"
        assert parse_l2_requirements_table(text) == {"ETH-001"}

    def test_multiple_rows(self) -> None:
        text = (
            "| `ETH-001` | a | x | spec | n |\n"
            "| `VLAN-002` | b | y | spec | n |\n"
            "| `AES128-001` | c | z | implemented | n |\n"
        )
        assert parse_l2_requirements_table(text) == {
            "ETH-001", "VLAN-002", "AES128-001",
        }

    def test_ignores_legend_rows(self) -> None:
        # Header / legend rows don't open with a backticked REQ-ID.
        text = (
            "| Status | Meaning |\n"
            "| `spec` | description |\n"
            "| `ETH-001` | a | x | spec | n |\n"
        )
        assert parse_l2_requirements_table(text) == {"ETH-001"}


class TestDecisionDataclass:
    """Decision is frozen and predictable."""

    def test_frozen(self) -> None:
        d = Decision(
            id="D001", title="t", requirements_line=None,
        )
        try:
            d.id = "D999"  # type: ignore[misc]
        except Exception:  # pylint: disable=broad-except
            return
        raise AssertionError("Decision should be frozen")
