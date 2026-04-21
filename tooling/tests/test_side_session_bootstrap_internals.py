"""Unit tests for ``side_session_bootstrap`` internal modules
(``template`` and ``ontology_writer``).

These cover the pieces that come together as ``Bootstrapper.run()``
in C5. Tests here are GREEN immediately because each module is
exercised directly — no orchestration, no worktree setup, no
subprocess. The behavioral end-to-end tests in
``test_side_session_bootstrap.py`` stay xfail'd until C5 wires
the orchestration through these units.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ontology import Entity, Ontology, OntologyDAG, SideSessionTask
from ontology.dag import save_dag, save_snapshot
from side_session_bootstrap.ontology_writer import (
    OntologyWriteError,
    write_dispatch_node,
)
from side_session_bootstrap.template import render_briefing


# ---------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------


def _task(
    *,
    slug: str = "demo_task",
    date: str = "2026-04-20",
    deliverables: str = "demo deliverables",
    rationale: str = "",
    scope_paths: list[str] | None = None,
    required_reading: list[str] | None = None,
) -> SideSessionTask:
    """Test helper — build a SideSessionTask with sensible
    defaults so individual tests override only what they care
    about. ``None`` triggers the default; an empty list passes
    through unchanged so tests can exercise the empty-scope
    rendering."""
    return SideSessionTask(
        slug=slug,
        date=date,
        deliverables=deliverables,
        rationale=rationale,
        scope_paths=(
            scope_paths if scope_paths is not None
            else ["tooling/src/demo/"]
        ),
        required_reading=(
            required_reading if required_reading is not None
            else ["DECISIONS.md:D049"]
        ),
    )


@pytest.fixture(name="empty_dag_repo")
def _empty_dag_repo(tmp_path: Path) -> Path:
    """Create a tmp repo-shaped directory with an empty DAG file
    at the conventional path. Returns the repo root."""
    repo = tmp_path / "primary"
    (repo / "tooling").mkdir(parents=True)
    dag = OntologyDAG(project_name="fireasmserver")
    (repo / "tooling" / "qemu-harness.json").write_text(dag.to_json())
    return repo


# ---------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------


# Match against the actual markdown header lines
# (``## <name>`` form) rather than bare substrings — the bare
# ``Task`` form would collide with the word "task" appearing in
# earlier prose, since `find()` is just substring matching.
CANONICAL_HEADERS = [
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

CORE_REQUIRED_ANCHORS = [
    "CLAUDE.md",
    "D049",
    "D051",
    "D052",
    "project_parallelization_strategy",
]


class TestRenderBriefingShape:
    """The rendered markdown must contain every canonical
    section header, in order. The behavioral test in
    ``test_side_session_bootstrap.py`` asserts the same shape
    end-to-end via ``Bootstrapper.run()``; this unit test
    pins the same contract on the renderer alone."""

    def test_all_canonical_headers_in_order(self) -> None:
        text = render_briefing(_task())
        last = -1
        for header in CANONICAL_HEADERS:
            pos = text.find(header)
            assert pos != -1, f"missing section header: {header!r}"
            assert pos > last, (
                f"section header out of order: {header!r}"
            )
            last = pos

    def test_core_required_reading_always_present(self) -> None:
        """The core anchors appear regardless of the
        caller-supplied required-reading tags."""
        text_no_extras = render_briefing(_task(required_reading=[]))
        for anchor in CORE_REQUIRED_ANCHORS:
            assert anchor in text_no_extras, (
                f"core required-reading anchor missing: {anchor!r}"
            )

    def test_caller_required_reading_appears_alongside_core(
        self,
    ) -> None:
        """Caller-supplied tags ADD to the core set, not
        replace it."""
        text = render_briefing(_task(required_reading=[
            "docs/l2/DESIGN.md", "memory:user_role",
        ]))
        for anchor in CORE_REQUIRED_ANCHORS:
            assert anchor in text
        assert "docs/l2/DESIGN.md" in text
        assert "memory:user_role" in text

    def test_branch_name_in_output(self) -> None:
        text = render_briefing(_task())
        assert "side/2026-04-20_demo_task" in text

    def test_deliverables_in_output(self) -> None:
        text = render_briefing(_task(deliverables="custom thing"))
        assert "custom thing" in text

    def test_rationale_omitted_when_empty(self) -> None:
        text = render_briefing(_task(rationale=""))
        assert "Why this task exists" not in text

    def test_rationale_included_when_provided(self) -> None:
        text = render_briefing(_task(rationale="because reasons"))
        assert "Why this task exists" in text
        assert "because reasons" in text

    def test_scope_paths_listed(self) -> None:
        text = render_briefing(_task(
            scope_paths=["tooling/src/foo/", "docs/foo/"],
        ))
        assert "tooling/src/foo/" in text
        assert "docs/foo/" in text

    def test_empty_scope_paths_renders_warning(self) -> None:
        text = render_briefing(_task(scope_paths=[]))
        # No scope paths → an explicit "(none declared — confirm
        # with main session)" notice appears, not a silent empty
        # bullet list.
        assert "none declared" in text

    def test_caller_tag_with_backtick_escapes_safely(self) -> None:
        """A caller-supplied tag containing a backtick must not
        break the surrounding markdown — the renderer wraps with
        a longer backtick fence so the embedded backtick renders
        verbatim rather than terminating the code span."""
        text = render_briefing(_task(required_reading=[
            "has`backtick",
        ]))
        # The naive wrap ``has`backtick`` would close the first
        # code span after "has". The safe wrap pads with double
        # backticks and spaces.
        assert "`` has`backtick ``" in text

    def test_caller_tag_with_double_backticks_escapes_safely(
        self,
    ) -> None:
        """A tag with a double-backtick run requires a triple-
        backtick fence."""
        text = render_briefing(_task(required_reading=[
            "has``pair",
        ]))
        assert "``` has``pair ```" in text

    def test_caller_tag_without_backtick_uses_simple_wrap(
        self,
    ) -> None:
        """The common case — no backticks — still renders with
        the compact single-backtick form."""
        text = render_briefing(_task(required_reading=[
            "DECISIONS.md:D049",
        ]))
        assert "`DECISIONS.md:D049`" in text


# ---------------------------------------------------------------
# Ontology writer tests
# ---------------------------------------------------------------


def _load_dag_dict(repo: Path) -> dict[str, Any]:
    data = json.loads(
        (repo / "tooling" / "qemu-harness.json").read_text()
    )
    assert isinstance(data, dict), "DAG file must be a JSON object"
    return data


class TestWriteDispatchNode:
    """``write_dispatch_node`` appends a new SideSessionTask to
    the DAG at the canonical path, under
    ``ontology.dag_transaction``'s flock."""

    def test_appends_task_to_fresh_dag(
        self, empty_dag_repo: Path,
    ) -> None:
        node_id = write_dispatch_node(empty_dag_repo, _task())
        assert node_id  # non-empty uuid

        dag = OntologyDAG.model_validate(_load_dag_dict(empty_dag_repo))
        assert dag.current_node_id == node_id
        current = dag.get_current_node()
        assert current is not None
        assert len(current.ontology.side_session_tasks) == 1
        landed = current.ontology.side_session_tasks[0]
        assert landed.slug == "demo_task"
        assert landed.status == "dispatched"

    def test_inherits_existing_ontology_content(
        self, tmp_path: Path,
    ) -> None:
        """When the DAG already has a current node, the new
        snapshot includes everything the old one had — the
        bootstrap shouldn't drop unrelated entities or
        constraints."""
        repo = tmp_path / "primary"
        (repo / "tooling").mkdir(parents=True)
        dag = OntologyDAG(project_name="fireasmserver")
        save_snapshot(dag, Ontology(entities=[
            Entity(id="seeded_entity", name="seeded"),
        ]), label="seed")
        save_dag(dag, str(repo / "tooling" / "qemu-harness.json"))

        write_dispatch_node(repo, _task())

        reloaded = OntologyDAG.model_validate(_load_dag_dict(repo))
        current = reloaded.get_current_node()
        assert current is not None
        # Both the seeded entity AND the new task are present
        # in the new snapshot.
        entity_ids = {e.id for e in current.ontology.entities}
        assert "seeded_entity" in entity_ids
        assert len(current.ontology.side_session_tasks) == 1

    def test_duplicate_slug_date_raises(
        self, empty_dag_repo: Path,
    ) -> None:
        """Second dispatch of the same (slug, date) pair must be
        rejected — the ontology-layer uniqueness check catches it
        and the transaction rolls back."""
        write_dispatch_node(empty_dag_repo, _task())
        with pytest.raises(OntologyWriteError) as exc:
            write_dispatch_node(empty_dag_repo, _task(
                deliverables="different deliverables",
            ))
        assert "demo_task" in str(exc.value)

    def test_rollback_on_duplicate_leaves_dag_unchanged(
        self, empty_dag_repo: Path,
    ) -> None:
        """The on-disk DAG content after a rejected duplicate must
        match its content before the rejected call — `dag_transaction`
        skips the save on exception."""
        first_id = write_dispatch_node(empty_dag_repo, _task())
        before = _load_dag_dict(empty_dag_repo)

        with pytest.raises(OntologyWriteError):
            write_dispatch_node(empty_dag_repo, _task(
                deliverables="different deliverables",
            ))

        after = _load_dag_dict(empty_dag_repo)
        assert before == after
        # Sanity: the surviving DAG still has only the first node.
        dag = OntologyDAG.model_validate(after)
        assert dag.current_node_id == first_id

    def test_distinct_slug_or_date_succeeds(
        self, empty_dag_repo: Path,
    ) -> None:
        """Tasks differing in either slug OR date are permitted —
        the uniqueness key is the pair, not either alone."""
        write_dispatch_node(empty_dag_repo, _task(
            slug="task_a", date="2026-04-20",
        ))
        write_dispatch_node(empty_dag_repo, _task(
            slug="task_b", date="2026-04-20",
        ))
        write_dispatch_node(empty_dag_repo, _task(
            slug="task_a", date="2026-04-21",
        ))
        dag = OntologyDAG.model_validate(_load_dag_dict(empty_dag_repo))
        current = dag.get_current_node()
        assert current is not None
        assert len(current.ontology.side_session_tasks) == 3
