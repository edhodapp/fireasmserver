"""Diff observed execution against required branch outcomes."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from branch_cov.disasm import ConditionalBranch


class BranchOutcome(str, Enum):
    """Possible per-branch execution outcomes we require to observe."""

    TAKEN = "taken"
    NOT_TAKEN = "not_taken"


class CoverageGap(BaseModel):
    """A single (branch, outcome) pair that was never observed."""

    branch: ConditionalBranch
    missing: BranchOutcome


class CoverageReport(BaseModel):
    """Aggregate coverage over a set of branches and a trace."""

    total_branches: int
    observed_outcomes: int
    gaps: list[CoverageGap]

    @property
    def fully_covered(self) -> bool:
        """True when no (addr, outcome) pair is missing."""
        return not self.gaps


def _classify(
    branch: ConditionalBranch, next_pc: int,
) -> BranchOutcome | None:
    """Return the outcome implied by `next_pc` following `branch`."""
    if next_pc == branch.target_taken:
        return BranchOutcome.TAKEN
    if next_pc == branch.target_not_taken:
        return BranchOutcome.NOT_TAKEN
    return None


def _observed_outcomes(
    branches: list[ConditionalBranch], trace: list[int],
) -> set[tuple[int, BranchOutcome]]:
    """Scan adjacent PC pairs and classify each against a known branch."""
    by_addr = {b.addr: b for b in branches}
    observed: set[tuple[int, BranchOutcome]] = set()
    for i in range(len(trace) - 1):
        branch = by_addr.get(trace[i])
        if branch is None:
            continue
        outcome = _classify(branch, trace[i + 1])
        if outcome is not None:
            observed.add((trace[i], outcome))
    return observed


def _required_outcomes(
    branches: list[ConditionalBranch],
) -> list[tuple[ConditionalBranch, BranchOutcome]]:
    """Return (branch, TAKEN) and (branch, NOT_TAKEN) for every branch."""
    out: list[tuple[ConditionalBranch, BranchOutcome]] = []
    for b in branches:
        out.append((b, BranchOutcome.TAKEN))
        out.append((b, BranchOutcome.NOT_TAKEN))
    return out


def compute_coverage(
    branches: list[ConditionalBranch], trace: list[int],
) -> CoverageReport:
    """Compute a coverage report for the given branches and trace."""
    observed = _observed_outcomes(branches, trace)
    gaps = [
        CoverageGap(branch=b, missing=oc)
        for (b, oc) in _required_outcomes(branches)
        if (b.addr, oc) not in observed
    ]
    return CoverageReport(
        total_branches=len(branches),
        observed_outcomes=len(observed),
        gaps=gaps,
    )
