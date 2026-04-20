"""Unit tests for ``SideSessionTask`` / ``SideSessionStatus`` / ``IsoDate``.

These pin the Pydantic validation contract the bootstrap tool
relies on — in particular the slug / date / status constraints
that close the 2026-04-20 Gemini MEDIUM finding about
unvalidated input reaching the worktree-creation path.

Behavioral (outside-in) coverage of the same invariants — the
user-visible refusal of a bootstrap run with a bad slug — lives
in ``test_side_session_bootstrap.py`` and stays RED until the
orchestration layer lands (C5–C6). These unit tests are GREEN
in C3 and guard against regressions in the model layer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from ontology import (
    Ontology,
    SideSessionStatus,
    SideSessionTask,
    make_branch_name,
)


class TestConstruction:
    """Happy-path + minimum-required-fields."""

    def test_minimum_valid(self) -> None:
        task = SideSessionTask(
            slug="demo_task",
            date="2026-04-20",
            deliverables="demo",
        )
        assert task.slug == "demo_task"
        assert task.date == "2026-04-20"
        assert task.deliverables == "demo"
        assert task.status == "dispatched"
        assert not task.scope_paths
        assert not task.required_reading
        assert task.parent_commit_sha == ""
        assert not task.commit_shas
        assert task.merge_commit_sha == ""

    def test_all_fields_explicit(self) -> None:
        task = SideSessionTask(
            slug="big_task",
            date="2026-04-20",
            scope_paths=["tooling/src/foo/", "docs/foo/"],
            required_reading=["DECISIONS.md:D049", "memory:feedback_x"],
            deliverables="does a big thing",
            rationale="because reasons",
            parent_commit_sha="deadbeef1234",
            status="in_progress",
            commit_shas=["abc", "def"],
            merge_commit_sha="fedcba",
        )
        assert task.status == "in_progress"
        assert task.scope_paths == ["tooling/src/foo/", "docs/foo/"]
        assert task.commit_shas == ["abc", "def"]
        assert task.merge_commit_sha == "fedcba"


class TestSlugValidation:
    """``SafeId`` constraints on slug close the 2026-04-20
    Gemini MEDIUM finding about path-traversal-through-slug.

    ``SafeId`` regex is ``^[a-zA-Z0-9_-]+$`` — the character set
    is small enough to guarantee both path safety and git-ref
    legality, and the non-empty constraint rules out the
    zero-length case."""

    @pytest.mark.parametrize("bad_slug", [
        "../escape",        # parent-dir traversal
        "has space",        # space (illegal in git refs + ugly in paths)
        "with/slash",       # path separator
        "dot.dot",          # period (git refs can't start with '.')
        ".",                # single-dot path reference
        "..",               # parent-dir literal
        "-leading",         # leading dash — argparse/shell confusion
        "at@host",          # illegal git ref char
        "back\\slash",      # Windows path separator
        "colon:thing",      # git ref component separator
        "question?mark",    # git ref illegal
        "star*",            # git ref illegal
        "tilde~",           # git ref illegal
        "caret^",           # git ref illegal
        "",                 # empty
        "a" * 101,          # exceeds SafeId max_length=100
    ])
    def test_rejects_unsafe_slug(self, bad_slug: str) -> None:
        with pytest.raises(ValidationError):
            SideSessionTask(
                slug=bad_slug,
                date="2026-04-20",
                deliverables="d",
            )

    @pytest.mark.parametrize("good_slug", [
        "demo_task",
        "abc123",
        "with-dash",
        "MixedCaseOk",
        "a",
        "with_many_underscores_and-dashes",
        "_leading_underscore_ok",
        "1_starts_with_digit",
        "a" * 100,          # at SafeId max_length boundary
    ])
    def test_accepts_safe_slug(self, good_slug: str) -> None:
        task = SideSessionTask(
            slug=good_slug,
            date="2026-04-20",
            deliverables="d",
        )
        assert task.slug == good_slug


class TestDateValidation:
    """``IsoDate`` regex enforces ``YYYY-MM-DD`` literal form."""

    @pytest.mark.parametrize("bad_date", [
        "2026-4-20",        # missing leading zero on month
        "2026-04-2",        # missing leading zero on day
        "26-04-20",         # two-digit year
        "20260420",         # no separators
        "2026/04/20",       # wrong separator
        "2026-04-20 ",      # trailing whitespace
        " 2026-04-20",      # leading whitespace
        "not-a-date",
        "",
        # Structurally valid YYYY-MM-DD but impossible on the
        # calendar — these pass the IsoDate regex and must be
        # caught by the datetime.fromisoformat validator.
        "2026-02-30",       # Feb 30 doesn't exist
        "2026-13-01",       # month 13
        "2026-00-15",       # month 0
        "2026-04-31",       # April has 30 days
        "2025-02-29",       # non-leap year
        "0000-01-01",       # ISO 8601 astronomical year 0 —
                            # valid ISO but Python's MINYEAR=1
    ])
    def test_rejects_bad_date(self, bad_date: str) -> None:
        with pytest.raises(ValidationError):
            SideSessionTask(
                slug="demo_task",
                date=bad_date,
                deliverables="d",
            )

    @pytest.mark.parametrize("good_date", [
        "2026-04-20",
        "0001-01-01",
        "9999-12-31",
    ])
    def test_accepts_iso_date(self, good_date: str) -> None:
        task = SideSessionTask(
            slug="demo_task",
            date=good_date,
            deliverables="d",
        )
        assert task.date == good_date


class TestStatusLifecycle:
    """``SideSessionStatus`` accepts only the four documented
    states."""

    @pytest.mark.parametrize("status", [
        "dispatched", "in_progress", "merged", "reverted",
    ])
    def test_accepts_valid_status(self, status: SideSessionStatus) -> None:
        task = SideSessionTask(
            slug="demo_task",
            date="2026-04-20",
            deliverables="d",
            status=status,
        )
        assert task.status == status

    @pytest.mark.parametrize("bad_status", [
        "DISPATCHED",       # case-sensitive
        "in-progress",      # wrong separator
        "done",             # not in the lifecycle set
        "",                 # empty
    ])
    def test_rejects_invalid_status(self, bad_status: str) -> None:
        with pytest.raises(ValidationError):
            SideSessionTask(
                slug="demo_task",
                date="2026-04-20",
                deliverables="d",
                status=bad_status,  # type: ignore[arg-type]
            )


class TestMakeBranchName:
    """Canonical branch-name derivation — single source of truth
    for the ``side/<date>_<slug>`` convention."""

    def test_combines_slug_and_date(self) -> None:
        got = make_branch_name("demo_task", "2026-04-20")
        assert got == "side/2026-04-20_demo_task"

    def test_is_pure_formatter_not_validator(self) -> None:
        """``make_branch_name`` does NOT re-validate its inputs;
        validation happens at ``SideSessionTask`` construction.
        This test pins the separation of concerns so a future
        change doesn't silently add a second validation site."""
        assert make_branch_name("x", "y") == "side/y_x"


