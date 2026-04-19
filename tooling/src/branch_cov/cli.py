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
    parser.add_argument(
        "--entry",
        default=None,
        help=(
            "Symbol name to start disassembly from, skipping any earlier "
            "bytes in the executable sections (e.g., '_entry' for aarch64 "
            "stubs whose code is preceded by a 64-byte Image header)."
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


def _run(args: argparse.Namespace) -> int:
    """Execute the coverage pipeline; raises on I/O or parse errors."""
    branches = enumerate_branches(args.elf, entry_symbol=args.entry)
    trace = parse_trace(args.trace)
    report = compute_coverage(branches, trace)
    _print_report(report)
    return 0 if report.fully_covered else 1


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Exit codes:
      0 — fully covered, no gaps
      1 — one or more uncovered (branch, outcome) pairs
      2 — an I/O or parse error prevented the analysis from running
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
