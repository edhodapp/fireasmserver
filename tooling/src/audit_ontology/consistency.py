"""Status vs refs consistency checks for ontology constraints.

Per the briefing plus two Ed-confirmed extensions, a constraint is
internally consistent iff its ``status`` agrees with its
``implementation_refs`` and ``verification_refs`` shapes:

* ``status == "implemented"`` MUST have non-empty
  ``implementation_refs`` AND ``verification_refs``. "Implemented"
  without verification is not implementation — it is an untested
  claim (Ed, 2026-04-19 C2 call).
* ``status == "tested"`` MUST have non-empty
  ``implementation_refs`` AND ``verification_refs``. Verifying
  something we haven't implemented is incoherent.
* ``status == "deviation"`` MUST have non-empty ``rationale`` — a
  deviation without reason is a silent failure mode.
* ``status == "spec"`` with non-empty ``implementation_refs`` is a
  warning-severity gap (likely stale status); not a hard failure.

``status == "n_a"`` is a design-decision "not applicable" state
tracked in the ontology for traceability only; it imposes no
constraint on refs or rationale.

Each check emits a human-readable gap string in the flat
``gaps`` list the briefing's JSON schema pins. The shape is
deliberately plain-text strings rather than structured records
so a future reader reviewing the audit output does not need a
schema translator.
"""

from __future__ import annotations

from ontology import DomainConstraint, PerformanceConstraint

_ConstraintLike = DomainConstraint | PerformanceConstraint


def check_constraint(constraint: _ConstraintLike) -> list[str]:
    """Return all consistency-gap messages for one constraint.

    Empty list means consistent. A single constraint may produce
    several messages (e.g., implemented with both impl and verify
    refs empty) — the audit tool does not short-circuit, because
    the user wants the complete picture per ref row.
    """
    status = constraint.status
    gaps: list[str] = []
    gaps.extend(_check_implemented(status, constraint))
    gaps.extend(_check_tested(status, constraint))
    gaps.extend(_check_deviation(status, constraint))
    gaps.extend(_check_stale_spec(status, constraint))
    return gaps


def _check_implemented(
    status: str, constraint: _ConstraintLike,
) -> list[str]:
    """``implemented`` demands non-empty impl AND verify refs."""
    if status != "implemented":
        return []
    gaps: list[str] = []
    if not constraint.implementation_refs:
        gaps.append(
            "status=implemented but implementation_refs empty",
        )
    if not constraint.verification_refs:
        gaps.append(
            "status=implemented but verification_refs empty",
        )
    return gaps


def _check_tested(
    status: str, constraint: _ConstraintLike,
) -> list[str]:
    """``tested`` demands non-empty impl AND verify refs."""
    if status != "tested":
        return []
    gaps: list[str] = []
    if not constraint.implementation_refs:
        gaps.append("status=tested but implementation_refs empty")
    if not constraint.verification_refs:
        gaps.append("status=tested but verification_refs empty")
    return gaps


def _check_deviation(
    status: str, constraint: _ConstraintLike,
) -> list[str]:
    """``deviation`` demands a non-empty rationale."""
    if status != "deviation":
        return []
    if not constraint.rationale:
        return ["status=deviation but rationale empty"]
    return []


def _check_stale_spec(
    status: str, constraint: _ConstraintLike,
) -> list[str]:
    """``spec`` with refs is a likely-stale-status warning."""
    if status != "spec":
        return []
    if constraint.implementation_refs:
        return [
            "status=spec with non-empty implementation_refs "
            "(likely stale status)",
        ]
    return []
