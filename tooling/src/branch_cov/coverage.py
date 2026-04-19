"""Diff observed execution against required branch outcomes.

Classification assumes the trace is a **contiguous, interrupt-free**
sequence of executed PCs: for each branch instruction at addr A, the
immediately-following PC in the trace is classified as TAKEN if it
equals A's taken-target or NOT_TAKEN if it equals A + insn_size.

Traces that include interrupt-handler entry PCs, exception dispatch,
or other discontinuities between a branch and its successor will
silently miss those outcomes. Callers with interrupt-heavy traces
should strip handler ranges with branch_cov.trace.filter_trace
before calling compute_coverage.

Baseline ratchet
----------------
A CoverageReport's gaps are raw facts about "what did / didn't
execute." Most assembly programs have paths that a single-boot trace
legitimately cannot exercise (secondary-CPU entry, saturated-FIFO
branches, error handlers). Baselines capture the currently-accepted
set of gaps and compare an incoming report against them:

    load_baseline(path)       → set[(addr, outcome)]
    compare_to_baseline(r, b) → BaselineComparison  (new / closed)

Any delta — new gap OR closed-but-still-in-baseline gap — is a
signal. New gaps mean a regression; closed gaps mean the baseline is
stale and should be tightened.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

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


class BaselineComparison(BaseModel):
    """Delta between a fresh report's gaps and a stored baseline."""

    new_gaps: list[tuple[int, BranchOutcome]]       # observed ∖ baseline
    closed_gaps: list[tuple[int, BranchOutcome]]    # baseline ∖ observed

    @property
    def matches(self) -> bool:
        """True when the report's gaps exactly equal the baseline."""
        return not self.new_gaps and not self.closed_gaps


def _parse_baseline_entry(
    path: Path, lineno: int, raw: str,
) -> tuple[int, BranchOutcome] | None:
    """Parse one baseline line; return None for blanks/comments."""
    line = raw.split("#", 1)[0].strip()
    if not line:
        return None
    parts = line.split()
    if len(parts) != 2:
        msg = (
            f"{path}:{lineno}: expected '<hex-addr> <outcome>', "
            f"got {raw.rstrip()!r}"
        )
        raise ValueError(msg)
    try:
        return int(parts[0], 16), BranchOutcome(parts[1])
    except ValueError as exc:
        msg = f"{path}:{lineno}: {raw.rstrip()!r}: {exc}"
        raise ValueError(msg) from exc


def load_baseline(path: Path) -> set[tuple[int, BranchOutcome]]:
    """Parse a baseline file into a set of (addr, outcome) tuples.

    File format: one baseline entry per line, whitespace-separated
    ``<hex-addr> <outcome>`` where outcome is 'taken' or 'not_taken'.
    Lines beginning with '#' and blank lines are ignored. Example:

        # baseline for aarch64/qemu tracer bullet
        0x48 taken       # secondary-CPU entry, not exercised at vcpu=1
        0x64 taken       # UART-FIFO-full, never seen under PL011 emu
    """
    entries: set[tuple[int, BranchOutcome]] = set()
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            entry = _parse_baseline_entry(path, lineno, raw)
            if entry is not None:
                entries.add(entry)
    return entries


def compare_to_baseline(
    report: CoverageReport,
    baseline: set[tuple[int, BranchOutcome]],
) -> BaselineComparison:
    """Diff a CoverageReport's gaps against a baseline set.

    Naming note: `current_gaps` is the report's *gap* set (pairs NOT
    observed). Don't confuse this with `_observed_outcomes`, which
    counts pairs that WERE observed. Semantics intentionally inverted
    here because baselines list acknowledged gaps, not observed paths.
    """
    current_gaps = {(g.branch.addr, g.missing) for g in report.gaps}
    return BaselineComparison(
        new_gaps=sorted(current_gaps - baseline),
        closed_gaps=sorted(baseline - current_gaps),
    )