class TestOntologyRoundTrip:
    """``SideSessionTask`` survives JSON serialization + reload
    via ``Ontology``. This is the contract the ontology_writer
    (C4) and ``audit-ontology`` rely on."""

    def test_empty_ontology_has_empty_task_list(self) -> None:
        onto = Ontology()
        assert not onto.side_session_tasks

    def test_pre_existing_json_without_task_field_loads_cleanly(self) -> None:
        """A JSON payload that predates the ``SideSessionTask``
        field — as every currently-committed qemu-harness.json
        snapshot does — must still load, with the field
        defaulting to an empty list. Guards against breaking
        the D051 audit gate when this commit lands."""
        raw: dict[str, Any] = {"entities": [], "relationships": []}
        onto = Ontology.model_validate(raw)
        assert not onto.side_session_tasks

    def test_task_serializes_and_deserializes_losslessly(self) -> None:
        task = SideSessionTask(
            slug="demo_task",
            date="2026-04-20",
            scope_paths=["tooling/src/demo/"],
            required_reading=["DECISIONS.md:D049"],
            deliverables="demo",
        )
        onto = Ontology(side_session_tasks=[task])
        blob = onto.model_dump_json()
        reloaded = Ontology.model_validate_json(blob)
        assert len(reloaded.side_session_tasks) == 1
        got = reloaded.side_session_tasks[0]
        assert got.slug == "demo_task"
        assert got.date == "2026-04-20"
        assert got.scope_paths == ["tooling/src/demo/"]
        assert got.required_reading == ["DECISIONS.md:D049"]
        assert got.status == "dispatched"

    def test_task_json_key_matches_field_name(self) -> None:
        """Pins the on-disk JSON key ``side_session_tasks`` —
        ``_find_side_session_task`` in the behavioral tests and
        the C4 ontology_writer both depend on this shape."""
        task = SideSessionTask(
            slug="demo_task",
            date="2026-04-20",
            deliverables="d",
        )
        blob = json.loads(
            Ontology(side_session_tasks=[task]).model_dump_json()
        )
        assert "side_session_tasks" in blob
        assert blob["side_session_tasks"][0]["slug"] == "demo_task"
