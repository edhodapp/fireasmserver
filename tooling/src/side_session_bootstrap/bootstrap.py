"""Orchestrates the D052 dispatch flow.

Public API: ``Bootstrapper(...)`` + ``.run()`` → ``BootstrapResult``.
Failure modes raise ``BootstrapError`` with a human-readable
message; the CLI maps to exit-code 1 and the tests use
``pytest.raises``.

The six D052 steps — validate, DAG-write, worktree-add, venv,
briefing-render, emit-prompt — land incrementally across
commits C3–C6. This module's `run()` currently raises
``NotImplementedError`` so the C2 behavioral tests go RED at
the expected point, not at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BootstrapResult:
    """Successful dispatch outcome — what ``cli.main`` prints and
    what behavioral tests inspect."""

    worktree_path: Path
    branch_name: str
    briefing_path: Path
    launch_prompt: str


class BootstrapError(Exception):
    """Raised when dispatch cannot proceed. Message is
    operator-facing — the CLI prints it to stderr before
    exit 1."""


class Bootstrapper:
    """Dispatches a side-session task per D052.

    Fields mirror the ``SideSessionTask`` ontology model
    (landing in C3). ``run()`` is the only public entry point.
    """

    def __init__(
        self,
        slug: str,
        scope_paths: list[str],
        required_reading: list[str],
        deliverables: str,
        rationale: str,
        date: str,
        repo_root: Path,
    ) -> None:
        self.slug = slug
        self.scope_paths = list(scope_paths)
        self.required_reading = list(required_reading)
        self.deliverables = deliverables
        self.rationale = rationale
        self.date = date
        self.repo_root = Path(repo_root)

    def run(self) -> BootstrapResult:
        """Perform the six D052 dispatch steps with full rollback
        on any mid-run failure."""
        raise NotImplementedError(
            "Bootstrapper.run() lands in C5 (worktree + venv ops); "
            "the C2 commit ships only the interface."
        )

    # The following internal hooks exist as C2 stubs so the
    # behavioral rollback tests can monkeypatch them with
    # ``raising=True`` (strict attribute presence check). C5 fills
    # in the real bodies; until then calling them directly is a
    # programmer error.

    def _setup_venv(self, worktree_path: Path) -> None:
        """Provision ``<worktree>/.venv`` and ``pip install -e
        .[dev]``. Lands in C5."""
        raise NotImplementedError(
            "_setup_venv lands in C5 (worktree + venv ops)"
        )

    def _render_briefing(self, worktree_path: Path) -> None:
        """Render the canonical briefing markdown into
        ``<worktree>/docs/side_sessions/<date>_<slug>.md``.
        Lands in C4."""
        raise NotImplementedError(
            "_render_briefing lands in C4 (renderer + writer)"
        )
