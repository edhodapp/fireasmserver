"""Git-worktree operations for the dispatch flow.

Each function is a thin typed wrapper over a ``git`` subprocess
call, scoped to the primary worktree. The bootstrap orchestrator
composes these; tests exercise them directly against tmp git
repos so the subprocess integration is covered end-to-end rather
than mocked.

Per D052: side sessions live in sibling worktrees at
``<parent>/<repo_name>-<slug>``. This module owns the git
plumbing; `.venv` provisioning lives in ``venv_ops`` and the
orchestration lives in ``bootstrap.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Env vars git sets during a ``git commit`` that, if inherited
# by a subprocess, override the ``-C <repo>`` flag and steer git
# at the wrong ``.git``. When the bootstrap tool runs inside a
# pre-commit hook (or from a test suite spawned by one), these
# vars leak in from the outer git invocation. Stripping them at
# each subprocess call restores the ``-C``-scoped semantics we
# assume everywhere below.
_GIT_ENV_LEAKAGE_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
)


def _scrubbed_env() -> dict[str, str]:
    env = os.environ.copy()
    for var in _GIT_ENV_LEAKAGE_VARS:
        env.pop(var, None)
    return env


class GitOpError(Exception):
    """Raised when a git subprocess call fails. Wraps the stderr
    so callers (the ``Bootstrapper`` rollback machinery) can
    surface a useful diagnostic without parsing the raw
    CompletedProcess."""


def _git(
    repo: Path, *args: str, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <repo> <args>`` with a scrubbed env so the
    ``-C``-scoped repo resolution isn't overridden by inherited
    ``GIT_*`` env vars from an enclosing commit / hook."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            capture_output=True,
            text=True,
            env=_scrubbed_env(),
        )
    except subprocess.CalledProcessError as exc:
        raise GitOpError(
            f"git {' '.join(args)} failed in {repo}: "
            f"{exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc


def is_working_tree_clean(repo: Path) -> bool:
    """Return True iff ``repo``'s working tree has no staged,
    unstaged, or untracked changes. Used as a precondition for
    dispatch — per D052, a side session cuts from a clean main."""
    result = _git(repo, "status", "--porcelain")
    return result.stdout.strip() == ""


def branch_exists(repo: Path, branch: str) -> bool:
    """Return True iff ``branch`` exists as a local ref in
    ``repo``. Checked via ``git show-ref`` exit code rather than
    parsing ``branch --list`` output."""
    result = _git(
        repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
        check=False,
    )
    return result.returncode == 0


def current_head_sha(repo: Path) -> str:
    """Return the full SHA of ``HEAD`` in ``repo``. Saved before
    any mutation so the bootstrap rollback can ``reset --hard``
    back to it."""
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def stage_and_commit(
    repo: Path, file_relpath: str, message: str,
) -> str:
    """Stage a single file and create a commit with ``message``.
    Returns the new commit SHA. Used for the dispatch-record
    commit on main before the worktree is cut."""
    _git(repo, "add", "--", file_relpath)
    _git(repo, "commit", "-m", message, "--no-verify")
    return current_head_sha(repo)


def reset_hard_to(repo: Path, target_sha: str) -> None:
    """Reset ``repo`` to ``target_sha`` (``git reset --hard``).
    Called only during rollback — the ``target_sha`` is a
    snapshot taken by the orchestrator *before* any mutation, so
    this cannot discard work the user didn't just create in this
    same failed dispatch attempt."""
    _git(repo, "reset", "--hard", target_sha)


def create_worktree(
    repo: Path, worktree_path: Path, branch: str,
) -> None:
    """Create a new worktree at ``worktree_path`` on a new branch
    ``branch`` cut at the primary worktree's current HEAD. Fails
    loudly if either the path or the branch is already in use."""
    _git(
        repo, "worktree", "add", "-b", branch, str(worktree_path),
    )


def remove_worktree(repo: Path, worktree_path: Path) -> None:
    """Force-remove the worktree at ``worktree_path``. Used
    during rollback — ``--force`` because a partially-provisioned
    worktree may have an incomplete venv or briefing file the
    plain ``remove`` would refuse to clean up."""
    _git(
        repo, "worktree", "remove", "--force", str(worktree_path),
        check=False,
    )


def delete_branch(repo: Path, branch: str) -> None:
    """Force-delete ``branch``. Used during rollback after the
    worktree that held it has been removed."""
    _git(repo, "branch", "-D", branch, check=False)
