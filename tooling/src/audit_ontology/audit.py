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
from ontology import (
    DomainConstraint,
    PerformanceConstraint,
    VerificationCase,
)
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


class VerificationCaseReport(BaseModel):
    """Audit output for a single ``VerificationCase`` — the SysE
    test record that points at ``implementation_refs`` (the test
    code itself) and declares which constraints it ``covers``.

    The model-level validators already enforce status-vs-refs
    consistency (``written``/``passing`` require non-empty
    ``implementation_refs``; ``superseded`` requires a
    ``rationale``). This report layer adds the
    resolve-in-working-tree check that D051 needs: a test that
    claims ``status='passing'`` but whose file no longer exists
    becomes a broken traceability link the pre-push gate can
    catch.
    """

    name: str
    tier: str
    status: str
    covers: list[str]
    implementation_refs: list[ResolvedRef]
    gaps: list[str]


class Summary(BaseModel):
    """Rolled-up counts across constraints + verification cases."""

    total_constraints: int
    with_impl_refs: int
    with_verify_refs: int
    total_verification_cases: int
    gap_count: int
    resolved_ref_count: int
    broken_ref_count: int


class AuditReport(BaseModel):
    """Top-level audit result returned by ``run_audit``."""

    dag_path: str
    ontology_node_id: str
    constraints: list[ConstraintReport]
    verification_cases: list[VerificationCaseReport]
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
    case_reports = _audit_verification_cases(
        node.ontology, repo_root,
    )
    return AuditReport(
        dag_path=str(dag_path),
        ontology_node_id=node.id,
        constraints=reports,
        verification_cases=case_reports,
        summary=_summarize(reports, case_reports),
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


def _audit_verification_cases(
    ontology: object, repo_root: Path,
) -> list[VerificationCaseReport]:
    """Walk verification_cases, resolving each test's
    ``implementation_refs`` against the working tree. The
    model layer already enforced that ``written`` / ``passing``
    cases have non-empty refs; this layer adds the
    do-they-actually-exist check."""
    cases = getattr(ontology, "verification_cases", [])
    reports: list[VerificationCaseReport] = []
    for case in cases:
        reports.append(_audit_one_case(case, repo_root))
    return reports


def _audit_one_case(
    case: VerificationCase, repo_root: Path,
) -> VerificationCaseReport:
    """Resolve ``implementation_refs`` and record any gaps."""
    impl = [
        resolve_ref(parse_ref(raw), repo_root)
        for raw in case.implementation_refs
    ]
    gaps = _ref_resolution_gaps(impl, "implementation_refs")
    return VerificationCaseReport(
        name=case.name,
        tier=case.tier,
        status=case.status,
        covers=list(case.covers),
        implementation_refs=impl,
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


def _summarize(
    reports: list[ConstraintReport],
    case_reports: list[VerificationCaseReport],
) -> Summary:
    """Aggregate per-constraint + per-verification-case stats."""
    total = len(reports)
    with_impl = sum(1 for r in reports if r.implementation_refs)
    with_verify = sum(1 for r in reports if r.verification_refs)
    gap_count = (
        sum(len(r.gaps) for r in reports)
        + sum(len(c.gaps) for c in case_reports)
    )
    resolved = (
        sum(_count_resolution(r, "resolved") for r in reports)
        + sum(
            _count_case_resolution(c, "resolved")
            for c in case_reports
        )
    )
    broken = (
        sum(_count_broken(r) for r in reports)
        + sum(_count_case_broken(c) for c in case_reports)
    )
    return Summary(
        total_constraints=total,
        with_impl_refs=with_impl,
        with_verify_refs=with_verify,
        total_verification_cases=len(case_reports),
        gap_count=gap_count,
        resolved_ref_count=resolved,
        broken_ref_count=broken,
    )


def _count_case_resolution(
    report: VerificationCaseReport, target: str,
) -> int:
    """Count refs on one verification case whose resolution
    equals ``target``."""
    return sum(
        1 for ref in report.implementation_refs
        if ref.resolution == target
    )


def _count_case_broken(report: VerificationCaseReport) -> int:
    """Count refs on one verification case that failed to
    resolve (any non-``resolved`` status)."""
    return sum(
        1 for ref in report.implementation_refs
        if ref.resolution != "resolved"
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
