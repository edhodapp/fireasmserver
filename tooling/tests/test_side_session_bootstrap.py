"""Behavioral tests for ``side_session_bootstrap`` (DECISIONS.md D052).

**Written before implementation.** Each test describes an
observable outcome of the dispatch flow — the success path,
four refusal modes, two rollback cases, the canonical briefing
shape, and the launch-prompt contents. Until the implementation
commits (C3 ontology model, C4 renderer + ontology-writer, C5
worktree + venv ops, C6 CLI) land, these tests go RED at
``Bootstrapper.run()`` raising ``NotImplementedError``. Each
subsequent commit turns a subset green.

Each test runs against a throwaway ``tmp_path`` git repo with a
minimal DAG fixture. Tests never touch the real fireasmserver
working tree.

Cross-reference: DECISIONS.md D052, D049 (ontology schema),
D051 (pre-push audit gate).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from side_session_bootstrap import (
    Bootstrapper,
    BootstrapError,
    BootstrapResult,
)
from side_session_bootstrap import cli as cli_module


# All behavioral tests in this file are RED until the
# implementation lands across commits C3–C6. ``strict=True`` means
# an xpass (test unexpectedly passes before I've removed the
# marker) becomes a real failure — forcing me to keep the marker
# structure in sync with reality as each commit turns a slice
# green. As tests go green, this module-level marker narrows to
# per-test markers for the ones still waiting.
pytestmark = pytest.mark.xfail(
    strict=True,
    reason=(
        "RED until C3 (ontology model), C4 (renderer + writer), "
        "C5 (worktree + venv + rollback), C6 (CLI wiring)"
    ),
)


# ---------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke git in ``repo``; raise on non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(name="minimal_repo")
def _minimal_repo(tmp_path: Path) -> Path:
    """Throwaway git repo that looks like fireasmserver enough for
    the bootstrapper: initial commit, empty DAG at the expected
    path, ``tooling/src/`` tree present so an editable install
    has something to point at.

    Returns the primary-worktree path. Sibling worktrees the
    bootstrapper creates live at ``<parent>/<repo_name>-<slug>``.
    """
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test")
    _git(repo, "config", "user.name", "test")

    (repo / "tooling").mkdir()
    (repo / "tooling" / "src").mkdir()
    dag_fixture: dict[str, Any] = {
        "project_name": "test-project",
        "nodes": [],
    }
    (repo / "tooling" / "qemu-harness.json").write_text(
        json.dumps(dag_fixture, indent=2)
    )
    (repo / "tooling" / "src" / "__init__.py").write_text("")
    _git(repo, "add", "tooling/")
    _git(repo, "commit", "-m", "init test repo")
    return repo


def _make_bootstrapper(
    repo: Path,
    *,
    slug: str = "demo_task",
    scope_paths: list[str] | None = None,
    required_reading: list[str] | None = None,
    deliverables: str = "demo deliverables",
    rationale: str = "",
    date: str = "2026-04-20",
) -> Bootstrapper:
    """Test helper — builds a Bootstrapper with sensible defaults
    so individual tests override only the fields they care about."""
    return Bootstrapper(
        slug=slug,
        scope_paths=scope_paths or ["tooling/src/demo/"],
        required_reading=required_reading or ["DECISIONS.md:D049"],
        deliverables=deliverables,
        rationale=rationale,
        date=date,
        repo_root=repo,
    )


def _load_dag(repo: Path) -> dict[str, Any]:
    """Read the DAG JSON from the test repo."""
    data = json.loads(
        (repo / "tooling" / "qemu-harness.json").read_text()
    )
    assert isinstance(data, dict), "DAG fixture must be a JSON object"
    return data


def _tasks_from_flat_nodes(dag: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract SideSessionTask candidates from a flat ``nodes``
    list with a ``kind`` discriminator."""
    return [
        node for node in dag.get("nodes", [])
        if isinstance(node, dict)
        and node.get("kind") == "SideSessionTask"
    ]


def _tasks_from_snapshots(dag: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract SideSessionTask candidates from
    ``snapshots[...].ontology.side_session_tasks`` nesting."""
    result: list[dict[str, Any]] = []
    for snap in dag.get("snapshots", []):
        if not isinstance(snap, dict):
            continue
        onto = snap.get("ontology", {})
        for task in onto.get("side_session_tasks", []):
            if isinstance(task, dict):
                result.append(task)
    return result


def _find_side_session_task(
    dag: dict[str, Any], slug: str, date: str
) -> dict[str, Any] | None:
    """Locate a SideSessionTask node in the DAG fixture by slug+date.

    Accepts two reasonable DAG shapes — flat ``nodes`` list with
    a ``kind`` discriminator, or nested under
    ``snapshots[...].ontology.side_session_tasks`` — so the
    behavioral test doesn't constrain C4's ontology-writer
    layout choice. Will narrow to the actual shape once C4
    lands (flagged post-review).
    """
    for task in _tasks_from_flat_nodes(dag) + _tasks_from_snapshots(dag):
        if task.get("slug") == slug and task.get("date") == date:
            return task
    return None


# ---------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------


def test_bootstrap_happy_path_creates_all_artifacts(
    minimal_repo: Path,
) -> None:
    """End-to-end success: peer worktree created at the sibling
    path, branch cut, ``SideSessionTask`` dispatched on main +
    inherited by the worktree, briefing rendered inside the
    worktree, launch prompt returned."""
    bs = _make_bootstrapper(minimal_repo)
    result = bs.run()

    assert isinstance(result, BootstrapResult)

    # Sibling worktree convention per D052.
    expected_wt = minimal_repo.parent / "primary-demo_task"
    assert result.worktree_path == expected_wt
    assert expected_wt.is_dir()

    # Branch exists, worktree is on it.
    head = _git(
        expected_wt, "rev-parse", "--abbrev-ref", "HEAD"
    ).stdout.strip()
    assert head == "side/2026-04-20_demo_task"
    assert result.branch_name == "side/2026-04-20_demo_task"

    # Main has the dispatch record (committed on main before the
    # worktree was cut, so main sees it).
    dag_main = _load_dag(minimal_repo)
    task = _find_side_session_task(dag_main, "demo_task", "2026-04-20")
    assert task is not None, (
        "SideSessionTask must be committed on main so the main "
        "session can enumerate dispatched tasks"
    )
    assert task["status"] == "dispatched"
    assert task["slug"] == "demo_task"

    # Side worktree inherits the node (it was cut from the commit
    # that added it).
    dag_side = _load_dag(expected_wt)
    side_task = _find_side_session_task(
        dag_side, "demo_task", "2026-04-20"
    )
    assert side_task is not None

    # Briefing rendered inside the side worktree.
    briefing = (
        expected_wt / "docs" / "side_sessions"
        / "2026-04-20_demo_task.md"
    )
    assert briefing.is_file()
    assert result.briefing_path == briefing


def test_launch_prompt_includes_worktree_path_and_branch(
    minimal_repo: Path,
) -> None:
    """The prompt printed for Ed to paste into a new terminal
    must name (a) the worktree path to ``cd`` into, (b) the
    branch name, and (c) the briefing path to read."""
    bs = _make_bootstrapper(minimal_repo)
    result = bs.run()

    assert str(result.worktree_path) in result.launch_prompt
    assert "side/2026-04-20_demo_task" in result.launch_prompt
    assert "2026-04-20_demo_task.md" in result.launch_prompt


# ---------------------------------------------------------------
# Refusal modes — each must raise BootstrapError AND leave no
# partial state (no worktree, no branch, no DAG mutation, no
# briefing file).
# ---------------------------------------------------------------


def _assert_no_side_effects(
    minimal_repo: Path, slug: str, date: str
) -> None:
    """Post-refusal invariants: nothing was created anywhere."""
    sibling = minimal_repo.parent / f"primary-{slug}"
    assert not sibling.exists(), (
        f"refusal must not leave a partial worktree at {sibling}"
    )
    branches = _git(minimal_repo, "branch", "--list").stdout
    assert f"side/{date}_{slug}" not in branches
    dag = _load_dag(minimal_repo)
    assert _find_side_session_task(dag, slug, date) is None


def test_bootstrap_refuses_dirty_main_worktree(
    minimal_repo: Path,
) -> None:
    """Staged or unstaged changes in the primary worktree must
    abort dispatch before any state is created."""
    (minimal_repo / "new.txt").write_text("uncommitted")
    _git(minimal_repo, "add", "new.txt")

    bs = _make_bootstrapper(minimal_repo)
    with pytest.raises(BootstrapError, match=r"(?i)dirty|uncommitted"):
        bs.run()

    _assert_no_side_effects(minimal_repo, "demo_task", "2026-04-20")


def test_bootstrap_refuses_existing_worktree_path(
    minimal_repo: Path,
) -> None:
    """If the sibling worktree path already exists on disk,
    dispatch must abort — never overwrite."""
    sibling = minimal_repo.parent / "primary-demo_task"
    sibling.mkdir()
    (sibling / "squatter.txt").write_text("already here")

    bs = _make_bootstrapper(minimal_repo)
    with pytest.raises(BootstrapError, match=r"(?i)exists|present"):
        bs.run()

    # The pre-existing sibling dir is NOT considered partial
    # state — it wasn't ours. But nothing new should have been
    # created inside the primary.
    dag = _load_dag(minimal_repo)
    assert _find_side_session_task(
        dag, "demo_task", "2026-04-20"
    ) is None
    branches = _git(minimal_repo, "branch", "--list").stdout
    assert "side/2026-04-20_demo_task" not in branches


def test_bootstrap_refuses_existing_branch(
    minimal_repo: Path,
) -> None:
    """If the target branch already exists, dispatch must abort."""
    _git(minimal_repo, "branch", "side/2026-04-20_demo_task")

    bs = _make_bootstrapper(minimal_repo)
    with pytest.raises(BootstrapError, match=r"(?i)branch"):
        bs.run()

    _assert_no_side_effects(minimal_repo, "demo_task", "2026-04-20")


def test_bootstrap_refuses_duplicate_slug_same_date(
    minimal_repo: Path,
) -> None:
    """If the DAG already has a SideSessionTask with the same
    slug + date, dispatch must abort — the (slug, date) pair is
    the uniqueness key."""
    # Seed the DAG with a pre-existing task matching the fixture.
    dag_path = minimal_repo / "tooling" / "qemu-harness.json"
    dag = json.loads(dag_path.read_text())
    dag.setdefault("nodes", []).append({
        "kind": "SideSessionTask",
        "slug": "demo_task",
        "date": "2026-04-20",
        "status": "dispatched",
    })
    dag_path.write_text(json.dumps(dag, indent=2))
    _git(minimal_repo, "add", "tooling/qemu-harness.json")
    _git(minimal_repo, "commit", "-m", "seed duplicate task")

    bs = _make_bootstrapper(minimal_repo)
    with pytest.raises(BootstrapError, match=r"(?i)duplicate|exists"):
        bs.run()

    _assert_no_side_effects(minimal_repo, "demo_task", "2026-04-20")


# ---------------------------------------------------------------
# Rollback on mid-run failure — transactional guarantee per D052.
# ---------------------------------------------------------------


def test_bootstrap_rollback_on_venv_failure(
    minimal_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If venv setup fails after the worktree is created, the
    tool must clean up the worktree, delete the branch, and
    revert the main-side DAG commit so no partial state
    survives. Simulated by monkeypatching the venv helper."""
    # The exact module path the venv helper lands in is a C5
    # implementation detail. We patch a predictable location —
    # Bootstrapper._setup_venv if it exists; otherwise the test
    # fails naturally at run() and the implementer adapts this
    # hook when C5 lands.
    def _boom(self: Bootstrapper, worktree_path: Path) -> None:
        del self, worktree_path  # unused by the simulated failure
        raise RuntimeError("simulated venv failure")

    monkeypatch.setattr(
        Bootstrapper,
        "_setup_venv",
        _boom,
        raising=True,
    )

    bs = _make_bootstrapper(minimal_repo)
    with pytest.raises(BootstrapError, match=r"(?i)rollback|venv"):
        bs.run()

    # All state unwound.
    _assert_no_side_effects(minimal_repo, "demo_task", "2026-04-20")


def test_bootstrap_rollback_on_briefing_write_failure(
    minimal_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If briefing rendering fails after the worktree and venv
    are in place, everything (worktree, branch, DAG commit) must
    roll back. Exercises the last-step failure path."""
    def _boom(self: Bootstrapper, worktree_path: Path) -> None:
        del self, worktree_path  # unused by the simulated failure
        raise RuntimeError("simulated briefing write failure")

    monkeypatch.setattr(
        Bootstrapper,
        "_render_briefing",
        _boom,
        raising=True,
    )

    bs = _make_bootstrapper(minimal_repo)
    with pytest.raises(BootstrapError, match=r"(?i)rollback|briefing"):
        bs.run()

    _assert_no_side_effects(minimal_repo, "demo_task", "2026-04-20")


# ---------------------------------------------------------------
# Canonical briefing shape — the rendered markdown must include
# every load-bearing section in order, regardless of caller
# inputs.
# ---------------------------------------------------------------


# Markdown header form (``## <name>``) — bare substrings would
# match the word "task" appearing earlier in prose, since the
# test uses ``find()``.
CANONICAL_SECTION_HEADERS = [
    "## Read these before writing any code",
    "## Two firm rules from Ed",
    "## Task",
    "## Quality expectations",
    "## Directory scope",
    "## Coordination with parent",
    "## Gates to respect",
    "## Commit + push discipline",
    "## Definition of done",
    "## Status",
    "## Deviations from briefing",
    "## Observations for the main session",
]


def test_briefing_renders_canonical_sections_in_order(
    minimal_repo: Path,
) -> None:
    """The rendered briefing must contain every section header
    in the canonical order. The core required-reading set (global
    + project CLAUDE.md, D049, D051, D052, the
    parallelization-strategy + shared-index + face-and-move
    feedback memories) is always included regardless of
    caller-supplied ``--required-reading`` tags."""
    bs = _make_bootstrapper(minimal_repo)
    result = bs.run()

    text = result.briefing_path.read_text()

    last_pos = -1
    for header in CANONICAL_SECTION_HEADERS:
        pos = text.find(header)
        assert pos != -1, f"section header missing: {header!r}"
        assert pos > last_pos, (
            f"section header out of order: {header!r} at {pos} "
            f"followed header at {last_pos}"
        )
        last_pos = pos

    # The core required reading is always present, regardless of
    # caller input.
    for anchor in [
        "CLAUDE.md",
        "D049",
        "D051",
        "D052",
        "project_parallelization_strategy",
    ]:
        assert anchor in text, (
            f"briefing must always reference {anchor}"
        )


def test_briefing_includes_caller_supplied_required_reading(
    minimal_repo: Path,
) -> None:
    """Caller-supplied required-reading tags must ADD to the core
    set, not replace it. The rendered briefing shows both."""
    bs = _make_bootstrapper(
        minimal_repo,
        required_reading=["docs/l2/DESIGN.md", "D040"],
    )
    result = bs.run()
    text = result.briefing_path.read_text()

    assert "docs/l2/DESIGN.md" in text
    assert "D040" in text
    # Still has the core references.
    assert "D052" in text


# ---------------------------------------------------------------
# CLI-layer tests — thin argparse + exit-code behavior. The
# success path invokes ``Bootstrapper.run()`` in-process (no
# subprocess here — the end-to-end console-script test lives in
# C6 once pyproject exposes the entry point).
# ---------------------------------------------------------------


def test_cli_success_returns_zero_and_prints_launch_prompt(
    minimal_repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cli.main`` returns 0 on success and prints the launch
    prompt to stdout."""
    monkeypatch.chdir(minimal_repo)
    exit_code = cli_module.main([
        "--slug", "demo_task",
        "--scope", "tooling/src/demo/",
        "--required-reading", "DECISIONS.md:D049",
        "--deliverables", "demo deliverables",
        "--date", "2026-04-20",
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "primary-demo_task" in out
    assert "side/2026-04-20_demo_task" in out


def test_cli_refusal_returns_nonzero_and_prints_to_stderr(
    minimal_repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BootstrapError from the core must surface as exit 1 with
    the message on stderr, not stdout — so scripts can tell
    success from failure by exit code AND by which stream
    carries the message."""
    (minimal_repo / "dirty.txt").write_text("uncommitted")
    _git(minimal_repo, "add", "dirty.txt")
    monkeypatch.chdir(minimal_repo)

    exit_code = cli_module.main([
        "--slug", "demo_task",
        "--scope", "tooling/src/demo/",
        "--required-reading", "DECISIONS.md:D049",
        "--deliverables", "demo deliverables",
        "--date", "2026-04-20",
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err != ""
    assert captured.out == ""


def test_cli_rejects_slug_with_path_traversal_or_special_chars(
    minimal_repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closes the 2026-04-20 Gemini MEDIUM finding about
    unvalidated slugs: a slug containing ``..``, spaces, slashes,
    or git-ref-illegal characters must be rejected before ANY
    state is created.

    The model-layer validation (see
    ``test_side_session_task_model.py``) locks the raw Pydantic
    rejection; this test locks the user-observable behavior: the
    CLI exits non-zero, no error leaks to stdout, and nothing is
    created on disk. Goes GREEN once the CLI layer wires to the
    model validator (C6 at the latest; may go green earlier if
    the Bootstrapper constructs a ``SideSessionTask`` during
    ``run()``)."""
    # Snapshot the sibling-directory set BEFORE the run so we can
    # assert exactly this set persists after the refusal — a
    # stronger post-condition than substring-matching for known
    # bogus fragments.
    siblings_before = {p.name for p in minimal_repo.parent.iterdir()}
    branches_before = _git(
        minimal_repo, "branch", "--list"
    ).stdout

    monkeypatch.chdir(minimal_repo)
    exit_code = cli_module.main([
        "--slug", "../escape",
        "--scope", "tooling/src/demo/",
        "--required-reading", "DECISIONS.md:D049",
        "--deliverables", "demo deliverables",
        "--date", "2026-04-20",
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err != ""

    # No new directories — anywhere outside minimal_repo — and no
    # new branches. Catches bypasses the old "endswith('..')"
    # pattern would miss (e.g., a slug that creates a wholly
    # unrelated sibling name through some unforeseen substitution).
    siblings_after = {p.name for p in minimal_repo.parent.iterdir()}
    branches_after = _git(minimal_repo, "branch", "--list").stdout
    assert siblings_after == siblings_before
    assert branches_after == branches_before
