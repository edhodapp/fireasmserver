"""Text and JSON renderers for the coverage Report."""

from __future__ import annotations

import json

from req_coverage.audit import Report


def format_text(report: Report) -> str:
    """Human-readable text rendering of the audit report."""
    lines: list[str] = []
    lines.append("=== D→REQ coverage audit ===\n")
    lines.append(f"Decisions:          {len(report.decisions)}\n")
    lines.append(f"Known REQ-IDs:      {len(report.known_req_ids)}\n")
    lines.append(f"Findings:           {len(report.findings)}\n")
    lines.append(
        f"  missing-Requirements: {report.missing_count}\n"
    )
    lines.append(
        f"  broken-ref:           {report.broken_ref_count}\n"
    )
    lines.append("\n")
    if report.findings:
        lines.append("--- Findings ---\n")
        for finding in report.findings:
            lines.append(
                f"  [{finding.kind}] {finding.decision_id}: "
                f"{finding.detail}\n"
            )
        lines.append("\n")
    else:
        lines.append("All decisions have valid Requirements coverage.\n")
    return "".join(lines)


def format_json(report: Report) -> str:
    """JSON rendering for machine consumption."""
    return json.dumps(
        {
            "decision_count": len(report.decisions),
            "known_req_id_count": len(report.known_req_ids),
            "findings": [
                {
                    "decision_id": f.decision_id,
                    "kind": f.kind,
                    "detail": f.detail,
                }
                for f in report.findings
            ],
            "missing_count": report.missing_count,
            "broken_ref_count": report.broken_ref_count,
        },
        indent=2,
    ) + "\n"
