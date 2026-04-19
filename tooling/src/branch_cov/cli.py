"""Command-line entry point for the branch-cov tool."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from branch_cov.coverage import (
    BaselineComparison,
    BranchOutcome,
    CoverageReport,
    compare_to_baseline,
    compute_coverage,
    load_baseline,
)
from branch_cov.disasm import enumerate_branches
from branch_cov.trace import parse_trace


def parse_args(
    argv: list[str] | None = None,
) -> argparse.Namespace:
    """Parse --elf and --trace from argv."""
    parser = argparse.ArgumentParser(
        prog="branch-cov",
        description="Report unvisited conditional-branch outcomes.",
    )
    parser.add_argument("--elf", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument(
        "--entry",
        default=None,
        help=(
            "Symbol name to start disassembly from, skipping any earlier "
            "bytes in the executable sections (e.g., '_entry' for aarch64 "
            "stubs whose code is preceded by a 64-byte Image header)."
        ),
    )
    parser.add_argument(
        "--load-offset",
        type=lambda s: int(s, 0),
        default=0,
        help=(
            "Subtracted from every trace PC before matching. Use when the "
            "guest was linked at one VMA but loaded at another — e.g., "
            "aarch64 Linux Image stubs are linked at 0x0 but QEMU's -M virt "
            "loads them at 0x40080000, so pass --load-offset=0x40080000."
        ),
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=(
            "Path to a baseline file listing (addr, outcome) pairs that "
            "are currently accepted as gaps. If provided, the run fails "
            "(exit 1) on any delta — a NEW gap indicates a regression, a "
            "CLOSED gap indicates the baseline is stale and should be "
            "tightened. Without --baseline, all gaps are advisory and "
            "exit code is 0 even when gaps exist."
        ),
    )
    return parser.parse_args(argv)


def _print_report(report: CoverageReport) -> None:
    """Print a one-line summary plus any coverage gaps."""
    print(f"Branches: {report.total_branches}")
    print(f"Observed outcomes: {report.observed_outcomes}")
    print(f"Gaps: {len(report.gaps)}")
    for gap in report.gaps:
        print(
            f"  0x{gap.branch.addr:x} "
            f"{gap.branch.mnemonic} missing {gap.missing.value}"
        )


def _print_gap_list(
    label: str,
    tail: str,
    gaps: list[tuple[int, BranchOutcome]],
) -> None:
    """Print one section of a baseline delta (NEW or CLOSED)."""
    if not gaps:
        return
    print(f"{label} gaps ({len(gaps)}) — {tail}:")
    for addr, outcome in gaps:
        print(f"  0x{addr:x} {outcome.value}")


def _print_baseline_delta(cmp_: BaselineComparison) -> None:
    """Print added/closed gaps relative to a baseline."""
    if cmp_.matches:
        print("Baseline: MATCH (no new gaps, no closed gaps)")
        return
    _print_gap_list("NEW", "regression", cmp_.new_gaps)
    _print_gap_list(
        "CLOSED", "baseline is stale and should be tightened",
        cmp_.closed_gaps,
    )


def _run(args: argparse.Namespace) -> int:
    """Execute the coverage pipeline; raises on I/O or parse errors."""
    # Load + validate the baseline BEFORE the expensive disassembly and
    # trace parsing phases, so a malformed baseline file fails fast
    # (~milliseconds) rather than after minutes of analysis.
    baseline = (
        load_baseline(args.baseline) if args.baseline else None
    )
    branches = enumerate_branches(args.elf, entry_symbol=args.entry)
    trace = parse_trace(args.trace)
    if args.load_offset:
        # List comp doubles peak memory (original + adjusted) which is
        # fine at today's ~hundred-kPC scale. When parse_trace's docstring
        # scale-threshold (~1M PCs) is reached, fold the offset into a
        # generator-based _observed_outcomes to keep peak constant.
        trace = [pc - args.load_offset for pc in trace]
    report = compute_coverage(branches, trace)
    _print_report(report)
    if baseline is None:
        # Advisory mode — gaps do not fail the run.
        return 0
    cmp_ = compare_to_baseline(report, baseline)
    _print_baseline_delta(cmp_)
    return 0 if cmp_.matches else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Exit codes:
      0 — advisory mode (no --baseline): gaps are printed but always
          pass. Ratchet mode (--baseline): gaps exactly equal the
          baseline.
      1 — ratchet mode only: at least one NEW gap (regression) or
          CLOSED gap (stale baseline) detected.
      2 — an I/O or parse error prevented the analysis from running.

    The advisory-mode exit 0 is deliberate: gap counts without a
    baseline have no "expected" to ratchet against, so treating them
    as failure would noise the cell red on every run.
    """
    args = parse_args(argv)
    try:
        return _run(args)
    except FileNotFoundError as exc:
        print(
            f"branch-cov: file not found: {exc.filename}",
            file=sys.stderr,
        )
        return 2
    except PermissionError as exc:
        print(
            f"branch-cov: permission denied: {exc.filename}",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(
            f"branch-cov: invalid input: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
