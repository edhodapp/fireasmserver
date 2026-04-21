"""Orchestrates the D052 dispatch flow.

Public API: ``Bootstrapper(...)`` + ``.run()`` →
``BootstrapResult``. Failure modes raise ``BootstrapError`` with
a human-readable message; the CLI maps to exit-code 1 and the
tests use ``pytest.raises``.

The six D052 steps — validate, DAG-write, commit-on-main,
worktree-add, venv, briefing-render — run with explicit rollback
hooks registered as each step succeeds. On any mid-run failure
the hooks fire in reverse order so no partial state survives.
``_setup_venv`` and ``_render_briefing`` are methods (not free
functions) specifically so the behavioral rollback tests can
monkeypatch them with ``raising=True``.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ontology import SideSessionTask, make_branch_name

from side_session_bootstrap import venv_ops, worktree_ops
from side_session_bootstrap.ontology_writer import (
    OntologyWriteError,
    write_dispatch_node,
)
from side_session_bootstrap.template import render_briefing

_DAG_RELPATH = "tooling/qemu-harness.json"


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

    Fields mirror the ``SideSessionTask`` ontology model.
    ``run()`` is the only public entry point.
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

    # -- Public entry --

    def run(self) -> BootstrapResult:
        """Perform the D052 dispatch steps with full rollback on
        any mid-run failure."""
        # Read-only validation first — dirty tree, correct branch
        # (D052 requires "main"), capture main's tip sha.
        self._check_clean_main()
        saved_head = worktree_ops.current_head_sha(self.repo_root)

        # Build the task WITH parent_commit_sha baked in. Runs
        # SideSessionTask's SafeId/IsoDate/Description validators
        # at the earliest possible point, so a malicious slug
        # never reaches path construction. ``parent_commit_sha``
        # pins the DAG record back to the git HEAD it was cut
        # from, closing hygiene-gaps.md #14.
        task = self._build_task(parent_commit_sha=saved_head)
        worktree_path = self._worktree_path()
        branch = make_branch_name(task.slug, task.date)
        self._check_no_preexisting_paths(worktree_path, branch)

        # Mutation block. Rollback hooks are pre-registered with
        # idempotent helpers so a mid-step failure anywhere below
        # reliably unwinds. reset_hard_to restores both the
        # commit and the working tree. cleanup_worktree_and_branch
        # is a no-op if the worktree / branch never got created
        # (remove/delete use check=False), which is why we can
        # register it up-front rather than after create_worktree
        # returns.
        rollback: list[Callable[[], None]] = [
            lambda: worktree_ops.reset_hard_to(
                self.repo_root, saved_head,
            ),
            lambda: self._cleanup_worktree_and_branch(
                worktree_path, branch,
            ),
        ]
        try:
            self._execute_mutation(
                task, worktree_path, branch,
            )
        except BootstrapError as exc:
            _handle_dispatch_failure(exc, rollback, wrap=False)
        # pylint: disable=broad-exception-caught
        # Intentional: any non-BootstrapError / non-BaseException
        # exception from the mutation block must trigger
        # rollback and get wrapped as BootstrapError so the CLI
        # surfaces a consistent operator-facing error type.
        except Exception as exc:
            _handle_dispatch_failure(exc, rollback, wrap=True)
        except BaseException:
            # Ctrl-C / SystemExit: best-effort cleanup without
            # annotation (the interpreter is likely tearing down
            # anyway). hygiene-gaps.md #12.
            _run_rollback(rollback, reraise_annotation=False)
            raise

        briefing_path = self._briefing_path(worktree_path)
        return BootstrapResult(
            worktree_path=worktree_path,
            branch_name=branch,
            briefing_path=briefing_path,
            launch_prompt=self._launch_prompt(
                worktree_path, branch, briefing_path,
            ),
        )

    # -- Validation --

    def _check_clean_main(self) -> None:
        """Dispatch requires the primary worktree to be on
        ``main`` with no dirty state — the dispatch record
        commits there (D052), and any uncommitted changes would
        be swept up by the commit."""
        if not worktree_ops.is_working_tree_clean(self.repo_root):
            raise BootstrapError(
                "primary worktree has dirty / uncommitted changes; "
                "stash or commit before dispatching a side session",
            )
        try:
            head_branch = worktree_ops.current_branch_name(
                self.repo_root,
            )
        except worktree_ops.GitOpError as exc:
            raise BootstrapError(
                f"primary worktree HEAD is detached or unresolvable "
                f"— cannot determine current branch: {exc}",
            ) from exc
        if head_branch != "main":
            raise BootstrapError(
                f"primary worktree must be on 'main' for dispatch "
                f"(D052); currently on {head_branch!r}",
            )

    def _check_no_preexisting_paths(
        self, worktree_path: Path, branch: str,
    ) -> None:
        """Target sibling path and branch name must both be free
        before any mutation. Duplicate (slug, date) detection
        happens inside ``_write_and_commit_dispatch`` via the
        ontology's uniqueness model_validator."""
        if worktree_path.exists():
            raise BootstrapError(
                f"target worktree path {worktree_path} already "
                "exists; remove it or choose a different slug",
            )
        if worktree_ops.branch_exists(self.repo_root, branch):
            raise BootstrapError(
                f"branch {branch!r} already exists; delete it or "
                "choose a different slug / date",
            )

    # -- Mutation steps --

    def _execute_mutation(
        self,
        task: SideSessionTask,
        worktree_path: Path,
        branch: str,
    ) -> None:
        """The four ordered mutation steps. Extracted to keep
        ``run()``'s cyclomatic complexity under the project
        cap."""
        self._write_and_commit_dispatch(task)
        worktree_ops.create_worktree(
            self.repo_root, worktree_path, branch,
        )
        self._setup_venv(worktree_path)
        self._render_briefing(worktree_path, task)

    def _write_and_commit_dispatch(self, task: SideSessionTask) -> None:
        """Update ``qemu-harness.json`` with the new
        ``SideSessionTask`` and commit it on ``main``. A duplicate
        (slug, date) surfaces here as an ``OntologyWriteError``
        wrapping the Pydantic ``ValidationError`` — re-raised as
        a ``BootstrapError`` matching the
        ``test_bootstrap_refuses_duplicate_slug_same_date``
        regex."""
        try:
            write_dispatch_node(self.repo_root, task)
        except OntologyWriteError as exc:
            raise BootstrapError(
                f"duplicate or invalid SideSessionTask "
                f"(slug={self.slug!r}, date={self.date!r}) — "
                f"exists? {exc}"
            ) from exc
        try:
            worktree_ops.stage_and_commit(
                self.repo_root,
                _DAG_RELPATH,
                f"side_session: dispatch {self.slug}@{self.date}",
            )
        except worktree_ops.GitOpError as exc:
            raise BootstrapError(
                f"git commit of dispatch record failed: {exc}"
            ) from exc

    def _setup_venv(self, worktree_path: Path) -> None:
        """Provision ``<worktree>/.venv`` with ``pip install -e
        .[dev]``. Wrapped by rollback — a failure here removes
        the worktree and resets main.

        Defined as a method (not a free call) so behavioral
        rollback tests can monkeypatch it with raising=True."""
        try:
            venv_ops.create_venv(worktree_path)
        except venv_ops.VenvSetupError as exc:
            raise BootstrapError(f"venv setup failed: {exc}") from exc

    def _render_briefing(
        self, worktree_path: Path, task: SideSessionTask,
    ) -> None:
        """Render the canonical briefing markdown into
        ``<worktree>/docs/side_sessions/<date>_<slug>.md``.
        Takes the pre-built ``task`` to avoid re-running
        SideSessionTask's Pydantic validators a second time.

        Defined as a method so behavioral rollback tests can
        monkeypatch it with raising=True."""
        briefing_path = self._briefing_path(worktree_path)
        briefing_path.parent.mkdir(parents=True, exist_ok=True)
        briefing_path.write_text(
            render_briefing(task), encoding="utf-8",
        )

    def _cleanup_worktree_and_branch(
        self, worktree_path: Path, branch: str,
    ) -> None:
        """Remove the worktree then delete the branch. Order
        matters: ``git branch -D`` refuses to delete a branch
        that's still checked out, so the worktree must go
        first."""
        worktree_ops.remove_worktree(self.repo_root, worktree_path)
        worktree_ops.delete_branch(self.repo_root, branch)

    # -- Helpers --

    def _build_task(
        self, *, parent_commit_sha: str = "",
    ) -> SideSessionTask:
        """Construct and validate the SideSessionTask. SafeId /
        IsoDate / Description validators fire here; invalid input
        (path-traversal slug, malformed date, empty deliverables)
        is converted to ``BootstrapError`` with the underlying
        Pydantic detail so the CLI can surface a clean error.

        ``parent_commit_sha`` pins the task back to main's git
        HEAD at dispatch time — closes hygiene-gaps.md #14.
        Caller passes the value captured by
        ``worktree_ops.current_head_sha`` before any mutation."""
        try:
            return SideSessionTask(
                slug=self.slug,
                date=self.date,
                scope_paths=self.scope_paths,
                required_reading=self.required_reading,
                deliverables=self.deliverables,
                rationale=self.rationale,
                parent_commit_sha=parent_commit_sha,
            )
        except ValidationError as exc:
            raise BootstrapError(
                f"invalid task input: {exc.errors()}",
            ) from exc

    def _worktree_path(self) -> Path:
        return (
            self.repo_root.parent
            / f"{self.repo_root.name}-{self.slug}"
        )

    def _briefing_path(self, worktree_path: Path) -> Path:
        return (
            worktree_path / "docs" / "side_sessions"
            / f"{self.date}_{self.slug}.md"
        )

    def _launch_prompt(
        self, worktree_path: Path, branch: str, briefing_path: Path,
    ) -> str:
        rel_briefing = briefing_path.relative_to(worktree_path)
        # shlex.quote guarantees the cd target survives any
        # special char in the path (spaces, semicolons, shell
        # metacharacters). Slug validation should already
        # prevent these, but the quote here is a second defense
        # — Ed copy-pastes this prompt into a terminal, and a
        # shell-injection surface in the generated command is
        # not a surface to leave open.
        quoted_path = shlex.quote(str(worktree_path))
        return (
            f"cd {quoted_path}\n"
            f"claude\n\n"
            f"Then paste:\n"
            f"  Read {rel_briefing} and execute it. "
            f"You are on branch {branch} in an isolated "
            f"worktree. Do not checkout other branches. "
            f"Report your plan before writing implementation code."
        )


