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

import os
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

# 1 MiB ceiling on --briefing-from-file content. Real briefings
# today are ~10 KB (2026-04-21_sha256.md is ~12 KB); an
# operator-supplied path orders of magnitude larger is almost
# certainly a wrong-file-in-flight mistake, and we would rather
# catch that at validation time than OOM the tool on a
# gigabyte file. hygiene-gaps.md #29.
_BRIEFING_SOURCE_MAX_BYTES = 1 << 20


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
        briefing_source: Path | None = None,
    ) -> None:
        self.slug = slug
        self.scope_paths = list(scope_paths)
        self.required_reading = list(required_reading)
        self.deliverables = deliverables
        self.rationale = rationale
        self.date = date
        self.repo_root = Path(repo_root)
        self.briefing_source = (
            Path(briefing_source) if briefing_source is not None
            else None
        )
        self._briefing_source_content: str | None = None

    # -- Public entry --

    def run(self) -> BootstrapResult:
        """Perform the D052 dispatch steps with full rollback on
        any mid-run failure."""
        # Read-only validation first — dirty tree, correct branch
        # (D052 requires "main"), briefing source (if given) is a
        # readable file, capture main's tip sha.
        self._check_clean_main()
        self._check_briefing_source_readable()
        saved_head = worktree_ops.current_head_sha(self.repo_root)

        # Build the task WITH parent_commit_sha baked in. Runs
        # SideSessionTask's SafeId/IsoDate/Description validators
        # at the earliest possible point, so a malicious slug
        # never reaches path construction. ``parent_commit_sha``
        # pins the DAG record back to the git HEAD it was cut
        # from, closing hygiene-gaps.md #14.
        task = self._build_task(parent_commit_sha=saved_head)
        # Pass the SafeId-validated task.slug / task.date through
        # to the path helpers, not the raw self.* fields. Today
        # _build_task raises BootstrapError before this line if
        # validation fails, so self.slug can never leak into
        # _worktree_path — but expressing the invariant in types
        # (the helpers now demand a validated slug) is defense-
        # in-depth against a future refactor that reorders run()
        # or weakens SafeId. hygiene-gaps.md #28.
        worktree_path = self._worktree_path(task.slug)
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

        briefing_path = self._briefing_path(
            worktree_path, task.date, task.slug,
        )
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
        ``main`` with no dirty state. The dispatch commit itself
        stages a single named file (``_DAG_RELPATH``), not ``-a``,
        so a dirty tree wouldn't be silently swept into the
        commit — but the clean-tree precondition remains the
        right discipline: a dispatch that lands while other
        unrelated work is half-done leaves ambiguity about
        which commit introduced what. Fail fast and let the
        operator stash / commit first."""
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

    def _check_briefing_source_readable(self) -> None:
        """When ``--briefing-from-file`` is in play, read the source
        now and cache the bytes. Validated pre-mutation — a typo,
        permission issue, encoding error, size-above-ceiling, or
        directory-as-source surfaces before any worktree / branch
        / DAG state is created. Caching also closes the TOCTOU
        window between validation and the later copy in
        ``_render_briefing``."""
        if self.briefing_source is None:
            return
        _check_briefing_source_size(self.briefing_source)
        try:
            self._briefing_source_content = (
                self.briefing_source.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError) as exc:
            raise BootstrapError(
                f"briefing source {self.briefing_source} is not a "
                f"readable UTF-8 file: {exc}",
            ) from exc

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

        When ``briefing_source`` is set the file's bytes are
        copied verbatim into the worktree path — lets an
        operator hand-author a rich briefing (vector tables,
        ISA-instruction references) that the
        ``render_briefing(task)`` template couldn't otherwise
        carry. Templated rendering is still the default.

        Defined as a method so behavioral rollback tests can
        monkeypatch it with raising=True."""
        briefing_path = self._briefing_path(
            worktree_path, task.date, task.slug,
        )
        briefing_path.parent.mkdir(parents=True, exist_ok=True)
        if self._briefing_source_content is not None:
            content = self._briefing_source_content
        else:
            content = render_briefing(task)
        briefing_path.write_text(content, encoding="utf-8")

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
                f"invalid task input: {_format_validation_error(exc)}",
            ) from exc

    def _worktree_path(self, slug: str) -> Path:
        """Derive the sibling-worktree path. Callers MUST pass a
        ``SafeId``-validated slug (``task.slug``), not the raw
        ``self.slug`` field — expressing the invariant at the
        helper's signature is defense-in-depth against a future
        refactor that reorders ``run()``'s validation step.
        hygiene-gaps.md #28."""
        return (
            self.repo_root.parent
            / f"{self.repo_root.name}-{slug}"
        )

    def _briefing_path(
        self, worktree_path: Path, date: str, slug: str,
    ) -> Path:
        """Derive the briefing markdown path inside the peer
        worktree. Callers MUST pass ``IsoDate``- and ``SafeId``-
        validated date + slug values (``task.date``, ``task.slug``)
        — same rationale as ``_worktree_path``. hygiene-gaps.md
        #28."""
        return (
            worktree_path / "docs" / "side_sessions"
            / f"{date}_{slug}.md"
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
        # Default agent binary is claude-code; override via
        # FIREASMSERVER_AGENT_CMD for cases where the operator
        # runs a different shell wrapper or the binary isn't
        # on PATH. hygiene-gaps.md #21.
        agent_cmd = os.environ.get(
            "FIREASMSERVER_AGENT_CMD", "claude",
        )
        return (
            f"cd {quoted_path}\n"
            f"{agent_cmd}\n\n"
            f"Then paste:\n"
            f"  Read {rel_briefing} and execute it. "
            f"You are on branch {branch} in an isolated "
            f"worktree. Do not checkout other branches. "
            f"Report your plan before writing implementation code."
        )


def _check_briefing_source_size(path: Path) -> None:
    """Pre-flight size check for ``--briefing-from-file`` paths.
    Extracted from ``_check_briefing_source_readable`` to keep
    that method under the project's cyclomatic-complexity cap
    (flake8 ``--max-complexity=5``). hygiene-gaps.md #29."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BootstrapError(
            f"briefing source {path} is not stat-able: {exc}",
        ) from exc
    if size > _BRIEFING_SOURCE_MAX_BYTES:
        raise BootstrapError(
            f"briefing source {path} is {size} bytes, above the "
            f"{_BRIEFING_SOURCE_MAX_BYTES}-byte ceiling; refusing "
            "to read (likely a wrong-file-in-flight operator error)",
        )


def _format_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic ``ValidationError`` into a flat
    operator-readable string. Pydantic's ``.errors()`` returns a
    list of dicts whose Python repr leaks to stderr unreadably;
    this formatter joins per-field lines with "; ". Closes
    ``project_ontology_hygiene_gaps.md`` #20."""
    lines: list[str] = []
    for err in exc.errors():
        loc_path = err.get("loc", ())
        loc = ".".join(str(part) for part in loc_path) or "<root>"
        msg = err.get("msg", "validation failed")
        lines.append(f"{loc}: {msg}")
    return "; ".join(lines)


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
        except BaseException as exc:  # pylint: disable=broad-except
            # BaseException not Exception: a KeyboardInterrupt
            # raised inside a rollback hook MUST NOT abort the
            # remaining hooks — the operator wants every bit of
            # cleanup we can give them before the interpreter
            # tears down. hygiene-gaps.md #27.
            #
            # Trade-off the future reader should know about: a
            # Ctrl-C struck DURING rollback is absorbed here
            # rather than re-raised. The operator needs a second
            # Ctrl-C (or for run()'s outer BaseException arm to
            # fire) to actually exit. Best-effort cleanup is
            # judged more important than single-Ctrl-C exit
            # responsiveness — don't "fix" this by narrowing
            # back to Exception without re-reading #27.
            if reraise_annotation:
                errors.append(f"{type(exc).__name__}: {exc}")
    return errors
