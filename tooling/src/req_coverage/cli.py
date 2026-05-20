"""Command-line entry point for ``req-coverage``.

Three modes mirroring the audit-ontology CLI shape:

* ``req-coverage`` — human-readable report on stdout, exit 0.
* ``req-coverage --json`` — JSON report, exit 0.
* ``req-coverage --exit-nonzero-on-error`` — exit 1 if any
  finding (missing Requirements or broken REQ-ID ref) is present.
  Pre-commit hook integration relies on this flag.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from req_coverage.audit import audit_repo
from req_coverage.formatter import format_json, format_text

_REPO_ROOT = Path(__file__).resolve().parents[3]


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    args = _parse_args(argv)
    report = audit_repo(args.repo_root)
    output = (
        format_json(report) if args.json else format_text(report)
    )
    print(output, end="")
    if args.exit_nonzero_on_error and not report.is_clean:
        return 1
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Build the argparse.Namespace for ``main``."""
    parser = argparse.ArgumentParser(
        prog="req-coverage",
        description=(
            "Audit D→REQ coverage per the policy memo: each "
            "non-superseded DECISIONS.md entry must declare its "
            "REQ-IDs, and each REQ-ID must resolve."
        ),
    )
    parser.add_argument(
        "--repo-root", type=Path, default=_REPO_ROOT,
        help="Repo root for resolving DECISIONS.md, REQUIREMENTS.md",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of text",
    )
    parser.add_argument(
        "--exit-nonzero-on-error", action="store_true",
        help="Exit 1 if any coverage gap or broken ref found",
    )
    return parser.parse_args(argv)
