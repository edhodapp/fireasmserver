"""Cross-check D-entries' Requirements against known REQ-IDs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from req_coverage.parser import (
    Decision,
    parse_decisions,
    parse_l2_requirements_table,
    parse_requirements_md,
)


@dataclass(frozen=True)
class Finding:
    """One audit issue."""

    decision_id: str
    kind: str                     # "missing" | "broken-ref"
    detail: str                   # human-readable explanation


@dataclass(frozen=True)
class Report:
    """Coverage audit outcome."""

    decisions: tuple[Decision, ...]
    known_req_ids: frozenset[str]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def missing_count(self) -> int:
        return sum(1 for f in self.findings if f.kind == "missing")

    @property
    def broken_ref_count(self) -> int:
        return sum(1 for f in self.findings if f.kind == "broken-ref")

    @property
    def is_clean(self) -> bool:
        return not self.findings


def audit_repo(repo_root: Path) -> Report:
    """Run the audit against the live files in the repo."""
    decisions_path = repo_root / "DECISIONS.md"
    req_path = repo_root / "REQUIREMENTS.md"
    l2_req_path = repo_root / "docs" / "l2" / "REQUIREMENTS.md"
    return audit_texts(
        decisions_text=decisions_path.read_text(),
        requirements_text=req_path.read_text(),
        l2_requirements_text=(
            l2_req_path.read_text() if l2_req_path.exists() else ""
        ),
    )


def audit_texts(
    decisions_text: str,
    requirements_text: str,
    l2_requirements_text: str,
) -> Report:
    """Pure-function audit driver for tests and CLI integration."""
    decisions = parse_decisions(decisions_text)
    known = (
        parse_requirements_md(requirements_text)
        | parse_l2_requirements_table(l2_requirements_text)
    )
    findings = tuple(_collect_findings(decisions, known))
    return Report(
        decisions=tuple(decisions),
        known_req_ids=frozenset(known),
        findings=findings,
    )


def _collect_findings(
    decisions: list[Decision], known: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    for decision in decisions:
        if decision.requirements_line is None:
            findings.append(Finding(
                decision_id=decision.id,
                kind="missing",
                detail=(
                    "no `**Requirements:**` line found in entry body"
                ),
            ))
            continue
        for req_id in decision.req_ids:
            if req_id not in known:
                findings.append(Finding(
                    decision_id=decision.id,
                    kind="broken-ref",
                    detail=(
                        f"REQ-ID `{req_id}` cited but not defined "
                        f"in REQUIREMENTS.md or "
                        f"docs/l2/REQUIREMENTS.md"
                    ),
                ))
    return findings
