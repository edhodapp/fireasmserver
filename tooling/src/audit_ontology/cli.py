"""Command-line entry point for ``audit-ontology``.

Three invocation modes (see briefing §Outputs):

* ``audit-ontology`` — human-readable matrix on stdout, exit 0.
* ``audit-ontology --json`` — JSON per the schema pinned in the
  briefing's appendix, exit 0.
* ``audit-ontology --exit-nonzero-on-gap`` — exit 1 iff any gap
  or broken ref was found. Designed for future pre-push / CI
  integration once main session wires it into the pipeline.

``--dag-path`` and ``--repo-root`` defaults point at the live
artifacts; both are overridable so test fixtures can feed
synthetic inputs.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from audit_ontology.audit import run_audit
from audit_ontology.formatter import format_json, format_text

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DAG = _REPO_ROOT / "tooling" / "qemu-harness.json"


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    args = _parse_args(argv)
    report = run_audit(args.dag_path, args.repo_root)
    output = (
        format_json(report) if args.json else format_text(report)
    )
    print(output, end="")
    if args.exit_nonzero_on_gap:
        return _exit_code_for(report)
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Build the argparse.Namespace for ``main``. Factored out so
    tests can call it directly without sys.argv mocking."""
    parser = argparse.ArgumentParser(
        prog="audit-ontology",
        description=(
            "Audit the fireasmserver ontology's traceability refs "
            "against the repo working tree."
        ),
    )
    parser.add_argument(
        "--dag-path", type=Path, default=_DEFAULT_DAG,
        help="Path to the DAG JSON (default: tooling/qemu-harness.json)",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=_REPO_ROOT,
        help="Repo root for resolving refs (default: tool's repo)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON per the briefing schema instead of text",
    )
    parser.add_argument(
        "--exit-nonzero-on-gap", action="store_true",
        help="Exit 1 if any gap or broken ref is found",
    )
    return parser.parse_args(argv)


def _exit_code_for(report: object) -> int:
    """1 if the report has any gaps or broken refs, else 0.
    ``getattr`` access keeps this function structurally
    independent of the AuditReport type for easier testing."""
    summary = getattr(report, "summary", None)
    gap_count = getattr(summary, "gap_count", 0)
    broken = getattr(summary, "broken_ref_count", 0)
    if gap_count + broken > 0:
        return 1
    return 0
