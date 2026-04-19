"""Tests for the ontology DAG helpers.

Covers ``ontology_content_hash``, ``git_snapshot_label``,
``_git_head_sha``, ``_git_is_dirty``, ``save_dag`` / ``load_dag``
round-trip, and ``snapshot_if_changed``'s idempotency + bootstrap
+ change-detection behaviour. The git helpers are tested against
both a successful-subprocess path (via monkeypatch) and a
subprocess-failure path so the fallback-to-None / fallback-to-False
branches are covered.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ontology import Entity, Ontology, OntologyDAG
from ontology.dag import (
    _git_head_sha,
    _git_is_dirty,
    git_snapshot_label,
    load_dag,
    make_node_id,
    ontology_content_hash,
    save_dag,
    save_snapshot,
    snapshot_if_changed,
)


def _ontology_with(name: str) -> Ontology:
    """Build a trivial Ontology whose content hash depends on
    the provided name, used to exercise change-detection branches."""
    return Ontology(
        entities=[Entity(id=name, name=name, description="")],
    )


class TestMakeNodeId:
    def test_returns_uuid_shape(self) -> None:
        node_id = make_node_id()
        # uuid4 canonical form: 8-4-4-4-12 hex with hyphens.
        parts = node_id.split("-")
        assert [len(p) for p in parts] == [8, 4, 4, 4, 12]

    def test_returns_unique_ids(self) -> None:
        assert make_node_id() != make_node_id()


class TestOntologyContentHash:
    def test_hash_is_deterministic(self) -> None:
        left = _ontology_with("A")
        right = _ontology_with("A")
        assert ontology_content_hash(left) == ontology_content_hash(right)

    def test_hash_differs_for_different_content(self) -> None:
        assert ontology_content_hash(_ontology_with("A")) != (
            ontology_content_hash(_ontology_with("B"))
        )

    def test_hash_is_sha256_hex(self) -> None:
        # SHA-256 hex digest is always 64 characters, lowercase hex.
        digest = ontology_content_hash(_ontology_with("A"))
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


class TestGitHeadSha:
    def test_returns_sha_when_git_succeeds(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="deadbee\n", stderr="",
        )
        with patch("ontology.dag.subprocess.run", return_value=fake):
            assert _git_head_sha() == "deadbee"

    def test_full_sha_form_strips_newline(self) -> None:
        full = "a" * 40
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=full + "\n", stderr="",
        )
        with patch("ontology.dag.subprocess.run", return_value=fake):
            assert _git_head_sha(short=False) == full

    def test_returns_none_on_nonzero_exit(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="not a repo",
        )
        with patch("ontology.dag.subprocess.run", return_value=fake):
            assert _git_head_sha() is None

    def test_returns_none_on_empty_stdout(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="\n", stderr="",
        )
        with patch("ontology.dag.subprocess.run", return_value=fake):
            assert _git_head_sha() is None

    def test_returns_none_on_oserror(self) -> None:
        with patch(
            "ontology.dag.subprocess.run",
            side_effect=OSError("git missing"),
        ):
            assert _git_head_sha() is None

    def test_returns_none_on_timeout(self) -> None:
        with patch(
            "ontology.dag.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
        ):
            assert _git_head_sha() is None


class TestGitIsDirty:
    def test_clean_tree(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with patch("ontology.dag.subprocess.run", return_value=fake):
            assert _git_is_dirty() is False

    def test_dirty_tree(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=" M some/file.py\n", stderr="",
        )
        with patch("ontology.dag.subprocess.run", return_value=fake):
            assert _git_is_dirty() is True

    def test_nonzero_exit_reports_clean(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="",
        )
        with patch("ontology.dag.subprocess.run", return_value=fake):
            assert _git_is_dirty() is False

    def test_oserror_reports_clean(self) -> None:
        with patch(
            "ontology.dag.subprocess.run",
            side_effect=OSError("git missing"),
        ):
            assert _git_is_dirty() is False

    def test_timeout_reports_clean(self) -> None:
        with patch(
            "ontology.dag.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
        ):
            assert _git_is_dirty() is False


class TestGitSnapshotLabel:
    def test_label_with_clean_sha(self) -> None:
        with patch("ontology.dag._git_head_sha", return_value="cafef00"), \
             patch("ontology.dag._git_is_dirty", return_value=False):
            label = git_snapshot_label()
        assert "@cafef00" in label
        assert "+dirty" not in label

    def test_label_with_dirty_sha(self) -> None:
        with patch("ontology.dag._git_head_sha", return_value="cafef00"), \
             patch("ontology.dag._git_is_dirty", return_value=True):
            label = git_snapshot_label()
        assert "@cafef00+dirty" in label

    def test_label_with_prefix(self) -> None:
        with patch("ontology.dag._git_head_sha", return_value="cafef00"), \
             patch("ontology.dag._git_is_dirty", return_value=False):
            label = git_snapshot_label(prefix="baseline")
        assert label.startswith("baseline ")
        assert "@cafef00" in label

    def test_label_without_git(self) -> None:
        with patch("ontology.dag._git_head_sha", return_value=None):
            label = git_snapshot_label()
        assert "@" not in label
        # Still has the ISO timestamp — starts with 4-digit year.
        assert label[:4].isdigit()


class TestSaveAndLoadDag:
    def test_roundtrip_preserves_content(self, tmp_path: Path) -> None:
        dag_path = str(tmp_path / "dag.json")
        dag = OntologyDAG(project_name="roundtrip")
        save_snapshot(dag, _ontology_with("A"), "first")
        save_dag(dag, dag_path)

        loaded = load_dag(dag_path, project_name="roundtrip")
        assert len(loaded.nodes) == 1
        assert loaded.nodes[0].label == "first"

    def test_load_missing_file_returns_empty_dag(
        self, tmp_path: Path,
    ) -> None:
        dag_path = str(tmp_path / "does-not-exist.json")
        loaded = load_dag(dag_path, project_name="bootstrap")
        assert loaded.project_name == "bootstrap"
        assert loaded.nodes == []


class TestSnapshotIfChanged:
    def test_bootstrap_appends_first_node(self) -> None:
        dag = OntologyDAG(project_name="bootstrap")
        onto = _ontology_with("A")
        node_id, created = snapshot_if_changed(dag, onto, "initial")
        assert created is True
        assert node_id == dag.current_node_id
        assert len(dag.nodes) == 1

    def test_identical_content_is_noop(self) -> None:
        dag = OntologyDAG(project_name="noop")
        onto = _ontology_with("A")
        first_id, first_created = snapshot_if_changed(
            dag, onto, "first",
        )
        assert first_created is True

        second_id, second_created = snapshot_if_changed(
            dag, _ontology_with("A"), "second",
        )
        assert second_created is False
        assert second_id == first_id
        assert len(dag.nodes) == 1

    def test_different_content_appends_new_node(self) -> None:
        dag = OntologyDAG(project_name="diff")
        snapshot_if_changed(dag, _ontology_with("A"), "first")
        second_id, second_created = snapshot_if_changed(
            dag, _ontology_with("B"), "second",
        )
        assert second_created is True
        assert len(dag.nodes) == 2
        assert dag.current_node_id == second_id

    def test_explicit_decision_preserved_on_fork_edge(self) -> None:
        """Covers the ``decision is not None`` branch in
        ``save_snapshot`` where the caller supplies its own
        Decision (the typical case for a branching audit trail)."""
        from ontology import Decision as Dec  # avoid top-level dup
        dag = OntologyDAG(project_name="explicit")
        snapshot_if_changed(dag, _ontology_with("A"), "first")
        chosen = Dec(
            question="add entity B?",
            options=["add", "skip"],
            chosen="add",
            rationale="need B for downstream constraint",
        )
        save_snapshot(dag, _ontology_with("B"), "second", chosen)
        assert len(dag.edges) == 1
        assert dag.edges[0].decision.question == "add entity B?"
        assert dag.edges[0].decision.chosen == "add"

    def test_saves_full_content_round_trip(
        self, tmp_path: Path,
    ) -> None:
        """Exercise save/load with snapshot_if_changed in the
        middle — simulates the builder's on-disk workflow."""
        dag_path = str(tmp_path / "builder.json")
        dag = load_dag(dag_path, project_name="builder")

        _, first_created = snapshot_if_changed(
            dag, _ontology_with("A"), "first",
        )
        assert first_created is True
        save_dag(dag, dag_path)

        reloaded = load_dag(dag_path, project_name="builder")
        _, second_created = snapshot_if_changed(
            reloaded, _ontology_with("A"), "second",
        )
        assert second_created is False


