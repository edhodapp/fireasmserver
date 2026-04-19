"""Tests for the ontology model's DAG navigation helpers and the
top-level ``validate_ontology_strict`` function. These round out
branch / statement coverage on the forked module."""
from __future__ import annotations

from ontology import (
    DAGEdge,
    DAGNode,
    Decision,
    Entity,
    Ontology,
    OntologyDAG,
    validate_ontology_strict,
)


def _entity(id_: str) -> Entity:
    return Entity(id=id_, name=id_, description="")


def _dag_with_three_nodes() -> OntologyDAG:
    """A: root. B: child of A. C: child of A (sibling branch to B).
    Lets us exercise get_node, children_of, parents_of,
    root_nodes, edges_from, edges_to against a deterministic shape.
    """
    dag = OntologyDAG(project_name="nav")
    now = "2026-04-19T00:00:00+00:00"
    for node_id in ("A", "B", "C"):
        dag.nodes.append(
            DAGNode(
                id=node_id,
                ontology=Ontology(entities=[_entity(node_id)]),
                created_at=now,
                label=node_id,
            ),
        )
    decision = Decision(
        question="fork", options=["X"], chosen="X", rationale="r",
    )
    dag.edges.append(DAGEdge(
        parent_id="A", child_id="B",
        decision=decision, created_at=now,
    ))
    dag.edges.append(DAGEdge(
        parent_id="A", child_id="C",
        decision=decision, created_at=now,
    ))
    dag.current_node_id = "B"
    return dag


class TestOntologyDAGNavigation:
    """Coverage for OntologyDAG navigation helpers against a small
    branching shape (A as root, B and C as sibling children)."""

    def test_get_node_returns_matching_node(self) -> None:
        dag = _dag_with_three_nodes()
        node = dag.get_node("B")
        assert node is not None and node.label == "B"

    def test_get_node_returns_none_when_missing(self) -> None:
        dag = _dag_with_three_nodes()
        assert dag.get_node("nonexistent") is None

    def test_get_current_node_returns_current(self) -> None:
        dag = _dag_with_three_nodes()
        current = dag.get_current_node()
        assert current is not None and current.id == "B"

    def test_children_of_returns_both_siblings(self) -> None:
        dag = _dag_with_three_nodes()
        child_labels = sorted(n.label for n in dag.children_of("A"))
        assert child_labels == ["B", "C"]

    def test_children_of_leaf_is_empty(self) -> None:
        dag = _dag_with_three_nodes()
        assert dag.children_of("B") == []

    def test_parents_of_identifies_fork_parent(self) -> None:
        dag = _dag_with_three_nodes()
        parent_labels = sorted(n.label for n in dag.parents_of("B"))
        assert parent_labels == ["A"]

    def test_parents_of_root_is_empty(self) -> None:
        dag = _dag_with_three_nodes()
        assert dag.parents_of("A") == []

    def test_root_nodes_returns_only_roots(self) -> None:
        dag = _dag_with_three_nodes()
        root_labels = sorted(n.label for n in dag.root_nodes())
        assert root_labels == ["A"]

    def test_edges_from_returns_outgoing(self) -> None:
        dag = _dag_with_three_nodes()
        outgoing = dag.edges_from("A")
        child_ids = sorted(e.child_id for e in outgoing)
        assert child_ids == ["B", "C"]

    def test_edges_to_returns_incoming(self) -> None:
        dag = _dag_with_three_nodes()
        incoming = dag.edges_to("B")
        assert len(incoming) == 1
        assert incoming[0].parent_id == "A"


class TestOntologyDAGSerialization:
    def test_to_json_and_from_json_roundtrip(self) -> None:
        dag = _dag_with_three_nodes()
        text = dag.to_json()
        restored = OntologyDAG.from_json(text)
        # Round-trip preserves structure.
        assert [n.id for n in restored.nodes] == [
            n.id for n in dag.nodes
        ]
        assert restored.current_node_id == dag.current_node_id


class TestValidateOntologyStrict:
    def test_valid_ontology_returns_empty_list(self) -> None:
        valid = Ontology(entities=[_entity("x")]).model_dump()
        assert validate_ontology_strict(valid) == []

    def test_invalid_ontology_surfaces_error_strings(self) -> None:
        # `entities` must be a list of Entity dicts; passing a dict
        # where a list is expected surfaces as a pydantic error.
        bad = {"entities": "not a list"}
        errors = validate_ontology_strict(bad)
        assert errors
        assert all(isinstance(msg, str) for msg in errors)
