"""Command-line entry point for ``side-session-bootstrap``.

Thin argparse adapter over ``Bootstrapper``: parse args,
construct the instance, run it, print the launch prompt on
success or the error on failure. Installed as a console script
via ``[project.scripts]`` in ``pyproject.toml``.

Contract:
- ``repo_root`` is resolved from the current working directory
  (the main worktree's root — the caller runs the tool from
  there). Tests use ``monkeypatch.chdir`` to set this.
- Success: exit 0, launch prompt on stdout.
- Failure (``BootstrapError``): exit 1, message on stderr,
  nothing on stdout — so shell scripts can tell success from
  failure by exit code AND by which stream carries the message.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date as _date
from pathlib import Path

from side_session_bootstrap.bootstrap import (
    Bootstrapper,
    BootstrapError,
)


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    parser = _make_parser()
    args = parser.parse_args(argv)
    try:
        bootstrapper = Bootstrapper(
            slug=args.slug,
            scope_paths=args.scope,
            required_reading=args.required_reading,
            deliverables=args.deliverables,
            rationale=args.rationale or "",
            date=args.date or _date.today().isoformat(),
            repo_root=Path.cwd(),
        )
        result = bootstrapper.run()
    except BootstrapError as exc:
        print(f"side-session-bootstrap: {exc}", file=sys.stderr)
        return 1
    print(result.launch_prompt)
    return 0


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="side-session-bootstrap",
        description=(
            "Dispatch a scoped task to a peer git worktree "
            "per DECISIONS.md D052."
        ),
    )
    parser.add_argument(
        "--slug", required=True,
        help=(
            "short snake_case identifier for the task. "
            "SafeId-validated downstream; path-traversal / "
            "git-ref-illegal slugs are rejected."
        ),
    )
    parser.add_argument(
        "--scope", action="append", default=[],
        help=(
            "repo-relative file or directory the side session "
            "may touch. May be given multiple times."
        ),
    )
    parser.add_argument(
        "--required-reading", action="append", default=[],
        dest="required_reading",
        help=(
            "reference tag added to the briefing's required-"
            "reading list. May be given multiple times. The "
            "canonical set (CLAUDE.md, D049, D051, D052, the "
            "parallelization feedback memories) is included "
            "automatically."
        ),
    )
    parser.add_argument(
        "--deliverables", required=True,
        help=(
            "one-sentence summary of what the side session "
            "produces. Shown in the briefing header and the "
            "dispatch DAG node."
        ),
    )
    parser.add_argument(
        "--rationale", default="",
        help="optional longer justification for the task.",
    )
    parser.add_argument(
        "--date", default="",
        help=(
            "dispatch date in YYYY-MM-DD form. Defaults to "
            "today's UTC-ish local date. Combined with --slug "
            "as the (slug, date) uniqueness key."
        ),
    )
    return parser
