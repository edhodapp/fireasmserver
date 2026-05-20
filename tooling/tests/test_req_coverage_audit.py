"""Tests for req_coverage.audit."""

from __future__ import annotations

from req_coverage.audit import audit_texts


def _decisions(*entries: str) -> str:
    """Glue helper for synthetic DECISIONS.md fragments."""
    return "\n".join(entries) + "\n"


class TestAuditTexts:
    """audit_texts cross-checks Decisions against known REQ-IDs."""

    def test_clean_when_all_refs_resolve(self) -> None:
        decisions = _decisions(
            "### D001: T1\n\n**Requirements:** MR-001\n\nbody\n",
        )
        reqs = "### MR-001: Owner\n\nbody\n"
        report = audit_texts(decisions, reqs, "")
        assert report.is_clean
        assert report.missing_count == 0
        assert report.broken_ref_count == 0

    def test_missing_requirements_line_flagged(self) -> None:
        decisions = "### D999: legacy\n\nbody without annotation\n"
        report = audit_texts(decisions, "", "")
        assert not report.is_clean
        assert report.missing_count == 1
        assert report.findings[0].kind == "missing"
        assert report.findings[0].decision_id == "D999"

    def test_broken_ref_flagged(self) -> None:
        decisions = _decisions(
            "### D001: T1\n\n**Requirements:** FAKE-001\n\nbody\n",
        )
        reqs = "### MR-001: Owner\n\nbody\n"
        report = audit_texts(decisions, reqs, "")
        assert not report.is_clean
        assert report.broken_ref_count == 1
        assert report.findings[0].kind == "broken-ref"
        assert "FAKE-001" in report.findings[0].detail

    def test_na_form_skips_validation(self) -> None:
        decisions = _decisions(
            "### D001: T1\n\n**Requirements:** N/A — governance\n\n",
        )
        report = audit_texts(decisions, "", "")
        assert report.is_clean

    def test_see_block_form_skips_validation(self) -> None:
        decisions = _decisions(
            "### D066: T\n\n**Requirements:** see block below\n\n",
        )
        report = audit_texts(decisions, "", "")
        assert report.is_clean

    def test_l2_table_resolves(self) -> None:
        # An L2-defined REQ-ID cited by a D-entry should resolve
        # through the docs/l2/REQUIREMENTS.md table parse.
        decisions = _decisions(
            "### D050: Crypto\n\n**Requirements:** ETH-005\n\n",
        )
        l2 = "| `ETH-005` | FCS | 802.3 | implemented | notes |\n"
        report = audit_texts(decisions, "", l2)
        assert report.is_clean

    def test_mixed_findings(self) -> None:
        decisions = _decisions(
            "### D001: ok\n\n**Requirements:** MR-001\n\n",
            "### D002: missing\n\nbody only\n",
            "### D003: broken\n\n**Requirements:** FAKE-009\n\n",
        )
        reqs = "### MR-001: Owner\n\nbody\n"
        report = audit_texts(decisions, reqs, "")
        assert report.missing_count == 1
        assert report.broken_ref_count == 1
        assert len(report.findings) == 2

    def test_decisions_tuple_preserved(self) -> None:
        decisions = _decisions(
            "### D001: a\n\n**Requirements:** MR-001\n\n",
            "### D002: b\n\n**Requirements:** N/A — ops\n\n",
        )
        reqs = "### MR-001: x\n\nbody\n"
        report = audit_texts(decisions, reqs, "")
        assert len(report.decisions) == 2
        assert report.decisions[0].id == "D001"
        assert report.decisions[1].is_na

    def test_known_set_includes_both_files(self) -> None:
        decisions = ""
        reqs = "### MR-001: a\n\nbody\n"
        l2 = "| `ETH-001` | a | b | c | d |\n"
        report = audit_texts(decisions, reqs, l2)
        assert report.known_req_ids == frozenset({"MR-001", "ETH-001"})
