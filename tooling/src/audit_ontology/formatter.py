"""Render an ``AuditReport`` as human-readable text or JSON.

Two output modes keep the tool useful for both human readers (the
requirement → impl → verification matrix an SysE reviewer scans)
and machine consumers (future pre-push / CI integration keyed to
``gap_count``). The JSON shape is pinned in the briefing's
appendix — any change to the schema is a breaking change for
downstream consumers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from audit_ontology.audit import (
    AuditReport,
    ConstraintReport,
    Summary,
)


def format_text(report: AuditReport) -> str:
    """Render the matrix + gaps + summary block as plain text.

    Format: one constraint per stanza, ``[✓]``/``[!]`` status
    marker, name, status, impl + verify lines. Sections are
    separated by ``===`` headers so a reader can grep for the
    gaps section or the summary block.
    """
    lines: list[str] = []
    lines.append(
        "=== Requirement → Implementation → Verification matrix ===",
    )
    for row in report.constraints:
        lines.extend(_format_constraint(row))
    lines.append("")
    lines.extend(_format_gaps(report.constraints))
    lines.append("")
    lines.extend(_format_summary(report.summary))
    return "\n".join(lines) + "\n"


def format_json(report: AuditReport) -> str:
    """Render the report as the schema pinned in the briefing.

    ``ResolvedRef`` serializes into the ``{raw, resolved, kind}``
    triple the briefing's appendix specifies; all other fields
    come straight out of pydantic's ``model_dump``.
    """
    payload = {
        "dag_path": report.dag_path,
        "ontology_node_id": report.ontology_node_id,
        "constraints": [
            _constraint_to_dict(row) for row in report.constraints
        ],
        "summary": report.summary.model_dump(),
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _format_constraint(row: ConstraintReport) -> list[str]:
    """Lines for one constraint in the human matrix section."""
    marker = "[✓]" if not row.gaps else "[!]"
    header = f"{marker} {row.name:<35} ({row.status})"
    if row.gaps:
        header += " ← gaps present"
    lines = [header]
    lines.append(_ref_line("impl", row.implementation_refs))
    lines.append(_ref_line("verify", row.verification_refs))
    return lines


def _ref_line(label: str, refs: Sequence[object]) -> str:
    """Render one impl/verify line; ``—`` when no refs declared."""
    if not refs:
        return f"    {label}: — (none declared)"
    rendered = ", ".join(_ref_display(r) for r in refs)
    return f"    {label}: {rendered}"


def _ref_display(ref: object) -> str:
    """Compact representation of a single resolved ref.

    Resolved refs show only the raw string; unresolved refs
    annotate with ``!resolution``  so a reader scanning the
    matrix can spot the break without dropping into the gaps
    section.
    """
    # Duck-type access: tests pass a ResolvedRef but the
    # formatter is happier not importing it across modules.
    resolution = getattr(ref, "resolution", "unknown")
    parsed = getattr(ref, "parsed", None)
    raw = getattr(parsed, "raw", "?") if parsed else "?"
    if resolution == "resolved":
        return raw
    return f"{raw}!{resolution}"


def _format_gaps(reports: list[ConstraintReport]) -> list[str]:
    """Flat list of every gap across every constraint, prefixed
    by constraint name so the reader can locate the row."""
    total = sum(len(r.gaps) for r in reports)
    header = f"=== Gaps ({total} total) ==="
    if total == 0:
        return [header, "  (none)"]
    lines = [header]
    for row in reports:
        for gap in row.gaps:
            lines.append(f"  - {row.name}: {gap}")
    return lines


def _format_summary(summary: Summary) -> list[str]:
    """Rolled-up counts block at the end of the matrix."""
    lines = ["=== Summary ==="]
    total = summary.total_constraints
    impl_pct = _percent(summary.with_impl_refs, total)
    verify_pct = _percent(summary.with_verify_refs, total)
    lines.append(f"  Total constraints:     {total}")
    lines.append(
        f"  With impl refs:        {summary.with_impl_refs}"
        f" ({impl_pct}%)",
    )
    lines.append(
        f"  With verify refs:      {summary.with_verify_refs}"
        f" ({verify_pct}%)",
    )
    lines.append(
        f"  Gaps / inconsistencies: {summary.gap_count}",
    )
    lines.append(
        f"  Resolved refs:         {summary.resolved_ref_count}",
    )
    lines.append(
        f"  Broken refs:           {summary.broken_ref_count}",
    )
    return lines


def _percent(numerator: int, denominator: int) -> int:
    """Integer percentage; ``0`` when denominator is 0 so an
    empty ontology doesn't divide-by-zero."""
    if denominator == 0:
        return 0
    return round(100 * numerator / denominator)


def _constraint_to_dict(row: ConstraintReport) -> dict[str, object]:
    """JSON-shape dict for one constraint, matching the briefing
    appendix schema."""
    return {
        "name": row.name,
        "kind": row.kind,
        "status": row.status,
        "rationale": row.rationale,
        "implementation_refs": [
            _ref_to_dict(ref) for ref in row.implementation_refs
        ],
        "verification_refs": [
            _ref_to_dict(ref) for ref in row.verification_refs
        ],
        "gaps": list(row.gaps),
    }


def _ref_to_dict(ref: object) -> dict[str, object]:
    """JSON-shape dict for one resolved ref: ``raw``,
    ``resolved`` (bool), ``kind`` (parser-level kind)."""
    resolution = getattr(ref, "resolution", "invalid")
    parsed = getattr(ref, "parsed", None)
    raw = getattr(parsed, "raw", "") if parsed else ""
    kind = getattr(parsed, "kind", "invalid") if parsed else "invalid"
    return {
        "raw": raw,
        "resolved": resolution == "resolved",
        "kind": kind,
        "resolution": resolution,
        "detail": getattr(ref, "detail", ""),
    }
