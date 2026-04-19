"""Tests for the ontology model's DAG navigation helpers and the
top-level ``validate_ontology_strict`` function. These round out
branch / statement coverage on the forked module."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ontology import (
    DAGEdge,
    DAGNode,
    DataModel,
    Decision,
    DomainConstraint,
    Entity,
    Ontology,
    OntologyDAG,
    PerformanceConstraint,
    Relationship,
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
    """Coverage for the top-level validate_ontology_strict helper."""

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


class TestReferentialIntegrity:
    """The `Ontology` @model_validator refuses to construct a graph
    with dangling cross-references. These tests exercise each
    reference-emitting model type against a known-entity set."""

    def test_valid_references_construct_cleanly(self) -> None:
        Ontology(
            entities=[_entity("a"), _entity("b")],
            relationships=[Relationship(
                source_entity_id="a", target_entity_id="b",
                name="rel", cardinality="one_to_one",
            )],
            domain_constraints=[DomainConstraint(
                name="dc", description="", entity_ids=["a"],
            )],
            performance_constraints=[PerformanceConstraint(
                name="pc", description="", entity_ids=["b"],
                metric="x", budget=1.0, unit="ns", direction="max",
            )],
            data_models=[DataModel(
                entity_id="a", storage="memory", class_name="A",
            )],
        )

    def test_dangling_relationship_source_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                relationships=[Relationship(
                    source_entity_id="ghost", target_entity_id="a",
                    name="bad", cardinality="one_to_one",
                )],
            )
        assert "'ghost'" in str(exc.value)
        assert "source" in str(exc.value)

    def test_dangling_relationship_target_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                relationships=[Relationship(
                    source_entity_id="a", target_entity_id="ghost",
                    name="bad", cardinality="one_to_one",
                )],
            )
        assert "'ghost'" in str(exc.value)
        assert "target" in str(exc.value)

    def test_dangling_domain_constraint_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                domain_constraints=[DomainConstraint(
                    name="dc", description="",
                    entity_ids=["a", "ghost"],
                )],
            )
        assert "DomainConstraint" in str(exc.value)
        assert "'ghost'" in str(exc.value)

    def test_dangling_performance_constraint_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                performance_constraints=[PerformanceConstraint(
                    name="pc", description="", entity_ids=["ghost"],
                    metric="x", budget=0.0, unit="ns",
                    direction="max",
                )],
            )
        assert "PerformanceConstraint" in str(exc.value)
        assert "'ghost'" in str(exc.value)

    def test_dangling_data_model_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                data_models=[DataModel(
                    entity_id="ghost", storage="mem", class_name="X",
                )],
            )
        assert "DataModel" in str(exc.value)
        assert "'ghost'" in str(exc.value)

    def test_all_dangling_refs_surface_together(self) -> None:
        """When multiple cross-references are broken, the error
        lists all of them, not just the first. Lets an auditor
        see the full picture in one validation pass."""
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[_entity("a")],
                relationships=[Relationship(
                    source_entity_id="x", target_entity_id="y",
                    name="r", cardinality="one_to_one",
                )],
                data_models=[DataModel(
                    entity_id="z", storage="mem", class_name="Z",
                )],
            )
        message = str(exc.value)
        assert "'x'" in message
        assert "'y'" in message
        assert "'z'" in message
