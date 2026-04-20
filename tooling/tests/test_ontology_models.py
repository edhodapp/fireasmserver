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
    ModuleSpec,
    Ontology,
    OntologyDAG,
    PerformanceConstraint,
    Property,
    PropertyType,
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


# ---------------------------------------------------------------
# PropertyType cross-field validation — 2026-04-20 hygiene pass.
# ---------------------------------------------------------------


class TestPropertyTypeScalarKinds:
    """Scalar kinds must not carry a reference."""

    @pytest.mark.parametrize("kind", [
        "str", "int", "float", "bool", "datetime",
    ])
    def test_accepts_none_reference(self, kind: str) -> None:
        pt = PropertyType(kind=kind)  # type: ignore[arg-type]
        assert pt.reference is None

    @pytest.mark.parametrize("kind", [
        "str", "int", "float", "bool", "datetime",
    ])
    def test_rejects_any_reference(self, kind: str) -> None:
        with pytest.raises(ValidationError):
            PropertyType(
                kind=kind,                  # type: ignore[arg-type]
                reference="unexpected",
            )
        with pytest.raises(ValidationError):
            PropertyType(
                kind=kind,                  # type: ignore[arg-type]
                reference=["also", "bad"],
            )


class TestPropertyTypeEntityRef:
    """``entity_ref`` requires a non-empty string reference."""

    def test_accepts_non_empty_string(self) -> None:
        pt = PropertyType(kind="entity_ref", reference="target_id")
        assert pt.reference == "target_id"

    @pytest.mark.parametrize("bad", [None, "", [], ["a"]])
    def test_rejects_non_string_or_empty(self, bad: object) -> None:
        with pytest.raises(ValidationError):
            PropertyType(
                kind="entity_ref",
                reference=bad,              # type: ignore[arg-type]
            )


class TestPropertyTypeEnum:
    """``enum`` requires a non-empty list of strings."""

    def test_accepts_list_of_strings(self) -> None:
        pt = PropertyType(kind="enum", reference=["a", "b", "c"])
        assert pt.reference == ["a", "b", "c"]

    @pytest.mark.parametrize("bad", [
        None,                           # missing
        "single_string",                # string, not list
        [],                             # empty list
    ])
    def test_rejects_non_list_or_empty(self, bad: object) -> None:
        with pytest.raises(ValidationError):
            PropertyType(
                kind="enum",
                reference=bad,              # type: ignore[arg-type]
            )

    def test_rejects_mixed_type_list(self) -> None:
        with pytest.raises(ValidationError):
            PropertyType(
                kind="enum",
                reference=["a", 1, "c"],    # type: ignore[list-item]
            )

    def test_rejects_empty_string_element(self) -> None:
        """An empty string in an enum's allowed-values list is
        always a data mistake — the list represents the literal
        choices the field may take."""
        with pytest.raises(ValidationError):
            PropertyType(kind="enum", reference=["a", "", "c"])


# ---------------------------------------------------------------
# Ontology RI check — Property.property_type.reference resolution
# for entity_ref properties.
# ---------------------------------------------------------------


class TestPropertyEntityRefReferentialIntegrity:
    """An entity_ref Property whose reference doesn't resolve
    in this Ontology's entities list must be rejected at
    construction time, the same way Relationship and
    DomainConstraint references are."""

    def test_resolvable_entity_ref_accepted(self) -> None:
        onto = Ontology(entities=[
            _entity("a"),
            Entity(id="b", name="B", properties=[
                Property(
                    name="owner",
                    property_type=PropertyType(
                        kind="entity_ref", reference="a",
                    ),
                ),
            ]),
        ])
        assert len(onto.entities) == 2

    def test_dangling_entity_ref_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Ontology(entities=[
                Entity(id="b", name="B", properties=[
                    Property(
                        name="owner",
                        property_type=PropertyType(
                            kind="entity_ref", reference="ghost",
                        ),
                    ),
                ]),
            ])
        message = str(exc.value)
        assert "b.owner" in message
        assert "'ghost'" in message

    def test_dangling_entity_ref_joins_other_ri_errors(self) -> None:
        """Dangling entity_ref pointers surface alongside other
        RI errors in a single validation pass, consistent with
        the existing all-errors-together contract."""
        with pytest.raises(ValidationError) as exc:
            Ontology(
                entities=[Entity(id="b", name="B", properties=[
                    Property(
                        name="owner",
                        property_type=PropertyType(
                            kind="entity_ref", reference="ghost_a",
                        ),
                    ),
                ])],
                relationships=[Relationship(
                    source_entity_id="ghost_b", target_entity_id="b",
                    name="r", cardinality="one_to_one",
                )],
            )
        message = str(exc.value)
        assert "'ghost_a'" in message
        assert "'ghost_b'" in message


# ---------------------------------------------------------------
# ModuleSpec.dependencies hygiene.
# ---------------------------------------------------------------


class TestModuleSpecDependenciesHygiene:
    """String-level hygiene: no empty entries, no
    leading/trailing whitespace, no duplicates."""

    def test_accepts_clean_list(self) -> None:
        mod = ModuleSpec(
            name="m",
            responsibility="r",
            dependencies=["subprocess", "pathlib", "vm_launcher"],
        )
        assert len(mod.dependencies) == 3

    def test_accepts_empty_list(self) -> None:
        mod = ModuleSpec(name="m", responsibility="r")
        assert not mod.dependencies

    def test_rejects_empty_string_entry(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ModuleSpec(
                name="m", responsibility="r",
                dependencies=["pathlib", ""],
            )
        assert "empty string" in str(exc.value)

    @pytest.mark.parametrize("bad", [
        " subprocess",      # leading space
        "pathlib ",         # trailing space
        "\turllib",         # tab
        "subprocess\n",     # newline
        "path lib",         # interior space — never a valid import
        "url\tlib",         # interior tab
    ])
    def test_rejects_any_whitespace(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc:
            ModuleSpec(
                name="m", responsibility="r",
                dependencies=[bad],
            )
        assert "whitespace" in str(exc.value)

    def test_rejects_duplicates(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ModuleSpec(
                name="m", responsibility="r",
                dependencies=["pathlib", "subprocess", "pathlib"],
            )
        assert "duplicate" in str(exc.value)
        assert "'pathlib'" in str(exc.value)


# ---------------------------------------------------------------
# Property.name type tightening.
# ---------------------------------------------------------------


class TestPropertyNameShortName:
    """``Property.name`` is ``ShortName`` (max_length=100). Catches
    accidental paragraphs-as-names without over-constraining the
    character set."""

    def test_accepts_normal_length_name(self) -> None:
        prop = Property(
            name="serial_path",
            property_type=PropertyType(kind="str"),
        )
        assert prop.name == "serial_path"

    def test_accepts_at_length_boundary(self) -> None:
        prop = Property(
            name="a" * 100,
            property_type=PropertyType(kind="str"),
        )
        assert len(prop.name) == 100

    def test_rejects_over_length(self) -> None:
        with pytest.raises(ValidationError):
            Property(
                name="a" * 101,
                property_type=PropertyType(kind="str"),
            )