def _handle_dispatch_failure(
    exc: BaseException,
    rollback: list[Callable[[], None]],
    *,
    wrap: bool,
) -> None:
    """Run rollback hooks, then re-raise with rollback-failure
    annotations appended when any cleanup step failed.
    ``wrap=True`` turns a non-BootstrapError into a
    BootstrapError; ``wrap=False`` preserves an already
    operator-facing BootstrapError's message. Always raises."""
    errs = _run_rollback(rollback, reraise_annotation=True)
    annotation = (
        f" — rollback issues: {_fmt_errs(errs)}" if errs else ""
    )
    if wrap:
        raise BootstrapError(
            f"rollback fired: {exc}{annotation}",
        ) from exc
    if annotation:
        raise BootstrapError(f"{exc}{annotation}") from exc
    raise exc


def _fmt_errs(errors: list[str]) -> str:
    """Human-readable joining of rollback-hook error strings.
    Python's default ``[...]`` repr is noisy inside a flat
    error message; semicolon-joined reads better in a terminal.
    hygiene-gaps.md #15."""
    return "; ".join(errors)


def _run_rollback(
    rollback: list[Callable[[], None]],
    *,
    reraise_annotation: bool,
) -> list[str]:
    """Fire registered rollback hooks in reverse order.

    Each hook's exception is caught so later hooks still run
    (partial cleanup beats no cleanup). When
    ``reraise_annotation`` is True, the caller receives a list
    of error-summary strings to append to the outer
    ``BootstrapError`` — so a silent cleanup failure can't
    leave the operator staring at a "rolled back OK" message
    while the filesystem has a ghost worktree. Returns an empty
    list when no hook errored."""
    errors: list[str] = []
    for hook in reversed(rollback):
        try:
            hook()
        except Exception as exc:  # pylint: disable=broad-except
            if reraise_annotation:
                errors.append(f"{type(exc).__name__}: {exc}")
    return errors
