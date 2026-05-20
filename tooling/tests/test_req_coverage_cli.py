"""Tests for req_coverage.cli + formatter integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from req_coverage.audit import Finding, Report, audit_texts
from req_coverage.cli import main
from req_coverage.formatter import format_json, format_text
from req_coverage.parser import Decision


def _write_repo(tmp_path: Path, decisions: str,
                reqs: str = "", l2_reqs: str = "") -> Path:
    (tmp_path / "DECISIONS.md").write_text(decisions)
    (tmp_path / "REQUIREMENTS.md").write_text(reqs)
    if l2_reqs:
        l2_dir = tmp_path / "docs" / "l2"
        l2_dir.mkdir(parents=True)
        (l2_dir / "REQUIREMENTS.md").write_text(l2_reqs)
    return tmp_path


class TestCLIExitCodes:
    """The --exit-nonzero-on-error flag drives hook integration."""

    def test_clean_repo_exit_zero(self, tmp_path: Path) -> None:
        _write_repo(
            tmp_path,
            "### D001: T1\n\n**Requirements:** MR-001\n\n",
            "### MR-001: Owner\n\nbody\n",
        )
        rc = main([
            "--repo-root", str(tmp_path),
            "--exit-nonzero-on-error",
        ])
        assert rc == 0

    def test_missing_req_exit_one(self, tmp_path: Path) -> None:
        _write_repo(
            tmp_path,
            "### D999: legacy\n\nbody only\n",
        )
        rc = main([
            "--repo-root", str(tmp_path),
            "--exit-nonzero-on-error",
        ])
        assert rc == 1

    def test_broken_ref_exit_one(self, tmp_path: Path) -> None:
        _write_repo(
            tmp_path,
            "### D001: T1\n\n**Requirements:** FAKE-001\n\n",
            "### MR-001: Owner\n\nbody\n",
        )
        rc = main([
            "--repo-root", str(tmp_path),
            "--exit-nonzero-on-error",
        ])
        assert rc == 1

    def test_default_does_not_exit_nonzero_on_findings(
        self, tmp_path: Path,
    ) -> None:
        # Without the flag, a dirty repo still emits a report
        # cleanly with exit 0 — humans get the matrix; hooks opt
        # in to the failure exit.
        _write_repo(
            tmp_path,
            "### D999: legacy\n\nbody only\n",
        )
        rc = main(["--repo-root", str(tmp_path)])
        assert rc == 0


class TestCLIOutput:
    """Output formats."""

    def test_text_output_contains_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_repo(
            tmp_path,
            "### D001: T1\n\n**Requirements:** MR-001\n\n",
            "### MR-001: Owner\n\nbody\n",
        )
        main(["--repo-root", str(tmp_path)])
        out = capsys.readouterr().out
        assert "D→REQ coverage audit" in out
        assert "Decisions:" in out
        assert "Known REQ-IDs:" in out

    def test_json_output_parseable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_repo(
            tmp_path,
            "### D001: T1\n\n**Requirements:** FAKE-001\n\n",
            "### MR-001: Owner\n\nbody\n",
        )
        main(["--repo-root", str(tmp_path), "--json"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["decision_count"] == 1
        assert data["broken_ref_count"] == 1
        assert data["findings"][0]["kind"] == "broken-ref"


class TestFormatter:
    """Direct format_text / format_json coverage."""

    def _make_report(self, with_finding: bool = False) -> Report:
        decisions = (Decision(id="D001", title="t",
                              requirements_line="MR-001",
                              req_ids=("MR-001",)),)
        findings: tuple[Finding, ...] = ()
        if with_finding:
            findings = (Finding(
                decision_id="D999", kind="missing",
                detail="no `**Requirements:**` line found",
            ),)
        return Report(
            decisions=decisions,
            known_req_ids=frozenset({"MR-001"}),
            findings=findings,
        )

    def test_format_text_clean(self) -> None:
        out = format_text(self._make_report(with_finding=False))
        assert "All decisions have valid" in out
        assert "Findings:           0" in out

    def test_format_text_with_finding(self) -> None:
        out = format_text(self._make_report(with_finding=True))
        assert "Findings:           1" in out
        assert "[missing] D999" in out

    def test_format_json_structure(self) -> None:
        out = format_json(self._make_report(with_finding=True))
        data = json.loads(out)
        assert data["missing_count"] == 1
        assert data["findings"][0]["decision_id"] == "D999"
        assert data["findings"][0]["kind"] == "missing"


class TestLiveRepoIntegration:
    """audit_texts is exercised against the live repo's actual
    files via the CLI's default --repo-root."""

    def test_live_repo_is_clean(self) -> None:
        # The #44 sweep landed all 66 D entries with valid
        # Requirements lines. This test is the contract that
        # future commits must preserve.
        rc = main(["--exit-nonzero-on-error"])
        assert rc == 0


class TestAuditTextsSmoke:
    """Smoke coverage for audit_texts to fill any branch holes."""

    def test_empty_inputs(self) -> None:
        report = audit_texts("", "", "")
        assert report.is_clean
        assert len(report.decisions) == 0
        assert len(report.known_req_ids) == 0
