"""Audit orchestration — load ontology, parse + resolve refs, check
consistency, assemble the report.

Pure glue: ``run_audit`` is the one public entry point. All
filesystem interaction and ontology access lives in the modules it
dispatches to (``parser``, ``resolver``, ``consistency``, plus the
existing ``ontology.dag.load_dag``). Keeping this module thin makes
the CLI and tests trivial — feed in a ``dag_path`` + ``repo_root``,
get back an ``AuditReport`` that the formatter can serialize.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from audit_ontology.consistency import check_constraint
from audit_ontology.parser import parse_ref
from audit_ontology.resolver import ResolvedRef, resolve_ref
from ontology import DomainConstraint, PerformanceConstraint
from ontology.dag import load_dag

_ConstraintLike = DomainConstraint | PerformanceConstraint


class ConstraintReport(BaseModel):
    """Audit output for a single ontology constraint."""

    name: str
    kind: str
    status: str
    rationale: str
    implementation_refs: list[ResolvedRef]
    verification_refs: list[ResolvedRef]
    gaps: list[str]


class Summary(BaseModel):
    """Rolled-up counts across all audited constraints."""

    total_constraints: int
    with_impl_refs: int
    with_verify_refs: int
    gap_count: int
    resolved_ref_count: int
    broken_ref_count: int


class AuditReport(BaseModel):
    """Top-level audit result returned by ``run_audit``."""

    dag_path: str
    ontology_node_id: str
    constraints: list[ConstraintReport]
    summary: Summary


def run_audit(dag_path: Path, repo_root: Path) -> AuditReport:
    """Load the DAG, audit its current node, and return a report.

    Raises ``ValueError`` when the DAG has no current node — an
    empty DAG has nothing to audit, and silently returning an
    empty report would mask a misconfigured caller. All other
    failures (malformed DAG JSON, file read errors) propagate
    from ``load_dag`` and are not caught here.
    """
    dag = load_dag(str(dag_path), "audit")
    node = dag.get_current_node()
    if node is None:
        raise ValueError(
            f"DAG at {dag_path} has no current node to audit",
        )
    reports = _audit_constraints(node.ontology, repo_root)
    return AuditReport(
        dag_path=str(dag_path),
        ontology_node_id=node.id,
        constraints=reports,
        summary=_summarize(reports),
    )


def _audit_constraints(
    ontology: object, repo_root: Path,
) -> list[ConstraintReport]:
    """Walk domain + performance constraints, build one
    ``ConstraintReport`` per row."""
    domain = getattr(ontology, "domain_constraints", [])
    perf = getattr(ontology, "performance_constraints", [])
    reports: list[ConstraintReport] = []
    for constraint in domain:
        reports.append(_audit_one(constraint, "domain", repo_root))
    for constraint in perf:
        reports.append(
            _audit_one(constraint, "performance", repo_root),
        )
    return reports


def _audit_one(
    constraint: _ConstraintLike, kind: str, repo_root: Path,
) -> ConstraintReport:
    """Resolve all refs and run consistency checks for one row."""
    impl = [
        resolve_ref(parse_ref(raw), repo_root)
        for raw in constraint.implementation_refs
    ]
    verify = [
        resolve_ref(parse_ref(raw), repo_root)
        for raw in constraint.verification_refs
    ]
    gaps = list(check_constraint(constraint))
    gaps.extend(_ref_resolution_gaps(impl, "implementation_refs"))
    gaps.extend(_ref_resolution_gaps(verify, "verification_refs"))
    return ConstraintReport(
        name=constraint.name,
        kind=kind,
        status=constraint.status,
        rationale=constraint.rationale,
        implementation_refs=impl,
        verification_refs=verify,
        gaps=gaps,
    )


def _ref_resolution_gaps(
    refs: list[ResolvedRef], label: str,
) -> list[str]:
    """One gap string per unresolved ref — the caller asked for
    honest signal, so say exactly which ref failed and why."""
    gaps: list[str] = []
    for ref in refs:
        if ref.resolution == "resolved":
            continue
        gaps.append(
            f"{label}[{ref.parsed.raw!r}] unresolved: "
            f"{ref.resolution} ({ref.detail})",
        )
    return gaps


def _summarize(reports: list[ConstraintReport]) -> Summary:
    """Aggregate per-constraint stats into the report summary."""
    total = len(reports)
    with_impl = sum(1 for r in reports if r.implementation_refs)
    with_verify = sum(1 for r in reports if r.verification_refs)
    gap_count = sum(len(r.gaps) for r in reports)
    resolved = sum(
        _count_resolution(r, "resolved") for r in reports
    )
    broken = sum(
        _count_broken(r) for r in reports
    )
    return Summary(
        total_constraints=total,
        with_impl_refs=with_impl,
        with_verify_refs=with_verify,
        gap_count=gap_count,
        resolved_ref_count=resolved,
        broken_ref_count=broken,
    )


def _count_resolution(
    report: ConstraintReport, target: str,
) -> int:
    """Count refs in one report whose resolution equals ``target``."""
    count = 0
    for ref in report.implementation_refs:
        if ref.resolution == target:
            count += 1
    for ref in report.verification_refs:
        if ref.resolution == target:
            count += 1
    return count


def _count_broken(report: ConstraintReport) -> int:
    """Count refs whose resolution is anything other than
    ``resolved`` — file_missing, symbol_missing, line_out_of_range,
    or invalid."""
    count = 0
    for ref in report.implementation_refs:
        if ref.resolution != "resolved":
            count += 1
    for ref in report.verification_refs:
        if ref.resolution != "resolved":
            count += 1
    return count