def test_save_dag_recovers_from_write_failure(
    tmp_path: Path,
) -> None:
    """save_dag uses atomic rename; if the write itself fails,
    the temp file must be cleaned up rather than left behind."""
    dag_path = str(tmp_path / "fail.json")
    dag = OntologyDAG(project_name="fail")
    save_snapshot(dag, _ontology_with("A"), "sentinel")

    class _BoomHandle:
        """Drop-in replacement for the temp file that fails on write.

        Used to exercise save_dag's cleanup path — the real temp
        file is created (so we have a name to unlink), but the
        write itself raises, which forces the except branch to run.
        """

        def __init__(self, real: Any) -> None:
            self._real = real
            self.name = real.name

        def write(self, payload: str) -> None:
            del payload
            raise RuntimeError("simulated write failure")

        def close(self) -> None:
            self._real.close()

    real_tempfile = __import__("tempfile")
    original = real_tempfile.NamedTemporaryFile

    def _factory(**kwargs: Any) -> _BoomHandle:
        return _BoomHandle(original(**kwargs))

    with patch(
        "ontology.dag.tempfile.NamedTemporaryFile",
        side_effect=_factory,
    ):
        with pytest.raises(RuntimeError, match="simulated"):
            save_dag(dag, dag_path)
    # No leftover .tmp files in the target directory.
    assert not list(tmp_path.glob("*.tmp"))
