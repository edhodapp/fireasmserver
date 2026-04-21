"""Per-worktree virtualenv provisioning.

D052 requires each side worktree to own its own ``.venv``: the
editable ``pip install -e .[dev]`` locks the console-script and
package paths to the worktree's own ``tooling/src/``, so the
side session's package edits take effect when it imports its
own code. A shared venv would resolve against the main
worktree's source and defeat the isolation.

This module exposes a single entry point, ``create_venv``, that
the ``Bootstrapper`` calls after ``worktree_ops.create_worktree``
succeeds. Tests mock the subprocess calls — a real venv takes
10–20 s and adds no information beyond exit code.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class VenvSetupError(Exception):
    """Raised when ``python -m venv`` or the editable install
    fails. The Bootstrapper catches this and fires its rollback
    pipeline (remove worktree, delete branch, reset main)."""


def create_venv(worktree_path: Path) -> Path:
    """Create ``<worktree_path>/.venv`` and install the project
    as editable with dev extras. Returns the venv path on
    success.

    Uses ``sys.executable`` rather than a bare ``python`` to
    guarantee the same Python that's running the bootstrap tool
    creates the new venv — avoids surprising version mismatches
    if the shell's ``python`` resolves to a different
    interpreter. The venv's own ``pip`` then drives the editable
    install, so its Python version is inherited.
    """
    venv_path = worktree_path / ".venv"
    _run(
        [sys.executable, "-m", "venv", str(venv_path)],
        cwd=worktree_path,
        step="venv creation",
    )
    pip = venv_path / "bin" / "pip"
    _run(
        [str(pip), "install", "--quiet", "-e", ".[dev]"],
        cwd=worktree_path,
        step="editable install",
    )
    return venv_path


def _run(
    argv: list[str], *, cwd: Path, step: str,
) -> None:
    """Run ``argv`` in ``cwd``, wrapping any failure as
    ``VenvSetupError`` with the stderr preserved for the
    operator's benefit."""
    try:
        subprocess.run(
            argv, cwd=str(cwd), check=True,
            capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise VenvSetupError(
            f"{step} failed in {cwd}: "
            f"{exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc
