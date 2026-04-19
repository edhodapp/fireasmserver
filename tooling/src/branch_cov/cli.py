"""Command-line entry point for the branch-cov tool."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from branch_cov.coverage import CoverageReport, compute_coverage
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


def main(argv: list[str] | None = None) -> int:
    """Entry point. 0 = fully covered, 1 = one or more gaps."""
    args = parse_args(argv)
    branches = enumerate_branches(args.elf)
    trace = parse_trace(args.trace)
    report = compute_coverage(branches, trace)
    _print_report(report)
    return 0 if report.fully_covered else 1


if __name__ == "__main__":
    sys.exit(main())
